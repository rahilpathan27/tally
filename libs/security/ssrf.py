"""Outbound-request guard for merchant-supplied webhook URLs.

Validation happens twice: the URL shape when the endpoint is registered, and the resolved
addresses immediately before every delivery. The dispatcher then connects to the vetted IP
(with the original host in ``Host`` and TLS SNI), so a DNS answer that changes between the
check and the connection (DNS rebinding) cannot redirect the request to an internal address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

_EXTRA_BLOCKED = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "100.64.0.0/10",  # carrier-grade NAT
        "192.0.0.0/24",
        "198.18.0.0/15",  # benchmarking
        "::ffff:0:0/96",  # IPv4-mapped; checked separately after unmapping
        "64:ff9b::/96",  # NAT64
        "2002::/16",  # 6to4 can embed private IPv4
    )
)
ALLOWED_PORTS = frozenset({443, 8443})


class UnsafeDestination(ValueError):
    """The URL or one of its resolved addresses is not a public internet destination."""


@dataclass(frozen=True, slots=True)
class WebhookTarget:
    scheme: str
    host: str
    port: int
    path: str
    trusted: bool


def is_public_address(address: IPAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return is_public_address(address.ipv4_mapped)
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    ):
        return False
    return not any(address in network for network in _EXTRA_BLOCKED)


def parse_webhook_url(url: str, trusted_hosts: Iterable[str] = ()) -> WebhookTarget:
    """Check URL shape. ``trusted_hosts`` is an explicit local-development escape hatch."""
    if len(url) > 2048 or any(ch in url for ch in ("\r", "\n", "\t", " ", "\\")):
        raise UnsafeDestination("URL contains forbidden characters")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    trusted = host in {h.lower() for h in trusted_hosts}
    if parts.scheme not in ({"https", "http"} if trusted else {"https"}):
        raise UnsafeDestination("webhook URLs must use https")
    if parts.username or parts.password:
        raise UnsafeDestination("credentials in URLs are not allowed")
    if not host or parts.fragment:
        raise UnsafeDestination("URL must have a host and no fragment")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeDestination("invalid port") from exc
    if not trusted:
        if port not in ALLOWED_PORTS:
            raise UnsafeDestination("port is not allowed")
        if host == "localhost" or host.endswith((".localhost", ".internal", ".local")):
            raise UnsafeDestination("internal host names are not allowed")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not is_public_address(literal):
            raise UnsafeDestination("address is not public")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return WebhookTarget(parts.scheme, host, port, path, trusted)


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return sorted({str(info[4][0]) for info in infos})


async def vetted_address(target: WebhookTarget, resolver: Resolver = system_resolver) -> str:
    """Resolve once and require every answer to be public; return the IP to connect to."""
    if target.trusted:
        return target.host
    try:
        literal = ipaddress.ip_address(target.host)
    except ValueError:
        literal = None
    addresses = [str(literal)] if literal is not None else await resolver(target.host, target.port)
    if not addresses:
        raise UnsafeDestination("host did not resolve")
    for raw in addresses:
        if not is_public_address(ipaddress.ip_address(raw.split("%", 1)[0])):
            raise UnsafeDestination("host resolves to a non-public address")
    return addresses[0]
