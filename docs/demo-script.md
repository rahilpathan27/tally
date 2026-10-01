# Demo script (about 15 minutes)

A walkthrough for showing Tally to someone. Everything is synthetic; say so at the start.

## Before you start (5 minutes ahead)

```bash
make demo
```

Wait for the tour to finish (about a minute) and keep that terminal visible: its output is the
first part of the demo. Open <http://localhost:3000> in a browser. Passwords for the demo users
are in `.data/dev-stack.json`.

| User | Role |
| --- | --- |
| `admin@demo.test` | merchant admin |
| `ops@tally.test` | operations analyst |
| `risk@tally.test` | risk analyst |
| `approver@tally.test` | second person for maker-checker |
| `operator@tally.test` | can inject simulator failures |

## 1. The one rule (1 minute)

"Money only exists in the ledger. The ledger only changes through balanced, append-only,
idempotent entries, chained with SHA-256. Everything else decides which entries to post, and
proves afterwards that the right ones were." Show the system diagram in
[architecture](architecture.md).

## 2. The terminal tour (3 minutes)

Walk through the `make demo` output:

- **Step 1:** a merchant's signed UPI payment, and the two ledger lines it produced (bank asset
  debited, merchant payable credited, same amount).
- **Step 2:** the same request sent twice returns the same payment; the same key with a different
  amount is refused. Retries are safe.
- **Step 3:** the card goes from the browser straight to the vault; the merchant and core only
  ever see `vlt_…` tokens.
- **Step 6:** the interesting one. The bank failed mid-payment, so nobody knew whether money moved
  (`pending_unknown`). Nothing guessed: the recovery worker asked the bank once it was back and
  settled the payment to match, ledger first.
- **Step 7:** integrity verifier and a trial balance where debits equal credits to the paisa.

## 3. Merchant console (3 minutes)

Log in as `admin@demo.test`.

- **Overview:** volumes and success rate.
- **Payments:** open one; show the timeline (every state change, including rejected attempts) and
  the linked ledger postings.
- **Refunds:** refund part of a payment; try to refund more than is left (refused). Refunds above
  the merchant's threshold go to an approver.
- **API keys:** create one; the secret is shown exactly once.

## 4. Operations console (5 minutes)

Log in as `ops@tally.test` (or `risk@`, `operator@` as needed).

- **Switch monitor:** live success rate per bank (server-sent events).
- **Chaos** (`operator@`): set `bank-a` to `http_500`, make a payment from the checkout, watch the
  breaker and the success rate react, then restore it.
- **Risk** (`risk@`): pay ₹45,000 by UPI from `ravi@bank-c` (not `asha@bank-a`, which the demo
  rules trust) to create a case. The review queue shows reason codes from the model's SHAP values
  and the rule that fired; approve or decline it.
- **Reconciliation:** fetch today's bank statement and run reconciliation; a statement is matched three ways (ledger, switch log, bank file); open a
  break, propose an adjustment, then approve it as `approver@` (the proposer cannot approve
  their own).
- **Ledger explorer:** integrity checks, trial balance, a statement for one account.
- **Audit log:** every staff action, hash-chained; the chain status is shown.

## 5. Evidence (2 minutes)

Pick the reports that match the audience:

- [chaos report](chaos-report.md): 100,000 seeded failure scenarios, plus real pods killed on a
  Kubernetes cluster under load, with a money audit after each;
- [canary drill](k8s-drill-report.md): a broken release aborted automatically in 71 s;
- [load test](load-test-report.md): ~120 payments/s on a laptop, and exactly why not more;
- [backup and restore](backup-restore-report.md): RPO 46 s, restored entries hash-identical;
- [recon report](recon-report.md): 100% of 17,280 planted breaks found.

## Questions people ask

- *Is this production-ready?* No. It is a simulation built with production practices; nothing is
  certified and it has never handled real money. See [PROGRESS](PROGRESS.md#known-gaps).
- *Why is throughput only ~120/s?* One hash chain over every entry gives a total order and
  tamper evidence, at the cost of serialising writes. Partitioned chains with periodic anchors
  would lift it; that is documented, not built.
- *What happens if the bank never answers?* Each bank and amount tier has a deadline and a
  policy (`auto_reverse` or `deemed_success`). A late answer after the deadline is corrected with
  a new entry to suspense and flagged for reconciliation; history is never edited.

## Afterwards

```bash
make demo-stop
make down
```
