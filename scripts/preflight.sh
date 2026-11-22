#!/usr/bin/env bash
# Read only readiness check. Changes nothing. Exits 0 when the cluster can run
# NanoClaw Warden Cinder, 1 when something required is missing or too old.
#
#   ./scripts/preflight.sh
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source-path=SCRIPTDIR/.. source=versions.env
. "$ROOT/versions.env"

if [ -t 1 ]; then G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; N=$'\e[0m'; else G=; R=; Y=; N=; fi
fails=0
warns=0
ok()   { printf '  %s[ok]%s    %s\n' "$G" "$N" "$1"; }
warn() { printf '  %s[warn]%s  %s\n' "$Y" "$N" "$1"; warns=$((warns + 1)); }
fail() { printf '  %s[fail]%s  %s\n' "$R" "$N" "$1"; fails=$((fails + 1)); }
have() { command -v "$1" >/dev/null 2>&1; }
# version_ge A B: true when A >= B (leading v ignored)
version_ge() { [ "$(printf '%s\n%s\n' "${2#v}" "${1#v}" | sort -V | head -1)" = "${2#v}" ]; }

echo "NanoClaw Warden Cinder preflight (read only)"
echo "Pins: KEDA $KEDA_VERSION, Cilium $CILIUM_VERSION, Calico $CALICO_VERSION, Redis $REDIS_VERSION, Kubernetes >= $K8S_MIN_VERSION"

echo
echo "[1/6] Tools"
if have kubectl; then ok "kubectl $(kubectl version --client -o json 2>/dev/null | sed -n 's/.*"gitVersion": *"\([^"]*\)".*/\1/p' | head -1)"; else fail "kubectl not found"; fi
if have helm; then ok "helm $(helm version --short 2>/dev/null)"; else warn "helm not found (only needed by install.sh)"; fi
if have openssl; then ok "openssl present (gen-secrets.sh)"; else fail "openssl not found (gen-secrets.sh needs it)"; fi

echo
echo "[2/6] Cluster"
if ! kubectl version -o json >/dev/null 2>&1; then
  fail "cannot reach a cluster with the current kubeconfig context"
  echo; echo "Result: $fails failure(s). Fix cluster access first."; exit 1
fi
server="$(kubectl version -o json 2>/dev/null | tr -d '\n ' | sed -n 's/.*"serverVersion":{[^}]*"gitVersion":"\([^"]*\)".*/\1/p')"
server_mm="$(printf '%s' "${server#v}" | cut -d. -f1,2)"
if version_ge "$server_mm" "$K8S_MIN_VERSION"; then ok "Kubernetes $server"; else fail "Kubernetes $server is older than $K8S_MIN_VERSION"; fi
if kubectl api-resources --api-group=admissionregistration.k8s.io 2>/dev/null | grep -q '^validatingadmissionpolicies'; then
  ok "ValidatingAdmissionPolicy API available"
else
  fail "ValidatingAdmissionPolicy API missing (needs Kubernetes 1.30 or newer)"
fi

echo
echo "[3/6] CNI and NetworkPolicy enforcement"
ds="$(kubectl get daemonsets -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}={.spec.template.spec.containers[0].image}{"\n"}{end}' 2>/dev/null)"
cni="none"
case "$ds" in
  *"/cilium="*) cni="cilium" ;;
  *"/calico-node="*) cni="calico" ;;
  *"/kindnet="*) cni="kindnet" ;;
  *"/kube-flannel-ds="*|*"/flannel="*) cni="flannel" ;;
esac
img_ver() { printf '%s\n' "$ds" | grep "/$1=" | head -1 | sed -n 's/.*:\(v\{0,1\}[0-9][0-9.]*\).*/\1/p'; }
case "$cni" in
  cilium)
    v="$(img_ver cilium)"
    ok "Cilium ${v:-unknown} found. Use deploy/overlays/cilium (FQDN egress)."
    [ -n "$v" ] && ! version_ge "$v" "$CILIUM_VERSION" && warn "Cilium $v is behind the pinned $CILIUM_VERSION" ;;
  calico)
    v="$(img_ver calico-node)"
    ok "Calico ${v:-unknown} found. Use deploy/overlays/calico (CIDR egress)."
    [ -n "$v" ] && ! version_ge "$v" "$CALICO_VERSION" && warn "Calico $v is behind the pinned $CALICO_VERSION" ;;
  kindnet)
    warn "kindnet found. It enforces standard NetworkPolicy, fine for labs. Use deploy/overlays/calico." ;;
  flannel)
    fail "Flannel alone does not enforce NetworkPolicy. Add Calico for policy, or move to Cilium." ;;
  *)
    fail "no NetworkPolicy enforcing CNI recognized (looked for cilium, calico-node, kindnet daemonsets)" ;;
esac

echo
echo "[4/6] KEDA"
if kubectl get crd scaledjobs.keda.sh >/dev/null 2>&1; then
  kimg="$(kubectl get deploy -n keda keda-operator -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)"
  kv="$(printf '%s' "$kimg" | sed -n 's/.*:\(v\{0,1\}[0-9][0-9.]*\).*/\1/p')"
  if [ -z "$kimg" ]; then
    fail "KEDA CRDs exist but no keda-operator Deployment in namespace keda (the network policy and admission policy expect that namespace)"
  elif [ -z "$kv" ]; then
    warn "KEDA operator image $kimg has no readable version tag"
  elif version_ge "$kv" "$KEDA_MIN_VERSION"; then
    ok "KEDA $kv"
  else
    fail "KEDA $kv is older than $KEDA_MIN_VERSION (CVE-2026-77524). Upgrade: ./scripts/install.sh --upgrade-keda"
  fi
else
  fail "KEDA not installed. Run ./scripts/install.sh"
fi

echo
echo "[5/6] Namespace and secrets"
if kubectl get ns nanoclaw >/dev/null 2>&1; then
  missing=""
  for s in nanoclaw-redis-acl nanoclaw-redis-bootstrap nanoclaw-redis-controller nanoclaw-redis-agent nanoclaw-redis-vault nanoclaw-redis-keda nanoclaw-upstream; do
    kubectl -n nanoclaw get secret "$s" >/dev/null 2>&1 || missing="$missing $s"
  done
  if [ -z "$missing" ]; then ok "all seven secrets present"; else warn "missing secrets:$missing (run ./scripts/gen-secrets.sh)"; fi
else
  warn "namespace nanoclaw not created yet (quick start step 3 creates it)"
fi

echo
echo "[6/6] Optional hardening"
if kubectl get runtimeclass gvisor >/dev/null 2>&1; then ok "RuntimeClass gvisor present (components/gvisor usable)"; else warn "no gvisor RuntimeClass (agents run on the shared kernel)"; fi

echo
if [ "$fails" -eq 0 ]; then
  printf '%sResult: ready%s (%d warning(s)). Detected CNI: %s\n' "$G" "$N" "$warns" "$cni"
  exit 0
fi
printf '%sResult: %d failure(s), %d warning(s).%s Fix the failures, then rerun.\n' "$R" "$fails" "$warns" "$N"
exit 1
