"""The Vast provider against a fake Vast REST API (httpx MockTransport). Every request is checked
against the field lists of Vast's own client (`vast_schema.json`, extracted from vastai 1.8.3 on
2026-10-07): an unknown search field, create field or offer type fails the test. No live call is
made: no key, no approved spend, no GPU rented."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from ce_gpu.provider import (
    NoCapacityError,
    ProviderError,
    ProvisionOutcomeUnknown,
    ProvisionSpec,
    create_gpu_provider,
)
from ce_plugin_gpu_vast.client import OfferUnavailableError, VastClient, api_key_from
from ce_plugin_gpu_vast.common import VastConfig, country_code, driver_version, offer_price, offer_vram_gb
from ce_plugin_gpu_vast.provider import VastProvider, instance_body, state_of

SCHEMA = json.loads((Path(__file__).parent / "vast_schema.json").read_text(encoding="utf-8"))
KEY = "vast_test_key_not_real"
BASE = "https://console.vast.ai"


def _offer(offer_id: int, **over: Any) -> dict[str, Any]:
    offer = {
        "id": offer_id, "machine_id": 9000 + offer_id, "host_id": 1, "num_gpus": 1, "gpu_name": "A100 SXM4",
        "gpu_ram": 81920, "dph_total": 1.2, "min_bid": 0.6, "geolocation": "Sweden, SE", "reliability": 0.995,
        "driver_vers": 570124006, "cuda_max_good": 12.8, "verified": True, "rentable": True, "rented": False,
    }  # fmt: skip
    offer.update(over)
    assert set(offer) <= set(SCHEMA["offer_fields"]) | {"verified"}, set(offer) - set(SCHEMA["offer_fields"])
    return offer


def _check_query(query: dict[str, Any]) -> None:
    fields = set(SCHEMA["search_query_fields"])
    controls = set(SCHEMA["search_query_controls"])
    for name, value in query.items():
        if name in controls:
            continue
        assert name in fields, f"search: Vast has no field {name!r}"
        assert isinstance(value, dict) and value, f"search: {name} needs {{op: value}}"
        assert set(value) <= set(SCHEMA["search_operators"]), f"search: {name} uses {set(value)}"
    assert query["type"] in SCHEMA["search_types"]


def _check_create(body: dict[str, Any]) -> None:
    unknown = set(body) - set(SCHEMA["create_instance_fields"])
    assert not unknown, f"create: Vast has no fields {unknown}"
    if "volume_info" in body:
        assert set(body["volume_info"]) <= set(SCHEMA["volume_info_fields"])


class FakeVast:
    """Enough of Vast's API: search, rent, show, start/stop, destroy, current user."""

    def __init__(self, offers: list[dict[str, Any]] | None = None, *, taken: set[int] | None = None) -> None:
        self.offers = [_offer(101), _offer(102, dph_total=1.4)] if offers is None else offers
        self.taken = set(taken or ())
        self.instances: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.queries: list[dict[str, Any]] = []
        self.bodies: list[dict[str, Any]] = []
        self.fail: dict[tuple[str, str], httpx.Response] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert str(request.url).startswith((BASE + "/api/v0/", BASE + "/api/v1/instances/"))
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if (method, path) in self.fail:
            return self.fail[(method, path)]
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        if (method, path) == ("GET", "/api/v1/instances/"):  # paged, two per page, JSON query values
            params = {k: json.loads(v) for k, v in request.url.params.items()}
            assert params["order_by"] == [{"col": "id", "dir": "asc"}] and params["select_filters"] == {}
            rows = sorted(self.instances.values(), key=lambda r: r["id"])
            start = int(params.get("after_token", 0))
            page = rows[start : start + 2]
            more = start + 2 < len(rows)
            return httpx.Response(200, json={"instances": page, **({"next_token": str(start + 2)} if more else {})})
        if method == "PUT" and path.startswith("/api/v0/instances/reboot/"):
            row = self.instances.get(path.split("/")[5])
            if row is None:
                return httpx.Response(404, json={"success": False, "msg": "no such instance"})
            row["actual_status"], row["intended_status"] = "running", "running"
            return httpx.Response(200, json={"success": True})
        if (method, path) == ("POST", "/api/v0/bundles/"):
            _check_query(body)
            self.queries.append(body)
            return httpx.Response(200, json={"offers": list(self.offers)})
        if method == "PUT" and path.startswith("/api/v0/asks/"):
            _check_create(body)
            self.bodies.append(body)
            offer_id = int(path.split("/")[4])
            if offer_id in self.taken:
                taken = {"success": False, "error": "no_such_ask", "msg": "Instance type is no longer available."}
                return httpx.Response(400, json=taken)
            contract = 7_000_000 + len(self.instances) + 1
            offer = next(o for o in self.offers if o["id"] == offer_id)
            self.instances[str(contract)] = {
                "id": contract,
                "actual_status": "loading",
                "intended_status": "running",
                "machine_id": offer["machine_id"],
                "gpu_name": offer["gpu_name"],
                "gpu_ram": offer["gpu_ram"],
                "num_gpus": 1,
                "geolocation": offer["geolocation"],
                "dph_total": body["price"] if body["price"] is not None else offer["dph_total"],
                "start_date": 1791300000.0,
                "label": body["label"],
                "extra_env": [[k, v] for k, v in body["env"].items()],
            }
            return httpx.Response(
                200, json={"success": True, "new_contract": contract, "instance_api_key": "inst-secret"}
            )
        if path == "/api/v0/users/current" and method == "GET":
            return httpx.Response(200, json={"id": 1, "username": "u", "api_key": KEY, "credit": 10})
        if path.startswith("/api/v0/instances/"):
            instance_id = path.split("/")[4]
            row = self.instances.get(instance_id)
            if method == "GET":
                assert request.url.params["owner"] == "me"
                if row is not None and row["actual_status"] == "loading" and row["intended_status"] == "running":
                    shown = dict(row)
                    row["actual_status"] = "running"
                    return httpx.Response(200, json={"instances": shown})
                return httpx.Response(200, json={"instances": row})
            if row is None:
                return httpx.Response(404, json={"success": False, "msg": "no such instance"})
            if method == "PUT":
                assert set(body) == {"state"} and body["state"] in {"running", "stopped"}
                row["intended_status"] = body["state"]
                row["actual_status"] = "running" if body["state"] == "running" else "exited"
                return httpx.Response(200, json={"success": True})
            if method == "DELETE":
                del self.instances[instance_id]
                return httpx.Response(200, json={"success": True})
        return httpx.Response(404, json={"error": "not found"})


