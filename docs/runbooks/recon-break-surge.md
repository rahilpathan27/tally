# Runbook: reconciliation break surge (`ReconBreakSurge`)

1. Reconciliation dashboard and workbench: which break types and which source/date?
2. Many `missing_at_bank` or `timing_difference` near the cut-off: check whether the next
   statement has arrived; re-run the date (idempotent) after ingesting it.
3. Many `amount_mismatch`/`fee_tax_mismatch` on one source: the bank changed its file format or
   charges. Check parse issues on the file (`recon_files.issues`) before touching the ledger.
4. `missing_internally` with real bank credits: an internal outage may have lost records; check
   the ledger for suspense postings and the switch log.
5. Resolve through maker-checker adjustments only; SLA timers are on each break.
