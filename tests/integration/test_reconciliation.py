"""Phase 9: three-way reconciliation over real ledger/switch data and a bank statement."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from chaos.stack import LocalStack, build_stack, provision_databases
from libs.common.business_time import business_date, cutoff_instant
from libs.common.object_store import FilesystemObjectStore
from services.core.money_routes import run_money_workers
from services.core.recovery_worker import process_recovery_batch
from services.recon.api import create_app as create_recon_app
from services.recon.engine import BankRecord, BreakType, Kind, ReconConfig
from services.recon.formats import parse_statement, write_statement
from services.recon.service import ReconContext, bank_records_from_journal

HEADERS = {"x-internal-key": "recon-key", "x-actor": "alice"}


async def _upi(stack: LocalStack, amount: int, tag: str) -> str:
    created = await stack.send(
        "POST",
        "/v1/payment_intents",
        {
            "amount_minor": amount,
            "currency": "INR",
            "payment_method_type": "upi",
            "payer_vpa": "payer@bank-a",
            "payee_vpa": "merchant@bank-b",
        },
        f"{tag}-c",
    )
    payment_id = str(created.json()["payment_id"])
    await stack.send("POST", f"/v1/payment_intents/{payment_id}/confirm", {}, f"{tag}-f")
    return payment_id


async def _late_success(stack: LocalStack) -> str:
    """A bank approval whose response was lost, reversed at the deadline, then revealed."""
    state = stack.core_app.state  # type: ignore[attr-defined]
    bank = stack.bank_app.state.config  # type: ignore[attr-defined]
    bank.modes = {"bank-a": "late_success"}
    payment_id = await _upi(stack, 77_700, "late")
    bank.modes = {}
    hidden = bank.statuses.pop(payment_id)
    await stack.general_pool.execute(
        """UPDATE payment_intents SET recovery_deadline = clock_timestamp() - interval '1s',
               next_recovery_at = clock_timestamp() WHERE payment_id = $1""",
        UUID(payment_id),
    )
    args = (state.payment_repository, state.bank_http, state.ledger_http, state.bank_breaker)
    await process_recovery_batch(*args)
    bank.statuses[payment_id] = hidden
    await stack.general_pool.execute(
        "UPDATE payment_intents SET next_recovery_at = clock_timestamp() WHERE payment_id = $1",
        UUID(payment_id),
    )
    await process_recovery_batch(*args)
    status = await stack.general_pool.fetchval(
        "SELECT status FROM payment_intents WHERE payment_id = $1", UUID(payment_id)
    )
    assert status == "reversed"
    return payment_id


def test_three_way_recon_finds_every_planted_break(tmp_path: Path) -> None:
    if os.environ.get("TALLY_STACK_TESTS") != "1":
        pytest.skip("set TALLY_STACK_TESTS=1 with local Compose services to run stack tests")

    async def exercise() -> None:
        general, ledger, vault = await provision_databases("tally_it_recon")
        store = FilesystemObjectStore(tmp_path)
        stack = await build_stack(
            general,
            ledger,
            vault,
            os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            object_store=store,
        )
        state = stack.core_app.state  # type: ignore[attr-defined]
        bank_state = stack.bank_app.state.config  # type: ignore[attr-defined]
        ledger_http = state.ledger_http
        ctx = ReconContext(stack.general_pool, ledger_http, store, ReconConfig())
        recon = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_recon_app(ctx, internal_key="recon-key")),
            base_url="http://recon",
            headers=HEADERS,
        )
        try:
            payments = [await _upi(stack, 10_000 + 1_111 * i, f"p{i}") for i in range(24)]
            for i, payment_id in enumerate(payments[:4]):
                await stack.send(
                    "POST",
                    "/v1/refunds",
                    {"payment_id": payment_id, "amount_minor": 2_000},
                    f"refund-{i}",
                )
            await run_money_workers(state)
            await stack.general_pool.execute(
                "UPDATE payment_intents SET succeeded_at = succeeded_at - interval '3 days' "
                "WHERE payment_id = ANY($1::uuid[])",
                [UUID(p) for p in payments[:12]],
            )
            today = business_date(datetime.now(UTC))
            await stack.client.post(
                "/internal/v1/settlements/run",
                json={"merchant_id": stack.merchant_id, "business_date": today.isoformat()},
                headers={"x-internal-key": "stack-recovery-key"},
            )
            await run_money_workers(state)
            late = await _late_success(stack)

            # The bank's statement for today, rendered from its decision journal.
            end = cutoff_instant(today)
            truth = bank_records_from_journal(
                list(bank_state.journal), end - timedelta(days=1), end
            )
            kinds = {line.kind for line in truth}
            assert {Kind.UPI_TRANSFER, Kind.REFUND, Kind.PAYOUT} <= kinds
            by_ref = {line.reference: line for line in truth}
            upi = [by_ref[p] for p in payments[12:]]
            planted: dict[str, BreakType] = {}
            statement = [line for line in truth if line.reference != upi[0].reference]
            planted[str(upi[0].reference)] = BreakType.MISSING_AT_BANK
            timing = upi[1]
            statement = [line for line in statement if line.reference != timing.reference]
            planted[str(timing.reference)] = BreakType.TIMING_DIFFERENCE
            mutated: list[BankRecord] = []
            for line in statement:
                if line.reference == upi[2].reference:
                    line = replace(line, amount_minor=line.amount_minor + 13)
                    planted[str(line.reference)] = BreakType.AMOUNT_MISMATCH
                elif line.reference == upi[3].reference:
                    line = replace(line, status="failed")
                    planted[str(line.reference)] = BreakType.STATUS_MISMATCH
                mutated.append(line)
            mutated.append(replace(upi[4], line_no=0))  # duplicate line
            planted[upi[4].bank_reference] = BreakType.DUPLICATE
            stranger = BankRecord(
                0,
                "ffffffff-0000-4000-8000-000000000001",
                "UTRSTRANGER01",
                Kind.UPI_TRANSFER,
                31_337,
                "success",
                end - timedelta(hours=3),
            )
            mutated.append(stranger)
            planted[str(stranger.reference)] = BreakType.MISSING_INTERNALLY
            unknown = BankRecord(
                0,
                None,
                "UTRNOREF00001",
                Kind.UPI_TRANSFER,
                9_999_999,
                "success",
                end - timedelta(hours=2),
                "ghost@bank-z",
            )
            mutated.append(unknown)
            planted[unknown.bank_reference] = BreakType.UNKNOWN
            payout_line = next(line for line in mutated if line.kind == Kind.PAYOUT)
            mutated = [
                replace(line, amount_minor=line.amount_minor - 590) if line is payout_line else line
                for line in mutated
            ]
            planted[str(payout_line.reference)] = BreakType.FEE_TAX_MISMATCH
            # The genuine late success is an expected status mismatch, not a planted one.
            expected_natural = {late: BreakType.STATUS_MISMATCH}
            numbered = [replace(line, line_no=n) for n, line in enumerate(mutated, start=1)]

            for fmt in ("csv_rupees_ist", "fixed_paise_utc", "json_offset"):
                parsed, issues = parse_statement(write_statement(numbered, fmt), fmt)
                assert not issues and len(parsed) == len(numbered)
            # An earlier, partial version of the day's statement (an intraday pull). The full
            # statement below replaces it; combining both would report spurious duplicates.
            early = await recon.post(
                "/v1/files",
                params={
                    "source": "sponsor-bank",
                    "business_date": today.isoformat(),
                    "format": "fixed_paise_utc",
                },
                content=write_statement(numbered[:5], "fixed_paise_utc"),
            )
            assert early.status_code == 201, early.text
            uploaded = await recon.post(
                "/v1/files",
                params={
                    "source": "sponsor-bank",
                    "business_date": today.isoformat(),
                    "format": "fixed_paise_utc",
                },
                content=write_statement(numbered, "fixed_paise_utc"),
            )
            assert uploaded.status_code == 201, uploaded.text
            again = await recon.post(
                "/v1/files",
                params={
                    "source": "sponsor-bank",
                    "business_date": today.isoformat(),
                    "format": "fixed_paise_utc",
                },
                content=write_statement(numbered, "fixed_paise_utc"),
            )
            assert again.json()["file_id"] == uploaded.json()["file_id"]
            next_day = [replace(timing, line_no=1, occurred_at=end + timedelta(minutes=3))]
            await recon.post(
                "/v1/files",
                params={
                    "source": "sponsor-bank",
                    "business_date": (today + timedelta(days=1)).isoformat(),
                    "format": "csv_rupees_ist",
                },
                content=write_statement(next_day, "csv_rupees_ist"),
            )

            run = await recon.post(
                "/v1/runs", json={"source": "sponsor-bank", "business_date": today.isoformat()}
            )
            assert run.status_code == 201, run.text
            breaks = (await recon.get("/v1/breaks", params={"limit": 500})).json()
            found: dict[str, str] = {}
            for item in breaks:
                for key in (item["reference"], item["bank_reference"]):
                    if key in planted or key in expected_natural:
                        found[key] = item["break_type"]
            assert {k: planted[k].value for k in planted} == {k: found.get(k) for k in planted}
            assert found[late] == "status_mismatch"
            unexplained = [
                b
                for b in breaks
                if not ({b["reference"], b["bank_reference"]} & (set(planted) | {late}))
            ]
            assert unexplained == [], unexplained
            timing_break = next(b for b in breaks if b["reference"] == timing.reference)
            assert timing_break["status"] == "auto_resolved"

            # Idempotent re-run: same breaks, no duplicates.
            count = await stack.general_pool.fetchval("SELECT count(*) FROM recon_breaks")
            await recon.post(
                "/v1/runs", json={"source": "sponsor-bank", "business_date": today.isoformat()}
            )
            assert await stack.general_pool.fetchval("SELECT count(*) FROM recon_breaks") == count

            # Maker-checker: the maker cannot approve; a second person can.
            stranger_break = next(b for b in breaks if b["reference"] == stranger.reference)
            detail = (await recon.get(f"/v1/breaks/{stranger_break['break_id']}")).json()
            assert detail["evidence"]["bank"][0]["amount_minor"] == 31_337
            proposal = await recon.post(
                f"/v1/breaks/{stranger_break['break_id']}/adjustments",
                json={"action": "book_to_suspense", "note": "unattributed credit"},
            )
            assert proposal.status_code == 201, proposal.text
            request_id = proposal.json()["request_id"]
            self_approve = await recon.post(
                f"/v1/approvals/{request_id}/approve", json={"reason": "ok"}
            )
            assert self_approve.status_code == 403
            approved = await recon.post(
                f"/v1/approvals/{request_id}/approve",
                json={"reason": "verified with bank"},
                headers={**HEADERS, "x-actor": "bob"},
            )
            assert approved.json()["status"] == "executed", approved.text
            suspense = await stack.ledger_pool.fetchval(
                "SELECT posted_minor FROM ledger_account_balances "
                "WHERE account_id = 'platform:suspense:INR'"
            )
            assert suspense == 31_337 + 77_700  # adjustment plus the late-success correction
            await recon.post(
                "/v1/runs", json={"source": "sponsor-bank", "business_date": today.isoformat()}
            )
            resolved = (await recon.get(f"/v1/breaks/{stranger_break['break_id']}")).json()
            assert resolved["status"] == "resolved"

            # Next day: the late line is carried over, not reported as missing internally.
            tomorrow = await recon.post(
                "/v1/runs",
                json={
                    "source": "sponsor-bank",
                    "business_date": (today + timedelta(days=1)).isoformat(),
                },
            )
            assert tomorrow.status_code == 201, tomorrow.text
            assert tomorrow.json()["matched_lines"] == 1
            assert tomorrow.json()["breaks_by_type"] == {}

            report = (
                await recon.get("/v1/reports/daily", params={"business_date": today.isoformat()})
            ).json()
            assert report["sources"][0]["bank_lines"] == len(numbered)
            assert 0 < report["match_rate_bps"] < 10_000
            integrity = await stack.ledger_pool.fetch("SELECT ok FROM ledger_verify_integrity()")
            assert all(row["ok"] for row in integrity)
        finally:
            await recon.aclose()
            await stack.close()

    asyncio.run(exercise())
