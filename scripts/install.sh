#!/usr/bin/env bash
# =============================================================================
# NanoClaw Warden Cinder dependency installer.
# Installs ONLY what is missing. Checks before each step. Prompts before acting.
# All versions pinned and verified current as of 2026-06-22.
#
# CNI choice: pass --cni=cilium (default) or --cni=calico.
#   Cilium  -> eBPF dataplane, replaces kube-proxy, strong egress policy.
#   Calico  -> add policy enforcement, also good if you already run Flannel.
#
# Usage:
#   ./install.sh                 # interactive, Cilium
#   ./install.sh --cni=calico
#   ./install.sh --yes           # non-interactive (CI); still skips what exists
# =============================================================================
set -euo pipefail

KEDA_CHART_VERSION="2.20.1"
CILIUM_VERSION="1.19.5"
CILIUM_CLI_VERSION="v0.18.8"
CALICO_VERSION="v3.32.0"

CNI="cilium"
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --cni=*) CNI="${arg#*=}";;
    --yes|-y) ASSUME_YES=1;;
    *) echo "unknown arg: $arg"; exit 2;;
  esac
done

confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  read -r -p "  -> $1 [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]]
}

have() { command -v "$1" >/dev/null 2>&1; }
step() { echo; echo "==> $1"; }

# ---- Hard prerequisites we will NOT auto-install ----------------------------
for bin in kubectl helm; do
  have "$bin" || { echo "FATAL: $bin is required and not installed. Install it first."; exit 1; }
done
kubectl cluster-info >/dev/null 2>&1 || { echo "FATAL: no reachable cluster."; exit 1; }

echo "================================================================"
echo " NanoClaw Warden Cinder installer   CNI=${CNI}"
echo " Versions pinned & verified current as of 2026-06-22"
echo "================================================================"

# ---- 1. CNI -----------------------------------------------------------------
step "CNI / NetworkPolicy engine (${CNI})"
case "$CNI" in
  cilium)
    if kubectl get pods -n kube-system 2>/dev/null | grep -qi cilium; then
      echo "  Cilium already present. Skipping. (target v${CILIUM_VERSION})"
    else
      if confirm "Install Cilium v${CILIUM_VERSION} via Helm?"; then
        helm repo add cilium https://helm.cilium.io/ >/dev/null
        helm repo update >/dev/null
        helm install cilium cilium/cilium --version "$CILIUM_VERSION" \
          --namespace kube-system \
          --set kubeProxyReplacement=true \
          --set policyEnforcementMode=default
        echo "  Cilium installed."
      else
        echo "  Skipped Cilium. NetworkPolicy will NOT be enforced without a policy CNI."
      fi
    fi
    ;;
  calico)
    if kubectl get pods -A 2>/dev/null | grep -qiE 'calico'; then
      echo "  Calico already present. Skipping. (target ${CALICO_VERSION})"
    else
      if confirm "Install Calico ${CALICO_VERSION} via tigera-operator Helm chart?"; then
        helm repo add projectcalico https://docs.tigera.io/calico/charts >/dev/null
        helm repo update >/dev/null
        helm install calico projectcalico/tigera-operator \
          --version "$CALICO_VERSION" --namespace tigera-operator --create-namespace
        echo "  Calico operator installed. Wait for calico-system pods to be Ready."
      else
        echo "  Skipped Calico."
      fi
    fi
    ;;
  *) echo "FATAL: unknown CNI '${CNI}' (use cilium or calico)"; exit 2;;
esac

# ---- 2. KEDA ----------------------------------------------------------------
step "KEDA (scale-to-zero)"
if kubectl get crd scaledjobs.keda.sh >/dev/null 2>&1; then
  echo "  KEDA already present (ScaledJob CRD found). Skipping. (target chart ${KEDA_CHART_VERSION})"
else
  if confirm "Install KEDA chart ${KEDA_CHART_VERSION}?"; then
    helm repo add kedacore https://kedacore.github.io/charts >/dev/null
    helm repo update >/dev/null
    helm install keda kedacore/keda --version "$KEDA_CHART_VERSION" \
      --namespace keda --create-namespace
    echo "  KEDA installed."
  else
    echo "  Skipped KEDA. Scale-to-zero will not function."
  fi
fi

# ---- 3. Verify --------------------------------------------------------------
step "Post-install verification"
echo "  Waiting for KEDA operator (if installed)..."
kubectl wait --for=condition=available --timeout=120s \
  deploy/keda-operator -n keda 2>/dev/null && echo "  KEDA operator ready." || echo "  (KEDA not present or not ready yet)"

echo
echo "================================================================"
echo " Dependencies handled. Next:"
echo "   ./scripts/preflight.sh        # confirm everything is green"
echo "   kubectl apply -f k8s/         # deploy NanoClaw Warden Cinder (after editing secrets)"
echo "================================================================"