def _defaults() -> dict[str, Any]:
    from ce_contracts.plugins import discover

    return discover(app_env="prod", include_mocks=False).providers("gpu")["vast"].manifest.defaults


def _config(fake: FakeVast, **extra: Any) -> dict[str, Any]:
    return {**_defaults(), "api_key": KEY, "transport": httpx.MockTransport(fake.handler),
            "image_template": "ghcr.io/example/creator-engine-worker-{family}:{variant}", **extra}  # fmt: skip


SPEC = ProvisionSpec(
    gpu_class="a100_80gb", region="eu", runtime_family="wan", variant="wan22",
    env={"SCHEDULER_URL": "https://sched.example", "WORKER_TOKEN": "t", "WORKER_RUNTIME_FAMILY": "wan"}, spot_ok=True,
)  # fmt: skip


# ------------------------------------------------------------------ 1-2 discovery and manifest
def test_the_manifest_is_registered_next_to_runpod_and_inert_by_default() -> None:
    from ce_contracts.plugins import discover

    registry = discover(app_env="prod", include_mocks=False)
    providers = registry.providers("gpu")
    assert {"vast", "runpod_pod", "runpod_serverless", "local", "local_docker"} <= set(providers)
    assert not [r for r in registry.rejected if "vast" in r.name]
    manifest = providers["vast"].manifest
    assert (manifest.id, manifest.provider.key) == ("gpu.vast", "vast")  # type: ignore[union-attr]
    assert manifest.entrypoint == "ce_plugin_gpu_vast.provider:create"
    assert (manifest.status, manifest.validation) == ("experimental", "untested_on_gpu")
    assert manifest.defaults["allow_paid"] is False and manifest.defaults["interruptible"]["enabled"] is False
    assert "api_key" not in manifest.defaults  # no secret in the manifest
    with_mocks = discover(app_env="dev", include_mocks=True).providers("gpu")
    assert {"vast", "runpod_pod", "mock", "example_cloud"} <= set(with_mocks)


