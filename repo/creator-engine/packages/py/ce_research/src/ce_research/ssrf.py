"""The SSRF-guarded fetcher (§33). Every URL fetch in the system goes through it.

For each hop (the URL, then every redirect target):

1. only `http`/`https`, no credentials in the URL, a host, a port from `allowed_ports`;
2. **resolve** the host (IP literals in any form the resolver accepts, such as `2130706433` or
   `0x7f.1`, are resolved too);
3. **check** every resolved address: anything that is not globally routable is refused — private,
   loopback, link-local (including the cloud metadata address 169.254.169.254), shared CGNAT,
   multicast, reserved and unspecified ranges, and IPv6 forms that embed such an IPv4 address
   (mapped, NAT64, 6to4, Teredo). One bad address refuses the host, so a DNS answer that mixes a
   public and a private address cannot be used to rebind;
4. **connect** to the checked address itself — the request goes to the IP with the original `Host`
   header and TLS server name — so a second DNS lookup can never swap in another address.

The response is streamed with a size cap, under one deadline for the whole fetch (redirects
included), and only the configured text content types are accepted. Proxies from the environment
are ignored: an egress proxy would resolve the name itself and defeat step 4.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.message import Message
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from ce_config.schemas import ResearchFetchConfig

__all__ = [
    "FetchError",
    "FetchedDocument",
    "GuardedFetcher",
    "Resolver",
    "SSRFBlocked",
    "blocked_reason",
    "system_resolver",
]

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_EXTRA_BLOCKED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",  # "this network"
        "100.64.0.0/10",  # shared address space (CGNAT)
        "192.0.0.0/24",  # IETF protocol assignments
        "198.18.0.0/15",  # benchmarking
        "64:ff9b:1::/48",  # local-use NAT64
        "fd00:ec2::/32",  # cloud metadata over IPv6 (inside fc00::/7, listed for clarity)
    )
)


class FetchError(Exception):
    """The fetch failed for a reason that is not a policy refusal (status, size, time, type)."""


class SSRFBlocked(FetchError):
    """The URL or an address it resolves to is not allowed."""


@dataclass(frozen=True)
class FetchedDocument:
    url: str  # as requested
    final_url: str  # after redirects
    status: int
    content_type: str
    text: str
    size_bytes: int
    address: str  # the checked address the final hop connected to
    redirects: int
    body: bytes = b""  # the raw body (binary documents such as PDFs; Phase 12 ingestion)


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def blocked_reason(address: str | IPAddress) -> str | None:
    """Why this address may not be fetched from, or None when it is globally routable."""
    try:
        ip = ipaddress.ip_address(address) if isinstance(address, str) else address
    except ValueError:
        return f"{address!r} is not an IP address"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:
            return f"{ip} has a zone index"
        inner = _embedded_ipv4(ip)
        if inner is not None:
            reason = blocked_reason(inner)
            return f"{ip} embeds {inner}: {reason}" if reason else None
    for network in _EXTRA_BLOCKED:
        if ip.version == network.version and ip in network:
            return f"{ip} is in the blocked range {network}"
    checks = (
        ("loopback", ip.is_loopback),
        ("link-local", ip.is_link_local),
        ("private", ip.is_private),
        ("multicast", ip.is_multicast),
        ("reserved", ip.is_reserved),
        ("unspecified", ip.is_unspecified),
    )
    for name, hit in checks:
        if hit:
            return f"{ip} is a {name} address"
    if not ip.is_global:
        return f"{ip} is not globally routable"
    return None


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(f"cannot resolve {host}: {exc}") from exc
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _content_type(header: str) -> tuple[str, str | None]:
    message = Message()
    message["content-type"] = header or "application/octet-stream"
    return message.get_content_type(), message.get_content_charset()


class GuardedFetcher:
    def __init__(
        self,
        limits: ResearchFetchConfig,
        *,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.limits = limits
        self.resolver = resolver or system_resolver
        self.transport = transport

    def check_url(self, url: str) -> tuple[str, str, int]:
        """(scheme, host, port) of an acceptable URL, or SSRFBlocked."""
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise SSRFBlocked(f"scheme {scheme or '(none)'!r} is not allowed")
        if parts.username is not None or parts.password is not None:
            raise SSRFBlocked("credentials in URLs are not allowed")
        host = parts.hostname
        if not host:
            raise SSRFBlocked("the URL has no host")
        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise SSRFBlocked(f"invalid port: {exc}") from exc
        if port not in self.limits.allowed_ports:
            raise SSRFBlocked(f"port {port} is not allowed")
        return scheme, host.rstrip("."), port

    def precheck(self, url: str) -> None:
        """The checks that need no network (API request validation): `check_url`, plus IP-literal
        hosts that are blocked and `localhost` names. Resolution still decides at fetch time."""
        _, host, _ = self.check_url(url)
        lowered = host.lower()
        if lowered == "localhost" or lowered.endswith(".localhost"):
            raise SSRFBlocked(f"{host} is a loopback name")
        try:
            literal: IPAddress | None = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and (reason := blocked_reason(literal)) is not None:
            raise SSRFBlocked(f"{host} is a blocked address: {reason}")

    async def resolve_checked(self, host: str, port: int) -> str:
        """Resolves and checks every address; returns the one to connect to."""
        try:
            literal: IPAddress | None = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        addresses = [str(literal)] if literal is not None else await self.resolver(host, port)
        if not addresses:
            raise FetchError(f"{host} has no addresses")
        for address in addresses:
            reason = blocked_reason(address)
            if reason is not None:
                raise SSRFBlocked(f"{host} resolves to a blocked address: {reason}")
        return addresses[0]

    async def fetch(self, url: str) -> FetchedDocument:
        try:
            async with asyncio.timeout(self.limits.timeout_s):
                return await self._fetch(url)
        except TimeoutError as exc:
            raise FetchError(f"fetching {url} took longer than {self.limits.timeout_s} s") from exc

    async def _fetch(self, url: str) -> FetchedDocument:
        current = url
        timeout = httpx.Timeout(self.limits.timeout_s)
        async with httpx.AsyncClient(
            transport=self.transport, trust_env=False, follow_redirects=False, timeout=timeout
        ) as client:
            for hop in range(self.limits.max_redirects + 1):
                scheme, host, port = self.check_url(current)
                address = await self.resolve_checked(host, port)
                parts = urlsplit(current)
                ip_host = f"[{address}]" if ":" in address else address
                default_port = 443 if scheme == "https" else 80
                netloc = ip_host if port == default_port else f"{ip_host}:{port}"
                pinned = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
                host_header = host if port == default_port else f"{host}:{port}"
                request = client.build_request(
                    "GET",
                    pinned,
                    headers={
                        "Host": host_header,
                        "User-Agent": self.limits.user_agent,
                        "Accept": ", ".join(self.limits.allowed_content_types),
                        "Accept-Encoding": "identity",  # the size cap counts what we decode
                    },
                    extensions={"sni_hostname": host} if scheme == "https" else {},
                )
                response = await client.send(request, stream=True)
                try:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError(f"redirect from {current} without a Location")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise FetchError(f"{current} answered HTTP {response.status_code}")
                    media_type, charset = _content_type(response.headers.get("content-type", ""))
                    if media_type not in self.limits.allowed_content_types:
                        raise FetchError(f"content type {media_type} is not accepted")
                    body = await self._read_capped(response)
                finally:
                    await response.aclose()
                textual = media_type.startswith("text/") or media_type in ("application/xhtml+xml", "application/json")
                text = body.decode(charset or "utf-8", errors="replace") if textual else ""
                return FetchedDocument(
                    url=url,
                    final_url=current,
                    status=response.status_code,
                    content_type=media_type,
                    text=text,
                    size_bytes=len(body),
                    address=address,
                    redirects=hop,
                    body=body,
                )
        raise FetchError(f"more than {self.limits.max_redirects} redirects")

    async def _read_capped(self, response: httpx.Response) -> bytes:
        cap = self.limits.max_bytes
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > cap:
            raise FetchError(f"the body is {declared} bytes; the limit is {cap}")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > cap:
                raise FetchError(f"the body exceeds the limit of {cap} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
