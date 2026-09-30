from __future__ import annotations

import pytest
from libs.idempotency.store import request_fingerprint


def test_fingerprint_is_stable_and_binds_method_path_and_body() -> None:
    value = request_fingerprint("post", "/v1/payments?mode=test", b'{"amount_minor":125}')
    assert len(value) == 32
    assert value == request_fingerprint("POST", "/v1/payments?mode=test", b'{"amount_minor":125}')
    assert value != request_fingerprint("GET", "/v1/payments?mode=test", b'{"amount_minor":125}')
    assert value != request_fingerprint("POST", "/v1/payments", b'{"amount_minor":125}')
    assert value != request_fingerprint("POST", "/v1/payments?mode=test", b'{"amount_minor":126}')


def test_fingerprint_requires_absolute_path() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        request_fingerprint("POST", "v1/payments", b"{}")
