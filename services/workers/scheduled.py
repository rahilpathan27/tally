"""Scheduled jobs (Kubernetes CronJobs): ``python -m services.workers.scheduled <job>``.

* ``aml``: run the AML-lite detectors (idempotent; alerts are de-duplicated by key);
* ``audit-anchor``: anchor the audit-log head to object storage (Object Lock in AWS);
* ``verify-anchors``: re-check every anchor; exits non-zero if the chain or an anchor changed.

Exit status is what the CronJob reports, so failures surface as failed Jobs and alerts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

import asyncpg
from libs.common.object_store import s3_store_from_env
from libs.observability.logging import configure_logging

from services.workers.aml import run_detectors
from services.workers.audit_anchor import anchor, verify_anchors

JOBS = ("aml", "audit-anchor", "verify-anchors")
log = logging.getLogger("tally.scheduled")


async def run(job: str) -> int:
    pool = await asyncpg.create_pool(os.environ["TALLY_DATABASE_URL"], min_size=1, max_size=2)
    try:
        if job == "aml":
            log.info("aml detectors finished", extra={"raised": await run_detectors(pool)})
            return 0
        store = s3_store_from_env()
        if store is None:
            log.error("object storage is not configured (TALLY_S3_BUCKET)")
            return 2
        if job == "audit-anchor":
            result = await anchor(pool, store)
            log.info("audit anchor", extra={"anchor": json.dumps(result, default=str)})
            return 0
        problems = await verify_anchors(pool, store)
        for problem in problems:
            log.error("audit anchor verification failed", extra={"problem": problem})
        return 1 if problems else 0
    finally:
        await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=JOBS)
    args = parser.parse_args()
    configure_logging(f"job-{args.job}")
    return asyncio.run(run(args.job))


if __name__ == "__main__":
    sys.exit(main())
