import asyncio

import pytest
from libs.security.ssrf import UnsafeDestination, parse_webhook_url, vetted_address
from libs.security.webhook_signing import sign_payload, verify_signature

BLOCKED_URLS = [
    "http://example.com/hook",
    "https://127.0.0.1/hook",
    "https://localhost/hook",
    "https://10.0.0.5/hook",
    "https://169.254.169.254/latest/meta-data",
    "https://[::1]/hook",
    "https://[::ffff:127.0.0.1]/hook",
    "https://[fd00::1]/hook",
    "https://100.64.1.1/hook",
    "https://0.0.0.0/hook",
    "https://user:pass@example.com/hook",
    "https://example.com:22/hook",
    "https://metadata.internal/hook",
    "https://example.com/hook#frag",
    "https://exa mple.com/hook",
    "ftp://example.com/hook",
]


@pytest.mark.parametrize("url", BLOCKED_URLS)
def test_unsafe_urls_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeDestination):
        parse_webhook_url(url)


def test_public_https_url_is_accepted() -> None:
    target = parse_webhook_url("https://hooks.example.com/tally?x=1")
    assert (target.host, target.port, target.path) == ("hooks.example.com", 443, "/tally?x=1")


def test_dns_answers_are_vetted_every_time() -> None:
    target = parse_webhook_url("https://rebind.example.com/hook")

    async def public(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    async def rebound(host: str, port: int) -> list[str]:
        return ["93.184.216.34", "10.1.2.3"]

    async def mapped(host: str, port: int) -> list[str]:
        return ["::ffff:192.168.1.1"]

    assert asyncio.run(vetted_address(target, public)) == "93.184.216.34"
    for resolver in (rebound, mapped):
        with pytest.raises(UnsafeDestination):
            asyncio.run(vetted_address(target, resolver))


def test_trusted_dev_host_is_explicit() -> None:
    target = parse_webhook_url("http://127.0.0.1:9000/hook", trusted_hosts=["127.0.0.1"])
    assert target.trusted and asyncio.run(vetted_address(target)) == "127.0.0.1"


def test_webhook_signature_round_trip_tamper_and_replay() -> None:
    secret = b"whsec-test-secret-value"
    body = b'{"id":"evt_1"}'
    header = sign_payload(secret, body, 1_000)
    assert verify_signature(secret, header, body, now=1_010)
    assert not verify_signature(secret, header, body + b" ", now=1_010)
    assert not verify_signature(b"other", header, body, now=1_010)
    assert not verify_signature(secret, header, body, now=2_000)
    assert not verify_signature(secret, "garbage", body, now=1_010)
