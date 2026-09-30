# Runbook: security events (`RefreshTokenReuse`, `LoginFailureSpike`)

1. Refresh-token reuse: the family is already revoked. Identify the user from the audit log
   (`refresh_token_reuse`), force a password reset and MFA re-enrolment, and review their recent
   approvals and adjustments.
2. Login failure spike: check source IPs at the WAF; lockout limits per-account guessing.
3. Verify the audit chain (`/bff/v1/ops/audit` shows chain status) and the latest anchor.
