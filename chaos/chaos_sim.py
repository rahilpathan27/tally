"""Seeded model-based chaos runner for payment ledger commit/retry windows."""

from __future__ import annotations

import argparse
import random
from dataclasses import asdict, dataclass

from libs.money import Currency, Money
from services.ledger.invariants import check_ledger_invariants
from services.ledger.model import (
    Account,
    AccountType,
    Direction,
    HoldStatus,
    LedgerBook,
    Posting,
)


@dataclass(frozen=True, slots=True)
class ChaosReport:
    seed: int
    scenarios: int
    dropped_before_commit: int
    lost_ack_replayed: int
    duplicate_delivery: int
    hold_voided: int
    hold_captured: int
    entries_written: int


def run_scenarios(scenarios: int, seed: int) -> ChaosReport:
    if scenarios < 1:
        raise ValueError("scenario count must be positive")

    rng = random.Random(seed)
    counts = {
        "dropped": 0,
        "lost_ack": 0,
        "duplicate": 0,
        "voided": 0,
        "captured": 0,
        "entries": 0,
    }

    for scenario in range(scenarios):
        book = LedgerBook()
        book.add_account(Account("cash", AccountType.ASSET, Currency.INR))
        book.add_account(Account("clearing", AccountType.EQUITY, Currency.INR, allow_negative=True))
        book.post(
            f"seed:{scenario}",
            (
                Posting("cash", Direction.DEBIT, Money(10_000, Currency.INR)),
                Posting("clearing", Direction.CREDIT, Money(10_000, Currency.INR)),
            ),
        )

        amount = rng.randint(1, 10_000)
        request = (
            Posting("cash", Direction.CREDIT, Money(amount, Currency.INR)),
            Posting("clearing", Direction.DEBIT, Money(amount, Currency.INR)),
        )
        operation = rng.randrange(6)
        key = f"scenario:{scenario}:payment"

        if operation == 0:
            # Dispatch fails before reaching the ledger.
            counts["dropped"] += 1
        elif operation == 1:
            # The durable commit succeeds but its acknowledgement is lost; replay must be safe.
            committed = book.post(key, request)
            replayed = book.post(key, request)
            if committed.entry_id != replayed.entry_id:
                raise AssertionError(f"lost-ack replay duplicated a journal at scenario {scenario}")
            counts["lost_ack"] += 1
        elif operation == 2:
            # At-least-once delivery sends the exact same command twice.
            first = book.post(key, request)
            replay = book.post(key, request)
            if first.entry_id != replay.entry_id:
                raise AssertionError(
                    f"duplicate delivery duplicated a journal at scenario {scenario}"
                )
            counts["duplicate"] += 1
        else:
            hold = book.place_hold(f"scenario:{scenario}:hold", request)
            # The placement acknowledgement can also disappear before the caller observes it.
            replayed_hold = book.place_hold(f"scenario:{scenario}:hold", request)
            if replayed_hold.hold_id != hold.hold_id:
                raise AssertionError(f"hold replay duplicated a hold at scenario {scenario}")
            if operation in (3, 4):
                book.void_hold(hold.hold_id)
                book.void_hold(hold.hold_id)
                if book.holds[-1].status is not HoldStatus.VOID:
                    raise AssertionError(f"void retry changed hold state at scenario {scenario}")
                counts["voided"] += 1
            else:
                entry = book.post_hold(hold.hold_id, f"scenario:{scenario}:capture")
                replay = book.post_hold(hold.hold_id, f"scenario:{scenario}:capture")
                if entry.entry_id != replay.entry_id:
                    raise AssertionError(
                        f"capture replay duplicated a journal at scenario {scenario}"
                    )
                counts["captured"] += 1

        violations = check_ledger_invariants(book, ("cash",))
        if violations:
            raise AssertionError(
                f"scenario {scenario} violated invariants: {'; '.join(violations)}"
            )
        counts["entries"] += len(book.entries) - 1

    return ChaosReport(
        seed=seed,
        scenarios=scenarios,
        dropped_before_commit=counts["dropped"],
        lost_ack_replayed=counts["lost_ack"],
        duplicate_delivery=counts["duplicate"],
        hold_voided=counts["voided"],
        hold_captured=counts["captured"],
        entries_written=counts["entries"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=7_310_026)
    args = parser.parse_args()
    report = run_scenarios(args.scenarios, args.seed)
    print("TALLY CHAOS SIMULATION: PASS")
    for field, value in asdict(report).items():
        print(f"{field}: {value}")


if __name__ == "__main__":
    main()
