"""Synthetic reconciliation data with planted, labelled breaks and an evaluator.

``synthetic_day`` builds a consistent business day (ledger nostro postings, switch log, bank
statement) and then mutates the bank side to plant a known number of each break type. The planted
list is the ground truth used to measure recall, precision and classification accuracy.
"""

from __future__ import annotations

import random
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from libs.common.business_time import cutoff_instant

from services.recon.engine import (
    BankRecord,
    BreakType,
    Kind,
    LedgerRecord,
    ReconResult,
    SwitchRecord,
)

DEFAULT_PLANTS: dict[BreakType, int] = {
    BreakType.MISSING_AT_BANK: 5,
    BreakType.MISSING_INTERNALLY: 5,
    BreakType.AMOUNT_MISMATCH: 5,
    BreakType.DUPLICATE: 5,
    BreakType.STATUS_MISMATCH: 5,
    BreakType.TIMING_DIFFERENCE: 5,
    BreakType.FEE_TAX_MISMATCH: 3,
    BreakType.UNKNOWN: 3,
}


@dataclass(frozen=True, slots=True)
class PlantedBreak:
    break_type: BreakType
    key: str


@dataclass(slots=True)
class SyntheticDay:
    business_date: date
    day_end: datetime
    ledger: list[LedgerRecord]
    switch: list[SwitchRecord]
    bank_today: list[BankRecord]
    bank_next_day: list[BankRecord]
    planted: list[PlantedBreak] = field(default_factory=list)


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def synthetic_day(
    transactions: int,
    seed: int,
    business_date: date,
    *,
    style: str = "csv_rupees_ist",
    plants: dict[BreakType, int] | None = None,
    hard: bool = False,
) -> SyntheticDay:
    """Build one day. ``hard`` also plants breaks on lines that lack a reference (bank C)."""
    rng = random.Random(seed)
    plants = DEFAULT_PLANTS if plants is None else plants
    day_end = cutoff_instant(business_date)
    day_start = day_end - timedelta(days=1)
    batch_refunds = style == "json_offset"
    ledger: list[LedgerRecord] = []
    switch: list[SwitchRecord] = []
    bank: list[BankRecord] = []
    for index in range(transactions):
        roll = rng.randrange(100)
        kind = Kind.UPI_TRANSFER if roll < 80 else Kind.REFUND if roll < 92 else Kind.PAYOUT
        ref = _uuid(rng)
        amount = rng.randint(100, 5_000_000)
        # Keep ordinary activity away from the cut-off so only planted lines are near it.
        at = day_start + timedelta(seconds=rng.randint(60, 86_400 - 3_600))
        vpa = (
            f"user{rng.randint(1, 50_000)}@bank-{rng.choice('abc')}"
            if kind == Kind.UPI_TRANSFER
            else None
        )
        success = rng.randrange(100) < 95
        switch.append(SwitchRecord(ref, kind, amount, "success" if success else "failed", at, vpa))
        if success:
            ledger.append(
                LedgerRecord(
                    ref,
                    kind,
                    amount,
                    "in" if kind == Kind.UPI_TRANSFER else "out",
                    at,
                    f"e{index}",
                )
            )
        bank_ref = f"UTR{rng.getrandbits(48):014d}"
        if success or rng.randrange(100) < 30:
            bank.append(
                BankRecord(
                    0,
                    ref,
                    bank_ref,
                    kind,
                    amount,
                    "success" if success else "failed",
                    at + timedelta(seconds=rng.randint(0, 90)),
                    vpa,
                )
            )

    # The true reference of each bank line, kept even when the bank omits it.
    truth_ref = [str(line.reference) for line in bank]
    strip_before = hard and batch_refunds
    if strip_before:
        bank = [
            replace(line, reference=None)
            if line.kind == Kind.UPI_TRANSFER and rng.randrange(100) < 5
            else line
            for line in bank
        ]
    planted: list[PlantedBreak] = []
    next_day: list[BankRecord] = []
    eligible = [
        i
        for i, line in enumerate(bank)
        if line.status == "success" and not (batch_refunds and line.kind == Kind.REFUND)
    ]
    rng.shuffle(eligible)
    used: set[int] = set()

    def take(predicate: object = None) -> int | None:
        for i in eligible:
            if i not in used and (predicate is None or predicate(bank[i])):  # type: ignore[operator]
                used.add(i)
                return i
        return None

    removed: set[int] = set()
    extra: list[BankRecord] = []
    for _ in range(plants.get(BreakType.MISSING_AT_BANK, 0)):
        if (i := take()) is not None:
            removed.add(i)
            planted.append(PlantedBreak(BreakType.MISSING_AT_BANK, truth_ref[i]))
    for _ in range(plants.get(BreakType.AMOUNT_MISMATCH, 0)):
        if (i := take()) is not None:
            delta = rng.choice((-1, 1)) * rng.randint(1, 999)
            if bank[i].amount_minor + delta <= 0 or abs(delta) in (590, 1180):
                delta = 7
            bank[i] = replace(bank[i], amount_minor=bank[i].amount_minor + delta)
            planted.append(PlantedBreak(BreakType.AMOUNT_MISMATCH, truth_ref[i]))
    for _ in range(plants.get(BreakType.FEE_TAX_MISMATCH, 0)):
        if (
            i := take(lambda line: line.kind == Kind.PAYOUT and line.amount_minor > 2_000)
        ) is not None:
            bank[i] = replace(bank[i], amount_minor=bank[i].amount_minor - 590)
            planted.append(PlantedBreak(BreakType.FEE_TAX_MISMATCH, truth_ref[i]))
    for _ in range(plants.get(BreakType.STATUS_MISMATCH, 0)):
        if (i := take()) is not None:
            bank[i] = replace(bank[i], status="failed")
            planted.append(PlantedBreak(BreakType.STATUS_MISMATCH, truth_ref[i]))
    for _ in range(plants.get(BreakType.DUPLICATE, 0)):
        if (i := take()) is not None:
            extra.append(bank[i])
            planted.append(PlantedBreak(BreakType.DUPLICATE, bank[i].bank_reference))
    for _ in range(plants.get(BreakType.TIMING_DIFFERENCE, 0)):
        if (i := take()) is not None:
            late = day_end - timedelta(minutes=rng.randint(1, 20))
            ref = truth_ref[i]
            switch = [replace(s, occurred_at=late) if s.reference == ref else s for s in switch]
            ledger = [replace(e, posted_at=late) if e.reference == ref else e for e in ledger]
            next_day.append(replace(bank[i], occurred_at=day_end + timedelta(minutes=5)))
            removed.add(i)
            planted.append(PlantedBreak(BreakType.TIMING_DIFFERENCE, ref))
    for _ in range(plants.get(BreakType.MISSING_INTERNALLY, 0)):
        ref = _uuid(rng)
        extra.append(
            BankRecord(
                0,
                ref,
                f"UTR{rng.getrandbits(48):014d}",
                Kind.UPI_TRANSFER,
                rng.randint(100, 900_000),
                "success",
                day_start + timedelta(hours=rng.randint(1, 20)),
                "stranger@bank-z",
            )
        )
        planted.append(PlantedBreak(BreakType.MISSING_INTERNALLY, ref))
    for _ in range(plants.get(BreakType.UNKNOWN, 0)):
        bank_ref = f"UTR{rng.getrandbits(48):014d}"
        extra.append(
            BankRecord(
                0,
                None,
                bank_ref,
                Kind.UPI_TRANSFER,
                7_000_000 + rng.randint(1, 999_999),  # outside the normal amount range
                "success",
                day_start + timedelta(hours=rng.randint(1, 20)),
                "nobody@bank-z",
            )
        )
        planted.append(PlantedBreak(BreakType.UNKNOWN, bank_ref))

    statement = [line for i, line in enumerate(bank) if i not in removed] + extra
    if batch_refunds:
        refunds = [
            line for line in statement if line.kind == Kind.REFUND and line.status == "success"
        ]
        statement = [line for line in statement if line.kind != Kind.REFUND]
        if refunds:
            statement.append(
                BankRecord(
                    0,
                    None,
                    f"BATCH{business_date:%Y%m%d}",
                    Kind.REFUND_BATCH,
                    sum(line.amount_minor for line in refunds),
                    "success",
                    day_end - timedelta(hours=1),
                    batch_id=f"refunds-{business_date.isoformat()}",
                )
            )
        # Bank C omits the merchant reference on some UPI lines; they must match fuzzily.
        planted_keys = {p.key for p in planted}
        if not strip_before:
            statement = [
                replace(line, reference=None)
                if line.kind == Kind.UPI_TRANSFER
                and line.reference not in planted_keys
                and rng.randrange(100) < 5
                else line
                for line in statement
            ]
    statement.sort(key=lambda line: (line.occurred_at, line.bank_reference))
    numbered = [replace(line, line_no=n) for n, line in enumerate(statement, start=1)]
    next_numbered = [replace(line, line_no=n) for n, line in enumerate(next_day, start=1)]
    return SyntheticDay(business_date, day_end, ledger, switch, numbered, next_numbered, planted)


