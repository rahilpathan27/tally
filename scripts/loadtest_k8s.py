"""Run k6 payment load against the local Kubernetes deployment and record the results.

    uv run python -m scripts.loadtest_k8s --scenario steady --rate 100 --duration 5m

Needs `make k8s-up` with the load-test overlay (``--prepare`` applies it). k6 runs inside the
cluster as a Job (labelled as the load generator, with its own NetworkPolicy pair) so the
client is not behind a port-forward. Afterwards the script collects:

* k6's client-side view (offered vs achieved rate, latency percentiles, failures);
* server-side latency per service from Prometheus;
* correctness: payment status mix, payments stuck in non-terminal states two minutes after the
  load stops, and the ledger integrity verifier.

Results go to ``.data/loadtest/<scenario>-<rate>.json``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".data" / "k8s-local"
OUT = ROOT / ".data" / "loadtest"
CTX = ["--context", "tally"]
NS = "tally-local"
K6_IMAGE = "grafana/k6:1.3.0"


def kubectl(*args: str, stdin: str | None = None, check: bool = True) -> str:
    result = subprocess.run(
        ["kubectl", *CTX, *args], input=stdin, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def prepare() -> None:
    """Apply the load-test overlay and wait for both canaried services to settle."""
    tag = (STATE / "image-tag").read_text().strip()
    subprocess.run(
        [
            str(ROOT / ".data/bin/helm"),
            "--kube-context",
            "tally",
            "upgrade",
            "tally",
            str(ROOT / "infra/helm/tally"),
            "-n",
            NS,
            "-f",
            str(ROOT / "infra/helm/tally/ci/local-values.yaml"),
            "-f",
            str(ROOT / "infra/helm/tally/ci/loadtest-values.yaml"),
            "-f",
            str(STATE / "secrets.yaml"),
            "--set",
            f"global.image.tag={tag}",
            "--wait",
            "--timeout",
            "12m",
        ],
        check=True,
    )
    for rollout in ("tally-core", "tally-ledger"):
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            phase = kubectl("-n", NS, "get", "rollout", rollout, "-o", "jsonpath={.status.phase}")
            if phase == "Healthy":
                break
            time.sleep(5)
        else:
            raise RuntimeError(f"{rollout} did not become Healthy")


def seed(merchants: int, payers: int) -> None:
    created = kubectl(
        "-n",
        NS,
        "exec",
        "deploy/tally-core-worker",
        "--",
        "python",
        "-m",
        "scripts.canary_traffic",
        "seed",
        "--merchants",
        str(merchants),
        "--payers",
        str(payers),
        "--prefix",
        "load",
    )
    secret = kubectl(
        "-n",
        NS,
        "create",
        "secret",
        "generic",
        "load-merchants",
        f"--from-literal=merchants.json={created.strip()}",
        "--dry-run=client",
        "-o",
        "yaml",
    )
    kubectl("apply", "-f", "-", stdin=secret)


def k6_job(scenario: str, rate: int, duration: str, payers: int) -> str:
    name = f"k6-{scenario}-{rate}-{int(time.time())}"
    script = kubectl(
        "-n",
        NS,
        "create",
        "configmap",
        "k6-script",
        f"--from-file=payments.js={ROOT / 'loadtest/k6/payments.js'}",
        "--dry-run=client",
        "-o",
        "yaml",
    )
    kubectl("apply", "-f", "-", stdin=script)

    manifest = f"""
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {{name: drill-loadgen, namespace: {NS}}}
spec:
  podSelector: {{matchLabels: {{tally.io/role: loadgen}}}}
  policyTypes: [Ingress, Egress]
  egress:
    - to: [{{podSelector: {{matchLabels: {{app.kubernetes.io/name: tally-core}}}}}}]
      ports: [{{protocol: TCP, port: 8000}}]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {{name: drill-core-from-loadgen, namespace: {NS}}}
spec:
  podSelector: {{matchLabels: {{app.kubernetes.io/name: tally-core}}}}
  policyTypes: [Ingress]
  ingress:
    - from: [{{podSelector: {{matchLabels: {{tally.io/role: loadgen}}}}}}]
      ports: [{{protocol: TCP, port: 8000}}]
