# Runbook: failed settlement or returned payout (`SettlementFailed`)

1. Returned payout: funds are back in merchant payable and are settled again automatically as a
   `payout_return` item in the next cycle. Confirm the merchant's bank details out of band.
2. Settlement run error: re-run `POST /internal/v1/settlements/run` for the date; it is
   idempotent and replays any command left pending by a crash.
3. Verify: merchant payable equals not-yet-eligible sales after the run (settlement identity).
