#!/usr/bin/env bash
# Installs only what is missing: a policy enforcing CNI (when the cluster has none) and KEDA.
# Checks first, asks before every change, never replaces a CNI that is already running.
#
#   ./scripts/install.sh                                   KEDA only, CNI left alone
#   ./scripts/install.sh --cni cilium --k8s-api-host 10.0.0.10 --k8s-api-port 6443
#   ./scripts/install.sh --cni calico
#   ./scripts/install.sh --cni cilium ... --encrypt     add WireGuard node to node encryption (kernel 5.6+)
#   ./scripts/install.sh --upgrade-keda                    move an old KEDA to the pinned version
#   ./scripts/install.sh --dry-run                         print what would run, change nothing
#   ./scripts/install.sh --yes                             no prompts (CI)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source-path=SCRIPTDIR/.. source=versions.env
. "$ROOT/versions.env"

CNI=""
API_HOST=""
API_PORT="6443"
ASSUME_YES=0
DRY_RUN=0
UPGRADE_KEDA=0
ENCRYPT=0

while [ $# -gt 0 ]; do
  case "$1" in
    --cni) CNI="$2"; shift 2 ;;
    --cni=*) CNI="${1#*=}"; shift ;;
    --k8s-api-host) API_HOST="$2"; shift 2 ;;
    --k8s-api-port) API_PORT="$2"; shift 2 ;;
    --upgrade-keda) UPGRADE_KEDA=1; shift ;;
    --encrypt) ENCRYPT=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$CNI" in ""|cilium|calico) ;; *) echo "error: --cni must be cilium or calico" >&2; exit 2 ;; esac
for bin in kubectl helm; do command -v "$bin" >/dev/null || { echo "error: $bin is required" >&2; exit 1; }; done
kubectl version -o json >/dev/null 2>&1 || { echo "error: no reachable cluster" >&2; exit 1; }

run() {
  printf '  + %s\n' "$*"
  [ "$DRY_RUN" -eq 1 ] || "$@"
}
confirm() {
  [ "$ASSUME_YES" -eq 1 ] || [ "$DRY_RUN" -eq 1 ] && return 0
  read -r -p "  $1 [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]]
}
version_ge() { [ "$(printf '%s\n%s\n' "${2#v}" "${1#v}" | sort -V | head -1)" = "${2#v}" ]; }

echo "NanoClaw Warden Cinder installer (KEDA $KEDA_VERSION, Cilium $CILIUM_VERSION, Calico $CALICO_VERSION)"

echo
echo "==> CNI"
existing="$(kubectl get daemonsets -A -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
  | grep -E '^(cilium|calico-node|kindnet|kube-flannel-ds|flannel|weave-net|antrea-agent)$' | head -1 || true)"
if [ -n "$existing" ]; then
  echo "  found $existing. Leaving the CNI alone; swapping a live CNI breaks running pods."
  [ -n "$CNI" ] && echo "  ignoring --cni $CNI because a CNI is already installed."
elif [ -z "$CNI" ]; then
  echo "  no CNI found and none requested. Pass --cni cilium or --cni calico." >&2
  exit 1
elif [ "$CNI" = "cilium" ]; then
  if [ -z "$API_HOST" ]; then
    echo "  error: Cilium replaces kube-proxy here and needs the API server address the nodes use." >&2
    echo "  pass --k8s-api-host <address> [--k8s-api-port 6443]" >&2
    exit 2
  fi
  extra=()
  # WireGuard encrypts pod traffic between nodes, including Redis passwords and tokens.
  [ "$ENCRYPT" -eq 1 ] && extra+=(--set encryption.enabled=true --set encryption.type=wireguard)
  if confirm "Install Cilium $CILIUM_VERSION with kube-proxy replacement?"; then
    run helm repo add cilium https://helm.cilium.io/ --force-update
    run helm install cilium cilium/cilium --version "$CILIUM_VERSION" --namespace kube-system \
      --set kubeProxyReplacement=true --set k8sServiceHost="$API_HOST" --set k8sServicePort="$API_PORT" \
      --set policyEnforcementMode=default --set hubble.enabled=true "${extra[@]}" --wait
  fi
else
  if confirm "Install Calico $CALICO_VERSION through the tigera operator?"; then
    run helm repo add projectcalico https://docs.tigera.io/calico/charts --force-update
    run helm install calico projectcalico/tigera-operator --version "v$CALICO_VERSION" \
      --namespace tigera-operator --create-namespace --wait
  fi
fi

echo
echo "==> KEDA"
if kubectl get crd scaledjobs.keda.sh >/dev/null 2>&1; then
  img="$(kubectl get deploy -n keda keda-operator -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
  have_v="$(printf '%s' "$img" | sed -n 's/.*:\(v\{0,1\}[0-9][0-9.]*\).*/\1/p')"
  if [ -n "$have_v" ] && version_ge "$have_v" "$KEDA_MIN_VERSION"; then
    echo "  KEDA $have_v present. Nothing to do."
  elif [ "$UPGRADE_KEDA" -eq 1 ]; then
    echo "  KEDA ${have_v:-unknown} is below $KEDA_MIN_VERSION. Read the 2.21 upgrade notes before continuing."
    if confirm "Upgrade KEDA to $KEDA_VERSION?"; then
      run helm repo add kedacore https://kedacore.github.io/charts --force-update
      run helm upgrade keda kedacore/keda --version "$KEDA_VERSION" --namespace keda --wait
    fi
  else
    echo "  KEDA ${have_v:-unknown} is below $KEDA_MIN_VERSION (CVE-2026-77524). Rerun with --upgrade-keda." >&2
    exit 1
  fi
else
  if confirm "Install KEDA $KEDA_VERSION into namespace keda?"; then
    run helm repo add kedacore https://kedacore.github.io/charts --force-update
    run helm install keda kedacore/keda --version "$KEDA_VERSION" --namespace keda --create-namespace --wait
  fi
fi

echo
echo "Done. Next: ./scripts/preflight.sh, then follow docs/QUICKSTART.md."
