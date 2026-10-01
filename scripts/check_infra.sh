#!/usr/bin/env bash
# Validate the deployment code without a cloud account: every tool runs from a pinned container
# image, so the only local requirement is Docker. Used by `make infra-check` and CI.
#
#   helm lint + render (base, dev, staging, prod)   kubeconform (strict, pinned schemas)
#   kyverno CLI against rendered manifests          terraform fmt + validate (every env)
#   trivy config + checkov (IaC misconfiguration)   promtool (alert rules, via make)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/.data/infra-check"
SCHEMAS="$ROOT/.data/k8s-schemas"
HELM_IMAGE=alpine/helm:3.19.0
KUBECONFORM_IMAGE=ghcr.io/yannh/kubeconform:v0.7.0
KYVERNO_IMAGE=ghcr.io/kyverno/kyverno-cli:v1.15.2
TERRAFORM_IMAGE=hashicorp/terraform:1.13.3
TRIVY_IMAGE=aquasec/trivy:0.67.2
CHECKOV_IMAGE=bridgecrew/checkov:3.2.477
K8S_VERSION=1.31.0
STAGES="${STAGES:-helm kubeconform kyverno terraform trivy checkov}"

mkdir -p "$OUT" "$SCHEMAS"
run() { docker run --rm "$@"; }
want() { [[ " $STAGES " == *" $1 "* ]]; }

fetch_schemas() {
  local base=https://raw.githubusercontent.com/yannh/kubernetes-json-schema/master
  local crds=https://raw.githubusercontent.com/datreeio/CRDs-catalog/main
  local dir="$SCHEMAS/v$K8S_VERSION-standalone-strict"
  mkdir -p "$dir"
  for kind in _definitions configmap-v1 cronjob-batch-v1 deployment-apps-v1 \
    horizontalpodautoscaler-autoscaling-v2 ingress-networking-v1 job-batch-v1 \
    namespace-v1 networkpolicy-networking-v1 poddisruptionbudget-policy-v1 secret-v1 \
    service-v1 serviceaccount-v1; do
    [[ -s "$dir/$kind.json" ]] || curl -fsSL -m 120 -o "$dir/$kind.json" \
      "$base/v$K8S_VERSION-standalone-strict/$kind.json"
  done
  for crd in argoproj.io/rollout_v1alpha1 argoproj.io/analysistemplate_v1alpha1 \
    argoproj.io/application_v1alpha1 argoproj.io/appproject_v1alpha1 \
    external-secrets.io/externalsecret_v1 monitoring.coreos.com/servicemonitor_v1 \
    monitoring.coreos.com/prometheusrule_v1 kyverno.io/clusterpolicy_v1; do
    mkdir -p "$SCHEMAS/$(dirname "$crd")"
    [[ -s "$SCHEMAS/$crd.json" ]] || curl -fsSL -m 120 -o "$SCHEMAS/$crd.json" "$crds/$crd.json"
  done
}

if want helm; then
  echo "== helm lint and render"
  for env in base dev staging prod; do
    values=()
    [[ $env == base ]] || values=(-f "/repo/infra/argocd/values/$env.yaml")
    run -v "$ROOT:/repo" -w /repo/infra/helm/tally "$HELM_IMAGE" lint --strict . ${values[@]+"${values[@]}"} >/dev/null
    run -v "$ROOT:/repo" -w /repo/infra/helm/tally "$HELM_IMAGE" \
      template tally . --namespace "tally-$env" ${values[@]+"${values[@]}"} >"$OUT/render-$env.yaml"
    echo "   $env: $(grep -c '^kind:' "$OUT/render-$env.yaml") resources"
  done
fi

if want kubeconform; then
  echo "== kubeconform (strict, Kubernetes $K8S_VERSION)"
  fetch_schemas
  run --network none -v "$SCHEMAS:/schemas:ro" -v "$OUT:/out:ro" -v "$ROOT/infra:/infra:ro" \
    "$KUBECONFORM_IMAGE" -strict -summary -kubernetes-version "$K8S_VERSION" \
    -schema-location '/schemas/{{.NormalizedKubernetesVersion}}-standalone-strict/{{.ResourceKind}}{{.KindSuffix}}.json' \
    -schema-location '/schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
    /out/render-base.yaml /out/render-dev.yaml /out/render-staging.yaml /out/render-prod.yaml \
    /infra/argocd/project.yaml /infra/argocd/applications /infra/k8s/policies
