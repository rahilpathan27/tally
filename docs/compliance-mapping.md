# Compliance design mapping

**This is a design mapping for a synthetic simulation, not a certification, audit or legal
advice.** It shows where Tally's design addresses themes from the documents below. Regulations
change; verify every row against the current official text before relying on it. Where this
project deviates or only simulates a control, the row says so.

## Sources

- PCI Security Standards Council, *PCI DSS v4.0.1* (June 2024) —
  [announcement](https://blog.pcisecuritystandards.org/just-published-pci-dss-v4-0-1).
- RBI circular DPSS.CO.OD No.2785/06.08.005/2017-18, *Storage of Payment System Data*
  (6 April 2018) and FAQs — [RBI FAQ](https://www.rbi.org.in/commonman/english/scripts/FAQs.aspx?Id=2995).
- RBI Master Direction on *Digital Payment Security Controls*, DoS.CO.CSITE.SEC.No.1852/31.01.015/2020-21
  (18 February 2021) — [PDF](https://rbidocs.rbi.org.in/rdocs/notification/PDFs/MD7493544C24B5FC47D0AB12798C61CDB56F.PDF).
- RBI circular CO.DPSS.POLC.No.S-516/02-14-003/2021-22, *Tokenisation – Card Transactions:
  Permitting Card-on-File Tokenisation (CoFT) Services* (7 September 2021) —
  [RBI](https://rbi.org.in/Scripts/NotificationUser.aspx?Id=12159&Mode=0).
- RBI Master Direction on *Regulation of Payment Aggregators* (2025), including escrow
  requirements — secondary summary: [MediaNama](https://www.medianama.com/2025/09/223-explained-rbi-master-direction-payment-aggregators/).
  Read the RBI text itself before use; this project did not verify the primary document.
- *Digital Personal Data Protection Act, 2023* and *DPDP Rules, 2025* (notified 14 November
  2025, phased commencement) — [PIB release](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2190655&reg=48&lang=2).

## PCI DSS v4.0.1 (requirement areas)

| Area | Tally design | Evidence |
| --- | --- | --- |
| 1 Network security controls | Vault DB on an internal-only network; Kubernetes default-deny policies planned (Phase 14) | `docker-compose.yml`, `tests/integration/test_vault_network.py` |
| 3 Protect stored account data | PAN only in vault, envelope-encrypted; tokens elsewhere; CVV/SAD never stored (3.3.1) | `services/vault/`, `tests/integration/test_vault_api.py` |
| 4 Encrypt transmission | TLS at the edge planned in Terraform; local loopback is plaintext (deviation) | Phase 14 |
| 6 Secure software | Strict typing, SAST/dependency scans in CI (Phase 14), input validation, SSRF defence | `libs/security/`, CI |
| 7 Restrict access by need to know | Deny-by-default RBAC; RLS; column grants hide secrets | `rbac.py`, `_tenant_isolation` |
| 8 Identify and authenticate | Unique users, scrypt, MFA (TOTP), lockout, session expiry | `auth.py`, `_auth_flows` |
| 10 Log and monitor | Redacting structured logs; hash-chained audit log with anchors | `libs/observability/logging.py`, `audit_anchor.py` |
| 11 Test security | Security suites, chaos harness, authz matrix | `tests/security/` |
| 12 Policies | Out of scope for a simulation | — |

Scope reduction: only the vault and the network simulator's detokenise call touch PANs; every
other service handles tokens, last four and BIN.

## RBI-style expectations

| Theme | Tally design | Evidence / status |
| --- | --- | --- |
| Storage of payment system data in India | Terraform restricts resources to `ap-south-1` with backups in `ap-south-2`; no cross-border replication | Phase 14 (planned); simulation runs locally |
| Card-on-file tokenisation (merchants do not store card numbers) | Merchants receive opaque tokens; vault is the only PAN store. *Deviation:* the RBI CoFT framework makes card networks/issuers the token service providers; here the platform vault plays that role | `services/vault/` |
| Digital payment security controls: masking, authentication, fraud risk management, reconciliation | Masking in logs/UI, MFA for staff, risk engine with review queue, three-way reconciliation | Phases 10, 9, 11 |
| Merchant fund segregation (escrow-style) | Per-merchant liability accounts separate from platform income and suspense; settlement nets only merchant-scoped items | `services/core/settlement.py`, ADR-012 |
| Dispute handling timelines | Chargebacks have `respond_by` (7 days, illustrative) and lifecycle states | `services/core/disputes.py` |
| Audit trails | Immutable transition logs, ledger hash chain, audit log | multiple |

## DPDP-style privacy principles

| Principle | Tally design | Status |
| --- | --- | --- |
| Purpose limitation / minimisation | Risk context limited to device ID, IP, country, account age; no names in payment records | Implemented |
| Security safeguards | Encryption of secrets and PANs, access control, audit | Implemented (local keys, not KMS) |
| Retention and erasure | Financial records must be kept; the planned approach pseudonymises VPAs/device IDs on erasure while keeping amounts and references | **Not implemented** — design only |
| Consent records | Not modelled (synthetic users) | Gap |
| Breach notification | Runbooks planned in Phase 13 | Gap |
