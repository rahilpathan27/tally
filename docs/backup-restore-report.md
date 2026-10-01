# Backup and restore drill

`make backup-drill` (`scripts/backup_drill.py`) on 2026-10-01, local Docker (PostgreSQL 16,
Apple Silicon laptop). Throwaway containers; the Compose stack is not touched.

## What it does

1. A primary ledger database with WAL archiving (`archive_timeout = 60 s`) receives continuous
   postings through `ledger_post_entry`; every acknowledged commit is recorded by the client.
2. A base backup (`pg_basebackup`, WAL streamed) is taken while writes continue.
3. A recovery point is marked, then a "faulty release" starts posting wrong (`bad-*`) entries.
4. **Disaster:** the primary container and its data volume are destroyed. WAL that had not yet
   been archived is lost with them, exactly as with a lost instance and disk.
5. Restore the base backup and replay the archive to the latest point.
6. Separately, restore to the marked point in time (before the faulty release).

## Results

| Measure | Result | Target |
| --- | --- | --- |
| Entries acknowledged before the disaster | 10,266 | |
| Base backup (database of this size) | 0.3 s | |
| Latest restore: time until writable | 1.4 s | RTO ≤ 1 h |
| Latest restore: acknowledged entries lost | 2,094 (the last 46.2 s of writes) | |
| **RPO measured** | **46.2 s** (bounded by `archive_timeout`, 60 s) | ≤ 5 min |
| Recovered entries whose hash differs from the primary's | 0 | 0 |
| Unacknowledged entries present after restore | 0 | 0 |
| Integrity verifier after restore (balanced entries, balances vs postings, hash chain) | all pass | all pass |
| Point-in-time restore to the mark: time until writable | 1.2 s | |
| PITR: faulty-release entries present | 0 | 0 |
| PITR: entries acknowledged before the mark missing | 0 | 0 |
| PITR integrity verifier | all pass | all pass |

## What it shows, and what it does not

- Losing the primary loses every commit since the last archived WAL segment. Here that was 46 s
  of writes, within the 5-minute RPO; RDS ships transaction logs every 5 minutes, so the RPO in
  AWS would be up to 5 minutes. A synchronous standby (RDS Multi-AZ) protects against an instance
  or AZ failure without that loss; backups protect against regional loss and bad writes.
- Restored data is exactly the primary's data: the hash chain over every recovered entry matches,
  and nothing unacknowledged appears.
- Point-in-time recovery removes a bad batch cleanly. For a ledger the normal fix for wrong
  entries is a correcting entry, not a restore; PITR is the tool for corruption or a destructive
  operator error, and its output should be loaded into a separate instance for comparison.
- **Not shown:** restore time at production size. 1.4 s is for a small database; RTO for a
  real database is dominated by base-backup size and WAL volume to replay, which this laptop
  drill does not measure. Payments lost from the ledger by a restore would also need replay from
  the core database's ledger commands (deterministic keys make that idempotent); the drill
  restores the ledger alone.