def test_create_gpu_provider_builds_it_through_the_registry() -> None:
    provider = create_gpu_provider("vast", app_env="prod", include_mocks=False, config={"api_key": KEY})
    assert isinstance(provider, VastProvider) and provider.key == "vast"
    assert provider.cfg.client.base_url == BASE and provider.cfg.allow_paid is False


def test_the_scheduler_treats_it_as_a_paid_provider() -> None:
    from ce_scheduler.providers import is_paid

    assert is_paid("vast", app_env="prod", include_mocks=False)
    assert is_paid("runpod_pod", app_env="prod", include_mocks=False)


# ------------------------------------------------------------------ 3 authentication
async def test_requests_carry_the_bearer_key_and_the_configured_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": 1, "api_key": "should-be-discarded"})

    client = VastClient(KEY, base_url="https://vast.internal.example/", transport=httpx.MockTransport(handler))
    await client.current_user()
    assert seen[0].headers["Authorization"] == f"Bearer {KEY}"
    assert str(seen[0].url) == "https://vast.internal.example/api/v0/users/current"
    assert api_key_from({"api_key": "from-row"}) == "from-row"


async def test_a_missing_key_is_explicit_and_a_refused_key_never_echoes_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VAST_API_KEY", raising=False)
    fake = FakeVast()
    keyless = VastProvider({**_config(fake, allow_paid=True), "api_key": ""})
    assert not (await keyless.health()).ok
    with pytest.raises(ProviderError, match="no API key"):
        await keyless.provision(SPEC)
    monkeypatch.setenv("VAST_API_KEY", "env-key-not-real")
    assert api_key_from({}) == "env-key-not-real"
    fake.fail[("GET", "/api/v0/users/current")] = httpx.Response(401, json={"msg": "bad key"})
    health = await VastProvider(_config(fake)).health()
    assert not health.ok and "refused" in health.detail and KEY not in health.detail


# ------------------------------------------------------------------ 4-8 offers
async def test_offers_are_parsed_filtered_and_priced() -> None:
    fake = FakeVast([
        _offer(201, dph_total=1.1, gpu_ram=81920, geolocation="Germany, DE"),
        _offer(202, dph_total="bad", gpu_ram=81920),  # malformed price: skipped
        _offer(203, gpu_name="RTX 4090", gpu_ram=24564, dph_total=0.4),  # another class: skipped
        _offer(204, dph_total=3.9),  # above max_price_per_hour_usd 2.5: skipped
        {"id": "not-an-int", "gpu_name": "A100 SXM4"},  # malformed offer: skipped
    ])  # fmt: skip
    provider = VastProvider(_config(fake))
    offers = await provider.list_offers(gpu_class="a100_80gb", region="eu")
    assert [(o.gpu_class, o.region, o.price_per_hour_usd, o.vram_gb) for o in offers] == [
        ("a100_80gb", "eu", 1.1, 81.9)
    ]
    assert offers[0].driver_version == "570.124.6" and offers[0].available == 1 and offers[0].spot is False
    query = fake.queries[0]
    assert query["gpu_name"] == {"in": ["A100 SXM4", "A100 PCIE", "A100 SXM"]}
    assert query["num_gpus"] == {"eq": 1} and query["gpu_ram"] == {"gte": 79000}  # MB, so 40 GB A100s are out
    assert query["dph_total"] == {"lte": 2.5} and query["type"] == "on-demand"
    assert query["geolocation"]["in"][:2] == ["SE", "NO"] and query["reliability"] == {"gte": 0.98}
    assert query["verified"] == {"eq": True} and query["rented"] == {"eq": False}
    assert query["allocated_storage"] == 80.0 and query["order"] == [["dph_total", "asc"]]


