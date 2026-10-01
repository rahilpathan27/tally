#!/usr/bin/env bash
# Supply-chain and code security gates, runnable locally (Docker only) and in CI:
#   gitleaks   secrets in the working tree and full git history
#   semgrep    SAST (Python, TypeScript, Dockerfile, Terraform, Kubernetes rule packs)
#   pip-audit  known vulnerabilities in the locked Python dependencies
#   npm audit  known vulnerabilities in the console's dependencies (high and above)
#   trivy      OS and library vulnerabilities in the built images (fixable HIGH/CRITICAL)
#   syft       SPDX SBOMs for both images (.data/sbom/)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/.data/security"
SBOM="$ROOT/.data/sbom"
GITLEAKS_IMAGE=zricethezav/gitleaks:v8.28.0
SEMGREP_IMAGE=semgrep/semgrep:1.137.0
TRIVY_IMAGE=aquasec/trivy:0.67.2
SYFT_IMAGE=anchore/syft:v1.33.0
PY_IMAGE="${PY_IMAGE:-tally/python:dev}"
WEB_IMAGE="${WEB_IMAGE:-tally/web:dev}"
STAGES="${STAGES:-gitleaks semgrep pip-audit npm-audit trivy-image sbom}"
mkdir -p "$OUT" "$SBOM"
want() { [[ " $STAGES " == *" $1 "* ]]; }

if want gitleaks; then
  echo "== gitleaks (history)"
  docker run --rm -v "$ROOT:/repo" "$GITLEAKS_IMAGE" git /repo --config /repo/.gitleaks.toml \
    --redact --no-banner --exit-code 1
fi

if want semgrep; then
  echo "== semgrep"
  docker run --rm -v "$ROOT:/src:ro" -w /src "$SEMGREP_IMAGE" semgrep scan --metrics=off \
    --config p/python --config p/typescript --config p/secrets --config p/dockerfile \
    --config p/terraform --config p/kubernetes --severity ERROR --error --quiet \
    --exclude web/console/node_modules --exclude .data --exclude sources --exclude mutants \
    --exclude 'tests/**'
fi

if want pip-audit; then
  echo "== pip-audit (locked runtime dependencies)"
  (cd "$ROOT" && uv export --frozen --no-dev --no-emit-project --format requirements-txt \
    >"$OUT/requirements.txt")
  (cd "$ROOT" && uvx --quiet pip-audit==2.9.0 --strict --disable-pip --no-deps \
    -r "$OUT/requirements.txt")
fi

if want npm-audit; then
  echo "== npm audit (high and above)"
  (cd "$ROOT/web/console" && npm audit --audit-level=high --omit=dev)
fi

if want trivy-image; then
  echo "== trivy image (fixable HIGH/CRITICAL)"
  for image in "$PY_IMAGE" "$WEB_IMAGE"; do
    docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$ROOT:/repo:ro" \
      -v "$OUT/trivy-cache:/root/.cache" "$TRIVY_IMAGE" image --quiet --scanners vuln \
      --severity HIGH,CRITICAL --ignore-unfixed --ignorefile /repo/.trivyignore.yaml \
      --exit-code 1 "$image"
    echo "   $image: no fixable HIGH/CRITICAL vulnerabilities"
  done
fi

if want sbom; then
  echo "== syft SBOMs"
  for image in "$PY_IMAGE" "$WEB_IMAGE"; do
    name="$(echo "$image" | tr '/:' '__')"
    docker run --rm -v /var/run/docker.sock:/var/run/docker.sock "$SYFT_IMAGE" \
      "docker:$image" -o spdx-json >"$SBOM/$name.spdx.json"
    echo "   $SBOM/$name.spdx.json ($(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))['packages']))" "$SBOM/$name.spdx.json") packages)"
  done
fi

echo "security scans passed"
