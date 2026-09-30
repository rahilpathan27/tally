"""Bank statement formats. Each simulated bank has its own conventions.

* ``csv_rupees_ist`` (bank A): CSV, amounts as rupee decimal strings ("1234.50"), timestamps as
  ``DD-MM-YYYY HH:MM:SS`` in Asia/Kolkata, status words ``SUCCESS``/``FAILED``.
* ``fixed_paise_utc`` (bank B): fixed-width records, amounts as zero-padded paise, UTC
  ``YYYYMMDDHHMMSS`` timestamps, one-letter status and four-letter type codes.
* ``json_offset`` (bank C): JSON array, ``{"value": "1234.50", "currency": "INR"}`` amounts,
  ISO-8601 timestamps with offsets, optional VPA, refunds reported as daily batch lines, and some
  UPI lines without the merchant reference (matched fuzzily).

Parsing never rounds: an amount with more than two decimal places or a non-INR currency is a
validation error for that line.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from libs.common.business_time import IST

from services.recon.engine import BankRecord, Kind

FORMATS = ("csv_rupees_ist", "fixed_paise_utc", "json_offset")
_TYPE_CODES = {
    Kind.UPI_TRANSFER: "UPIC",
    Kind.REFUND: "RFND",
    Kind.PAYOUT: "PAYO",
    Kind.REFUND_BATCH: "RFBT",
}
_CODE_TYPES = {code: kind for kind, code in _TYPE_CODES.items()}
_FIXED = ((0, 36), (36, 60), (60, 64), (64, 79), (79, 80), (80, 94), (94, 144))


@dataclass(frozen=True, slots=True)
class ParseIssue:
    line_no: int
    message: str


def rupees_to_minor(text: str) -> int:
    try:
        value = Decimal(text.strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount {text!r}") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("amount must be a finite non-negative decimal")
    minor = value * 100
    if minor != minor.to_integral_value():
        raise ValueError("amount has more than two decimal places")
    return int(minor)


def minor_to_rupees(amount_minor: int) -> str:
    return f"{amount_minor // 100}.{amount_minor % 100:02d}"


def write_statement(records: list[BankRecord], fmt: str) -> bytes:
    if fmt == "csv_rupees_ist":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["txn_ref", "bank_ref", "type", "amount_inr", "status", "txn_time_ist"])
        for r in records:
            writer.writerow(
                [
                    r.reference or "",
                    r.bank_reference,
                    r.kind.value,
                    minor_to_rupees(r.amount_minor),
                    "SUCCESS" if r.status == "success" else "FAILED",
                    r.occurred_at.astimezone(IST).strftime("%d-%m-%Y %H:%M:%S"),
                ]
            )
        return buffer.getvalue().encode()
    if fmt == "fixed_paise_utc":
        lines = []
        for r in records:
            lines.append(
                _fit(r.reference or "", 36)
                + _fit(r.bank_reference, 24)
                + _TYPE_CODES[r.kind]
                + _fit(str(r.amount_minor).rjust(15, "0"), 15)
                + ("S" if r.status == "success" else "F")
                + r.occurred_at.astimezone(UTC).strftime("%Y%m%d%H%M%S")
                + _fit(r.batch_id or "", 50)
            )
        return ("\n".join(lines) + "\n").encode()
    if fmt == "json_offset":
        payload = [
            {
                "reference": r.reference,
                "utr": r.bank_reference,
                "kind": r.kind.value,
                "amount": {"value": minor_to_rupees(r.amount_minor), "currency": "INR"},
                "state": "SUCCESS" if r.status == "success" else "FAILED",
                "ts": r.occurred_at.astimezone(IST).isoformat(),
                "vpa": r.vpa,
                "batch_id": r.batch_id,
            }
            for r in records
        ]
        return json.dumps(payload, separators=(",", ":")).encode()
    raise ValueError(f"unknown statement format {fmt}")


def parse_statement(data: bytes, fmt: str) -> tuple[list[BankRecord], list[ParseIssue]]:
    """Normalize a statement; bad lines are reported, never silently dropped or rounded."""
    records: list[BankRecord] = []
    issues: list[ParseIssue] = []
    if fmt == "csv_rupees_ist":
        reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
        expected = {"txn_ref", "bank_ref", "type", "amount_inr", "status", "txn_time_ist"}
        if set(reader.fieldnames or ()) != expected:
            return [], [ParseIssue(1, "unexpected CSV header")]
        for line_no, row in enumerate(reader, start=2):
            try:
                records.append(
                    BankRecord(
                        line_no=line_no,
                        reference=row["txn_ref"] or None,
                        bank_reference=_required(row["bank_ref"]),
                        kind=Kind(row["type"]),
                        amount_minor=rupees_to_minor(row["amount_inr"]),
                        status=_status(row["status"]),
                        occurred_at=datetime.strptime(
                            row["txn_time_ist"], "%d-%m-%Y %H:%M:%S"
                        ).replace(tzinfo=IST),
                    )
                )
            except (ValueError, KeyError) as exc:
                issues.append(ParseIssue(line_no, str(exc)))
        return records, issues
    if fmt == "fixed_paise_utc":
        for line_no, raw in enumerate(data.decode("ascii").splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                if len(raw) != 144:
                    raise ValueError(f"record length {len(raw)} != 144")
                ref, bank_ref, code, amount, status, stamp, batch = (raw[a:b] for a, b in _FIXED)
                if not amount.isdigit():
                    raise ValueError("amount must be digits")
                records.append(
                    BankRecord(
                        line_no=line_no,
                        reference=ref.strip() or None,
                        bank_reference=_required(bank_ref.strip()),
                        kind=_CODE_TYPES[code],
                        amount_minor=int(amount),
                        status={"S": "success", "F": "failed"}[status],
                        occurred_at=datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC),
                        batch_id=batch.strip() or None,
                    )
                )
            except (ValueError, KeyError) as exc:
                issues.append(ParseIssue(line_no, str(exc)))
        return records, issues
    if fmt == "json_offset":
        try:
            # Numeric amounts must never pass through a binary float.
            rows = json.loads(data, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            return [], [ParseIssue(1, f"invalid JSON: {exc.msg}")]
        if not isinstance(rows, list):
            return [], [ParseIssue(1, "statement must be a JSON array")]
        for line_no, row in enumerate(rows, start=1):
            try:
                amount = row["amount"]
                if amount.get("currency") != "INR":
                    raise ValueError("only INR statements are supported")
                occurred = datetime.fromisoformat(row["ts"])
                if occurred.tzinfo is None:
                    raise ValueError("timestamp must include an offset")
                records.append(
                    BankRecord(
                        line_no=line_no,
                        reference=row.get("reference") or None,
                        bank_reference=_required(row["utr"]),
                        kind=Kind(row["kind"]),
                        amount_minor=rupees_to_minor(str(amount["value"])),
                        status=_status(row["state"]),
                        occurred_at=occurred,
                        vpa=row.get("vpa"),
                        batch_id=row.get("batch_id"),
                    )
                )
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                issues.append(ParseIssue(line_no, str(exc)))
        return records, issues
    raise ValueError(f"unknown statement format {fmt}")


def _fit(value: str, width: int) -> str:
    """Pad a fixed-width field; never truncate, because truncation corrupts identifiers."""
    if len(value) > width:
        raise ValueError(f"value {value[:12]!r}... exceeds fixed width {width}")
    return value.ljust(width)


def _required(value: str) -> str:
    if not value:
        raise ValueError("bank reference is required")
    return value


def _status(value: str) -> str:
    mapping = {"SUCCESS": "success", "FAILED": "failed"}
    if value not in mapping:
        raise ValueError(f"unknown status {value!r}")
    return mapping[value]
