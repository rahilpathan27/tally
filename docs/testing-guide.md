# Testing guide

How to check that Tally works, from a five-minute click-through to every failure mode. Everything
runs locally on synthetic data.

## 1. Start it

```bash
make demo
```

This starts the dependencies, the services with seeded data and the console, and runs a short
guided tour in the terminal. Open <http://localhost:3000>. All demo users share the password
`correct horse battery staple` (also in `.data/dev-stack.json`).

| User | Use it for |
| --- | --- |
| `admin@demo.test` | merchant: payments, refunds, settlements, disputes, API keys, webhooks |
| `viewer@demo.test` | merchant, read-only (should be refused any change) |
| `ops@tally.test` | operations: switch monitor, ledger, reconciliation, audit log |
| `risk@tally.test` | risk review queue |
| `approver@tally.test` | second person for maker-checker approvals |
| `operator@tally.test` | chaos control: make simulators fail |

Restarting `make demo` recreates the databases, so every start is a clean slate.
`make demo-stop` stops it.

## 2. Things that surprise testers

These are deliberate behaviours, not bugs:

- **`asha@bank-a` is a trusted customer.** The demo rules allow-list it, so its payments are
  never held for review or asked for a one-time code (block rules would still apply). Use
  `ravi@bank-c` or `meera@bank-a` to see risk holds. The checkout marks it as trusted.
- **Paying many times quickly from one browser looks like fraud.** After several payments the
  model starts holding small payments too (`RAPID_REPEAT`, `VELOCITY_DEVICE`). That is the risk
  engine working; approve or decline them as `risk@tally.test`, or restart `make demo`.
- **UPI of ₹40,000 or more** from a non-trusted payer goes to an analyst (rule D001).
- **Cards of ₹5,000 or more** need a one-time code (rule D002). The code arrives on the simulated
  payer phone: follow "Open payer phone" on the checkout and press "Check messages".
- **Dashboard refunds of ₹5,000 or more** wait for an approver; smaller ones go straight through.
- **Unknown bank outcomes take about 30 seconds to resolve.** That is the per-bank status-check
  deadline in the demo data.
- **Chaos settings are global and stay until changed.** The chaos page shows what is currently
  degraded; set everything back to `approve` when done.
- **Only published test cards are accepted:** `4242 4242 4242 4242`, `5555 5555 5555 4444`,
  `3782 822463 10005`. No CVV is ever collected.

## 3. Run every edge case automatically

With `make demo` running:

```bash
uv run python -m scripts.edge_cases
```

About 6 minutes. Each scenario sets the simulators the way an operator would, pays through the
signed merchant API, waits for the final state and checks both the status and the exact ledger
effect. Run single groups with e.g. `uv run python -m scripts.edge_cases upi card`.

| Group | Scenario | Expected |
| --- | --- | --- |
| upi | bank approves | `succeeded`, one ledger transfer |
| upi | bank declines | `failed`, no ledger entry |
| upi | bank outage (HTTP 500) | `pending_unknown`, then `reversed` after the deadline; nothing moved |
| upi | approval response lost | status check finds the approval: `succeeded`, one transfer |
| upi | debit taken, credit failed | bank reverses the debit: `failed`, nothing moved |
| upi | … and the reversal response is lost | `pending_unknown`, recovery confirms the reversal: `failed` |
| upi | bank status stays unknown | `reversed` at the deadline |
| upi | bank moved money but its status API never shows it | `reversed` internally; **reconciliation flags it** as a `status_mismatch` break (see §5) |
| card | authorise and capture | `succeeded`, hold placed then posted |
| card | authorise and cancel | `cancelled`, hold voided |
| card | network declines / outage | `failed` / `reversed`, no hold |
| card | approval response lost | status check authorises; capture `succeeded` |
| psp | payer PSP declines / outage | `failed`, nothing moved |
| api | idempotent replay | same key → same payment; same key, different body → 422 |
| api | confirm twice | money moves once; second confirm → 409 |
| api | authentication | wrong secret, tampered body, stale timestamp → 401; replayed nonce → 409 |
| api | invalid input | 0, negative, float, string amount, huge amount, USD, unknown VPA → 422 |
| api | vault | a real card number or a CVV field → 422 |
| api | rate limit | over 600 requests/minute (dev limit) → 429 |
| refunds | partial, full, over-refund, refund of a failed payment | 201, 201, 422, 409 |
| risk | ₹45,000 from `ravi@bank-c` vs the trusted payer | `risk_review` vs `succeeded` |
| risk | card ₹6,000 | one-time code required; wrong code → 422 |
| approvals | ₹6,000 dashboard refund | `pending_approval`, then executed by the approver |
| approvals | approving your own request; viewer refund | 403, 403 |

