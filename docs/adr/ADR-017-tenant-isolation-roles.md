# ADR-017: Row-level security roles for tenant isolation

## Context

Earlier phases enabled RLS policies, but every service connected as the database owner, which
bypasses RLS. Isolation therefore depended entirely on each query's `merchant_id` filter.

## Options

1. Split every service into request and worker pools with different roles now.
2. Enforce RLS for the merchant-facing back office first, keep workers on a privileged role, and
   document the remaining gap.

## Decision

Option 2. `tally_app` (NOLOGIN, NOBYPASSRLS) gets `SELECT` on tenant tables and column-level
grants that exclude ciphertext; `tally_ops` (BYPASSRLS, read-only) serves platform staff. The BFF
switches role per transaction (`SET LOCAL ROLE`) and sets `app.merchant_id` from the verified
session, never from input. Tests attack isolation directly in SQL (other tenant, no tenant set,
secret columns, writes).

## Consequences

- Merchant dashboard reads are isolated even if a query forgets a filter.
- The core merchant API and background workers still run as the owner; isolation there relies on
  explicit filters and the gateway principal. Splitting their pools is tracked in PROGRESS.
