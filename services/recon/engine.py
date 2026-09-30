"""Pure three-way reconciliation: ledger nostro postings vs switch log vs bank statement.

The engine has no I/O so it can be evaluated on millions of synthetic rows. Matching runs in
stages: (1) exact reference match, (2) many-to-one grouping of batched bank lines, (3) fuzzy match
(amount + kind + VPA + time window) for bank lines that lack a reference. Every remaining
difference is classified into the break taxonomy below with a deterministic ID, so re-running a
date updates the same breaks instead of duplicating them.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

BREAK_NAMESPACE = uuid.UUID("5b0c6a58-7a8e-4c5e-9d3f-1f2d3c4b5a69")


class Kind(StrEnum):
    UPI_TRANSFER = "upi_transfer"
    REFUND = "refund"
    PAYOUT = "payout"
    REFUND_BATCH = "refund_batch"


class BreakType(StrEnum):
    MISSING_AT_BANK = "missing_at_bank"
    MISSING_INTERNALLY = "missing_internally"
    AMOUNT_MISMATCH = "amount_mismatch"
    DUPLICATE = "duplicate"
    STATUS_MISMATCH = "status_mismatch"
    TIMING_DIFFERENCE = "timing_difference"
    FEE_TAX_MISMATCH = "fee_tax_mismatch"
    UNKNOWN = "unknown"


SUGGESTED_ACTIONS: dict[BreakType, str] = {
    BreakType.MISSING_AT_BANK: "Raise a trace with the bank; if confirmed not moved, reverse the "
    "internal entry through a maker-checker adjustment.",
    BreakType.MISSING_INTERNALLY: "Identify the counterparty; post the movement to suspense until "
    "it can be attributed.",
    BreakType.AMOUNT_MISMATCH: "Compare raw switch messages; post the difference to suspense and "
    "dispute with the bank.",
    BreakType.DUPLICATE: "Confirm with the bank whether the duplicate line moved funds; request "
    "a statement correction.",
    BreakType.STATUS_MISMATCH: "Check recovery incidents; refund the payer or credit the merchant "
    "from suspense once the final bank status is confirmed.",
    BreakType.TIMING_DIFFERENCE: "None: expected to clear on the next business day's statement.",
    BreakType.FEE_TAX_MISMATCH: "Book the bank charge and GST on charges to expense via an "
    "approved adjustment.",
    BreakType.UNKNOWN: "Investigate manually; the bank line could not be attributed.",
}


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    reference: str
    kind: Kind
    amount_minor: int
    direction: str  # "in" (nostro debit) or "out" (nostro credit)
    posted_at: datetime
    entry_id: str
    suspense: bool = False


@dataclass(frozen=True, slots=True)
class SwitchRecord:
    reference: str
    kind: Kind
    amount_minor: int
    status: str  # success | failed | pending | late_success
    occurred_at: datetime
    vpa: str | None = None
    merchant_id: str | None = None


@dataclass(frozen=True, slots=True)
class BankRecord:
    line_no: int
    reference: str | None
    bank_reference: str
    kind: Kind
    amount_minor: int
    status: str  # success | failed
    occurred_at: datetime
    vpa: str | None = None
    batch_id: str | None = None


@dataclass(frozen=True, slots=True)
class Break:
    break_id: str
    break_type: BreakType
    reference: str | None
    bank_reference: str | None
    kind: str
    amount_minor: int
    internal_amount_minor: int | None
    bank_amount_minor: int | None
    status: str  # open | auto_resolved
    suggested_action: str
    detail: str


@dataclass(frozen=True, slots=True)
class Match:
    match_type: str  # exact | fuzzy | group | carry_over | no_movement
    references: tuple[str, ...]
    bank_lines: tuple[int, ...]


@dataclass(slots=True)
class ReconConfig:
    cutoff_window: timedelta = timedelta(minutes=30)
    fuzzy_window: timedelta = timedelta(minutes=10)
    # Bank charge patterns (charge + GST on charge) that explain a short credit.
    fee_patterns: dict[Kind, frozenset[int]] = field(
        default_factory=lambda: {Kind.PAYOUT: frozenset({590, 1180}), Kind.REFUND: frozenset({})}
    )


@dataclass(slots=True)
class ReconResult:
    matches: list[Match]
    breaks: list[Break]

    @property
    def matched_bank_lines(self) -> int:
        return sum(len(m.bank_lines) for m in self.matches if m.match_type != "no_movement")

    @property
    def unmatched_bank_lines(self) -> int:
        return sum(1 for b in self.breaks if b.bank_amount_minor is not None and b.status == "open")


def break_id(business_date: str, source: str, break_type: BreakType, key: str) -> str:
    return str(uuid.uuid5(BREAK_NAMESPACE, f"{business_date}|{source}|{break_type.value}|{key}"))


def reconcile(
    ledger: Iterable[LedgerRecord],
    switch: Iterable[SwitchRecord],
    bank: Sequence[BankRecord],
    *,
    business_date: str,
    source: str,
    day_end: datetime,
    next_day_bank: Sequence[BankRecord] | None = None,
    carried_over: frozenset[str] = frozenset(),
    config: ReconConfig | None = None,
) -> ReconResult:
    config = config or ReconConfig()
    ledger_by_ref: dict[str, list[LedgerRecord]] = defaultdict(list)
    for record in ledger:
        ledger_by_ref[record.reference].append(record)
    switch_by_ref: dict[str, SwitchRecord] = {record.reference: record for record in switch}
    internal_refs = set(ledger_by_ref) | set(switch_by_ref)
    breaks: list[Break] = []
    matches: list[Match] = []

    def add_break(
        break_type: BreakType,
        key: str,
        *,
        reference: str | None,
        bank_reference: str | None,
        kind: str,
        amount: int,
        internal_amount: int | None,
        bank_amount: int | None,
        detail: str,
        status: str = "open",
    ) -> None:
        breaks.append(
            Break(
                break_id=break_id(business_date, source, break_type, key),
                break_type=break_type,
                reference=reference,
                bank_reference=bank_reference,
                kind=kind,
                amount_minor=amount,
                internal_amount_minor=internal_amount,
                bank_amount_minor=bank_amount,
                status=status,
                suggested_action=SUGGESTED_ACTIONS[break_type],
                detail=detail,
            )
        )

    # Duplicates: a bank reference must appear once; keep the first line.
    seen_bank_refs: set[str] = set()
    unique_bank: list[BankRecord] = []
    for line in bank:
        if line.bank_reference in seen_bank_refs:
            add_break(
                BreakType.DUPLICATE,
                f"{line.bank_reference}:{line.line_no}",
                reference=line.reference,
                bank_reference=line.bank_reference,
                kind=line.kind.value,
                amount=line.amount_minor,
                internal_amount=None,
                bank_amount=line.amount_minor,
                detail=f"bank reference repeated on line {line.line_no}",
            )
            continue
        seen_bank_refs.add(line.bank_reference)
        unique_bank.append(line)

    paired: dict[str, BankRecord] = {}
    unreferenced: list[BankRecord] = []
    batches: list[BankRecord] = []
    for line in unique_bank:
        if line.kind == Kind.REFUND_BATCH:
            batches.append(line)
        elif line.reference is None:
            unreferenced.append(line)
        elif line.reference in carried_over:
            matches.append(Match("carry_over", (line.reference,), (line.line_no,)))
        elif line.reference in internal_refs and line.reference not in paired:
            paired[line.reference] = line
        elif line.reference in paired:
            add_break(
                BreakType.DUPLICATE,
                f"{line.reference}:{line.line_no}",
                reference=line.reference,
                bank_reference=line.bank_reference,
                kind=line.kind.value,
                amount=line.amount_minor,
                internal_amount=None,
                bank_amount=line.amount_minor,
                detail=f"reference already reported on line {paired[line.reference].line_no}",
            )
        else:
            add_break(
                BreakType.MISSING_INTERNALLY,
                line.reference,
                reference=line.reference,
                bank_reference=line.bank_reference,
                kind=line.kind.value,
                amount=line.amount_minor,
                internal_amount=None,
                bank_amount=line.amount_minor,
                detail="bank reports a movement with no internal record",
            )

    grouped: set[str] = set()
    for batch in batches:
        members = sorted(
            ref
            for ref, record in switch_by_ref.items()
            if record.kind == Kind.REFUND
            and record.status == "success"
            and ref not in paired
            and ref not in grouped
        )
        total = sum(switch_by_ref[ref].amount_minor for ref in members)
        if members and total == batch.amount_minor:
            grouped.update(members)
            matches.append(Match("group", tuple(members), (batch.line_no,)))
        else:
            add_break(
                BreakType.AMOUNT_MISMATCH,
                batch.bank_reference,
                reference=batch.batch_id,
                bank_reference=batch.bank_reference,
                kind=batch.kind.value,
                amount=abs(batch.amount_minor - total),
                internal_amount=total,
                bank_amount=batch.amount_minor,
                detail=f"refund batch of {len(members)} internal refunds does not sum to the line",
            )
            grouped.update(members)

    # Fuzzy: unreferenced lines match a unique unpaired internal record by amount/kind/VPA/time.
    candidates_by_key: dict[tuple[Kind, int], list[SwitchRecord]] = defaultdict(list)
    for ref, candidate in switch_by_ref.items():
        if ref not in paired and ref not in grouped:
            candidates_by_key[(candidate.kind, candidate.amount_minor)].append(candidate)
    candidates_by_vpa: dict[tuple[Kind, str], list[SwitchRecord]] = defaultdict(list)
    for ref, candidate in switch_by_ref.items():
        if ref not in paired and ref not in grouped and candidate.vpa is not None:
            candidates_by_vpa[(candidate.kind, candidate.vpa)].append(candidate)
    for line in unreferenced:
        options = [
            record
            for record in candidates_by_key.get((line.kind, line.amount_minor), [])
            if record.reference not in paired
            and (line.vpa is None or record.vpa is None or record.vpa == line.vpa)
            and abs(record.occurred_at - line.occurred_at) <= config.fuzzy_window
        ]
        if not options and line.vpa is not None:
            # Same payer VPA and time but a different amount: pair it so the per-reference
            # pass reports an amount mismatch instead of two unrelated breaks.
            options = [
                record
                for record in candidates_by_vpa.get((line.kind, line.vpa), [])
                if record.reference not in paired
                and abs(record.occurred_at - line.occurred_at) <= config.fuzzy_window
            ]
        if options:
            options.sort(key=lambda r: (abs(r.occurred_at - line.occurred_at), r.reference))
            best = options[0]
            tie = len(options) > 1 and abs(options[1].occurred_at - line.occurred_at) == abs(
                best.occurred_at - line.occurred_at
            )
            if not tie:
                paired[best.reference] = line
                matches.append(Match("fuzzy", (best.reference,), (line.line_no,)))
                continue
        add_break(
            BreakType.UNKNOWN,
            line.bank_reference,
            reference=None,
            bank_reference=line.bank_reference,
            kind=line.kind.value,
            amount=line.amount_minor,
            internal_amount=None,
            bank_amount=line.amount_minor,
            detail="bank line has no reference and no unique internal candidate",
        )
    fuzzy_refs = {m.references[0] for m in matches if m.match_type == "fuzzy"}

    next_day_refs = (
        {line.reference for line in next_day_bank if line.reference}
        if next_day_bank is not None
        else None
    )
    # Reference-less next-day lines still identify a carry-over by kind, amount and VPA.
    next_day_fuzzy = (
        {
            (line.kind, line.amount_minor, line.vpa)
            for line in next_day_bank
            if line.reference is None
        }
        if next_day_bank is not None
        else set()
    )
    for ref in sorted(internal_refs - grouped):
        s = switch_by_ref.get(ref)
        postings = ledger_by_ref.get(ref, [])
        ledger_amount = sum(p.amount_minor for p in postings) if postings else None
        suspense = any(p.suspense for p in postings)
        kind = (s.kind if s else postings[0].kind).value
        switch_amount = s.amount_minor if s else 0
        internal_amount = ledger_amount if ledger_amount is not None else switch_amount
        expected_moved = (s is not None and s.status == "success") or (
            bool(postings) and not suspense
        )
        bank_line = paired.get(ref)

        def raise_break(
            break_type: BreakType,
            detail: str,
            *,
            amount: int | None = None,
            status: str = "open",
            line: BankRecord | None = bank_line,
            internal_amount: int = internal_amount,
            ref: str = ref,
            kind: str = kind,
        ) -> None:
            add_break(
                break_type,
                ref,
                reference=ref,
                bank_reference=line.bank_reference if line else None,
                kind=kind,
                amount=internal_amount if amount is None else amount,
                internal_amount=internal_amount,
                bank_amount=line.amount_minor if line else None,
                detail=detail,
                status=status,
            )

        if bank_line is None:
            if s is not None and s.status == "late_success" and postings:
                raise_break(
                    BreakType.MISSING_AT_BANK,
                    "late success recorded to suspense but absent from the statement",
                )
            elif expected_moved:
                if next_day_refs is not None and (
                    ref in next_day_refs
                    or (s is not None and (s.kind, s.amount_minor, s.vpa) in next_day_fuzzy)
                ):
                    raise_break(
                        BreakType.TIMING_DIFFERENCE,
                        "reported on the next business day's statement",
                        status="auto_resolved",
                    )
                elif (
                    next_day_refs is None
                    and s is not None
                    and s.occurred_at >= day_end - config.cutoff_window
                ):
                    raise_break(
                        BreakType.TIMING_DIFFERENCE,
                        "near the cut-off; expected on the next statement",
                    )
                else:
                    raise_break(BreakType.MISSING_AT_BANK, "internal movement absent at the bank")
            elif postings and s is not None and s.status != "success" and not suspense:
                raise_break(
                    BreakType.STATUS_MISMATCH, f"ledger moved money but switch says {s.status}"
                )
            else:
                matches.append(Match("no_movement", (ref,), ()))
            continue

        if bank_line.status != "success":
            if expected_moved:
                raise_break(BreakType.STATUS_MISMATCH, "bank reports failure; internal success")
            else:
                matches.append(Match("no_movement", (ref,), (bank_line.line_no,)))
            continue
        if s is not None and s.status in {"failed", "pending", "late_success"}:
            detail = (
                "bank success after the payment was reversed (late success in suspense)"
                if s.status == "late_success"
                else f"bank success but switch status is {s.status}"
            )
            raise_break(BreakType.STATUS_MISMATCH, detail)
            continue
        if not postings:
            raise_break(
                BreakType.MISSING_INTERNALLY,
                "switch and bank agree but no ledger entry exists",
                amount=bank_line.amount_minor,
            )
            continue
        compare_to = ledger_amount if ledger_amount is not None else internal_amount
        if bank_line.amount_minor != compare_to:
            difference = abs(compare_to - bank_line.amount_minor)
            patterns = config.fee_patterns.get(Kind(kind), frozenset())
            if bank_line.amount_minor < compare_to and difference in patterns:
                raise_break(
                    BreakType.FEE_TAX_MISMATCH,
                    f"bank deducted charges of {difference} minor units",
                    amount=difference,
                )
            else:
                raise_break(
                    BreakType.AMOUNT_MISMATCH,
                    f"ledger {compare_to} vs bank {bank_line.amount_minor}",
                    amount=difference,
                )
            continue
        matches.append(
            Match("fuzzy" if ref in fuzzy_refs else "exact", (ref,), (bank_line.line_no,))
        )
    return ReconResult(matches=matches, breaks=breaks)
