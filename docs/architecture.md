# Architecture

Tally is a set of small services around one rule: **money exists only in the ledger, and the
ledger only changes through balanced, idempotent, append-only entries.** Everything else
(payment state, risk decisions, reconciliation) is about deciding which entries to post, and
proving afterwards that the right ones were.

## System

```mermaid
flowchart TB
  subgraph Clients
    MS[Merchant server]
    BR[Browser: hosted checkout]
    ST[Staff browser: console]
    PH[Payer phone simulator]
  end

  subgraph Edge
    WEB[Console - Next.js]
  end

  subgraph Services
    CORE[Core API<br/>gateway auth, payments, UPI switch,<br/>refunds, settlement, disputes, webhooks]
    WORK[Core worker<br/>recovery, sweeper, settlement,<br/>outbox relay, webhook delivery]
    LEDGER[Ledger<br/>double entry, holds, hash chain,<br/>integrity verifier]
    VAULT[Vault<br/>tokenisation, envelope encryption]
    RISK[Risk engine<br/>features, rules, ONNX model, reviews]
    RECON[Reconciliation<br/>three-way matching, breaks]
    BFF[Back office API<br/>auth, RBAC, maker-checker, audit]
    JOBS[Scheduled jobs<br/>AML, audit anchoring]
  end

  subgraph Simulators[Synthetic counterparties]
    BANK[Banks]
    PSP[Payer PSP]
    NET[Card network]
  end

  subgraph Data
    GDB[(General PostgreSQL<br/>payments, risk, recon, back office)]
    LDB[(Ledger PostgreSQL)]
    VDB[(Vault PostgreSQL)]
    REDIS[(Redis<br/>rate limits, nonces, risk features)]
    KAFKA[(Kafka<br/>payment events)]
    S3[(Object storage<br/>payout files, evidence,<br/>bank statements, audit anchors)]
  end

  MS -- HMAC-signed /v1 --> CORE
  BR -- PAN, publishable key --> VAULT
  BR --> WEB
  ST --> WEB
  WEB -- same-origin /auth /bff --> BFF
  PH --> RISK
  CORE --> LEDGER
  CORE --> RISK
  CORE --> PSP
  CORE --> BANK
  CORE --> NET
  NET -- detokenise --> VAULT
  WORK --> LEDGER
  WORK --> BANK
  WORK --> KAFKA
  BFF --> CORE
  BFF --> LEDGER
  BFF --> RISK
  BFF --> RECON
  RECON --> LEDGER
  RECON --> BANK
  CORE --> GDB
  WORK --> GDB
  RISK --> GDB
  RECON --> GDB
  BFF --> GDB
  JOBS --> GDB
  LEDGER --> LDB
  VAULT --> VDB
  CORE --> REDIS
  RISK --> REDIS
  WORK --> S3
  RECON --> S3
  JOBS --> S3
```

| Service | Owns | Never does |
| --- | --- | --- |
| Core API and worker | payment intents, state machine, transition log, outbox, ledger *commands*, refunds, settlements, payouts, disputes, webhooks | edit balances; it asks the ledger to post by deterministic key |
| Ledger | accounts, journal entries, postings, holds, balances, hash chain | accept an unbalanced, duplicate-with-different-payload, or retroactive change |
| Vault | encrypted PANs (envelope encryption) and tokens | return a PAN to anyone but the card-network simulator |
| Risk | features, rule versions, model registry, decisions, review cases | move money; it only allows, reviews, steps up or blocks |
| Reconciliation | bank statements, matches, breaks, proposed adjustments | post without maker-checker approval |
| Back office API | staff identities, sessions, roles, approvals, audit log | let a user approve their own request |

## A UPI payment

```mermaid
sequenceDiagram
  autonumber
  participant M as Merchant
  participant C as Core
  participant R as Risk
  participant P as Payer PSP
  participant B as Banks
  participant L as Ledger
  M->>C: POST /v1/payment_intents (HMAC, Idempotency-Key)
  C->>C: verify signature, consume nonce, reserve idempotency key
  C-->>M: 201 created
  M->>C: POST /v1/payment_intents/{id}/confirm
  C->>R: decide (features, rules, model)
  R-->>C: allow / review / step-up / block
  Note over C: one transaction: state authorizing, transition row,<br/>outbox event, ledger command
  C->>P: authorise debit (key {id}:upi:psp:authorize)
  C->>B: transfer (key {id}:upi:bank:transfer)
  alt bank approves
    C->>L: post entry (key {id}:upi:transfer:post)
    L-->>C: entry (or the same entry again on retry)
    C-->>M: 200 succeeded
  else answer lost or unknown
    C-->>M: 200 pending_unknown
    Note over C,B: worker asks the bank, ledger-first:<br/>if the key already posted, it never reverses
  end
```

Card payments follow the same pattern with a ledger **hold** placed at authorisation and posted
or voided at capture or cancel. Crash windows and their recovery are listed in
[guarantees](guarantees.md#crash-windows-orchestrator-process-killed-between-durable-effects).

## Trust boundaries

1. **Internet → merchant API:** HMAC over method, path, body hash, timestamp and nonce; scopes;
   per-merchant rate limits; idempotency; admission control under overload.
2. **Browser → vault:** publishable key, CORS, published test cards only; the merchant server and
   core only ever see tokens.
3. **Staff → back office:** password + TOTP, short-lived ES256 JWTs with rotating refresh tokens,
   CSRF, deny-by-default RBAC, maker-checker for money-moving actions, hash-chained audit log.
4. **Service → service:** internal keys per caller; in Kubernetes, default-deny NetworkPolicies
   allow only the declared paths.
5. **Application → data:** separate databases for the ledger and the vault; the ledger's
   application role can only call its stored functions; merchant users are confined by
   PostgreSQL row-level security.

## Deployment

Locally everything runs from `make demo` (Compose dependencies plus the services in one process)
or on a local Kubernetes cluster (`make k8s-up`). For AWS the repository contains, validated but
not applied: one image per runtime, a Helm chart with Argo Rollouts canaries gated on ledger
integrity, Argo CD applications, Kyverno admission policies, and Terraform for EKS, three RDS
instances, ElastiCache, MSK, S3 with Object Lock, KMS and WAF in Mumbai with Hyderabad DR copies.
See [deployment](deployment.md) and [ADR-019](adr/ADR-019-deployment.md).

## Observability

Every service exports Prometheus RED metrics and domain metrics (payments by state, oldest unknown
outcome, ledger integrity, recon match rate, risk latency and drift), OpenTelemetry traces and
redacted JSON logs. Alert rules, dashboards and runbooks are in [observability](observability.md).
