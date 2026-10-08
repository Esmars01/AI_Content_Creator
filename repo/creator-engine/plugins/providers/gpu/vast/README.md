# Vast.ai GPU provider (`vast`)

An **optional, additional** GPU provider. It rents instances on the Vast.ai marketplace through the
same `GPUProvider` interface as the other providers.

- **RunPod remains supported and unchanged** (`runpod_pod`, `runpod_serverless`), as do `local`,
  `local_docker` and `mock`.
- The scheduler stays provider-agnostic: Vast is one more plugin found through the plugin registry
  (entry point `gpu.vast`).

> **Status: experimental, `untested_on_gpu`.**
> - No live Vast API call has been made.
> - No Vast GPU has been rented.
> - No A100 (or any other) test has run on it.
>
> The request and response shapes come from Vast's own Python client, `vastai` 1.8.3 (PyPI, repository
> `github.com/vast-ai/vast-cli`), and are checked in mocked tests. The first live session must follow
> the steps below.

## What it does

| Method | Vast API |
|---|---|
| `provision(spec)` | `POST /api/v0/bundles/` (offer search), then `PUT /api/v0/asks/{offer_id}/` (rent the cheapest acceptable offer) |
| `status(id)` | `GET /api/v0/instances/{id}/?owner=me` |
| `start(id)` / `stop(id)` | `PUT /api/v0/instances/{id}/` with `{"state": "running" \| "stopped"}` |
| `terminate(id)` | `DELETE /api/v0/instances/{id}/` (an instance Vast no longer knows counts as terminated) |
| `list_offers(class, region)` | live on-demand offers, one `GPUOffer` per Vast offer |
| `price(instance)` | the price captured at rental: the offer's `dph_total`, or the interruptible bid |
| `health()` | `GET /api/v0/users/current` (the answer, which contains the account key, is discarded) |
| `restart(id)` | `PUT /api/v0/instances/reboot/{id}/` (the disk and model cache survive) |
| `list_instances()` | `GET /api/v1/instances/` paged by `next_token`; only instances labeled `ce-worker-…` are returned, for reconciliation and orphan detection |

**The rental body**:
- `image`: from `spec.image`, else `images["family:variant"]`, else `image_template`;
- `env`: the provider's `env`, then `spec.env` (the worker's `SCHEDULER_URL`, `WORKER_TOKEN`, family,
  class, region…), plus `MODEL_CACHE_DIR`;
