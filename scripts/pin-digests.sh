#!/usr/bin/env bash
# Resolves every image in an overlay to its current digest and records the pin in
# that overlay's kustomization.yaml. Pair it with components/digest-pinning, which
# makes the admission policy refuse anything unpinned.
#
#   ./scripts/pin-digests.sh deploy/overlays/cilium
#
# Needs crane (go-containerregistry) or docker buildx, plus kustomize.
set -euo pipefail

OVERLAY="${1:?usage: pin-digests.sh <overlay dir>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source-path=SCRIPTDIR/.. source=versions.env
. "$ROOT/versions.env"
command -v kustomize >/dev/null || { echo "error: kustomize is required" >&2; exit 1; }

digest() {
  if command -v crane >/dev/null; then
    crane digest "$1"
  elif command -v docker >/dev/null; then
    docker buildx imagetools inspect "$1" --format '{{json .Manifest}}' | sed -n 's/.*"digest":"\(sha256:[0-9a-f]*\)".*/\1/p' | head -1
  else
    echo "error: need crane or docker buildx" >&2; exit 1
  fi
}

pin() {
  local name="$1" ref="$2" d
  d="$(digest "$ref")"
  [ -n "$d" ] || { echo "error: no digest for $ref" >&2; exit 1; }
  (cd "$OVERLAY" && kustomize edit set image "$name=${ref%%:*}@$d")
  echo "pinned $ref -> $d"
}

pin "$RUNTIME_IMAGE" "$RUNTIME_IMAGE:$RUNTIME_VERSION"
pin docker.io/library/redis "docker.io/library/redis:$REDIS_VERSION-alpine"
echo "Now enable components/digest-pinning in $OVERLAY/kustomization.yaml."