async def test_class_and_region_filters_limit_the_searches() -> None:
    fake = FakeVast([])
    provider = VastProvider(_config(fake))
    await provider.list_offers(region="us")
    assert len(fake.queries) == len(_defaults()["classes"]) and all(
        q["geolocation"] == {"in": ["US", "CA"]} for q in fake.queries
    )
    fake.queries.clear()
    await provider.list_offers(gpu_class="rtx_4090_24gb")
    assert len(fake.queries) == len(_defaults()["regions"]) and all(
        q["gpu_name"] == {"eq": "RTX 4090"} for q in fake.queries
    )
    fake.queries.clear()
    anywhere = VastProvider(_config(fake, regions={"any": []}))
    await anywhere.list_offers(gpu_class="rtx_4090_24gb")
    assert "geolocation" not in fake.queries[0]  # an empty country list means anywhere
    with pytest.raises(ProviderError, match="unknown region"):
        provider.cfg.countries("mars")


def test_price_vram_country_and_driver_parsing() -> None:
    assert offer_price({"dph_total": 1.234}) == 1.234 and offer_price({"dph_total": None}) == 0.0
    assert offer_price({"dph_total": -1}) == 0.0 and offer_price({"dph_total": "nan"}) == 0.0
    assert offer_vram_gb({"gpu_ram": 24564}) == 24.6 and offer_vram_gb({}) == 0.0
    assert country_code("Sweden, SE") == "SE" and country_code("US") == "US" and country_code(None) is None
    assert driver_version({"driver_version": "575.57.08"}) == "575.57.08"
    assert driver_version({"driver_vers": 570124006}) == "570.124.6" and driver_version({}) is None


# ------------------------------------------------------------------ 9-14 provisioning request
async def test_provision_rents_the_cheapest_offer_with_the_worker_environment() -> None:
    fake = FakeVast()
    provider = VastProvider(_config(fake, allow_paid=True, env={"HF_HUB_ENABLE_HF_TRANSFER": "1"}))
    instance = await provider.provision(SPEC)
    assert ("PUT", "/api/v0/asks/101/") in fake.calls  # 101 (1.2 USD/h) before 102 (1.4)
    body = fake.bodies[0]
    assert body["image"] == "ghcr.io/example/creator-engine-worker-wan:wan22"  # family/variant → image
    assert body["env"]["WORKER_TOKEN"] == "t" and body["env"]["SCHEDULER_URL"] == "https://sched.example"
    assert body["env"]["WORKER_RUNTIME_FAMILY"] == "wan" and body["env"]["HF_HUB_ENABLE_HF_TRANSFER"] == "1"
    assert body["env"]["MODEL_CACHE_DIR"] == "/models" and body["disk"] == 80.0
    assert body["runtype"] == "args" and body["cancel_unavail"] is True and body["price"] is None
    assert body["label"] == "ce-worker-wan-wan22" and "volume_info" not in body
    assert (instance.external_id, instance.state, instance.runtime_family) == ("7000001", "provisioning", "wan")
    assert instance.price_per_hour_usd == 1.2 and provider.price(instance) == 1.2  # the offer's price, captured
    assert instance.vram_gb == 81.9 and instance.detail["offer_id"] == 101 and instance.detail["country"] == "SE"
    assert "inst-secret" not in json.dumps(instance.model_dump(mode="json"))  # the instance key is never kept


async def test_an_explicit_image_and_per_variant_images_win_over_the_template() -> None:
    fake = FakeVast()
    provider = VastProvider(_config(fake, allow_paid=True, images={"wan:wan22": "registry.example/wan:pinned"}))
    await provider.provision(SPEC)
    await provider.provision(SPEC.model_copy(update={"image": "registry.example/explicit:1"}))
    assert [b["image"] for b in fake.bodies] == ["registry.example/wan:pinned", "registry.example/explicit:1"]
    bare = VastProvider({**_config(FakeVast(), allow_paid=True), "image_template": ""})
    with pytest.raises(ProviderError, match="no image"):
        await bare.provision(SPEC)


