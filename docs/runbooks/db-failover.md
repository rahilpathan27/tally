# Runbook: database failover

1. RDS Multi-AZ fails over automatically (typically 60-120 s). Services reconnect via pool retry;
   in-flight requests may fail with 5xx and merchants retry with the same idempotency key.
2. Crash windows are safe by design: stalled payments are resumed by the sweeper after 30 s and
   ledger commands replay with deterministic keys.
3. After failover: run the ledger integrity report, check `PendingUnknownStuck` and recon for the day.
4. For region loss follow the DR procedure (restore PITR backups copied to ap-south-2; see
   `docs/dr.md` once published in Phase 15).
