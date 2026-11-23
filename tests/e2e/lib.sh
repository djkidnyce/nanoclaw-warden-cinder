#!/usr/bin/env bash
# Helpers for kind_e2e.sh, kept separate so tests/scripts can exercise them with a fake kubectl.
# Expects NS to be set by the caller.

# Desired replicas for a Deployment. KEDA writes .spec.replicas; .status.replicas
# disappears entirely at zero, so kubectl prints nothing. Treat empty as 0.
replicas() {
  local r
  r="$(kubectl -n "$NS" get deploy "$1" -o jsonpath='{.spec.replicas}' 2>/dev/null)"
  printf '%s\n' "${r:-0}"
}

# Pods still running for a component, so a Deployment at 0 with a pod still
# terminating does not count as scaled down yet.
running() {
  kubectl -n "$NS" get pods -l "app.kubernetes.io/component=$1" \
    --field-selector=status.phase=Running -o name 2>/dev/null | grep -c . || true
}

agents() { running agent; }

is_zero() {
  [ "$(replicas controller)" = 0 ] && [ "$(replicas vault)" = 0 ] &&
    [ "$(running controller)" = 0 ] && [ "$(running vault)" = 0 ] && [ "$(agents)" = 0 ]
}

wait_for() {  # wait_for <seconds> <description> <command...>
  local limit="$1" what="$2"; shift 2
  local start=$SECONDS
  until "$@"; do
    if [ $((SECONDS - start)) -ge "$limit" ]; then echo "timed out waiting for: $what" >&2; return 1; fi
    sleep "${WAIT_INTERVAL:-5}"
  done
  echo "ok: $what ($((SECONDS - start))s)"
}