- `disk`: the larger of `storage.disk_gb` and the size computed for the spec (`ProvisionSpec.disk_gb`, from the
  manifests' declared model sizes; see `docs/PRODUCTION_MODEL_AND_GPU_OPERATIONS.md`);
- `label`: `ce-worker-<worker_id>`, so the scheduler can find an instance whose rental it never recorded;
- `image_login`: resolved from `image_login_ref`, when it is set;
- `runtype: args`, so the image's own entrypoint runs (no SSH or Jupyter injected);
- `cancel_unavail: true`, so a failed placement does not leave a stopped instance behind;
- `price`: `null` for on-demand, or the bid;
- optionally `volume_info` (a linked volume).

**Errors**:
- No acceptable offer: `NoCapacityError` (the fleet falls back to the next class or provider).
- An offer rented by someone else since the search: the next cheapest offer, up to `rent_attempts`,
  then `NoCapacityError`.
- Anything else: `ProviderError`.
- A rental is **never retried** after a 5xx or a timeout, because it may have gone through. It raises
  `ProvisionOutcomeUnknown`: the fleet keeps the worker row, and reconciliation adopts the instance by its
  label, or finds that none exists. So a timeout cannot double-rent.

## Safe by default

- **Paid: inert until enabled.** `allow_paid: false` by default, as for RunPod. A paid provider is used
  only through an administrator-created, enabled `gpu_providers` row.
- **Price ceiling.** `max_price_per_hour_usd` (default 2.5) must be set. Offers above it are never
  rented, even if Vast returns them.
- **Spot is off by default.** Interruptible ("bid") rentals happen only when all three of these allow
  it: `interruptible.enabled`, the pool's `spot_ok` and the class's `spot`.
- **No secrets** in the manifest, the database or logs. The API key is never echoed in an error, and
  neither is the per-instance key Vast returns at rental.

## Configure

### 1. The API key

Never put it in source, YAML, `.env.example` or the database. Use one of:

- the scheduler's environment: `VAST_API_KEY=…`, in a real `.env` that is not committed, or a secret
  manager;
- or a provider row's `credentials_ref`: `env:VAST_API_KEY`, or `file:/run/secrets/vast_api_key`.

### 2. The provider row

The row is created disabled for spending:

```bash
curl -X POST "$API/v1/admin/gpu/providers" -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"kind": "vast", "name": "vast-eu", "credentials_ref": "env:VAST_API_KEY", "regions": ["eu"],
       "enabled": true, "budget_daily_usd": 5,
       "config": {"max_price_per_hour_usd": 1.8,
                  "image_template": "ghcr.io/<org>/creator-engine-worker-{family}:{variant}"}}'
```

### 3. Enabling paid provisioning

This is the owner's spending approval. It needs a note, which is audited as
`gpu_provider.paid_enabled`:

```bash
curl -X PATCH "$API/v1/admin/gpu/providers/<id>" -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"config": {"allow_paid": true, "max_price_per_hour_usd": 1.8}, "note": "owner approved 5 USD/day for the A100 smoke test"}'
```

### 4. GPU classes and regions

These are manifest defaults, which a row's `config` can override.

- **`classes`** maps a fleet GPU class to Vast `gpu_name` values (spelled as in Vast's GPU list, for
  example `"A100 SXM4"` or `"RTX 4090"`). Each class also has:
  - `gpu_count`;
  - `vram_gb`, and optionally `min_vram_gb` (the search asks `gpu_ram ≥ min_vram_gb`, so 40 GB A100s are
    excluded from `a100_80gb`);
  - `spot`;
  - `price_per_hour_usd`: a placeholder estimate used only by the class guard. Real prices come from
    the search.

  The defaults cover `rtx_4090_24gb`, `rtx_5090_32gb`, `rtx_pro_6000_96gb` and `a100_80gb`.
- **`regions`** maps a fleet region to two-letter country codes (`eu: [SE, NO, …]`, `us: [US, CA]`). An
  empty list means anywhere.
- **`search`** holds the offer filters:
  - `verified` (true);
  - `min_reliability` (0.98);
  - `min_cuda` (12.8);
  - `limit`;
  - `rent_attempts`.
- **`storage`**:
  - `disk_gb` is the container disk, which holds the model cache.
  - `model_cache_dir` (`/models`) becomes `MODEL_CACHE_DIR` in the container.
  - `volume: {volume_id, machine_id, mount_path}` links an existing Vast volume. A Vast volume lives on
    one machine, so the search is then limited to that machine.

### 5. A pool for the fleet

The fleet uses a provider only through a pool (`config/gpu/pools.yaml`). The repository adds **no** Vast
pool and no new GPU class there, because the scheduler derives its VRAM escalation steps from those
classes. For an A100 test, an operator adds the class and a pool, kept `enabled: false` until the owner
approves:

```yaml
classes:
  a100_80gb: {vram_gb: 80}
pools:
  - {id: a100_vast, gpu_classes: [a100_80gb], providers: [vast], families: [wan, vllm], min: 0, max: 1,
     idle_timeout_s: 300, target_latency_s: 600, spot_ok: false, regions: [eu], enabled: false,
     min_driver_version: "570"}
```

Listing `vast` in an existing pool's `providers` after `runpod_pod` makes Vast a fallback when RunPod
has no capacity. The RunPod pools in the repository are not changed.

## Before the first live test

1. **Read-only check.** Set the key, then call `health()` and `list_offers("a100_80gb", "eu")`. These
   make no rental and spend nothing.
2. **Approve the spend.** The owner approves it, the administrator PATCHes `allow_paid` with a note,
   and the daily budget and `max_price_per_hour_usd` are set low.
3. **Provision one worker** through the fleet. Watch it register with the scheduler, then terminate it
   and confirm on Vast's console that no instance is left.
4. Only then call it validated, through the documented promotion gates, never by editing `validation:`
   by hand.

**Known limits:**
- A private registry login is set as `image_login_ref` (`env:NAME` or `file:/path`, holding Vast's
  `image_login` string, e.g. `-u USER -p TOKEN ghcr.io`). It is resolved when the instance is rented and is
  never stored or returned.
- `ProvisionSpec.volumes` is ignored, as for RunPod.
- Vast publishes no error schema for an offer taken between search and rental. The provider treats a
  4xx saying the offer is unavailable as "taken" and anything else as an error. This must be confirmed
  live.

## Tests

The tests are mocked: an httpx `MockTransport` fake of the Vast API. They make no network call and use
no key:

```bash
uv run pytest plugins/providers/gpu/vast        # Vast provider
uv run pytest plugins/providers/gpu             # every GPU provider (RunPod, local Docker, local, Vast)
```

Every request body and search query is checked against `tests/vast_schema.json`, the field lists
extracted from `vastai` 1.8.3.
