"""SSRF guard (§33, Phase 4 DoD): blocked ranges, resolve → check → connect to the checked address,
redirect re-checks, and size, time, type and scheme limits. No test touches the network: names
resolve through a fake resolver (or the system resolver for numeric forms, which needs no DNS) and
HTTP goes to an in-process transport that records what was dialled."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from ce_config.schemas import ResearchFetchConfig
from ce_research import FetchError, GuardedFetcher, SSRFBlocked, blocked_reason
from ce_research.ssrf import system_resolver

LIMITS = ResearchFetchConfig(timeout_s=2, max_bytes=1000, max_redirects=2)
PUBLIC = "93.184.215.14"
PUBLIC6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"

BLOCKED = [
    "127.0.0.1",
    "127.8.9.10",
    "10.0.0.5",
    "172.16.3.4",
    "172.31.255.255",
    "192.168.1.1",
    "169.254.169.254",  # cloud metadata
    "169.254.10.10",
    "100.64.0.1",  # CGNAT
    "0.0.0.0",
    "0.1.2.3",
    "224.0.0.1",
    "240.0.0.1",
    "255.255.255.255",
    "198.18.0.1",
    "::1",
    "::",
    "fe80::1",
    "fc00::1",
    "fd00:ec2::254",  # metadata over IPv6
    "::ffff:127.0.0.1",  # IPv4-mapped
    "::ffff:169.254.169.254",
    "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
    "2002:7f00:1::",  # 6to4 of 127.0.0.1
    "ff02::1",
]


class Recorder:
    """An in-process transport: answers from a route table and records each request."""

    def __init__(self, routes: dict[str, httpx.Response | list[httpx.Response]] | None = None) -> None:
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.headers["host"]
        answer = self.routes.get(f"{host}{request.url.path}")
        if isinstance(answer, list):
            return answer.pop(0)
        return answer or httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def resolver(table: dict[str, list[str]]):  # type: ignore[no-untyped-def]
    async def resolve(host: str, port: int) -> list[str]:
        return table[host]

    return resolve


def html(body: str, **headers: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8", **headers}, text=body)


@pytest.mark.parametrize("address", BLOCKED)
def test_non_global_addresses_are_blocked(address: str) -> None:
    assert blocked_reason(address) is not None


@pytest.mark.parametrize("address", [PUBLIC, "1.1.1.1", "8.8.8.8", PUBLIC6, "::ffff:8.8.8.8"])
def test_public_addresses_pass(address: str) -> None:
    assert blocked_reason(address) is None


@pytest.mark.parametrize("address", BLOCKED[:12])
async def test_hostnames_resolving_to_blocked_addresses_are_refused_before_any_request(address: str) -> None:
    recorder = Recorder()
    fetcher = GuardedFetcher(LIMITS, resolver=resolver({"evil.test": [address]}), transport=recorder.transport())
    with pytest.raises(SSRFBlocked):
        await fetcher.fetch("http://evil.test/")
    assert recorder.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://2130706433/",  # decimal 127.0.0.1
        "http://0x7f.1/",  # hex/short form
        "http://017700000001/",  # octal
        "http://localhost/",
    ],
)
async def test_ip_literals_in_any_form_are_refused(url: str) -> None:
    recorder = Recorder()
    fetcher = GuardedFetcher(LIMITS, resolver=system_resolver, transport=recorder.transport())
    with pytest.raises((SSRFBlocked, FetchError)) as caught:
        await fetcher.fetch(url)
    assert recorder.requests == []
    assert isinstance(caught.value, SSRFBlocked) or "resolve" in str(caught.value)


async def test_a_mixed_dns_answer_is_refused() -> None:
    """One private address among public ones refuses the host (no rebinding through a second pick)."""
    recorder = Recorder()
    fetcher = GuardedFetcher(
        LIMITS, resolver=resolver({"mixed.test": [PUBLIC, "10.0.0.1"]}), transport=recorder.transport()
    )
    with pytest.raises(SSRFBlocked):
        await fetcher.fetch("https://mixed.test/")
    assert recorder.requests == []


async def test_the_connection_goes_to_the_checked_address_with_host_and_sni() -> None:
    calls: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        calls.append(host)
        return [PUBLIC]

    recorder = Recorder({"news.test/a": html("<title>T</title><p>Hello</p>")})
    fetcher = GuardedFetcher(LIMITS, resolver=resolve, transport=recorder.transport())
    document = await fetcher.fetch("https://news.test/a")
    assert calls == ["news.test"]  # resolved once; the request never resolves again
    (request,) = recorder.requests
    assert request.url.host == PUBLIC and request.url.scheme == "https"
    assert request.headers["host"] == "news.test"
    assert request.extensions["sni_hostname"] == "news.test"
    assert document.address == PUBLIC and document.text.startswith("<title>")


async def test_ipv6_addresses_are_bracketed() -> None:
    recorder = Recorder({"six.test/": html("<p>v6</p>")})
    fetcher = GuardedFetcher(LIMITS, resolver=resolver({"six.test": [PUBLIC6]}), transport=recorder.transport())
    await fetcher.fetch("http://six.test/")
    assert recorder.requests[0].url.host == PUBLIC6


async def test_every_redirect_hop_is_checked_again() -> None:
    recorder = Recorder(
        {
            "start.test/": httpx.Response(302, headers={"location": "http://internal.test/admin"}),
        }
    )
    table = {"start.test": [PUBLIC], "internal.test": ["192.168.0.10"]}
    fetcher = GuardedFetcher(LIMITS, resolver=resolver(table), transport=recorder.transport())
    with pytest.raises(SSRFBlocked, match=r"internal\.test"):
        await fetcher.fetch("http://start.test/")
    assert [r.headers["host"] for r in recorder.requests] == ["start.test"]


async def test_redirects_to_other_schemes_and_too_many_redirects_fail() -> None:
    recorder = Recorder({"a.test/": httpx.Response(301, headers={"location": "file:///etc/passwd"})})
    fetcher = GuardedFetcher(LIMITS, resolver=resolver({"a.test": [PUBLIC]}), transport=recorder.transport())
    with pytest.raises(SSRFBlocked, match="scheme"):
        await fetcher.fetch("http://a.test/")
    loop = Recorder({"b.test/": [httpx.Response(302, headers={"location": "/"}) for _ in range(5)]})
    fetcher = GuardedFetcher(LIMITS, resolver=resolver({"b.test": [PUBLIC]}), transport=loop.transport())
    with pytest.raises(FetchError, match="redirects"):
        await fetcher.fetch("http://b.test/")
    assert len(loop.requests) == LIMITS.max_redirects + 1


async def test_a_relative_redirect_is_followed() -> None:
    recorder = Recorder(
        {"c.test/old": httpx.Response(308, headers={"location": "/new"}), "c.test/new": html("<p>moved</p>")}
    )
    fetcher = GuardedFetcher(LIMITS, resolver=resolver({"c.test": [PUBLIC]}), transport=recorder.transport())
    document = await fetcher.fetch("http://c.test/old")
    assert document.final_url == "http://c.test/new" and document.redirects == 1


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("file:///etc/passwd", "scheme"),
        ("ftp://a.test/x", "scheme"),
        ("gopher://a.test/", "scheme"),
        ("http://user:pass@a.test/", "credentials"),
        ("http://a.test:22/", "port"),
        ("http://a.test:6379/", "port"),
        ("http:///nohost", "host"),
    ],
)
async def test_url_shapes_outside_the_policy_are_refused(url: str, match: str) -> None:
    recorder = Recorder()
    fetcher = GuardedFetcher(LIMITS, resolver=resolver({"a.test": [PUBLIC]}), transport=recorder.transport())
    with pytest.raises(SSRFBlocked, match=match):
        await fetcher.fetch(url)
    assert recorder.requests == []


async def test_size_limits_apply_to_declared_and_streamed_bodies() -> None:
    big = "x" * (LIMITS.max_bytes + 1)
    declared = Recorder({"d.test/": html(big)})
    fetcher = GuardedFetcher(LIMITS, resolver=resolver({"d.test": [PUBLIC]}), transport=declared.transport())
    with pytest.raises(FetchError, match="limit"):
        await fetcher.fetch("http://d.test/")

    async def stream() -> AsyncIterator[bytes]:
        for _ in range(10):
            yield b"y" * 200

    streamed = Recorder({"e.test/": httpx.Response(200, headers={"content-type": "text/plain"}, content=stream())})
    fetcher = GuardedFetcher(LIMITS, resolver=resolver({"e.test": [PUBLIC]}), transport=streamed.transport())
    with pytest.raises(FetchError, match="exceeds"):
        await fetcher.fetch("http://e.test/")


async def test_the_whole_fetch_has_a_deadline() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return html("late")

    fetcher = GuardedFetcher(
        ResearchFetchConfig(timeout_s=0.2),
        resolver=resolver({"slow.test": [PUBLIC]}),
        transport=httpx.MockTransport(slow),
    )
    with pytest.raises(FetchError, match="longer than"):
        await fetcher.fetch("http://slow.test/")


async def test_only_text_content_types_and_200_are_accepted() -> None:
    recorder = Recorder(
        {
            "f.test/bin": httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"\0"),
            "f.test/gone": httpx.Response(410),
        }
    )
    fetcher = GuardedFetcher(LIMITS, resolver=resolver({"f.test": [PUBLIC]}), transport=recorder.transport())
    with pytest.raises(FetchError, match="content type"):
        await fetcher.fetch("http://f.test/bin")
    with pytest.raises(FetchError, match="410"):
        await fetcher.fetch("http://f.test/gone")


async def test_ingestion_limits_accept_pdf_bodies_as_bytes() -> None:
    pdf = b"%PDF-1.4\n" + bytes(range(256))
    recorder = Recorder(
        {"g.test/doc.pdf": httpx.Response(200, headers={"content-type": "application/pdf"}, content=pdf)}
    )
    limits = ResearchFetchConfig(timeout_s=2, max_bytes=10_000, allowed_content_types=["application/pdf", "text/html"])
    fetcher = GuardedFetcher(limits, resolver=resolver({"g.test": [PUBLIC]}), transport=recorder.transport())
    document = await fetcher.fetch("http://g.test/doc.pdf")
    assert document.body == pdf and document.text == ""  # binary bodies are never decoded as text
    with pytest.raises(FetchError, match="content type"):  # the planning limits still refuse PDFs
        await GuardedFetcher(LIMITS, resolver=resolver({"g.test": [PUBLIC]}), transport=recorder.transport()).fetch(
            "http://g.test/doc.pdf"
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::ffff:10.0.0.1]/",
        "http://localhost/",
        "http://api.localhost./",
        "ftp://example.com/",
        "https://u:p@example.com/",
    ],
)
def test_the_precheck_refuses_without_the_network(url: str) -> None:
    with pytest.raises(SSRFBlocked):
        GuardedFetcher(LIMITS).precheck(url)


def test_the_precheck_leaves_names_to_fetch_time() -> None:
    GuardedFetcher(LIMITS).precheck("https://example.com/report")  # resolution decides later
    GuardedFetcher(LIMITS).precheck(f"http://{PUBLIC}/")
