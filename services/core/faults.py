"""Named crash points used by the failure-injection harness.

Production code calls ``fault_point(state, name)`` at every step boundary where a process
could die between two durable effects. The default injector is ``None`` and the call is a
no-op. The harness installs an injector that raises ``SimulatedCrash``; because it derives
from ``BaseException`` it bypasses ``except Exception`` handlers, matching a killed process
that never runs its error paths.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class SimulatedCrash(BaseException):  # noqa: N818 - models a process kill, not an error
    """Abandon the current request exactly where a real process kill would stop it."""


FaultInjector = Callable[[str], None]

CRASH_POINTS: tuple[str, ...] = (
    "confirm.after_authorizing_committed",
    "card.after_network_approved",
    "card.after_hold_placed",
    "upi.after_psp_approved",
    "upi.after_bank_approved",
    "upi.after_ledger_posted",
    "capture.after_capturing_committed",
    "capture.after_hold_posted",
    "cancel.after_cancelled_committed",
    "recovery.after_ledger_call",
    "refund.after_command_committed",
    "refund_debit.after_ledger_posted",
    "refund.after_bank_sent",
    "refund_complete.after_ledger_posted",
    "settlement.after_command_committed",
    "settlement_post.after_ledger_posted",
    "payout.after_bank_sent",
    "payout_complete.after_ledger_posted",
    "dispute_debit.after_ledger_posted",
)


def fault_point(state: Any, name: str) -> None:
    injector: FaultInjector | None = getattr(state, "fault_injector", None)
    if injector is not None:
        injector(name)
