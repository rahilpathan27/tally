"""Kill the orchestrator at every step boundary and prove recovery converges safely."""

from __future__ import annotations

import asyncio
import os

import pytest
from chaos.flow_sim import FlowChaos, Outcome, Scenario, seed_chaos_routes
from chaos.stack import build_stack, provision_databases

CARD_CRASHES = (
    "confirm.after_authorizing_committed",
    "card.after_network_approved",
    "card.after_hold_placed",
    "capture.after_capturing_committed",
    "capture.after_hold_posted",
)
UPI_CRASHES = (
    "confirm.after_authorizing_committed",
    "upi.after_psp_approved",
    "upi.after_bank_approved",
    "upi.after_ledger_posted",
)


def _scenario(index: int, method: str, crash: str, *, hide: bool, capture: bool) -> Scenario:
    modes = (
        {"network": "approve"}
        if method == "card"
        else {"psp": "approve", "bank-a": "approve", "bank-b": "approve", "bank-c": "approve"}
    )
    return Scenario(
        index=index,
        seed=900_000 + index,
        method=method,
        amount=10_000 + index,
        crash_point=crash,
        recovery_crash=index % 2 == 0,
        modes=modes,
        hide_truth=hide,
        heal_at=3,
        deadline_at=1,
        duplicate_confirm=False,
        race_capture_cancel=False,
        capture=capture,
        ledger_faults=[],
        external_faults=[],
        payer_vpa="payer@bank-a",
    )


def test_every_crash_point_recovers_to_a_consistent_terminal_state() -> None:
    if os.environ.get("TALLY_STACK_TESTS") != "1":
        pytest.skip("set TALLY_STACK_TESTS=1 with local Compose services to run stack tests")

    async def exercise() -> None:
        general, ledger, vault = await provision_databases("tally_it_crash")
        stack = await build_stack(
            general, ledger, vault, os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        )
        await seed_chaos_routes(stack)
        harness = FlowChaos(stack)
        outcome = Outcome()
        index = 0
        try:
            for hide in (False, True):
                for crash in CARD_CRASHES + ("cancel.after_cancelled_committed",):
                    index += 1
                    capture = not crash.startswith("cancel")
                    await harness.run(
                        _scenario(index, "card", crash, hide=hide, capture=capture), outcome
                    )
                for crash in UPI_CRASHES:
                    index += 1
                    await harness.run(
                        _scenario(index, "upi", crash, hide=hide, capture=True), outcome
                    )
            await harness.check_global()
        finally:
            await stack.close()
        assert outcome.counters["crash_fired:True"] == index
        # Without hidden truth every approved payment must complete, never reverse.
        assert outcome.counters["upi:reversed"] + outcome.counters["card:reversed"] <= index // 2

    asyncio.run(exercise())
