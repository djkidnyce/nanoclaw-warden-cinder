#!/bin/sh
# Native sidecar for the Redis pod. Creates the consumer groups KEDA polls, then idles.
# Runs whenever the pod starts, so groups come back after any restart.
set -eu
PASS="$(cat /var/run/nanoclaw/redis/password)"
until redis-cli -h 127.0.0.1 --user bootstrap --pass "$PASS" --no-auth-warning ping >/dev/null 2>&1; do
  sleep 1
done
while read -r stream group; do
  out="$(redis-cli -h 127.0.0.1 --user bootstrap --pass "$PASS" --no-auth-warning XGROUP CREATE "$stream" "$group" 0 MKSTREAM 2>&1 || true)"
  case "$out" in
    OK|*BUSYGROUP*) echo "group $group on $stream ready" ;;
    *) echo "failed to create group $group on $stream: $out" >&2; exit 1 ;;
  esac
done <<EOF2
nanoclaw:tasks controller
nanoclaw:subtasks agents
nanoclaw:results controller
EOF2
touch /tmp/ready
exec sleep 2147483647
