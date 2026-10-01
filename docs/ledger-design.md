# Ledger design

## Money

Amounts are integer minor units and carry an explicit currency. The exponent map currently supports INR/USD/EUR/GBP/CHF/SGD/AUD/CAD (2), JPY (0), and KWD/BHD (3). This is a limited development subset, not a complete ISO 4217 registry. JSON integer amounts are bounded by JavaScript's safe-integer maximum. `round_to_minor` is the sole Decimal-to-minor conversion function and uses half-even rounding. Allocation uses integer largest remainders; ties resolve by input order.

## Posting rules

`services/ledger/model.py` is the thread-safe Python reference model. The PostgreSQL schema is in `services/ledger/migrations/0001_initial.sql`. A journal entry has at least two positive postings, uses one currency, and balances debit and credit totals exactly. FX must use separate same-currency entries connected by explicit clearing postings. Replays with the same idempotency key and payload return the original result; changed payloads are rejected.

Liability, equity, and income accounts report credit-normal balances; asset and expense accounts report debit-normal balances. PostgreSQL locks participating accounts in lexical ID order and commits journal rows and cached balances in one transaction. Each posting serializes on a singleton hash-chain head row. This trades write parallelism for a global chain; the Phase 15 load test measured it as the throughput ceiling (about 140 postings/s on the test laptop; see the load-test report).

## Holds

Balanced pending holds reserve negative natural-balance effects without changing posted balances. PostgreSQL supports placing, posting, and voiding holds atomically. Posting creates an immutable journal entry; voiding releases the reservation.

## Integrity and limitations

PostgreSQL's `ledger_verify_integrity()` checks journal balancing, cached balances against posting history, and the SHA-256 chain. The migration and integration assertions passed against a clean PostgreSQL 16 database, including duplicate replay, changed-payload rejection, overdraw/closed/currency rejection, holds, hash verification, and app-role direct-write denial. Guarantees assume application traffic uses `tally_ledger_app` and stored functions; owners and superusers can bypass them. The chain is not externally anchored. Snapshots, balance-as-of reads and an integrity report exist (Phase 7 review); no scheduler writes daily checkpoints. Mutation testing gates the money and ledger model at 70% (76% measured). This is not a production service.
