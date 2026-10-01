# Deployment

Tally is packaged for AWS (EKS in Mumbai, DR copies in Hyderabad) with Helm, Argo CD, Argo
Rollouts, Kyverno and Terraform. **None of it has been applied to AWS**: the project has no AWS
account, credentials or domain. Everything below has been validated, scanned and exercised on a
local Kubernetes cluster; applying it needs the steps in [Applying to AWS](#applying-to-aws).
Decisions are in [ADR-019](adr/ADR-019-deployment.md).

## Layout

| Path | What |
| --- | --- |
| `deploy/docker/python.Dockerfile` | One image for every Python service, worker and job (non-root, read-only rootfs) |
| `deploy/docker/web.Dockerfile` | Console on distroless Node 22 (Next standalone output) |
| `infra/helm/tally` | Umbrella chart: Deployments/Rollouts, Services, HPA, PDB, NetworkPolicies, IRSA ServiceAccounts, ExternalSecrets, migration hook, CronJobs, AnalysisTemplates, ServiceMonitor, PrometheusRule, Ingress |
| `infra/argocd` | AppProject, Applications (dev/staging auto-sync, prod manual) and per-environment values |
| `infra/k8s/policies` | Kyverno policies (registries, digests, signatures + SBOM, hardening, no inline secrets) |
| `infra/k8s/local` | Throwaway Postgres/Redis/Prometheus for the local drill |
| `infra/terraform/modules` | network, eks, rds-postgres, elasticache, kafka, s3, kms, iam, ecr, waf, route53-acm, cloudfront, security-baseline, observability, platform |
| `infra/terraform/envs/{dev,staging,prod}` | Environment roots (S3 backend with native locking, region and account guardrails) |
| `infra/terraform/bootstrap`, `org` | State bucket; organisation SCP for India-only regions |
| `.github/workflows/release.yml` | ECR push, keyless cosign signature and SBOM attestation (inactive without AWS variables) |

## Workloads

| Workload | Kind | Replicas (prod) | Talks to | Data stores |
| --- | --- | --- | --- | --- |
| core (merchant API) | Rollout canary | HPA 3–12 | ledger, risk, simulators | PostgreSQL general, Valkey, MSK, S3 |
| core-worker (recovery, settlement, webhooks, outbox, metrics) | Deployment | 3 | same as core | same as core |
| ledger | Rollout canary | HPA 3–8 | — | PostgreSQL ledger |
| vault | Deployment | HPA 2–6 | — | PostgreSQL vault |
| risk | Deployment | HPA 2–10 | core (resolution callbacks) | PostgreSQL general, Valkey |
| recon | Deployment | 2 | ledger, bank simulator | PostgreSQL general, S3 |
| backoffice (BFF) | Deployment | HPA 2–6 | core, ledger, risk, recon, simulators | PostgreSQL general, Valkey, S3 |
| web (console) | Deployment | HPA 2–6 | core, risk, backoffice | — |
| bank, payer PSP, card network simulators | Deployment | 1 each | network sim → vault | — |
| AML detectors, audit anchoring, anchor verification | CronJob | — | — | PostgreSQL general, S3 |
| migrations | PreSync / pre-upgrade Job | — | — | all three databases |

Every arrow in the table is a NetworkPolicy rule generated from `calls`/`stores` in
`values.yaml`; everything else is denied, including egress. Public routes: `api.<domain>/v1`
(core), `console.<domain>` (web, with `/auth` and `/bff` to the back office, same origin) and
`vault.<domain>/public/v1` (browser tokenization). `/internal` and `/metrics` are never routed.

## Release flow

1. CI (`ci.yml`) runs tests, security scans and `scripts/check_infra.sh`, builds both images, scans
   them with Trivy and produces SPDX SBOMs.
2. On `main`, `release.yml` pushes `tally/python:<sha>` and `tally/web:<sha>` to ECR (immutable
   tags), signs the digests keylessly with cosign (GitHub OIDC → Fulcio, logged in Rekor) and
   attaches the SBOM as a signed attestation.
3. Dev and staging track `main` automatically. Production is promoted by a PR that sets
   `global.image.pythonDigest`/`webDigest` in `infra/argocd/values/prod.yaml`; an operator syncs.
4. Argo CD runs the migration hook (additive migrations only), then updates workloads. `core` and
   `ledger` canary through the configured steps while three analyses run: the canary's 5xx ratio
   (< 1%), its p99 latency (< 1.5 s) and `min(tally_ledger_integrity_ok) == 1` with no tolerance.
   A failure aborts and removes the canary; production also waits for a manual final promotion.
5. Kyverno admits only ECR images, pinned by digest in staging/prod, signed by `release.yml` on
   `main`, with an SBOM attestation, running as non-root with a read-only root filesystem.

## Verified locally

`make infra-check` (every stage runs from a pinned container image; only Docker is needed):

| Stage | Result |
| --- | --- |
| helm lint `--strict` and render: base, dev, staging, prod | pass (87 resources each) |
| kubeconform `-strict`, Kubernetes 1.31, with CRD schemas (Rollouts, ExternalSecrets, Prometheus Operator, Argo CD, Kyverno) | 359 resources valid |
| Kyverno CLI against rendered manifests | dev 39/39 and prod pinned by digest 52/52 pass; prod with tags is rejected (13 failures), proving the digest rule bites |
| terraform fmt + validate: dev, staging, prod, bootstrap | valid |
| trivy config (HIGH/CRITICAL): Terraform, Kubernetes, Dockerfiles, rendered chart | no findings |
| checkov: Terraform 633, Dockerfile 144, Kubernetes 1162 checks | 0 failed (exceptions below) |

`make security-scan`: gitleaks over the full history, semgrep (Python, TypeScript, secrets,
Dockerfile, Terraform, Kubernetes packs), pip-audit on the locked runtime dependencies, npm audit,
Trivy on both images and Syft SBOMs. Results for this phase are in [PROGRESS](PROGRESS.md).

`make k8s-up` / `make k8s-drill` run the real chart on a separate minikube profile (`tally`,
Calico so NetworkPolicies are enforced, Pod Security `restricted`, Argo Rollouts, in-cluster
Postgres/Redis/Prometheus) and release a healthy and a broken core version under signed merchant
traffic. Results: [k8s drill report](k8s-drill-report.md).

### Scanner exceptions

`.checkov.yaml` lists every skipped check with its reason. In short: variable-driven settings the
scanner cannot evaluate (Multi-AZ, deletion protection); KMS key policies and
`ecr:GetAuthorizationToken`, which require `Resource: "*"`; CloudFront checks that do not apply to
a static-asset-only distribution; CPU limits (deliberately unset to avoid CFS throttling; requests
and memory limits are enforced); secrets as environment variables (the code reads env; Secrets
are KMS-encrypted in etcd). Two are real gaps: **cross-region S3 replication** and
**automated rotation of the Valkey auth token and MSK SCRAM secret** (rotation is by runbook).

## Applying to AWS

Blocked on things only the owner can provide. With them, the order is:

1. **Accounts and access.** Three AWS accounts (dev, staging, prod) in an Organization; apply
   `infra/terraform/org/region-guardrail-scp.json` at the OU. Authenticate with IAM Identity
   Center (SSO); do not create access keys.
2. **Domain.** A Route 53 hosted zone for the domain (or set `create_zone = true`).
3. **State.** In each account: `cd infra/terraform/bootstrap && terraform init && terraform apply -var account_id=<id>`.
4. **Platform.** In each environment root: copy `terraform.tfvars.example` to `terraform.tfvars`,
   then `terraform init -backend-config="bucket=tally-tfstate-<id>"`, `terraform plan`, `terraform apply`.
5. **Cluster add-ons** (not managed by this repository): AWS Load Balancer Controller (with its
   upstream IAM policy), External Secrets Operator (role: `external_secrets_role_arn` output) with
   a `ClusterSecretStore` named `tally-aws-secrets`, kube-prometheus-stack (remote write to
   `prometheus_remote_write_url`), Argo CD, Argo Rollouts and Kyverno.
6. **Secrets.** Create `tally/<env>/<workload>` entries in Secrets Manager with the keys listed
   under `secretKeys` in `values.yaml` (database URLs from the RDS endpoints and the RDS-managed
   master secrets, `REDIS_URL` from the Valkey endpoint and auth secret, generated service keys).
7. **Values.** Replace the placeholder account IDs and ARNs in `infra/argocd/values/<env>.yaml`
   with `terraform output -json platform` (`helm_values`).
8. **GitHub.** Set repository variables `AWS_RELEASE_ROLE_ARN` and `ECR_REGISTRY` (dev account)
   so `release.yml` starts publishing signed images.
9. **Argo CD.** `kubectl apply -f infra/argocd/project.yaml -f infra/argocd/applications/`.

Running cost for these sizes is estimated in `docs/cost.md` (Phase 15).
