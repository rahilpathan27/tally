"""On-cluster chaos: break things while merchant payments flow, then audit money.

    uv run python -m scripts.cluster_chaos --rate 30 --duration 8m

Runs a steady k6 load (see scripts/loadtest_k8s.py) and, on a fixed timeline, hard-kills pods
(core API, core worker, ledger, risk, bank simulator), crashes PostgreSQL (SIGKILL of a backend
forces crash recovery of the whole server), and partitions core from the ledger for 45 s by
removing that egress rule. After the load stops and recovery workers settle, it audits:

* every payment reached a terminal state (or is waiting in risk review by design);
* succeeded UPI payment => exactly one ledger transfer for its amount, no late-success
  correction; any other terminal state => no transfer;
* every ledger transfer belongs to a succeeded payment (no orphan money);
* the ledger integrity verifier (balanced entries, balances vs postings, hash chain, trial
  balance).

The bank simulator keeps its truth in memory and is one of the pods killed, so bank-side truth
is not compared here; that is what reconciliation and chaos/flow_sim.py cover.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

from scripts.loadtest_k8s import NS, k6_job, kubectl, psql, seed, wait_job

OUT = Path(".data/cluster-chaos")
TERMINAL = {"succeeded", "failed", "reversed", "cancelled", "expired"}


def pods(app: str) -> list[str]:
    names = kubectl(
        "-n",
        NS,
        "get",
        "pods",
        "-l",
        f"app.kubernetes.io/name=tally-{app}",
        "--field-selector=status.phase=Running",
        "-o",
        "jsonpath={.items[*].metadata.name}",
    ).split()
    return names


def kill_pod(app: str) -> str:
    victim = random.choice(pods(app))
    kubectl("-n", NS, "delete", "pod", victim, "--grace-period=0", "--force", "--wait=false")
    return f"SIGKILL pod {victim}"


def crash_postgres() -> str:
    # SIGKILL on any backend makes the postmaster reset every connection and run crash recovery.
    out = kubectl(
        "-n",
        "tally-deps",
        "exec",
        "deploy/postgres",
        "--",
        "sh",
        "-c",
        "pid=$(pgrep -f 'postgres: tally' | head -1); kill -9 $pid && echo $pid",
    ).strip()
    return f"SIGKILL postgres backend {out} (server crash recovery)"


async def partition_core_from_ledger(seconds: int) -> str:
    original = kubectl("-n", NS, "get", "networkpolicy", "tally-core", "-o", "json")
    policy = json.loads(original)
    for key in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields"):
        policy["metadata"].pop(key, None)
    policy.pop("status", None)
    restored = json.dumps(policy)
    policy["spec"]["egress"] = [
        rule
        for rule in policy["spec"]["egress"]
        if not any(
            peer.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/name")
            == "tally-ledger"
            for peer in rule.get("to", [])
        )
    ]
    kubectl("apply", "-f", "-", stdin=json.dumps(policy))
    await asyncio.sleep(seconds)
    kubectl("apply", "-f", "-", stdin=restored)
    return f"core -> ledger partitioned for {seconds} s"


async def timeline(events: list[tuple[str, str]], start: float) -> None:
    async def at(offset: float, action: Any) -> None:
        await asyncio.sleep(max(0.0, start + offset - time.monotonic()))
        result = action()
        description = await result if inspect.isawaitable(result) else result
        stamp = round(time.monotonic() - start)
        events.append((f"t+{stamp}s", description))
        print(f"  t+{stamp}s {description}", flush=True)

    await asyncio.gather(
        at(60, lambda: kill_pod("core")),
        at(100, lambda: kill_pod("core-worker")),
        at(140, lambda: kill_pod("ledger")),
        at(180, lambda: kill_pod("risk")),
        at(220, lambda: kill_pod("bank-sim")),
        at(260, crash_postgres),
        at(300, lambda: partition_core_from_ledger(45)),
        at(380, lambda: kill_pod("core")),
    )


def audit(since: str) -> dict[str, Any]:
    payments = json.loads(
        psql(
            "tally",
            "SELECT coalesce(json_agg(json_build_object('id', payment_id, 's', status, "
            "'a', amount_minor)), '[]') FROM payment_intents "
            f"WHERE merchant_id LIKE 'load-merchant-%' AND created_at >= '{since}'",
        )
    )
    entries = json.loads(
        psql(
            "tally_ledger",
            "SELECT coalesce(json_agg(json_build_object('k', e.idempotency_key, 'd', d.debits)), "
            "'[]') FROM ledger_journal_entries e JOIN LATERAL (SELECT sum(amount_minor) debits "
            "FROM ledger_postings p WHERE p.entry_id = e.entry_id AND p.direction = 'debit') d "
            "ON true WHERE e.idempotency_key LIKE '%:upi:transfer:post%' "
            f"AND e.created_at >= '{since}'",
        )
    )
    by_key = {e["k"]: e["d"] for e in entries}
    statuses: dict[str, int] = {}
    problems: list[str] = []
    ids = set()
    for p in payments:
        pid, status, amount = p["id"], p["s"], p["a"]
        ids.add(pid)
        statuses[status] = statuses.get(status, 0) + 1
        transfer = by_key.get(f"{pid}:upi:transfer:post")
        correction = by_key.get(f"{pid}:upi:transfer:post:late-success-correction")
        if status == "created":
            # The client never confirmed (create response lost or timed out): no money may move.
            if transfer is not None or correction is not None:
                problems.append(f"{pid} was never confirmed but has ledger entries")
        elif status not in TERMINAL and status != "risk_review":
            problems.append(f"{pid} stuck in {status}")
        elif status == "succeeded":
            if transfer is None:
                problems.append(f"{pid} succeeded without a ledger transfer")
            elif int(transfer) != int(amount):
                problems.append(f"{pid} transfer {transfer} != amount {amount}")
            if correction is not None:
                problems.append(f"{pid} succeeded and has a late-success correction")
        elif transfer is not None:
            problems.append(f"{pid} is {status} but has a merchant transfer")
    orphans = [k for k in by_key if k.split(":", 1)[0] not in ids]
    problems += [f"orphan ledger entry {k}" for k in orphans]
    corrections = sum(1 for k in by_key if k.endswith("late-success-correction"))
    integrity = json.loads(
        psql(
            "tally_ledger", "SELECT json_object_agg(check_name, ok) FROM ledger_verify_integrity()"
        )
    )
    if not all(integrity.values()):
        problems.append(f"ledger integrity failed: {integrity}")
    return {
        "payments": len(payments),
        "status_mix": statuses,
        "ledger_transfers": len(by_key) - corrections,
        "late_success_corrections": corrections,
        "ledger_integrity": integrity,
        "problems": problems[:50],
        "problem_count": len(problems),
    }


async def run(rate: int, duration: str, settle: int) -> dict[str, Any]:
    seed(merchants=20, payers=20000)
    since = psql("tally", "SELECT clock_timestamp()")
    job = k6_job("steady", rate, duration, 20000)
    print(f"load {job}: {rate} payments/s for {duration}", flush=True)
    events: list[tuple[str, str]] = []
    await timeline(events, time.monotonic())
    await asyncio.to_thread(wait_job, job, 3600)
    logs = kubectl("-n", NS, "logs", f"job/{job}")
    line = next(x for x in logs.splitlines() if x.startswith("K6_SUMMARY "))
    k6 = json.loads(line.removeprefix("K6_SUMMARY "))["metrics"]
    # Recovery: status-check deadlines are 30 s; give workers time to resolve everything.
    deadline = time.monotonic() + settle
    report: dict[str, Any] = {}
    while time.monotonic() < deadline:
        report = audit(since)
        stuck = [p for p in report["problems"] if " stuck in " in p]
        if not stuck:
            break
        print(f"  waiting for {len(stuck)} payments to settle", flush=True)
        await asyncio.sleep(20)
    report["settled_after_seconds"] = round(settle - (deadline - time.monotonic()))
    report["chaos_events"] = events
    report["k6"] = {
        "iterations": k6["iterations"]["values"]["count"],
        "http_req_failed_rate": round(k6["http_req_failed"]["values"]["rate"], 4),
        "payment_e2e_ms": {
            k: round(v, 1) for k, v in k6.get("payment_e2e_ms", {}).get("values", {}).items()
        },
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rate", type=int, default=30)
    parser.add_argument("--duration", default="8m")
    parser.add_argument("--settle", type=int, default=900)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(run(args.rate, args.duration, args.settle))
    (OUT / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    ok = report["problem_count"] == 0
    print("CLUSTER CHAOS:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
