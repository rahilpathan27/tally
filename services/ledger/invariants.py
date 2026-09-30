"""Independent checks for ledger journal, hold, and available-balance invariants."""

from __future__ import annotations

import hashlib
from collections import defaultdict

from libs.money import Currency

from services.ledger.model import Direction, HoldStatus, LedgerBook


def check_ledger_invariants(book: LedgerBook, non_negative_accounts: tuple[str, ...]) -> list[str]:
    """Return every detected violation without mutating the reference ledger."""
    violations: list[str] = []
    previous_hash = "0" * 64
    totals: dict[Currency, list[int]] = defaultdict(lambda: [0, 0])
    for entry in book.entries:
        if entry.previous_hash != previous_hash:
            violations.append(f"journal entry {entry.entry_id} has a broken previous-hash link")
        expected_hash = hashlib.sha256(
            f"{entry.entry_id}\0{entry.idempotency_key}\0{entry.payload_hash}\0{previous_hash}".encode()
        ).hexdigest()
        if entry.entry_hash != expected_hash:
            violations.append(f"journal entry {entry.entry_id} hash does not match its content")
        previous_hash = entry.entry_hash
        for posting in entry.postings:
            totals[posting.amount.currency][0 if posting.direction is Direction.DEBIT else 1] += (
                posting.amount.amount_minor
            )

    for currency, (debits, credits) in totals.items():
        if debits != credits:
            violations.append(f"journal does not balance for {currency.value}")

    entries_by_id = {entry.entry_id: entry for entry in book.entries}
    for hold in book.holds:
        hold_totals: dict[Currency, list[int]] = defaultdict(lambda: [0, 0])
        for posting in hold.postings:
            hold_totals[posting.amount.currency][
                0 if posting.direction is Direction.DEBIT else 1
            ] += posting.amount.amount_minor
        if any(debits != credits for debits, credits in hold_totals.values()):
            violations.append(f"hold {hold.hold_id} postings do not balance")

        if hold.status is HoldStatus.POSTED:
            journal_entry = entries_by_id.get(hold.entry_id or "")
            if journal_entry is None or journal_entry.postings != hold.postings:
                violations.append(f"posted hold {hold.hold_id} has no matching journal entry")
        elif hold.entry_id is not None:
            violations.append(f"unposted hold {hold.hold_id} has a journal entry reference")

    for account_id in non_negative_accounts:
        if book.balance(account_id).amount_minor < 0:
            violations.append(f"account {account_id} has a negative posted balance")
        if book.available_balance(account_id).amount_minor < 0:
            violations.append(f"account {account_id} has a negative available balance")

    return violations
