#!/usr/bin/env bash
# =============================================================================
# NanoClaw Warden Cinder preflight. CHECK ONLY by default. Detects what is installed,
# compares against the pinned current versions (as of 2026-06-22), and reports.
# It does NOT install anything unless you run `install.sh` and confirm.
#
# Usage:
#   ./preflight.sh           # check and report, exit non-zero if anything missing
# =============================================================================
set -uo pipefail

# ---- Pinned versions, verified current 2026-06-22 ---------------------------
KEDA_CHART_VERSION="2.20.1"        # kedacore/keda Helm chart (app v2.19.0 GA)
CILIUM_VERSION="1.19.5"            # quay.io/cilium stable, 2026-06-16
CILIUM_CLI_VERSION="v0.18.8"       # cilium-cli stable
CALICO_VERSION="v3.32.0"           # tigera-operator chart, Spring 2026
KUBECTL_MIN="1.28"                 # minimum tested control-plane

GREEN=$'\e[32m'; RED=$'\e[31m'; YELLOW=$'\e[33m'; NC=$'\e[0m'
missing=0
declare -a NOTES

ok()    { printf "  ${GREEN}[OK]${NC}    %s\n" "$1"; }
warn()  { printf "  ${YELLOW}[WARN]${NC}  %s\n" "$1"; }
fail()  { printf "  ${RED}[MISSING]${NC} %s\n" "$1"; missing=$((missing+1)); }

have() { command -v "$1" >/dev/null 2>&1; }

echo "================================================================"
echo " NanoClaw Warden Cinder preflight  (check only — no changes will be made)"
echo " Pinned versions verified current as of 2026-06-22"
echo "================================================================"

# ---- Core CLIs --------------------------------------------------------------
echo; echo "[1/5] Core tooling"
if have kubectl; then
  kv=$(kubectl version --client -o json 2>/dev/null | grep -o '"gitVersion":[^,]*' | head -1 | grep -o 'v[0-9.]*' | head -1)
  ok "kubectl present (${kv:-unknown})"
else
  fail "kubectl not found"
fi
have helm   && ok "helm present ($(helm version --short 2>/dev/null))" || fail "helm not found (needed for KEDA/Cilium/Calico charts)"
have jq     && ok "jq present" || warn "jq not found (recommended for parsing checks)"
have docker && ok "docker present" || warn "docker not found (only needed to build images locally)"

# ---- Cluster reachability ---------------------------------------------------
echo; echo "[2/5] Cluster connectivity"
if have kubectl && kubectl cluster-info >/dev/null 2>&1; then
  srv=$(kubectl version -o json 2>/dev/null | grep -A3 serverVersion | grep -o 'v[0-9.]*' | head -1)
  ok "cluster reachable (server ${srv:-unknown})"
else
  fail "no reachable cluster (check kubeconfig / context)"
fi

# ---- CNI detection ----------------------------------------------------------
# This is the load-bearing dependency: NetworkPolicy MUST be enforced.
echo; echo "[3/5] CNI / NetworkPolicy enforcement"
CNI="none"
if have kubectl; then
  if kubectl get pods -n kube-system 2>/dev/null | grep -qi cilium; then
    CNI="cilium"
    cur=$(kubectl get ds -n kube-system cilium -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null | grep -o 'v[0-9.]*' | head -1)
    ok "Cilium detected (${cur:-unknown}); target ${CILIUM_VERSION}"
    [ -n "$cur" ] && [ "$cur" != "v$CILIUM_VERSION" ] && NOTES+=("Cilium ${cur} installed; pinned target is v${CILIUM_VERSION}. Consider upgrading.")
  elif kubectl get pods -n kube-system 2>/dev/null | grep -qiE 'calico'; then
    CNI="calico"
    cur=$(kubectl get pods -n calico-system -o jsonpath='{.items[0].spec.containers[0].image}' 2>/dev/null | grep -o 'v[0-9.]*' | head -1)
    ok "Calico detected (${cur:-unknown}); target ${CALICO_VERSION}"
    [ -n "$cur" ] && [ "$cur" != "$CALICO_VERSION" ] && NOTES+=("Calico ${cur} installed; pinned target is ${CALICO_VERSION}. Consider upgrading.")
  elif kubectl get pods -n kube-system 2>/dev/null | grep -qi flannel; then
    CNI="flannel"
    fail "Flannel detected WITHOUT a policy engine. Flannel alone does NOT enforce NetworkPolicy."
    NOTES+=("CRITICAL: the threat model depends on NetworkPolicy. Install Calico for policy, or replace Flannel with Cilium.")
  else
    fail "No recognized NetworkPolicy-enforcing CNI found (looked for Cilium/Calico/Flannel)."
  fi
fi

# ---- KEDA -------------------------------------------------------------------
echo; echo "[4/5] KEDA (scale-to-zero)"
if have kubectl && kubectl get crd scaledjobs.keda.sh >/dev/null 2>&1; then
  kver=$(kubectl get deploy -n keda keda-operator -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null | grep -o '[0-9]*\.[0-9]*\.[0-9]*' | head -1)
  ok "KEDA installed (operator ${kver:-unknown}); ScaledJob CRD present"
else
  fail "KEDA not installed (ScaledJob CRD missing). Pinned chart ${KEDA_CHART_VERSION}."
fi

# ---- Container runtime / image registry check ------------------------------
echo; echo "[5/5] Images"
for img in controller agent vault; do
  warn "nanoclaw-${img} image must be built & pushed (pin by digest before prod)"
done

# ---- Summary ----------------------------------------------------------------
echo; echo "================================================================"
if [ "${#NOTES[@]}" -gt 0 ]; then
  echo " Notes:"
  for n in "${NOTES[@]}"; do printf "  - %s\n" "$n"; done
  echo "----------------------------------------------------------------"
fi
if [ "$missing" -eq 0 ]; then
  printf " ${GREEN}All required dependencies present.${NC} Safe to deploy manifests.\n"
  echo " Detected CNI: ${CNI}"
  echo "================================================================"
  exit 0
else
  printf " ${RED}%d required dependency/dependencies missing.${NC}\n" "$missing"
  echo " Run ./install.sh to install missing pieces (it will ask first)."
  echo "================================================================"
  exit 1
fi
