# Runbook: certificate expiry (`CertificateExpiringSoon`)

Certificates are issued by cert-manager (Phase 14). Check the `Certificate` resource status and
ACME challenge events; renewals start 30 days before expiry, so this alert means renewal failed.
