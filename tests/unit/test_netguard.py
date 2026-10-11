"""SSRF guard: allow-list, scheme/port/credential rules, public-IP enforcement."""

import pytest

from app.core.netguard import UnsafeUrlError, is_public_ip, validate_url

ALLOWED = ["example.com"]


def resolver_for(*addresses: str):
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


@pytest.mark.parametrize(
    ("ip", "public"),
    [
        ("93.184.216.34", True),
        ("8.8.8.8", True),
        ("2606:2800:220:1:248:1893:25c8:1946", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.5", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),  # cloud metadata endpoint
        ("0.0.0.0", False),  # noqa: S104
        ("100.64.0.1", False),  # carrier-grade NAT
        ("224.0.0.1", False),
        ("::1", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("::ffff:127.0.0.1", False),  # IPv4-mapped loopback
        ("::ffff:10.0.0.1", False),
    ],
)
def test_is_public_ip(ip: str, public: bool) -> None:
    assert is_public_ip(ip) is public


async def test_valid_url_returns_pinned_target() -> None:
    target = await validate_url("https://example.com/a?b=1", ALLOWED, resolver_for("93.184.216.34"))
    assert (target.host, target.port, target.ip, target.scheme) == (
        "example.com",
        443,
        "93.184.216.34",
        "https",
    )


async def test_subdomain_of_allowed_host_ok_but_lookalike_not() -> None:
    ok = await validate_url("http://docs.example.com", ALLOWED, resolver_for("93.184.216.34"))
    assert ok.port == 80
    for evil in ("http://evil-example.com", "http://example.com.evil.io", "http://notexample.com"):
        with pytest.raises(UnsafeUrlError, match="allow-list"):
            await validate_url(evil, ALLOWED, resolver_for("93.184.216.34"))


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://example.com/x", "only http"),
        ("file:///etc/passwd", "only http"),
        ("gopher://example.com", "only http"),
        ("http://user:pw@example.com/", "credentials"),
        ("http://example.com@evil.io/", "credentials"),
        ("http://127.0.0.1/", "IP-address"),
        ("http://[::1]/", "IP-address"),
        ("http://2130706433/", "allow-list"),  # decimal IP is not allow-listed either
        ("http://example.com:8080/", "ports"),
        ("http://example.com:99999/", "invalid port"),
        ("http:///nohost", "no host"),
    ],
)
async def test_rejected_urls(url: str, message: str) -> None:
    with pytest.raises(UnsafeUrlError, match=message):
        await validate_url(url, ALLOWED, resolver_for("93.184.216.34"))


async def test_hostname_resolving_to_private_address_is_blocked() -> None:
    for bad in ("127.0.0.1", "169.254.169.254", "10.0.0.8", "::1"):
        with pytest.raises(UnsafeUrlError, match="non-public"):
            await validate_url("https://example.com", ALLOWED, resolver_for(bad))


async def test_one_private_record_among_public_ones_blocks() -> None:
    with pytest.raises(UnsafeUrlError, match="non-public"):
        await validate_url(
            "https://example.com", ALLOWED, resolver_for("93.184.216.34", "10.0.0.1")
        )


async def test_unresolvable_host() -> None:
    async def fail(host: str, port: int) -> list[str]:
        raise OSError("nxdomain")

    with pytest.raises(UnsafeUrlError, match="resolved"):
        await validate_url("https://example.com", ALLOWED, fail)
    with pytest.raises(UnsafeUrlError, match="resolved"):
        await validate_url("https://example.com", ALLOWED, resolver_for())