async def test_a_linked_volume_holds_the_model_cache_on_its_machine() -> None:
    fake = FakeVast()
    volume = {"volume_id": 555, "machine_id": 9101, "mount_path": "/models"}
    storage = {"disk_gb": 40, "model_cache_dir": "/models", "volume": volume}
    provider = VastProvider(_config(fake, allow_paid=True, storage=storage))
    await provider.provision(SPEC)
    assert fake.queries[0]["machine_id"] == {"eq": 9101} and fake.queries[0]["allocated_storage"] == 40.0
    assert fake.bodies[0]["volume_info"] == {"volume_id": 555, "create_new": False, "mount_path": "/models"}
    assert fake.bodies[0]["disk"] == 40.0


async def test_spot_is_a_bid_only_when_enabled_and_allowed() -> None:
    fake = FakeVast()
    on_demand = VastProvider(_config(fake, allow_paid=True))  # interruptible disabled by default
    await on_demand.provision(SPEC)
    assert fake.queries[-1]["type"] == "on-demand" and fake.bodies[-1]["price"] is None
    bidding = VastProvider(_config(fake, allow_paid=True, interruptible={"enabled": True, "bid_margin": 0.1}))
    instance = await bidding.provision(SPEC)
    assert fake.queries[-1]["type"] == "bid" and fake.queries[-1]["order"] == [["min_bid", "asc"]]
    assert "dph_total" not in fake.queries[-1]
    assert fake.bodies[-1]["price"] == 0.66 and instance.price_per_hour_usd == 0.66 and instance.detail["interruptible"]
    await bidding.provision(SPEC.model_copy(update={"spot_ok": False}))  # the pool does not allow spot
    assert fake.bodies[-1]["price"] is None
    pro = SPEC.model_copy(update={"gpu_class": "rtx_pro_6000_96gb"})  # its class sets spot: false
    assert not bidding.cfg.interruptible_for(pro) and bidding.cfg.interruptible_for(SPEC)
    assert bidding.cfg.query("rtx_pro_6000_96gb", "eu", interruptible=False)["type"] == "on-demand"


def test_the_request_body_matches_vasts_payload_builder() -> None:
    cfg = VastConfig(_config(FakeVast(), allow_paid=True))
    body = instance_body(cfg, SPEC, bid=None)
    _check_create(body)
    assert body["client_id"] == "me"


# ------------------------------------------------------------------ 15-19 lifecycle
def test_status_mapping() -> None:
    assert state_of(None) == "terminated"
    assert state_of({"actual_status": "loading", "intended_status": "running"}) == "provisioning"
    assert state_of({"actual_status": "created", "intended_status": "running"}) == "provisioning"
    assert state_of({"actual_status": "running", "intended_status": "running"}) == "running"
    assert state_of({"actual_status": "exited", "intended_status": "stopped"}) == "stopped"
    assert state_of({"actual_status": "running", "intended_status": "stopped"}) == "stopped"
    assert state_of({"actual_status": "exited", "intended_status": "running"}) == "failed"  # the worker died
    assert state_of({"actual_status": "offline", "intended_status": "running"}) == "failed"
    assert state_of({"actual_status": "loading", "intended_status": "stopped"}) == "stopped"
    assert state_of({"actual_status": None, "intended_status": "running"}) == "provisioning"
    assert state_of({"actual_status": "something-new"}) == "failed"