@dataclass(slots=True)
class Evaluation:
    planted: int
    detected: int
    correctly_classified: int
    breaks_reported: int
    false_breaks: int
    by_type: dict[str, tuple[int, int, int]]  # planted, detected, correct

    @property
    def recall_bps(self) -> int:
        return 10_000 if self.planted == 0 else self.detected * 10_000 // self.planted

    @property
    def precision_bps(self) -> int:
        reported = self.breaks_reported
        return 10_000 if reported == 0 else (reported - self.false_breaks) * 10_000 // reported

    @property
    def accuracy_bps(self) -> int:
        return 10_000 if self.detected == 0 else self.correctly_classified * 10_000 // self.detected


def evaluate(result: ReconResult, planted: list[PlantedBreak]) -> Evaluation:
    by_key: dict[str, list[PlantedBreak]] = {}
    for plant in planted:
        by_key.setdefault(plant.key, []).append(plant)
    found: dict[PlantedBreak, bool] = {}
    false_breaks = 0
    for item in result.breaks:
        keys = {k for k in (item.reference, item.bank_reference) if k}
        candidates = [p for key in keys for p in by_key.get(key, [])]
        if not candidates:
            false_breaks += 1
            continue
        exact = [p for p in candidates if p.break_type == item.break_type]
        chosen = exact[0] if exact else candidates[0]
        found[chosen] = found.get(chosen, False) or bool(exact)
    by_type: dict[str, tuple[int, int, int]] = {}
    totals = Counter(p.break_type.value for p in planted)
    for break_type, total in totals.items():
        detected = sum(1 for p in found if p.break_type.value == break_type)
        correct = sum(1 for p, ok in found.items() if ok and p.break_type.value == break_type)
        by_type[break_type] = (total, detected, correct)
    return Evaluation(
        planted=len(planted),
        detected=len(found),
        correctly_classified=sum(1 for ok in found.values() if ok),
        breaks_reported=len(result.breaks),
        false_breaks=false_breaks,
        by_type=by_type,
    )
