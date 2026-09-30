"""Deterministic ledger domain model used by tests and local verification."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from libs.money import Currency, Money


class AccountType(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    INCOME = "income"
    EXPENSE = "expense"


class Direction(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class HoldStatus(StrEnum):
    PENDING = "pending"
    POSTED = "posted"
    VOID = "void"


@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    account_type: AccountType
    currency: Currency
    allow_negative: bool = False
    closed: bool = False


@dataclass(frozen=True, slots=True)
class Posting:
    account_id: str
    direction: Direction
    amount: Money

    def __post_init__(self) -> None:
        if self.amount.amount_minor <= 0:
            raise ValueError("posting amount must be positive")


@dataclass(frozen=True, slots=True)
class JournalEntry:
    entry_id: str
    idempotency_key: str
    created_at: datetime
    postings: tuple[Posting, ...]
    payload_hash: str
    previous_hash: str
    entry_hash: str


@dataclass(frozen=True, slots=True)
class Hold:
    hold_id: str
    idempotency_key: str
    postings: tuple[Posting, ...]
    payload_hash: str
    status: HoldStatus
    entry_id: str | None = None


@dataclass(frozen=True, slots=True)
class BalanceSnapshot:
    snapshot_id: str
    created_at: datetime
    entry_count: int
    balances: tuple[tuple[str, Money], ...]


@dataclass(frozen=True, slots=True)
class AccountBalance:
    posted: Money
    pending_debits: Money
    pending_credits: Money
    available: Money
    version: int


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for a different posting payload."""


