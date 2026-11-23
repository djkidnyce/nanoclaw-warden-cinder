#!/usr/bin/env bash
# The full local regression suite, in the same order CI runs it.
# Needs python3, redis-server 7.2 or newer, openssl, and the tools from fetch-tools.sh.
#
#   ./scripts/fetch-tools.sh && . .tools/env.sh && ./scripts/test-all.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUT="$ROOT/.pytest_cache/rendered"
mkdir -p "$OUT"
step() { printf '\n==== %s\n' "$*"; }

step "shell lint"
shellcheck -x -S style scripts/*.sh deploy/base/redis/bootstrap.sh tests/e2e/kind_e2e.sh tests/e2e/lib.sh

step "yaml lint"
yamllint -d '{extends: relaxed, rules: {line-length: {max: 160}}}' deploy .github

step "render overlays"
for o in cilium calico e2e-kind; do kustomize build "deploy/overlays/$o" > "$OUT/$o.yaml"; done

step "kubeconform (built in kinds; CRDs are validated by the API server tests)"
kubeconform -strict -summary -kubernetes-version "${ENVTEST_K8S_VERSION:-1.37.0}" -ignore-missing-schemas "$OUT"/*.yaml

step "kube-linter"
for o in cilium calico e2e-kind; do kube-linter lint --config .kube-linter.yaml "$OUT/$o.yaml"; done

step "checkov"
for o in cilium calico; do checkov -f "$OUT/$o.yaml" --config-file .checkov.yaml; done

step "pytest: unit, integration, policy, scripts, API server, scale emulation"
python3 -m pytest -q -W error::pytest.PytestUnhandledThreadExceptionWarning

echo
echo "ALL CHECKS PASSED"
