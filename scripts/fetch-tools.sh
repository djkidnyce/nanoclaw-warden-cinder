#!/usr/bin/env bash
# Downloads the pinned test toolchain into .tools/ (git ignored). Used by CI and by
# scripts/test-all.sh on a workstation. Linux amd64 only; on macOS install the same
# versions with Homebrew instead.
#
#   ./scripts/fetch-tools.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source-path=SCRIPTDIR/.. source=versions.env
. "$ROOT/versions.env"
DEST="$ROOT/.tools"
BIN="$DEST/bin"
mkdir -p "$BIN" "$DEST/crds"
cd "$DEST"

get() { curl -fsSL --retry 3 -o "$2" "$1"; }

[ -x "$BIN/kustomize" ] || { get "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv${KUSTOMIZE_VERSION}/kustomize_v${KUSTOMIZE_VERSION}_linux_amd64.tar.gz" kz.tgz; tar -xzf kz.tgz -C "$BIN"; }
[ -x "$BIN/kubeconform" ] || { get "https://github.com/yannh/kubeconform/releases/download/v${KUBECONFORM_VERSION}/kubeconform-linux-amd64.tar.gz" kc.tgz; tar -xzf kc.tgz -C "$BIN" kubeconform; }
[ -x "$BIN/kube-linter" ] || { get "https://github.com/stackrox/kube-linter/releases/download/v${KUBE_LINTER_VERSION}/kube-linter-linux.tar.gz" kl.tgz; tar -xzf kl.tgz -C "$BIN"; }
[ -x "$BIN/kind" ] || { get "https://github.com/kubernetes-sigs/kind/releases/download/v${KIND_VERSION}/kind-linux-amd64" "$BIN/kind"; chmod +x "$BIN/kind"; }
if [ ! -x "$DEST/envtest/kube-apiserver" ]; then
  get "https://github.com/kubernetes-sigs/controller-tools/releases/download/envtest-v${ENVTEST_K8S_VERSION}/envtest-v${ENVTEST_K8S_VERSION}-linux-amd64.tar.gz" et.tgz
  mkdir -p envtest && tar -xzf et.tgz --strip-components=2 -C envtest
fi
[ -s crds/keda.yaml ] || get "https://github.com/kedacore/keda/releases/download/v${KEDA_VERSION}/keda-${KEDA_VERSION}-crds.yaml" crds/keda.yaml
[ -s crds/cilium-cnp.yaml ] || get "https://raw.githubusercontent.com/cilium/cilium/v${CILIUM_VERSION}/pkg/k8s/apis/cilium.io/client/crds/v2/ciliumnetworkpolicies.yaml" crds/cilium-cnp.yaml
rm -f ./*.tgz

# checkov in its own virtualenv: its pins (packaging<24) clash with pytest 9.
if [ ! -x "$DEST/checkov/bin/checkov" ]; then
  python3 -m venv "$DEST/checkov"
  "$DEST/checkov/bin/pip" install --quiet -r "$ROOT/requirements-scan.txt"
fi
ln -sf "$DEST/checkov/bin/checkov" "$BIN/checkov"

cat > "$DEST/env.sh" <<EOF
export PATH="$BIN:$DEST/envtest:\$PATH"
export ENVTEST_DIR="$DEST/envtest"
export KEDA_CRDS="$DEST/crds/keda.yaml"
export CILIUM_CRDS="$DEST/crds/cilium-cnp.yaml"
EOF
echo "tools ready. Run: . .tools/env.sh"