class LedgerBook:
    """Thread-safe deterministic reference model; durable posting is a later phase."""

    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._entries: list[JournalEntry] = []
        self._keys: dict[str, JournalEntry] = {}
        self._holds: dict[str, Hold] = {}
        self._hold_keys: dict[str, Hold] = {}
        self._snapshots: list[BalanceSnapshot] = []
        self._lock = threading.RLock()

    def add_account(self, account: Account) -> None:
        with self._lock:
            if account.account_id in self._accounts:
                raise ValueError("account already exists")
            self._accounts[account.account_id] = account

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        with self._lock:
            return tuple(self._entries)

    def post(
        self,
        idempotency_key: str,
        postings: Iterable[Posting],
        *,
        _exclude_hold_id: str | None = None,
    ) -> JournalEntry:
        rows = tuple(postings)
        if not idempotency_key:
            raise ValueError("idempotency key is required")
        self._validate_balanced(rows)
        payload_hash = self._payload_hash(rows)
        with self._lock:
            prior = self._keys.get(idempotency_key)
            if prior is not None:
                if prior.payload_hash != payload_hash:
                    raise IdempotencyConflict("same idempotency key has a different payload")
                return prior
            self._validate_accounts(rows)
            self._validate_non_negative(rows, _exclude_hold_id)
            previous_hash = self._entries[-1].entry_hash if self._entries else "0" * 64
            created_at = datetime.now(UTC)
            entry_id = f"entry_{len(self._entries) + 1:016d}"
            entry_hash = self._entry_hash(entry_id, idempotency_key, payload_hash, previous_hash)
            entry = JournalEntry(
                entry_id,
                idempotency_key,
                created_at,
                rows,
                payload_hash,
                previous_hash,
                entry_hash,
            )
            self._entries.append(entry)
            self._keys[idempotency_key] = entry
            return entry

    @property
    def holds(self) -> tuple[Hold, ...]:
        with self._lock:
            return tuple(self._holds.values())

    def place_hold(self, idempotency_key: str, postings: Iterable[Posting]) -> Hold:
        rows = tuple(postings)
        if not idempotency_key:
            raise ValueError("idempotency key is required")
        self._validate_balanced(rows)
        payload_hash = self._payload_hash(rows)
        with self._lock:
            prior = self._hold_keys.get(idempotency_key)
            if prior is not None:
                if prior.payload_hash != payload_hash:
                    raise IdempotencyConflict("same hold key has a different payload")
                return prior
            self._validate_accounts(rows)
            self._validate_non_negative(rows)
            hold = Hold(
                hold_id=f"hold_{len(self._holds) + 1:016d}",
                idempotency_key=idempotency_key,
                postings=rows,
                payload_hash=payload_hash,
                status=HoldStatus.PENDING,
            )
            self._holds[hold.hold_id] = hold
            self._hold_keys[idempotency_key] = hold
            return hold

    def post_hold(self, hold_id: str, idempotency_key: str) -> JournalEntry:
        with self._lock:
            hold = self._holds[hold_id]
            if hold.status is HoldStatus.POSTED:
                if hold.entry_id is None:
                    raise RuntimeError("posted hold is missing its journal entry reference")
                return next(entry for entry in self._entries if entry.entry_id == hold.entry_id)
            if hold.status is HoldStatus.VOID:
                raise ValueError("void hold cannot be posted")
            entry = self.post(idempotency_key, hold.postings, _exclude_hold_id=hold_id)
            self._holds[hold_id] = Hold(
                hold.hold_id,
                hold.idempotency_key,
                hold.postings,
                hold.payload_hash,
                HoldStatus.POSTED,
                entry.entry_id,
            )
            return entry

    def void_hold(self, hold_id: str) -> Hold:
        with self._lock:
            hold = self._holds[hold_id]
            if hold.status is HoldStatus.POSTED:
                raise ValueError("posted hold cannot be voided")
            if hold.status is HoldStatus.VOID:
                return hold
            voided = Hold(
                hold.hold_id,
                hold.idempotency_key,
                hold.postings,
                hold.payload_hash,
                HoldStatus.VOID,
                None,
            )
            self._holds[hold_id] = voided
            self._hold_keys[hold.idempotency_key] = voided
            return voided

    def balance(self, account_id: str) -> Money:
        with self._lock:
            account = self._accounts[account_id]
            debit_total = sum(
                row.amount.amount_minor
                for entry in self._entries
                for row in entry.postings
                if row.account_id == account_id and row.direction is Direction.DEBIT
            )
            credit_total = sum(
                row.amount.amount_minor
                for entry in self._entries
                for row in entry.postings
                if row.account_id == account_id and row.direction is Direction.CREDIT
            )
            natural = debit_total - credit_total
            if account.account_type in (
                AccountType.LIABILITY,
                AccountType.EQUITY,
                AccountType.INCOME,
            ):
                natural = -natural
            return Money(natural, account.currency)

    def balance_as_of(self, account_id: str, at: datetime) -> Money:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("as-of time must be timezone-aware")
        with self._lock:
            account = self._accounts[account_id]
            debit_total = 0
            credit_total = 0
            for entry in self._entries:
                if entry.created_at > at:
                    continue
                for row in entry.postings:
                    if row.account_id != account_id:
                        continue
                    if row.direction is Direction.DEBIT:
                        debit_total += row.amount.amount_minor
                    else:
                        credit_total += row.amount.amount_minor
            natural = debit_total - credit_total
            if account.account_type in (
                AccountType.LIABILITY,
                AccountType.EQUITY,
                AccountType.INCOME,
            ):
                natural = -natural
            return Money(natural, account.currency)

    def create_snapshot(self) -> BalanceSnapshot:
        with self._lock:
            created_at = datetime.now(UTC)
            balances = tuple(
                (account_id, self.balance(account_id)) for account_id in sorted(self._accounts)
            )
            snapshot = BalanceSnapshot(
                snapshot_id=f"snapshot_{len(self._snapshots) + 1:016d}",
                created_at=created_at,
                entry_count=len(self._entries),
                balances=balances,
            )
            self._snapshots.append(snapshot)
            return snapshot

    def verify_snapshot(self, snapshot: BalanceSnapshot) -> bool:
        with self._lock:
            entries_as_of = tuple(
                entry for entry in self._entries if entry.created_at <= snapshot.created_at
            )
            if len(entries_as_of) != snapshot.entry_count:
                return False
            for account_id, expected in snapshot.balances:
                if self.balance_as_of(account_id, snapshot.created_at) != expected:
                    return False
            return True

    def verify(self) -> bool:
        with self._lock:
            currencies = {account.currency for account in self._accounts.values()}
            return (
                self.verify_hash_chain()
                and all(self.balances_net_to_zero(currency) for currency in currencies)
                and all(self.verify_snapshot(snapshot) for snapshot in self._snapshots)
            )

    def available_balance(self, account_id: str) -> Money:
        with self._lock:
            account = self._accounts[account_id]
            posted = self.balance(account_id).amount_minor
            return Money(posted + self._pending_natural_decrease(account_id), account.currency)

    def pending_totals(self, account_id: str) -> tuple[Money, Money]:
        """Return pending debit and credit amounts from active holds for this account."""
        with self._lock:
            account = self._accounts[account_id]
            debits = 0
            credits = 0
            for hold in self._holds.values():
                if hold.status is not HoldStatus.PENDING:
                    continue
                for row in hold.postings:
                    if row.account_id != account_id:
                        continue
                    if row.direction is Direction.DEBIT:
                        debits += row.amount.amount_minor
                    else:
                        credits += row.amount.amount_minor
            return Money(debits, account.currency), Money(credits, account.currency)

    def account_balance(self, account_id: str) -> AccountBalance:
        with self._lock:
            if account_id not in self._accounts:
                raise KeyError(account_id)
            pending_debits, pending_credits = self.pending_totals(account_id)
            version = sum(
                1
                for entry in self._entries
                for posting in entry.postings
                if posting.account_id == account_id
            )
            return AccountBalance(
                posted=self.balance(account_id),
                pending_debits=pending_debits,
                pending_credits=pending_credits,
                available=self.available_balance(account_id),
                version=version,
            )

    def verify_hash_chain(self) -> bool:
        with self._lock:
            previous_hash = "0" * 64
            for entry in self._entries:
                if entry.previous_hash != previous_hash:
                    return False
                expected = self._entry_hash(
                    entry.entry_id, entry.idempotency_key, entry.payload_hash, previous_hash
                )
                if expected != entry.entry_hash:
                    return False
                previous_hash = entry.entry_hash
            return True

    def balances_net_to_zero(self, currency: Currency) -> bool:
        with self._lock:
            debit_total = sum(
                row.amount.amount_minor
                for entry in self._entries
                for row in entry.postings
                if row.amount.currency == currency and row.direction is Direction.DEBIT
            )
            credit_total = sum(
                row.amount.amount_minor
                for entry in self._entries
                for row in entry.postings
                if row.amount.currency == currency and row.direction is Direction.CREDIT
            )
            return debit_total == credit_total

    def _validate_balanced(self, rows: tuple[Posting, ...]) -> None:
        if len(rows) < 2:
            raise ValueError("journal entry requires at least two postings")
        totals: dict[Currency, list[int]] = {}
        for row in rows:
            pair = totals.setdefault(row.amount.currency, [0, 0])
            pair[0 if row.direction is Direction.DEBIT else 1] += row.amount.amount_minor
        if len(totals) > 1:
            raise ValueError("cross-currency entries require explicit FX legs")
        if any(debit != credit for debit, credit in totals.values()):
            raise ValueError("journal entry must balance per currency")

    def _validate_accounts(self, rows: tuple[Posting, ...]) -> None:
        for row in rows:
            account = self._accounts.get(row.account_id)
            if account is None:
                raise ValueError(f"unknown account: {row.account_id}")
            if account.closed:
                raise ValueError(f"account is closed: {row.account_id}")
            if account.currency != row.amount.currency:
                raise ValueError("cross-currency posting requires explicit FX legs")

    def _validate_non_negative(
        self, rows: tuple[Posting, ...], exclude_hold_id: str | None = None
    ) -> None:
        deltas: dict[str, int] = {}
        for row in rows:
            account = self._accounts[row.account_id]
            natural_side_debit = account.account_type in (AccountType.ASSET, AccountType.EXPENSE)
            increases = (row.direction is Direction.DEBIT) == natural_side_debit
            deltas[row.account_id] = deltas.get(row.account_id, 0) + (
                row.amount.amount_minor if increases else -row.amount.amount_minor
            )
        for account_id, delta in deltas.items():
            account = self._accounts[account_id]
            outstanding = self._pending_natural_decrease(account_id, exclude_hold_id)
            if (
                not account.allow_negative
                and self.balance(account_id).amount_minor + outstanding + delta < 0
            ):
                raise ValueError(f"posting would make account negative: {account_id}")

    def _pending_natural_decrease(self, account_id: str, exclude_hold_id: str | None = None) -> int:
        account = self._accounts[account_id]
        reserved = 0
        for hold in self._holds.values():
            if hold.status is not HoldStatus.PENDING or hold.hold_id == exclude_hold_id:
                continue
            hold_delta = sum(
                self._natural_delta(account, row)
                for row in hold.postings
                if row.account_id == account_id
            )
            if hold_delta < 0:
                reserved += hold_delta
        return reserved

    @staticmethod
    def _natural_delta(account: Account, row: Posting) -> int:
        natural_side_debit = account.account_type in (AccountType.ASSET, AccountType.EXPENSE)
        increases = (row.direction is Direction.DEBIT) == natural_side_debit
        return row.amount.amount_minor if increases else -row.amount.amount_minor

    @staticmethod
    def _payload_hash(rows: tuple[Posting, ...]) -> str:
        serialized = sorted(
            (
                row.account_id,
                row.direction.value,
                row.amount.amount_minor,
                row.amount.currency.value,
            )
            for row in rows
        )
        payload = json.dumps(serialized, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _entry_hash(entry_id: str, key: str, payload_hash: str, previous_hash: str) -> str:
        data = f"{entry_id}\0{key}\0{payload_hash}\0{previous_hash}".encode()
        return hashlib.sha256(data).hexdigest()