async def test_lifecycle_status_stop_start_terminate_and_health() -> None:
    fake = FakeVast()
    provider = VastProvider(_config(fake, allow_paid=True))
    instance = await provider.provision(SPEC)
    iid = instance.external_id
    assert (await provider.status(iid)).state == "provisioning"  # loading
    running = await provider.status(iid)
    assert running.state == "running" and running.gpu_class == "a100_80gb" and running.started_at is not None
    assert (await provider.stop(iid)).state == "stopped"
    assert ("PUT", f"/api/v0/instances/{iid}/") in fake.calls
    assert (await provider.start(iid)).state == "running"
    gone = await provider.terminate(iid)
    assert gone.state == "terminated" and gone.price_per_hour_usd == 0.0
    assert ("DELETE", f"/api/v0/instances/{iid}/") in fake.calls
    assert (await provider.status(iid)).state == "terminated"  # Vast no longer knows it
    assert (await provider.terminate(iid)).state == "terminated"  # idempotent (404 = already gone)
    health = await provider.health()
    assert health.ok and KEY not in health.detail


# ------------------------------------------------------------------ 20-24 errors and guards
async def test_api_errors_are_provider_errors_without_retrying_a_rental() -> None:
    fake = FakeVast()
    fake.fail[("PUT", "/api/v0/asks/101/")] = httpx.Response(502, text="bad gateway")
    provider = VastProvider(_config(fake, allow_paid=True))
    with pytest.raises(ProviderError, match="HTTP 502") as err:
        await provider.provision(SPEC)
    assert not isinstance(err.value, NoCapacityError)
    assert [c for c in fake.calls if c[0] == "PUT"] == [("PUT", "/api/v0/asks/101/")]  # never a second rental
    fake.fail[("PUT", "/api/v0/instances/1/")] = httpx.Response(200, json={"success": False, "msg": "not yours"})
    with pytest.raises(ProviderError, match="not yours"):
        await provider.stop("1")


async def test_a_timeout_on_a_rental_says_it_may_have_gone_through() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"offers": [_offer(101)]})

    transport = httpx.MockTransport(handler)
    config = {
        **_defaults(),
        "api_key": KEY,
        "allow_paid": True,
        "transport": transport,
        "image_template": "img:{variant}",
    }
    provider = VastProvider(config)
    with pytest.raises(ProvisionOutcomeUnknown, match="may have gone through"):  # the fleet watches for it
        await provider.provision(SPEC)


async def test_no_capacity_and_taken_offers_fall_back() -> None:
    empty = VastProvider(_config(FakeVast([]), allow_paid=True))
    with pytest.raises(NoCapacityError, match="no offer"):
        await empty.provision(SPEC)
    fake = FakeVast(taken={101})
    instance = await VastProvider(_config(fake, allow_paid=True)).provision(SPEC)
    assert instance.detail["offer_id"] == 102 and instance.price_per_hour_usd == 1.4  # the next cheapest
    all_taken = FakeVast(taken={101, 102})
    with pytest.raises(NoCapacityError, match="taken"):
        await VastProvider(_config(all_taken, allow_paid=True)).provision(SPEC)
    with pytest.raises(OfferUnavailableError):
        await VastProvider(_config(FakeVast(taken={101}), allow_paid=True)).cfg.client.create_instance(
            101, {"image": "x"}
        )


async def test_paid_provisioning_needs_explicit_permission_and_a_price_ceiling() -> None:
    fake = FakeVast()
    with pytest.raises(ProviderError, match="allow_paid"):
        await VastProvider(_config(fake)).provision(SPEC)
    with pytest.raises(ProviderError, match="max_price_per_hour_usd"):
        await VastProvider(_config(fake, allow_paid=True, max_price_per_hour_usd=0)).provision(SPEC)
    with pytest.raises(ProviderError, match="above max_price"):  # the class's listed estimate is above the ceiling
        await VastProvider(_config(fake, allow_paid=True, max_price_per_hour_usd=1.0)).provision(SPEC)
    assert fake.calls == []  # nothing reached Vast
    cheap = FakeVast([_offer(301, dph_total=2.0)])
    classes = _defaults()["classes"]
    classes = {**classes, "a100_80gb": {**classes["a100_80gb"], "price_per_hour_usd": 1.0}}
    capped = VastProvider(_config(cheap, allow_paid=True, max_price_per_hour_usd=1.9, classes=classes))
    with pytest.raises(NoCapacityError):  # the only offer costs more than the ceiling: never rented
        await capped.provision(SPEC)
    assert not [c for c in cheap.calls if c[0] == "PUT"]


