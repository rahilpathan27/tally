from __future__ import annotations

import asyncio
from typing import Any

import pytest
from libs.observability.metrics import LEDGER_INTEGRITY_OK
from services.ledger import api


def gauge() -> float:
    return LEDGER_INTEGRITY_OK.labels("verifier_available")._value.get()  # type: ignore[no-any-return]


def test_transient_verifier_failure_clears_after_a_successful_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes: list[BaseException | None] = [OSError("database unreachable"), None]

    async def fake_check(pool: Any) -> bool:
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        return True

    monkeypatch.setattr(api, "run_integrity_check", fake_check)
    asyncio.run(api.verify_once(object()))
    assert gauge() == 0
    asyncio.run(api.verify_once(object()))
    assert gauge() == 1
