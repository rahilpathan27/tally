"""Seeded end-to-end failure injection through the real payment services.

Each scenario drives one payment through the in-process stack (real PostgreSQL general and
ledger databases, real FastAPI apps) while injecting:

* simulator outcomes: declines, lost responses, credit-leg failures, unknown status, outages;
* network faults on the ledger, bank and card-network hops (dropped request, lost response);
* a process crash at any named step boundary (``services.core.faults.CRASH_POINTS``);
* duplicate and concurrent merchant requests (confirm storms, capture/cancel races);
* late external truth: the simulator hides its decision and reveals it after the deadline.

Time is advanced by rewriting the recovery schedule columns of the scenario's own payment, so
deadlines and leases expire without waiting. After every scenario an independent checker
compares the payment's state, transition log and ledger effects with simulator truth; every
``--check-every`` scenarios it runs the ledger's integrity verifier and a global conservation
query. Any failure prints the reproducible scenario seed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
from services.core.faults import CRASH_POINTS, SimulatedCrash
from services.core.recovery_worker import process_recovery_batch, sweep_stalled_payments
from services.core.state_machine import ALLOWED_TRANSITIONS, PaymentState
from services.simulators.network.api import CardNetworkConfig

from chaos.stack import LocalStack, build_stack, provision_databases

TERMINAL = {"succeeded", "failed", "reversed", "cancelled"}
CARD_POINTS = tuple(
    p for p in CRASH_POINTS if p.split(".")[0] in {"confirm", "card", "capture", "cancel"}
)
UPI_POINTS = tuple(p for p in CRASH_POINTS if p.split(".")[0] in {"confirm", "upi"})


class InvariantViolation(AssertionError):
    pass


@dataclass(slots=True)
class Scenario:
    index: int
    seed: int
    method: str
    amount: int
    crash_point: str | None
    recovery_crash: bool
    modes: dict[str, str]
    hide_truth: bool
    heal_at: int
    deadline_at: int
    duplicate_confirm: bool
    race_capture_cancel: bool
    capture: bool
    ledger_faults: list[str]
    external_faults: list[str]
    payer_vpa: str


@dataclass(slots=True)
class Outcome:
    counters: Counter[str] = field(default_factory=Counter)
    recon_expected_breaks: int = 0


def _pick(rng: random.Random, weighted: dict[str, int]) -> str:
    return rng.choices(list(weighted), weights=list(weighted.values()))[0]


def make_scenario(index: int, run_seed: int) -> Scenario:
    seed = (run_seed * 1_000_003 + index) & 0xFFFFFFFF
    rng = random.Random(seed)
    method = rng.choice(("card", "upi"))
    points = CARD_POINTS if method == "card" else UPI_POINTS
    modes: dict[str, str]
    if method == "card":
        modes = {
            "network": _pick(
                rng,
                {"approve": 50, "decline": 10, "http_500": 10, "timeout": 20, "late_success": 10},
            )
        }
    else:
        bank_modes = {
            "approve": 45,
            "decline": 10,
            "credit_failure": 10,
            "timeout": 10,
            "status_unknown": 10,
            "http_500": 8,
            "reverse_timeout": 7,
        }
        modes = {
            "psp": _pick(rng, {"approve": 85, "decline": 8, "http_500": 7}),
            "bank-a": _pick(rng, bank_modes),
            "bank-b": _pick(rng, bank_modes),
            "bank-c": _pick(rng, bank_modes),
        }
    ledger_faults = [
        rng.choice(("drop_request", "lose_response")) for _ in range(rng.choice((0, 0, 0, 1, 2)))
    ]
    external_faults = [
        rng.choice(("drop_request", "lose_response")) for _ in range(rng.choice((0, 0, 0, 1)))
    ]
    return Scenario(
        index=index,
        seed=seed,
        method=method,
        amount=rng.randint(1, 250_000_00),
        crash_point=rng.choice(points) if rng.random() < 0.35 else None,
        recovery_crash=rng.random() < 0.1,
        modes=modes,
        hide_truth=rng.random() < 0.25,
        heal_at=rng.randint(0, 5),
        deadline_at=rng.randint(1, 6),
        duplicate_confirm=rng.random() < 0.2,
        race_capture_cancel=rng.random() < 0.15,
        capture=rng.random() < 0.75,
        ledger_faults=ledger_faults,
        external_faults=external_faults,
        # payer@bank-c uses the opt-in deemed-success policy; the others auto-reverse.
        payer_vpa=rng.choice(("payer@bank-a", "payer@bank-a", "payer@bank-c")),
    )


class CrashOnce:
    def __init__(self, points: set[str]) -> None:
        self.points = points
        self.fired: list[str] = []

    def __call__(self, name: str) -> None:
        if name in self.points:
            self.points.discard(name)
            self.fired.append(name)
            raise SimulatedCrash(name)


def _is_crash(exc: BaseException) -> bool:
    if isinstance(exc, SimulatedCrash):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_crash(inner) for inner in exc.exceptions)
    cause = exc.__cause__ or exc.__context__
    return cause is not None and _is_crash(cause)


async def _call(coro: Any) -> httpx.Response | None:
    """Run a merchant request; a simulated crash abandons it like a killed process."""
    try:
        return await coro  # type: ignore[no-any-return]
    except BaseException as exc:  # noqa: BLE001 - crash propagation is the point
        if _is_crash(exc):
            return None
        raise


class FlowChaos:
    def __init__(self, stack: LocalStack) -> None:
        self.stack = stack
        self.bank = stack.bank_app.state  # type: ignore[attr-defined]
        self.network = stack.network_app.state  # type: ignore[attr-defined]
        self.psp = stack.psp_app.state  # type: ignore[attr-defined]
        self.core = stack.core_app.state  # type: ignore[attr-defined]

    def _configure(self, scenario: Scenario) -> None:
        if scenario.method == "card":
            self.network.config = CardNetworkConfig(
                "http://vault",
                self.network.config.vault_network_key,
                self.network.config.service_key,
                scenario.modes["network"],
            )
        else:
            self.psp.config.mode = scenario.modes["psp"]
            self.bank.config.modes = {
                bank: scenario.modes[bank] for bank in ("bank-a", "bank-b", "bank-c")
            }
        self.stack.ledger_transport.faults = list(scenario.ledger_faults)  # type: ignore[arg-type]
        external = (
            self.stack.network_transport if scenario.method == "card" else self.stack.bank_transport
        )
        external.faults = list(scenario.external_faults)  # type: ignore[arg-type]

    def _heal(self, scenario: Scenario, payment_id: str, hidden: dict[str, str]) -> None:
        self.stack.ledger_transport.faults.clear()
        self.stack.bank_transport.faults.clear()
        self.stack.network_transport.faults.clear()
        # Healing models the outage ending and the breaker's reset period elapsing.
        for name in (
            "bank_breaker",
            "card_network_breaker",
            "bank_status_breaker",
            "card_network_status_breaker",
        ):
            getattr(self.core, name).record_success()
        if scenario.method == "card":
            config = self.network.config
            self.network.config = CardNetworkConfig(
                config.vault_url, config.vault_network_key, config.service_key, "approve"
            )
            if payment_id in hidden:
                self.network.statuses[payment_id] = hidden.pop(payment_id)
            return
        self.psp.config.mode = "approve"
        truth = self._bank_decision(payment_id)
        self.bank.config.modes = {}
        if payment_id in hidden:
            self.bank.config.statuses[payment_id] = hidden.pop(payment_id)
        elif truth is not None and payment_id not in self.bank.config.statuses:
            # status_unknown banks decided but never exposed the result; reveal it now.
            self.bank.config.statuses[payment_id] = truth

    def _bank_decision(self, payment_id: str) -> str | None:
        prior = self.bank.config.requests.get(f"{payment_id}:upi:bank:transfer")
        return None if prior is None else str(prior[1].status)

    def _hide(self, scenario: Scenario, payment_id: str, hidden: dict[str, str]) -> None:
        """Make the external party temporarily unaware of its own decision."""
        if not scenario.hide_truth:
            return
        statuses = self.network.statuses if scenario.method == "card" else self.bank.config.statuses
        if payment_id in statuses and payment_id not in hidden:
            hidden[payment_id] = statuses.pop(payment_id)

    async def _time_travel(self, payment_id: UUID, *, deadline: bool, late_window: bool) -> None:
        await self.stack.general_pool.execute(
            """UPDATE payment_intents
               SET updated_at = updated_at - interval '1 hour',
                   next_recovery_at = clock_timestamp() - interval '1 second',
                   recovery_lease_until = NULL,
                   recovery_deadline = CASE WHEN $2
                       THEN clock_timestamp() - interval '1 second' ELSE recovery_deadline END,
                   late_success_until = CASE WHEN $3 AND late_success_until IS NOT NULL
                       THEN clock_timestamp() - interval '1 second' ELSE late_success_until END
               WHERE payment_id = $1""",
            payment_id,
            deadline,
            late_window,
        )

    async def _status(self, payment_id: UUID) -> tuple[str, bool]:
        row = await self.stack.general_pool.fetchrow(
            """SELECT p.status, p.late_success_until IS NOT NULL OR EXISTS (
                   SELECT 1 FROM core_ledger_commands c WHERE c.payment_id = p.payment_id
                     AND c.state = 'pending' AND c.operation IN ('post_hold', 'void_hold')
               ) AS watching
               FROM payment_intents p WHERE p.payment_id = $1""",
            payment_id,
        )
        assert row is not None
        return str(row["status"]), bool(row["watching"])

    async def _recover(self, crash: CrashOnce) -> None:
        self.core.fault_injector = crash
        try:
            await sweep_stalled_payments(
                self.core.payment_repository,
                self.core.ledger_http,
                stale_after_seconds=30,
                faults=self.core,
            )
            await process_recovery_batch(
                self.core.payment_repository,
                self.core.bank_http,
                self.core.ledger_http,
                self.core.bank_status_breaker,
                network_http=self.core.card_network_http,
                network_breaker=self.core.card_network_status_breaker,
                faults=self.core,
            )
        except BaseException as exc:  # noqa: BLE001
            if not _is_crash(exc):
                raise
        finally:
            self.core.fault_injector = None

    async def run(self, scenario: Scenario, outcome: Outcome) -> None:
        stack = self.stack
        self._configure(scenario)
        # Breakers are process-local; a fresh process after a crash starts with them closed.
        for name in (
            "bank_breaker",
            "card_network_breaker",
            "bank_status_breaker",
            "card_network_status_breaker",
        ):
            getattr(self.core, name).record_success()
        body: dict[str, object] = {
            "amount_minor": scenario.amount,
            "currency": "INR",
            "payment_method_type": scenario.method,
        }
        if scenario.method == "card":
            body["payment_method_token"] = stack.card_token
        else:
            body["payer_vpa"] = scenario.payer_vpa
            body["payee_vpa"] = "merchant@bank-b"
        for _attempt in range(4):
            # A merchant retries 5xx with the same key; the gateway released the reservation.
            created = await stack.send(
                "POST", "/v1/payment_intents", body, f"chaos-{scenario.seed}-create"
            )
            if created.status_code < 500:
                break
            outcome.counters["create_retried_after_5xx"] += 1
        if created.status_code != 201:
            raise InvariantViolation(f"create failed: {created.status_code} {created.text}")
        payment_id = UUID(created.json()["payment_id"])
        pid = str(payment_id)
        hidden: dict[str, str] = {}

        crash = CrashOnce({scenario.crash_point} if scenario.crash_point else set())
        self.core.fault_injector = crash
        confirm_path = f"/v1/payment_intents/{pid}/confirm"
        try:
            if scenario.duplicate_confirm:
                await asyncio.gather(
                    _call(stack.send("POST", confirm_path, {}, f"chaos-{scenario.seed}-confirm")),
                    _call(stack.send("POST", confirm_path, {}, f"chaos-{scenario.seed}-confirm")),
                    _call(stack.send("POST", confirm_path, {}, f"chaos-{scenario.seed}-confirm2")),
                )
            else:
                await _call(stack.send("POST", confirm_path, {}, f"chaos-{scenario.seed}-confirm"))
        finally:
            self.core.fault_injector = None
        self._hide(scenario, pid, hidden)

        merchant_acted = False
        recovery_points = {"recovery.after_ledger_call"} if scenario.recovery_crash else set()
        recovery_crash = CrashOnce(recovery_points)
        for step in range(24):
            status, watching = await self._status(payment_id)
            if status == "authorized" and not merchant_acted:
                merchant_acted = True
                await self._merchant_finish_card(scenario, pid, crash)
                continue
            if status in TERMINAL and not watching:
                break
            if step == scenario.heal_at:
                self._heal(scenario, pid, hidden)
            await self._time_travel(
                payment_id,
                deadline=step >= scenario.deadline_at,
                late_window=step > max(scenario.heal_at, scenario.deadline_at) + 2,
            )
            await self._recover(recovery_crash)
        else:
            status, watching = await self._status(payment_id)
            raise InvariantViolation(f"payment {pid} did not settle: {status} watching={watching}")
        self._heal(scenario, pid, hidden)
        outcome.counters[f"crash_fired:{bool(crash.fired)}"] += 1
        await self.check_payment(scenario, payment_id, outcome)

    async def _merchant_finish_card(self, scenario: Scenario, pid: str, crash: CrashOnce) -> None:
        stack = self.stack
        capture = f"/v1/payment_intents/{pid}/capture"
        cancel = f"/v1/payment_intents/{pid}/cancel"
        crash.points |= {scenario.crash_point} if scenario.crash_point else set()
        self.core.fault_injector = crash
        try:
            if scenario.race_capture_cancel:
                await asyncio.gather(
                    _call(stack.send("POST", capture, {}, f"chaos-{scenario.seed}-capture")),
                    _call(stack.send("POST", cancel, {}, f"chaos-{scenario.seed}-cancel")),
                )
            elif scenario.capture:
                await _call(stack.send("POST", capture, {}, f"chaos-{scenario.seed}-capture"))
            else:
                await _call(stack.send("POST", cancel, {}, f"chaos-{scenario.seed}-cancel"))
        finally:
            self.core.fault_injector = None

    async def check_payment(self, scenario: Scenario, payment_id: UUID, outcome: Outcome) -> None:
        pid = str(payment_id)
        general = self.stack.general_pool
        ledger = self.stack.ledger_pool
        payment = await general.fetchrow(
            "SELECT status, amount_minor, recovery_policy FROM payment_intents "
            "WHERE payment_id = $1",
            payment_id,
        )
        assert payment is not None
        status = str(payment["status"])
        outcome.counters[f"{scenario.method}:{status}"] += 1

        # Transition log: accepted edges are legal and form one unbroken chain.
        transitions = await general.fetch(
            "SELECT from_state, to_state FROM payment_transitions "
            "WHERE payment_id = $1 AND accepted ORDER BY transition_id",
            payment_id,
        )
        previous: str | None = None
        for row in transitions:
            if row["from_state"] != previous:
                raise InvariantViolation(f"{pid}: broken transition chain at {dict(row)}")
            if (
                previous is not None
                and PaymentState(row["to_state"]) not in ALLOWED_TRANSITIONS[PaymentState(previous)]
            ):
                raise InvariantViolation(f"{pid}: illegal accepted transition {dict(row)}")
            previous = row["to_state"]
        if previous != status:
            raise InvariantViolation(f"{pid}: projection {status} != last transition {previous}")

        entries = {
            row["idempotency_key"]: row
            for row in await ledger.fetch(
                """SELECT e.idempotency_key, e.entry_id,
                          jsonb_agg(jsonb_build_object('a', p.account_id, 'd', p.direction,
                                    'm', p.amount_minor)) AS postings
                   FROM ledger_journal_entries e JOIN ledger_postings p USING (entry_id)
                   WHERE e.idempotency_key LIKE $1 GROUP BY e.entry_id""",
                f"{pid}:%",
            )
        }
        amount = int(payment["amount_minor"])
        if scenario.method == "upi":
            truth = self.bank.config.statuses.get(pid)
            transfer = entries.get(f"{pid}:upi:transfer:post")
            correction = entries.get(f"{pid}:upi:transfer:post:late-success-correction")
            if status == "succeeded":
                if transfer is None:
                    raise InvariantViolation(f"{pid}: succeeded without a ledger transfer")
                _assert_amounts(pid, transfer["postings"], amount)
                if truth != "approved":
                    if payment["recovery_policy"] != "deemed_success":
                        raise InvariantViolation(f"{pid}: succeeded but bank truth is {truth}")
                    # Accepted risk of the opt-in policy; reconciliation must flag it.
                    outcome.recon_expected_breaks += 1
                    outcome.counters["deemed_success_bank_mismatch"] += 1
                if correction is not None:
                    raise InvariantViolation(f"{pid}: success also has a late correction")
            else:
                if transfer is not None:
                    raise InvariantViolation(f"{pid}: {status} payment has a merchant transfer")
                if truth == "approved":
                    if correction is None:
                        raise InvariantViolation(f"{pid}: bank moved money; ledger has none")
                    _assert_amounts(pid, correction["postings"], amount)
                    outcome.counters["late_success_corrected"] += 1
                    outcome.recon_expected_breaks += 1
                elif correction is not None:
                    raise InvariantViolation(f"{pid}: correction posted without bank success")
            if len(entries) > 2:
                raise InvariantViolation(f"{pid}: unexpected ledger entries {sorted(entries)}")
            return

        truth = self.network.statuses.get(pid)
        hold = await ledger.fetchrow(
            "SELECT status::text AS status, entry_id FROM ledger_holds WHERE idempotency_key = $1",
            f"{pid}:card:authorization:hold",
        )
        if status == "succeeded":
            if hold is None or hold["status"] != "posted":
                raise InvariantViolation(f"{pid}: captured without a posted hold ({hold})")
            if truth != "approved":
                raise InvariantViolation(f"{pid}: captured but network truth is {truth}")
        elif hold is not None and hold["status"] != "void":
            raise InvariantViolation(f"{pid}: {status} payment left hold {hold['status']}")
        if status == "reversed" and truth == "approved":
            if hold is None:
                raise InvariantViolation(f"{pid}: late card approval not reflected as a void hold")
            outcome.counters["late_card_authorization_voided"] += 1
        if status == "failed" and truth == "approved" and hold is None:
            raise InvariantViolation(f"{pid}: network approved but payment failed without a hold")

    async def check_global(self) -> None:
        rows = await self.stack.ledger_pool.fetch(
            "SELECT check_name, ok, detail FROM ledger_verify_integrity()"
        )
        failed = [dict(row) for row in rows if not row["ok"]]
        if failed:
            raise InvariantViolation(f"ledger integrity failed: {failed}")
        unbalanced = await self.stack.ledger_pool.fetch(
            """SELECT currency, sum(amount_minor) FILTER (WHERE direction = 'debit') AS debits,
                      sum(amount_minor) FILTER (WHERE direction = 'credit') AS credits
               FROM ledger_postings GROUP BY currency
               HAVING sum(amount_minor) FILTER (WHERE direction = 'debit')
                   <> sum(amount_minor) FILTER (WHERE direction = 'credit')"""
        )
        if unbalanced:
            raise InvariantViolation(f"ledger does not conserve money: {unbalanced}")
        negative = await self.stack.ledger_pool.fetch(
            """SELECT a.account_id FROM ledger_accounts a JOIN ledger_account_balances b
               USING (account_id) WHERE NOT a.allow_negative AND b.posted_minor < 0"""
        )
        if negative:
            raise InvariantViolation(f"non-negative accounts went negative: {negative}")
        duplicates = await self.stack.general_pool.fetchval(
            """SELECT count(*) FROM (SELECT payment_id FROM payment_transitions
               WHERE accepted AND to_state IN ('succeeded', 'failed', 'reversed', 'cancelled')
               GROUP BY payment_id HAVING count(*) > 1) d"""
        )
        if duplicates:
            raise InvariantViolation(f"{duplicates} payments reached more than one terminal state")


def _assert_amounts(pid: str, postings: object, amount: int) -> None:
    lines = json.loads(postings) if isinstance(postings, str) else postings
    assert isinstance(lines, list)
    debits = sum(int(p["m"]) for p in lines if p["d"] == "debit")
    credits = sum(int(p["m"]) for p in lines if p["d"] == "credit")
    if debits != amount or credits != amount:
        raise InvariantViolation(f"{pid}: ledger amount {debits}/{credits} != payment {amount}")


async def seed_chaos_routes(stack: LocalStack) -> None:
    await stack.general_pool.execute(
        """INSERT INTO core_vpas(vpa, bank_id) VALUES ('payer@bank-c', 'bank-c')
           ON CONFLICT (vpa) DO NOTHING"""
    )
    await stack.general_pool.execute(
        """INSERT INTO core_bank_recovery_policies(
               bank_id, min_amount_minor, max_amount_minor, status_check_deadline_seconds,
               late_success_window_seconds, deemed_outcome
           ) VALUES ('bank-c', 1, NULL, 30, 600, 'deemed_success')
           ON CONFLICT (bank_id, min_amount_minor) DO NOTHING"""
    )


async def run(
    scenarios: int,
    seed: int,
    check_every: int,
    only: int | None,
    start: int = 0,
    db_prefix: str = "tally_chaos",
) -> dict[str, Any]:
    general, ledger, vault = await provision_databases(db_prefix)
    stack = await build_stack(
        general, ledger, vault, os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    )
    await seed_chaos_routes(stack)
    harness = FlowChaos(stack)
    outcome = Outcome()
    started = time.monotonic()
    indexes = [only] if only is not None else range(start, start + scenarios)
    try:
        for count, index in enumerate(indexes, start=1):
            scenario = make_scenario(index, seed)
            try:
                await harness.run(scenario, outcome)
            except InvariantViolation as exc:
                raise InvariantViolation(
                    f"scenario {index} (seed {seed}, scenario seed {scenario.seed}) failed: {exc}"
                    f"\n{scenario}"
                ) from exc
            if count % check_every == 0:
                await harness.check_global()
                rate = count / (time.monotonic() - started)
                print(f"  {count} scenarios, {rate:.0f}/s, invariants hold", flush=True)
        await harness.check_global()
    finally:
        await stack.close()
    return {
        "seed": seed,
        "first_index": indexes[0],
        "scenarios": len(indexes),
        "seconds": round(time.monotonic() - started, 1),
        "recon_expected_breaks": outcome.recon_expected_breaks,
        "outcomes": dict(sorted(outcome.counters.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=int, default=2_000)
    parser.add_argument("--seed", type=int, default=20_260_930)
    parser.add_argument("--check-every", type=int, default=250)
    parser.add_argument("--only", type=int, default=None, help="replay one scenario index")
    # Shards of one seeded run execute in parallel on separate databases (scripts/chaos_100k.sh).
    parser.add_argument("--start", type=int, default=0, help="first scenario index")
    parser.add_argument("--db-prefix", default="tally_chaos")
    args = parser.parse_args()
    report = asyncio.run(
        run(args.scenarios, args.seed, args.check_every, args.only, args.start, args.db_prefix)
    )
    print("TALLY FLOW CHAOS: PASS")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