fi

if want kyverno; then
  echo "== kyverno policies against rendered manifests"
  # Signature verification needs registry access and is exercised in-cluster; every other rule
  # runs here. dev (tags allowed) and prod pinned by digest must pass; prod rendered without
  # digests must be rejected, which proves the digest rule actually bites.
  zero=sha256:0000000000000000000000000000000000000000000000000000000000000000
  run -v "$ROOT:/repo" -w /repo/infra/helm/tally "$HELM_IMAGE" template tally . \
    --namespace tally-prod -f /repo/infra/argocd/values/prod.yaml \
    --set "global.image.pythonDigest=$zero,global.image.webDigest=$zero" \
    >"$OUT/render-prod-pinned.yaml"
  kyverno() {
    run --network none -v "$ROOT/infra/k8s/policies:/policies:ro" -v "$OUT:/out:ro" \
      "$KYVERNO_IMAGE" apply /policies/tally-policies.yaml --resource "/out/$1" \
      --remove-color --policy-report=false 2>&1
  }
  for render in render-dev.yaml render-prod-pinned.yaml; do
    result="$(kyverno "$render" | grep '^pass:')"
    echo "   $render: $result"
    [[ $result == *"fail: 0, warn: 0, error: 0"* ]] || { echo "unexpected policy failures"; exit 1; }
  done
  result="$(kyverno render-prod.yaml | grep '^pass:' || true)"
  echo "   render-prod.yaml (unpinned, must fail): $result"
  [[ $result != *"fail: 0,"* ]] || { echo "digest policy did not reject unpinned images"; exit 1; }
fi

if want terraform && [[ -d "$ROOT/infra/terraform" ]]; then
  echo "== terraform fmt and validate"
  run -v "$ROOT/infra/terraform:/tf" -w /tf "$TERRAFORM_IMAGE" fmt -check -recursive
  for root in "$ROOT"/infra/terraform/envs/*/ "$ROOT"/infra/terraform/bootstrap/; do
    name="${root#"$ROOT"/infra/terraform/}"
    name="${name%/}"
    run -v "$ROOT/infra/terraform:/tf" -v "$OUT/tf-plugins:/plugins" \
      -e TF_PLUGIN_CACHE_DIR=/plugins -w "/tf/$name" "$TERRAFORM_IMAGE" \
      init -backend=false -input=false >/dev/null
    run -v "$ROOT/infra/terraform:/tf" -v "$OUT/tf-plugins:/plugins" \
      -w "/tf/$name" "$TERRAFORM_IMAGE" validate -no-color | grep -v "^$" | sed "s#^#   $name: #"
  done
fi

if want trivy; then
  echo "== trivy config (HIGH/CRITICAL)"
  # infra/k8s/local holds throwaway drill dependencies (stock Postgres/Redis images, which run
  # as root); AWS uses RDS and ElastiCache instead, so they are not deployable code.
  for target in /repo/infra /repo/deploy/docker /repo/.data/infra-check/render-prod-pinned.yaml; do
    run -v "$ROOT:/repo:ro" -v "$OUT/trivy-cache:/root/.cache" "$TRIVY_IMAGE" config \
      --quiet --severity HIGH,CRITICAL --exit-code 1 --ignorefile /repo/.trivyignore.yaml \
      --skip-dirs /repo/infra/k8s/local \
      "$target" >"$OUT/trivy.txt" 2>&1 || { cat "$OUT/trivy.txt"; exit 1; }
    echo "   $target: no HIGH/CRITICAL findings"
  done
fi

if want checkov; then
  echo "== checkov (terraform, kubernetes, dockerfile)"
  run -v "$ROOT:/repo:ro" "$CHECKOV_IMAGE" --config-file /repo/.checkov.yaml \
    -d /repo/infra/terraform -d /repo/deploy/docker 2>/dev/null | grep -E "checks|FAILED|Check:"
  run -v "$ROOT:/repo:ro" -v "$OUT:/out:ro" "$CHECKOV_IMAGE" --config-file /repo/.checkov.yaml \
    --framework kubernetes -f /out/render-prod-pinned.yaml 2>/dev/null | grep -E "checks|FAILED|Check:"
fi

echo "infra checks passed"