---
apiVersion: batch/v1
kind: Job
metadata: {{name: {name}, namespace: {NS}}}
spec:
  backoffLimit: 0
  template:
    metadata: {{labels: {{tally.io/role: loadgen}}}}
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext:
        {{runAsNonRoot: true, runAsUser: 12345, seccompProfile: {{type: RuntimeDefault}}}}
      containers:
        - name: k6
          image: {K6_IMAGE}
          args: [run, --quiet, /scripts/payments.js]
          env:
            - {{name: CORE_URL, value: "http://tally-core.{NS}.svc:8000"}}
            - {{name: SCENARIO, value: "{scenario}"}}
            - {{name: RATE, value: "{rate}"}}
            - {{name: DURATION, value: "{duration}"}}
            - {{name: PAYERS, value: "{payers}"}}
            - {{name: MERCHANTS_FILE, value: /creds/merchants.json}}
          resources: {{requests: {{cpu: 500m, memory: 256Mi}}, limits: {{memory: 2Gi}}}}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {{drop: [ALL]}}
          volumeMounts:
            - {{name: script, mountPath: /scripts}}
            - {{name: creds, mountPath: /creds}}
            - {{name: tmp, mountPath: /tmp}}
      volumes:
        - {{name: script, configMap: {{name: k6-script}}}}
        - {{name: creds, secret: {{secretName: load-merchants}}}}
        - {{name: tmp, emptyDir: {{}}}}