It finishes with the ledger integrity verifier, which must be all `True`.

## 4. Click through it yourself

### Payments (checkout: <http://localhost:3000/checkout>)

1. UPI ₹499 from `asha@bank-a` → **succeeded**.
2. UPI ₹45,000 from `ravi@bank-c` → **risk review**. Sign in as `risk@tally.test`, Risk → open the
   case (reasons such as `LARGE_UPI_TRANSFER`) → Approve. The payment becomes **succeeded**.
3. Card ₹6,000 with `4242 4242 4242 4242`, expiry `12/30` → **verification needed**. Enter
   `000000` (refused), then the real code from "Open payer phone" → **succeeded**.
4. Card with `4111 1111 1111 1111` → refused: not an allowed test card.

### Merchant (`admin@demo.test`)

5. Payments → open the card payment: the timeline lists every state change, and the ledger
   section shows the hold and its balanced postings.
6. Refund ₹1,000 → done; "Refundable" drops by ₹1,000. Try refunding more than is left → refused.
7. Refund ₹5,000 or more → "pending approval". Sign in as `approver@tally.test` → Approvals →
   approve. The proposer cannot approve their own request.
8. Developers → create an API key: the secret is shown once only.
9. Sign in as `viewer@demo.test`: no refund or key actions are offered, and the API refuses them.

### Failures (`operator@tally.test`, Chaos control)

Set one mode, press Apply, make a UPI payment from `asha@bank-a` (its bank is `bank-a`), then set
the bank back to `approve`. Watch Switch monitor while it happens.

| Mode on bank-a | Checkout shows | After restoring the bank |
| --- | --- | --- |
| decline | declined, you have not been charged | — |
| http 500 | bank has not confirmed yet; do not pay again | after ~30 s: reversed |
| late success | not confirmed yet | succeeded (the bank had approved) |
| credit failure | declined (the bank reversed the debit) | — |
| status unknown | not confirmed yet | reversed at the deadline |
| timeout | not confirmed yet | reversed, but the bank moved money: find it in reconciliation (§5) |

The card network and payer PSP have their own modes on the same page (use a card payment for the
network).

### Books (`ops@tally.test`)

10. Ledger → all three integrity checks **ok**; trial balance debits equal credits.
11. Audit log → logins, refunds, risk decisions, approvals and chaos changes, with
    "hash chain verified".

## 5. Reconciliation

After a `timeout` scenario (or any time), as `ops@tally.test` → Reconciliation:

1. Fetch today's statement from the bank simulator, then run reconciliation for today.
2. The payment the bank moved but Tally reversed appears as a **status mismatch** break with a
   suggested action. Fetching the statement again does not create duplicate breaks.
3. Open a break and propose an adjustment; sign in as `approver@tally.test` to approve it.

## 6. The deeper suites

| Command | Proves | Time |
| --- | --- | --- |
| `make lint` and `make test` | style, strict types, money float-ban; unit and property tests | 1 min |
| `make stack-test-integration` | money movement, crash replay, reconciliation, risk and security suites on fresh databases | 1–2 min |
| `make e2e` | the console in a real browser, with accessibility scans (needs `make demo`) | 2 min |
| `make chaos` | ledger chaos model | 2 min |
| `make chaos-100k` | 100,000 end-to-end failure scenarios with a money checker | ~1 h |
| `make backup-drill` | ledger backup and point-in-time restore | 5 min |
| `make k8s-up`, `make k8s-drill`, `make cluster-chaos`, `make loadtest` | the Helm chart on a local Kubernetes cluster: canary rollback, pod kills under load, throughput | 15–30 min each |
| `make infra-check`, `make security-scan` | deployment code and supply-chain scans | 5–10 min |

Results of the long runs are in the reports linked from the [README](../README.md#documentation).

## 7. If something looks wrong

- A payment you expected to succeed is in risk review: check the payer (§2) and the reasons on
  the payment detail.
- Payments stay "not confirmed": a bank is probably still degraded; check the chaos page.
- Ports in use: `make demo-stop`, and stop any other process on 3000, 8000, 8002, 8030 or 8040.
- Logs: `.data/demo/services.log` and `.data/demo/console.log`.
