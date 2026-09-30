# ADR-015: Synchronous risk decisioning with rules, a calibrated model and fail policies

## Context

Every payment needs an allow/step-up/review/block decision before any external call, within a
100 ms hard timeout, explainable to analysts and merchants, and safe when the risk service is down.

## Decision

- **Order of evaluation:** shared online features (Redis) → versioned rules → champion model
  (ONNX Runtime score, isotonic calibration, TreeSHAP reason codes) → shadow challenger (logged
  only). Blocklist rules win; an allowlist rule overrides the model; otherwise the most severe of
  the rule actions and the model decision applies. Model review becomes step-up for cards
  (possession check) and analyst review for UPI.
- **State:** every attempt, including blocked ones, updates velocity features after the decision.
- **Idempotency:** one decision per payment ID; repeats return the stored decision.
- **Payment flow:** confirm is accepted only from `created`. Block → `failed` before any network
  call; review/step-up → `risk_review`. Only an analyst resolution (internal endpoint) or a
  verified step-up can move `risk_review` to `authorizing`.
- **Failure policy:** the core calls risk with a 100 ms timeout. Per-merchant
  `merchant_risk_policies.fail_mode` chooses fail-open (allow, reason `RISK_UNAVAILABLE`) or
  fail-closed (fail the payment, retryable by creating a new intent). Default is open.
- **Rules and models change under dual control**, validated before a proposal is accepted.

## Consequences

- Rules are hot-reloaded within five seconds of activation.
- A fail-open merchant accepts unscreened payments during a risk outage; this is logged on the
  payment's `risk_outcome` for later review. Fail-closed merchants lose sales during an outage.
- Step-up is simulated: the OTP is an HMAC of the challenge ID revealed to the payer simulator.