async def test_malformed_answers_are_provider_errors() -> None:
    fake = FakeVast()
    provider = VastProvider(_config(fake, allow_paid=True))
    fake.fail[("POST", "/api/v0/bundles/")] = httpx.Response(200, json={"unexpected": True})
    with pytest.raises(ProviderError, match="no `offers`"):
        await provider.list_offers(gpu_class="a100_80gb", region="eu")
    fake.fail[("POST", "/api/v0/bundles/")] = httpx.Response(200, text="<html>maintenance</html>")
    with pytest.raises(ProviderError, match="not JSON"):
        await provider.provision(SPEC)
    del fake.fail[("POST", "/api/v0/bundles/")]
    fake.fail[("PUT", "/api/v0/asks/101/")] = httpx.Response(200, json={"success": True})  # no new_contract
    with pytest.raises(ProviderError, match="new_contract"):
        await provider.provision(SPEC)
    fake.fail[("GET", "/api/v0/instances/5/")] = httpx.Response(200, json={"instances": "nonsense"})
    with pytest.raises(ProviderError, match="not an object"):
        await provider.status("5")
    unmapped = VastProvider(
        _config(fake, allow_paid=True, classes={"a100_80gb": {"vram_gb": 80, "price_per_hour_usd": 1}})
    )
    with pytest.raises(ProviderError, match="no GPU mapping"):
        await unmapped.provision(SPEC)


def test_no_secret_is_committed_with_the_plugin() -> None:
    root = Path(__file__).parents[1]
    text = "\n".join(
        p.read_text(encoding="utf-8") for p in root.rglob("*") if p.suffix in {".py", ".yaml", ".toml", ".md", ".json"}
    )
    key = os.environ.get("VAST_API_KEY", "")
    if key:  # a real key in the environment must not appear in any plugin file
        assert key not in text
    assert "api_key:" not in (root / "src/ce_plugin_gpu_vast/plugin.yaml").read_text(encoding="utf-8")


# ------------------------------------------------------------------ recovery (production cutover)
async def test_instances_are_labeled_with_the_worker_and_listed_for_recovery() -> None:
    fake = FakeVast()
    provider = VastProvider(_config(fake, allow_paid=True))
    worker_id = "01a11c3a-c666-7063-8902-5dfa6e48163d"
    spec = SPEC.model_copy(update={"env": {**SPEC.env, "WORKER_ID": worker_id}})
    first = await provider.provision(spec)
    assert fake.bodies[-1]["label"] == f"ce-worker-{worker_id}"
    second = await provider.provision(SPEC)  # no worker id: the old family label, never a worker match
    fake.instances["999"] = {"id": 999, "actual_status": "running", "intended_status": "running", "label": "my-own-box"}
    fake.instances["1000"] = {"id": 1000, "actual_status": "running", "intended_status": "running", "label": None}
    listed = await provider.list_instances()  # four rows over two pages
    assert {i.external_id for i in listed} == {first.external_id, second.external_id}  # never someone else's
    by_id = {i.external_id: i for i in listed}
    assert by_id[first.external_id].detail["worker_id"] == worker_id
    assert by_id[second.external_id].detail["worker_id"] is None
    assert [c for c in fake.calls if c[1] == "/api/v1/instances/"] == [("GET", "/api/v1/instances/")] * 2


async def test_restart_reboots_in_place() -> None:
    fake = FakeVast()
    provider = VastProvider(_config(fake, allow_paid=True))
    instance = await provider.provision(SPEC)
    restarted = await provider.restart(instance.external_id)
    assert ("PUT", f"/api/v0/instances/reboot/{instance.external_id}/") in fake.calls
    assert not [c for c in fake.calls if c == ("PUT", f"/api/v0/instances/{instance.external_id}/")]  # no stop/start
    assert restarted.state == "running"