"""
    kubectl("apply", "-f", "-", stdin=manifest)
    return name


def cpu_sample(samples: dict[str, list[int]]) -> None:
    """Record millicores per workload (pods aggregated) from metrics-server."""
    out = kubectl("top", "pods", "-A", "--no-headers", check=False)
    totals: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[2].endswith("m"):
            continue
        namespace, pod = parts[0], parts[1]
        if namespace not in (NS, "tally-deps"):
            continue
        workload = pod.rsplit("-", 2)[0] if pod.count("-") >= 2 else pod
        workload = workload.removeprefix("tally-")
        if workload.startswith("k6-"):
            workload = "k6 (load generator)"
        totals[workload] = totals.get(workload, 0) + int(parts[2][:-1])
    for workload, millicores in totals.items():
        samples.setdefault(workload, []).append(millicores)


def wait_job(name: str, timeout: float, samples: dict[str, list[int]] | None = None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if samples is not None:
            cpu_sample(samples)
        state = kubectl(
            "-n",
            NS,
            "get",
            "job",
            name,
            "-o",
            "jsonpath={.status.succeeded}/{.status.failed}",
        )
        # k6 exits non-zero when a threshold is crossed; that is a result, not an error.
        if state.startswith("1/") or state.endswith("/1"):
            return
        time.sleep(15)
    raise RuntimeError(f"{name} did not finish")


def prom(query: str, at: float | None = None) -> list[dict[str, Any]]:
    params = {"query": query}
    if at is not None:
        params["time"] = str(at)
    url = "http://localhost:9090/api/v1/query?" + urllib.parse.urlencode(params)
    raw = kubectl(
        "-n",
        "monitoring",
        "exec",
        "deploy/kube-prometheus-stack-prometheus",
        "--",
        "wget",
        "-qO-",
        url,
    )
    result: list[dict[str, Any]] = json.loads(raw)["data"]["result"]
    return result


def psql(database: str, sql: str) -> str:
    return kubectl(
        "-n",
        "tally-deps",
        "exec",
        "deploy/postgres",
        "--",
        "psql",
        "-U",
        "tally",
        "-d",
        database,
        "-tAc",
        sql,
    ).strip()


def server_side(window: str, at: float) -> dict[str, Any]:
    def quantile(q: float, metric: str, by: str) -> dict[str, float]:
        rows = prom(
            f"histogram_quantile({q}, sum by (le, {by}) (rate({metric}_bucket[{window}])))", at
        )
        return {
            r["metric"].get(by, "all"): round(float(r["value"][1]) * 1000, 1)
            for r in rows
            if r["value"][1] != "NaN"
        }

    return {
        "http_p50_ms": quantile(0.5, "tally_http_request_duration_seconds", "service"),
        "http_p99_ms": quantile(0.99, "tally_http_request_duration_seconds", "service"),
        "risk_decision_p99_ms": quantile(0.99, "tally_risk_decision_duration_seconds", "decision"),
        "ledger_post_p99_ms": quantile(0.99, "tally_ledger_post_duration_seconds", "operation"),
        "core_5xx_per_s": sum(
            float(r["value"][1])
            for r in prom(
                f'sum(rate(tally_http_requests_total{{service="core",status=~"5.."}}[{window}]))',
                at,
            )
        ),
    }


def correctness(since: str) -> dict[str, Any]:
    statuses = psql(
        "tally",
        "SELECT coalesce(json_object_agg(status, n), '{}') FROM (SELECT status, count(*) n "
        "FROM payment_intents WHERE merchant_id LIKE 'load-merchant-%' "
        f"AND created_at >= '{since}' GROUP BY status) s",
    )
    stuck = psql(
        "tally",
        "SELECT count(*) FROM payment_intents WHERE merchant_id LIKE 'load-merchant-%' "
        f"AND created_at >= '{since}' AND status NOT IN "
        # risk_review waits for an analyst by design; `created` means the client never
        # confirmed (it timed out on the create response). Everything else must have settled.
        "('succeeded','failed','reversed','cancelled','expired','risk_review','created')",
    )
    integrity = psql(
        "tally_ledger",
        "SELECT json_object_agg(check_name, ok) FROM ledger_verify_integrity()",
    )
    return {
        "status_mix": json.loads(statuses),
        "non_terminal_after_settle": int(stuck),
        "ledger_integrity": json.loads(integrity),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["ramp", "steady", "spike"], default="steady")
    parser.add_argument("--rate", type=int, default=50, help="payments per second (peak)")
    parser.add_argument("--duration", default="5m", help="steady duration")
    parser.add_argument("--merchants", type=int, default=20)
    parser.add_argument("--payers", type=int, default=20000)
    parser.add_argument("--prepare", action="store_true", help="apply the load-test overlay")
    parser.add_argument("--skip-seed", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.prepare:
        prepare()
    if not args.skip_seed:
        seed(args.merchants, args.payers)
    since = psql("tally", "SELECT clock_timestamp()")
    started = time.time()
    name = k6_job(args.scenario, args.rate, args.duration, args.payers)
    print(f"running {name}", flush=True)
    samples: dict[str, list[int]] = {}
    wait_job(name, timeout=3600, samples=samples)
    ended = time.time()
    logs = kubectl("-n", NS, "logs", f"job/{name}")
    summary_line = next(line for line in logs.splitlines() if line.startswith("K6_SUMMARY "))
    k6 = json.loads(summary_line.removeprefix("K6_SUMMARY "))
    window = f"{max(60, int(ended - started) - 30)}s"
    server = server_side(window, ended - 15)
    print("load finished; waiting 120 s for recovery workers to settle", flush=True)
    time.sleep(120)
    metrics = k6["metrics"]

    def trend(key: str) -> dict[str, float]:
        values = metrics.get(key, {}).get("values", {})
        return {k: round(v, 1) for k, v in values.items()}

    result = {
        "scenario": args.scenario,
        "rate": args.rate,
        "duration": args.duration,
        "seconds": round(ended - started),
        "iterations": metrics["iterations"]["values"],
        "dropped_iterations": metrics.get("dropped_iterations", {}).get("values", {}),
        "http_req_failed": metrics["http_req_failed"]["values"],
        "payment_e2e_ms": trend("payment_e2e_ms"),
        "create_ms": trend("create_ms"),
        "confirm_ms": trend("confirm_ms"),
        "thresholds_ok": all(
            all(t.get("ok", True) for t in m.get("thresholds", {}).values())
            for m in metrics.values()
        ),
        "server": server,
        # Busy samples only: the first and last 30 s are ramp-up/down.
        "cpu_millicores": {
            w: {"avg": sum(v[2:-2] or v) // len(v[2:-2] or v), "max": max(v)}
            for w, v in sorted(samples.items())
        },
        "correctness": correctness(since),
    }
    path = OUT / f"{args.scenario}-{args.rate}.json"
    path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
