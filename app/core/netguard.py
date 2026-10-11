"""SSRF protection: decide whether an outbound URL is safe to fetch.

What: ``validate_url`` checks scheme, port, credentials, host allow-list and - crucially -
that EVERY IP the hostname resolves to is a public address.
Why: Server-Side Request Forgery lets an attacker (or a prompt-injected model) make OUR server
call internal services: cloud metadata (169.254.169.254), databases, admin panels. Defence in
depth: allow-list of hosts first, then IP-range checks on the resolved addresses.
Why return the resolved IP: the caller connects to that exact IP, so a DNS-rebinding attacker
cannot answer "public" to our check and "127.0.0.1" to the real connection.
Connects to: ``tools/builtin/http_fetch.py``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]


class UnsafeUrlError(Exception):
    """The URL must not be fetched (message is safe to show to the caller)."""


@dataclass(frozen=True)
class SafeTarget:
    url: str
    scheme: str
    host: str
    port: int
    ip: str


async def default_resolver(host: str, port: int) -> list[str]:
    """Resolve ``host`` to all of its IP addresses using the system resolver."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def is_public_ip(address: str) -> bool:
    """True only for globally routable addresses (blocks private, loopback, link-local, ...)."""
    ip: IPAddress = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is just 127.0.0.1 in disguise
    return ip.is_global and not ip.is_multicast


def _host_allowed(host: str, allowed: list[str]) -> bool:
    return any(host == a or host.endswith("." + a) for a in (h.lower().strip(".") for h in allowed))


async def validate_url(
    url: str, allowed_hosts: list[str], resolver: Resolver = default_resolver
) -> SafeTarget:
    """Return a ``SafeTarget`` or raise ``UnsafeUrlError``."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrlError("only http and https URLs are allowed")
    if parts.username or parts.password:
        raise UnsafeUrlError("credentials in URLs are not allowed")
    host = (parts.hostname or "").lower().strip(".")
    if not host:
        raise UnsafeUrlError("URL has no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UnsafeUrlError("IP-address hosts are not allowed")
    if not _host_allowed(host, allowed_hosts):
        raise UnsafeUrlError(f"host '{host}' is not in the allow-list")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeUrlError("invalid port") from exc
    if port not in (80, 443):
        raise UnsafeUrlError("only ports 80 and 443 are allowed")

    try:
        addresses = await resolver(host, port)
    except OSError as exc:
        raise UnsafeUrlError("host could not be resolved") from exc
    if not addresses:
        raise UnsafeUrlError("host could not be resolved")
    # WHY all addresses: a hostname with one public and one private record is still dangerous.
    if not all(is_public_ip(a) for a in addresses):
        raise UnsafeUrlError("host resolves to a non-public address")
    return SafeTarget(url=url, scheme=parts.scheme, host=host, port=port, ip=addresses[0])
