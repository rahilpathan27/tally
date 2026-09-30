from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from services.recon.engine import BankRecord, Kind
from services.recon.formats import FORMATS, parse_statement, rupees_to_minor, write_statement

records = st.lists(
    st.builds(
        BankRecord,
        line_no=st.just(0),
        reference=st.one_of(st.none(), st.uuids().map(str)),
        bank_reference=st.from_regex(r"UTR[0-9]{13}", fullmatch=True),
        kind=st.sampled_from([Kind.UPI_TRANSFER, Kind.REFUND, Kind.PAYOUT]),
        amount_minor=st.integers(min_value=0, max_value=10**14),
        status=st.sampled_from(["success", "failed"]),
        occurred_at=st.integers(0, 10**8).map(
            lambda s: datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=s)
        ),
        vpa=st.none(),
        batch_id=st.none(),
    ),
    max_size=30,
)


@settings(max_examples=200)
@given(lines=records, fmt=st.sampled_from(FORMATS))
def test_every_format_round_trips_exactly(lines: list[BankRecord], fmt: str) -> None:
    numbered = [
        BankRecord(i + 1, *(getattr(r, f) for f in BankRecord.__slots__[1:]))
        for i, r in enumerate(lines)
    ]
    parsed, issues = parse_statement(write_statement(numbered, fmt), fmt)
    assert not issues
    first_line = 2 if fmt == "csv_rupees_ist" else 1
    assert len(parsed) == len(numbered)
    for original, back in zip(numbered, parsed, strict=True):
        assert back.line_no == original.line_no + first_line - 1
        assert (back.reference, back.bank_reference, back.kind, back.amount_minor, back.status) == (
            original.reference,
            original.bank_reference,
            original.kind,
            original.amount_minor,
            original.status,
        )
        assert back.occurred_at == original.occurred_at


def test_amounts_are_parsed_exactly_or_rejected() -> None:
    assert rupees_to_minor("1234.50") == 123_450
    assert rupees_to_minor("0.01") == 1
    assert rupees_to_minor("90071992547409.91") == 9_007_199_254_740_991
    for bad in ("1.005", "-1.00", "NaN", "1e3.5", "abc"):
        with pytest.raises(ValueError):
            rupees_to_minor(bad)


def test_bad_lines_are_reported_not_dropped() -> None:
    data = (
        b"txn_ref,bank_ref,type,amount_inr,status,txn_time_ist\n"
        b"a,UTR1,upi_transfer,10.001,SUCCESS,01-01-2026 10:00:00\n"
        b"b,UTR2,upi_transfer,10.00,MAYBE,01-01-2026 10:00:00\n"
        b"c,UTR3,upi_transfer,10.00,SUCCESS,01-01-2026 10:00:00\n"
    )
    parsed, issues = parse_statement(data, "csv_rupees_ist")
    assert [r.reference for r in parsed] == ["c"]
    assert [i.line_no for i in issues] == [2, 3]


def test_json_numeric_amounts_never_pass_through_binary_float() -> None:
    data = (
        b'[{"reference":"r","utr":"UTR1","kind":"upi_transfer",'
        b'"amount":{"value":90071992547409.91,"currency":"INR"},"state":"SUCCESS",'
        b'"ts":"2026-01-01T10:00:00+05:30"}]'
    )
    parsed, issues = parse_statement(data, "json_offset")
    assert not issues and parsed[0].amount_minor == 9_007_199_254_740_991


def test_fixed_width_refuses_to_truncate() -> None:
    line = BankRecord(1, "r", "U" * 25, Kind.PAYOUT, 1, "success", datetime.now(UTC))
    with pytest.raises(ValueError):
        write_statement([line], "fixed_paise_utc")
