#!/usr/bin/env bash
# Creates every secret NanoClaw Warden Cinder needs, with fresh random passwords.
#
#   ./scripts/gen-secrets.sh --api-key-file ./anthropic.key          apply to the cluster
#   ./scripts/gen-secrets.sh --api-key-file ./key --output ./secrets  write YAML instead (for SOPS, Sealed Secrets, etc.)
#
# Secrets created in the nanoclaw namespace:
#   nanoclaw-redis-acl          users.acl with SHA256 hashes only, mounted by Redis
#   nanoclaw-redis-bootstrap    password for the sidecar that creates consumer groups
#   nanoclaw-redis-controller   password for the controller
#   nanoclaw-redis-agent        password for agent Jobs
#   nanoclaw-redis-vault        password for the vault
#   nanoclaw-redis-keda         username and password KEDA uses to read lag
#   nanoclaw-upstream           the upstream API key, mounted only by the vault
# The client password (for the operator CLI) is written to ./nanoclaw-client.pass, mode 600.
#
# Rerunning rotates every password. Restart redis, then the other workloads pick up
# new files on their next start.
set -euo pipefail

NAMESPACE=nanoclaw
API_KEY_FILE=""
OUTPUT=""
CLIENT_PASS_FILE="./nanoclaw-client.pass"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

usage() { sed -n '2,20p' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --api-key-file) API_KEY_FILE="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --client-pass-file) CLIENT_PASS_FILE="$2"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
done

[ -n "$API_KEY_FILE" ] || { echo "error: --api-key-file is required (the key is never read from the command line)" >&2; exit 2; }
[ -r "$API_KEY_FILE" ] || { echo "error: cannot read $API_KEY_FILE" >&2; exit 2; }
[ -s "$API_KEY_FILE" ] || { echo "error: $API_KEY_FILE is empty" >&2; exit 2; }
command -v openssl >/dev/null || { echo "error: openssl is required" >&2; exit 2; }
if [ -z "$OUTPUT" ]; then
  command -v kubectl >/dev/null || { echo "error: kubectl is required unless --output is used" >&2; exit 2; }
fi

umask 077
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

rand() { openssl rand -hex 32; }
sha() { printf '%s' "$1" | openssl dgst -sha256 -r | cut -d' ' -f1; }

ACL="$(cat "$ROOT/deploy/base/redis/users.acl.tmpl")"
for role in bootstrap controller agent vault keda client; do
  pw="$(rand)"
  printf '%s' "$pw" > "$WORK/$role.pass"
  upper="$(printf '%s' "$role" | tr '[:lower:]' '[:upper:]')"
  ACL="${ACL//\{\{${upper}_HASH\}\}/$(sha "$pw")}"
done
case "$ACL" in *"{{"*) echo "error: ACL template still has placeholders" >&2; exit 1 ;; esac
printf '%s\n' "$ACL" > "$WORK/users.acl"

emit() {
  # emit NAME --from-file=key=path ...
  local name="$1"; shift
  if [ -n "$OUTPUT" ]; then
    mkdir -p "$OUTPUT"
    kubectl_create "$name" "$@" > "$OUTPUT/$name.yaml"
    echo "wrote $OUTPUT/$name.yaml"
  else
    kubectl_create "$name" "$@" | kubectl apply -f - >/dev/null
    echo "applied secret/$name"
  fi
}

kubectl_create() {
  local name="$1"; shift
  if command -v kubectl >/dev/null; then
    kubectl create secret generic "$name" --namespace "$NAMESPACE" --dry-run=client -o yaml "$@"
  else
    # Minimal renderer for --output mode on machines without kubectl.
    printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\n  namespace: %s\ntype: Opaque\ndata:\n' "$name" "$NAMESPACE"
    for arg in "$@"; do
      kv="${arg#--from-file=}"; key="${kv%%=*}"; file="${kv#*=}"
      printf '  %s: %s\n' "$key" "$(base64 < "$file" | tr -d '\n')"
    done
  fi
}

printf 'keda' > "$WORK/keda.user"
emit nanoclaw-redis-acl "--from-file=users.acl=$WORK/users.acl"
emit nanoclaw-redis-bootstrap "--from-file=password=$WORK/bootstrap.pass"
emit nanoclaw-redis-controller "--from-file=password=$WORK/controller.pass"
emit nanoclaw-redis-agent "--from-file=password=$WORK/agent.pass"
emit nanoclaw-redis-vault "--from-file=password=$WORK/vault.pass"
emit nanoclaw-redis-keda "--from-file=username=$WORK/keda.user" "--from-file=password=$WORK/keda.pass"
tr -d '\r\n' < "$API_KEY_FILE" > "$WORK/api-key"
emit nanoclaw-upstream "--from-file=api-key=$WORK/api-key"

cp "$WORK/client.pass" "$CLIENT_PASS_FILE"
chmod 600 "$CLIENT_PASS_FILE"
echo "client password written to $CLIENT_PASS_FILE (keep it out of git)"
