"""Bounded HTTP response reading and public-only provider DNS resolution."""

import asyncio
import ipaddress
import json
import socket
from typing import Any

import aiohttp

_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Same public-unicast rule as tooling.helpers._ip_is_public."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_multicast or ip.is_reserved or getattr(ip, "is_site_local", False):
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64:
        return False
    return bool(ip.is_global)


class _PublicOnlyResolver(aiohttp.abc.AbstractResolver):
    """Resolve only globally routable provider IPs and pin checked results."""

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        try:
            literal = ipaddress.ip_address(str(host).strip("[]"))
        except ValueError:
            literal = None
        if literal is not None:
            if not _ip_is_public(literal):
                raise OSError("provider host resolved to a non-public address")
            addresses = [
                (
                    socket.AF_INET6 if literal.version == 6 else socket.AF_INET,
                    str(literal),
                )
            ]
        else:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM
            )
            addresses = []
            for af, _socktype, _proto, _canonname, sockaddr in infos:
                ip = ipaddress.ip_address(sockaddr[0])
                if not _ip_is_public(ip):
                    raise OSError("provider host resolved to a non-public address")
                addresses.append((af, str(ip)))
        if not addresses:
            raise OSError("provider host did not resolve")
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": af,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST,
            }
            for af, address in addresses
        ]

    async def close(self):
        return None


async def _read_response_text_limited(resp, limit: int = 64 * 1024) -> str:
    """Bound untrusted provider error bodies before decoding/logging."""
    content = getattr(resp, "content", None)
    if content is None or not hasattr(content, "iter_chunked"):
        try:
            return (await resp.text())[:limit]
        except Exception:
            return ""
    body = bytearray()
    async for chunk in content.iter_chunked(8192):
        remaining = limit + 1 - len(body)
        if remaining > 0:
            body.extend(chunk[:remaining])
        if len(body) > limit:
            body = body[:limit]
            break
    return bytes(body).decode("utf-8", errors="replace")


async def _read_json_response_limited(resp, limit: int) -> Any:
    """Read and parse a bounded JSON response from an untrusted BYOK host."""
    content = getattr(resp, "content", None)
    if content is None or not hasattr(content, "read"):
        # Small test doubles and compatible response adapters may only expose
        # aiohttp's json() convenience method.
        return await resp.json()
    raw = bytearray()
    while True:
        chunk = await content.read(min(64 * 1024, limit + 1 - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
        if len(raw) > limit:
            raise RuntimeError("Provider response exceeded the configured size limit")
    charset = getattr(resp, "charset", None) or "utf-8"
    try:
        return json.loads(raw.decode(charset))
    except (UnicodeError, LookupError, json.JSONDecodeError) as exc:
        raise RuntimeError("Provider returned invalid JSON") from exc


def normalize_base_url(base_url: str) -> str:
    """Normalize an OpenAI-compatible base URL to the API root.

    Requests are built as ``{base_url}/chat/completions``, so the base has
    to include the API path segment. Everyone pastes the bare host
    ("http://localhost:11434", "https://api.openai.com"), which then 404s in
    a way that looks like a broken bot rather than a missing "/v1". If the
    URL carries no path at all we add the conventional one; a URL that
    already has a path (/v1, /v2, /api/v1, ...) is left exactly as given.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return base
    _, _, rest = base.partition("://")
    if not rest:  # no scheme: treat the whole thing as a host
        rest = base
    if "/" in rest:  # already carries a path — the operator's business
        return base
    return f"{base}/v1"
