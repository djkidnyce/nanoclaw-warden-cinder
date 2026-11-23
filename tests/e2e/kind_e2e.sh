#!/usr/bin/env bash
# Full end to end run on a real kind cluster with Cilium and KEDA. Used by CI and
# runnable on any machine with docker, kind, kubectl, helm and python3.
#
#   ./tests/e2e/kind_e2e.sh            create cluster, test, delete cluster
#   KEEP=1 ./tests/e2e/kind_e2e.sh     leave the cluster up afterwards
#
# Proves, on real infrastructure:
#   1. idle footprint is Redis only (controller, vault and agents at zero)
#   2. a burst of subtasks starts agent Jobs, the vault scales up, work completes
#   3. everything returns to zero after the cooldowns
#   4. an agent shaped pod cannot reach the internet but can reach Redis
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source-path=SCRIPTDIR/../.. source=versions.env
. "$ROOT/versions.env"
CLUSTER=nanoclaw-e2e
NS=nanoclaw
WORK="$(mktemp -d)"
diagnose() {
  echo "==== diagnostics"
  kubectl -n "$NS" get deploy,scaledjob,scaledobject,jobs,pods -o wide || true
  kubectl -n "$NS" describe scaledobject || true
  kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -40 || true
  kubectl -n keda logs deploy/keda-operator --tail=40 || true
  # Pod logs last, so they land in the tail of the CI output.
  local pod
  for pod in $(kubectl -n "$NS" get pods -o name 2>/dev/null); do
    echo "==== logs $pod (previous)"
    kubectl -n "$NS" logs "$pod" --all-containers --previous --tail=30 2>/dev/null || true
    echo "==== logs $pod"
    kubectl -n "$NS" logs "$pod" --all-containers --tail=30 2>/dev/null || true
  done
}
on_exit() {
  local rc=$?
  [ "$rc" -ne 0 ] && diagnose
  cleanup
}
trap on_exit EXIT
cleanup() {
  [ -n "${PF_PID:-}" ] && kill "$PF_PID" 2>/dev/null || true
  if [ "${KEEP:-0}" != 1 ]; then kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
log() { printf '\n==> %s\n' "$*"; }
# shellcheck source=tests/e2e/lib.sh
. "$ROOT/tests/e2e/lib.sh"

log "cluster"
kind create cluster --config "$ROOT/tests/e2e/kind-config.yaml" --wait 0s
helm repo add cilium https://helm.cilium.io/ --force-update >/dev/null
helm repo add kedacore https://kedacore.github.io/charts --force-update >/dev/null
helm install cilium cilium/cilium --version "$CILIUM_VERSION" -n kube-system \
  --set kubeProxyReplacement=true --set k8sServiceHost="$CLUSTER-control-plane" --set k8sServicePort=6443 \
  --set ipam.mode=kubernetes --wait --timeout 10m
helm install keda kedacore/keda --version "$KEDA_VERSION" -n keda --create-namespace --wait --timeout 10m
"$ROOT/scripts/preflight.sh" || true

log "image"
docker build -t nanoclaw-runtime:e2e --build-arg PYTHON_VERSION="$PYTHON_VERSION" "$ROOT"
kind load docker-image nanoclaw-runtime:e2e --name "$CLUSTER"

log "deploy"
kubectl apply -f "$ROOT/deploy/base/namespace.yaml"
printf 'test-upstream-key' > "$WORK/key"
"$ROOT/scripts/gen-secrets.sh" --api-key-file "$WORK/key" --client-pass-file "$WORK/client.pass"
kubectl apply -k "$ROOT/deploy/overlays/e2e-kind"
kubectl -n "$NS" rollout status deploy/redis --timeout 5m
kubectl -n "$NS" rollout status deploy/mock-upstream --timeout 5m

log "1. idle footprint scales to zero"
wait_for 900 "controller, vault and agents at zero" is_zero
kubectl -n "$NS" get pods

log "2. burst of work"
kubectl -n "$NS" port-forward svc/redis 16379:6379 >/dev/null 2>&1 &
PF_PID=$!
sleep 3
export PYTHONPATH="$ROOT/src" REDIS_HOST=127.0.0.1 REDIS_PORT=16379 REDIS_USERNAME=client REDIS_PASSWORD_FILE="$WORK/client.pass"
python3 -m pip install --quiet --require-hashes -r "$ROOT/requirements.txt"
args=(--goal "e2e burst")
for i in $(seq 1 12); do args+=(--subtask "part-$i"); done
TASK="$(python3 -m nanoclaw cli submit "${args[@]}")"
echo "task $TASK"
peak=0
for _ in $(seq 1 120); do
  n="$(agents)"; [ "$n" -gt "$peak" ] && peak="$n"
  status="$(python3 -m nanoclaw cli status "$TASK" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')"
  [ "$status" = completed ] && break
  sleep 5
done
echo "peak running agent pods: $peak, task status: $status"
[ "$status" = completed ] || { kubectl -n "$NS" get pods,jobs; exit 1; }
[ "$peak" -ge 1 ] || { echo "no agent pods were observed" >&2; exit 1; }

log "4a. people cannot create pods in the namespace (pod creator lock)"
if kubectl -n "$NS" run lockcheck --restart=Never --image=nanoclaw-runtime:e2e >/dev/null 2>&1; then
  echo "pod creator lock did not block a direct pod create" >&2; exit 1
fi
echo "ok: direct pod create refused"

log "4b. network isolation from an agent shaped pod"
kubectl -n "$NS" --as=system:serviceaccount:kube-system:job-controller run netcheck --restart=Never --image=nanoclaw-runtime:e2e \
  --labels=app.kubernetes.io/component=agent,app.kubernetes.io/name=nanoclaw-warden-cinder \
  --overrides='{"spec":{"serviceAccountName":"nanoclaw-agent","automountServiceAccountToken":false,"enableServiceLinks":false,
    "securityContext":{"runAsNonRoot":true,"runAsUser":65532,"seccompProfile":{"type":"RuntimeDefault"}},
    "containers":[{"name":"netcheck","image":"nanoclaw-runtime:e2e","command":["python","-c",
    "import socket\ndef t(h,p):\n  try:\n    socket.create_connection((h,p),5).close(); return True\n  except OSError: return False\nprint(\"REDIS\",t(\"redis\",6379)); print(\"INTERNET\",t(\"1.1.1.1\",443)); print(\"API\",t(\"api.anthropic.com\",443))"],
    "securityContext":{"allowPrivilegeEscalation":false,"readOnlyRootFilesystem":true,"capabilities":{"drop":["ALL"]}},
    "resources":{"requests":{"cpu":"10m","memory":"32Mi"},"limits":{"cpu":"100m","memory":"64Mi"}}}]}}'
wait_for 120 "netcheck finished" bash -c "[ \"\$(kubectl -n $NS get pod netcheck -o jsonpath='{.status.phase}')\" = Succeeded ]"
out="$(kubectl -n "$NS" logs netcheck)"
echo "$out"
grep -q "REDIS True" <<<"$out"
grep -q "INTERNET False" <<<"$out"
grep -q "API False" <<<"$out"
kubectl -n "$NS" delete pod netcheck --wait=false

log "3. back to zero"
wait_for 900 "controller, vault and agents back at zero" is_zero
kubectl -n "$NS" get deploy,scaledjob,scaledobject

log "PASS"
