# Runbook: suspected fraud wave or model drift (`FraudWave`, `ModelDrift`)

1. Risk console: decision mix and top reason codes. A single pattern (e.g. `DEVICE_MANY_CARDS`)
   suggests a card-testing attack; add blocklist entries through a rule proposal (maker-checker).
2. Drift without more fraud may be a legitimate shift (sale, new merchant); check PSI per feature
   and compare the challenger in shadow.
3. Review queue growth: add analysts or temporarily raise step-up for cards instead of review.
4. Retrain with new labels (`make retrain`); promotion still needs the evaluation gate and an approver.
