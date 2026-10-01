# ADR-019: Kubernetes on EKS with GitOps, canary releases and India-only data

## Context

Phase 14 packages Tally for AWS. Constraints from the plan: payment data stays in India
(Mumbai `ap-south-1`, DR in Hyderabad `ap-south-2`); secrets never appear in code, logs or git;
releases must roll back automatically when they hurt money or SLOs; the supply chain must be
verifiable. There is no AWS account for this project, so everything must be verifiable without
one, and nothing may claim to have been applied.

## Decision

**Images.** One Python image for every Python deployable (the command selects the module) and one
distroless Node image for the console. Both run as non-root with a read-only root filesystem.
The plan preferred distroless for Python too, but `gcr.io/distroless/python3` ships Python 3.11
while Tally targets 3.12 (and LightGBM needs `libgomp`), so the Python image is
`python:3.12-slim-bookworm` with only `libgomp1` added; the web image is distroless.

**Chart.** One umbrella Helm chart (`infra/helm/tally`) generated from a workload table in
`values.yaml`: each workload declares its peers (`calls`), data stores (`stores`) and secret
keys. From that table the chart derives Deployments or Argo Rollouts, Services, HPAs, PDBs,
ServiceAccounts (IRSA), ExternalSecrets and a default-deny NetworkPolicy set, so a new
dependency cannot be added without also opening exactly that network path. The core API and
the core background loops are separate Deployments of the same code
(`TALLY_BACKGROUND_WORKERS`), so API pods scale on traffic and workers do not race rollouts.

**Releases.** `core` and `ledger` are Argo Rollouts canaries. The Service selects stable and
canary pods alike (replica-weighted canary, no mesh), and three AnalysisTemplates gate every
step: the canary's own 5xx ratio, its p99 latency, and `min(tally_ledger_integrity_ok) == 1`
with zero tolerance. Any failure aborts and scales the canary away. Production adds a manual
final promotion. Migrations run as a PreSync hook and must be additive (expand, then contract in
a later release), so stable and canary pods can share a schema.

**GitOps.** Argo CD applications per environment; dev and staging sync automatically, prod syncs
by hand after a promotion PR pins image digests. Namespaces enforce Pod Security `restricted`.
Kyverno adds what PSA does not: ECR-only images, digests in staging/prod, keyless cosign
signatures from this repository's `release.yml` on `main` with an SPDX SBOM attestation,
read-only root filesystems, resource bounds, no default ServiceAccount, no inline Secrets in
real environments, and protection of the default-deny policy.

**Secrets.** AWS Secrets Manager → External Secrets Operator (IRSA, read-only on
`tally/<env>/*`) → Kubernetes Secrets encrypted in etcd with a KMS key. RDS generates and rotates
its own master passwords (`manage_master_user_password`). CI authenticates to AWS and Sigstore
with OIDC; there are no long-lived keys anywhere.

**Terraform.** Small modules (network, eks, rds-postgres, elasticache, kafka, s3, kms, iam, ecr,
waf, route53-acm, cloudfront, security-baseline, observability) composed by a `platform` module;
`envs/dev|staging|prod` differ only in sizing. The three databases are separate RDS instances
with separate KMS keys. Guardrails: the `region` variable accepts only `ap-south-1`/`ap-south-2`,
every provider pins `allowed_account_ids`, and an organisation SCP (`infra/terraform/org`) denies
other regions. CloudFront needs its certificate and edge WAF in `us-east-1`; to keep that
exception free of payment data, CloudFront serves only the console's hashed static assets from a
separate `static.` host, while pages, the API and the vault stay on the Mumbai ALB.

**Verification without AWS.** `make infra-check` runs pinned container images for helm lint and
render (base, dev, staging, prod), kubeconform (strict), the Kyverno CLI (dev and pinned prod
must pass; unpinned prod must be rejected), terraform fmt/validate for every root, trivy config
and checkov. `make k8s-drill` runs the real chart on a local minikube profile with Calico (so
NetworkPolicies are enforced) and Argo Rollouts, drives signed merchant traffic, and releases a
healthy and a broken core version.

## Consequences

- The chart and Terraform are validated, scanned and exercised on a real cluster, but have **not
  been applied to AWS**: that needs an account, credentials and a domain, which this project does
  not have. Account IDs and ARNs in `infra/argocd/values/*.yaml` are placeholders to be replaced
  by `terraform output` (`module.platform.helm_values`).
- The replica-weighted canary cannot send exactly 5% of traffic; with three pods the smallest
  step is one pod. An ALB or mesh traffic router would give finer weights later.
- Cross-region S3 replication and Lambda-based rotation of the Redis and MSK secrets are
  documented gaps (checkov exceptions are listed with reasons in `.checkov.yaml`).
