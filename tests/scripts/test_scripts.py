"""Shell scripts under test with fake kubectl and helm binaries on PATH.

Each scenario describes a cluster: its version, which CNI daemonset exists, which KEDA
image is installed. The fakes answer the exact queries the scripts make and log every
mutating call, so the tests can assert what would have changed."""

from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

FAKE_KUBECTL = r'''#!/usr/bin/env bash
# Fake kubectl driven by environment variables. Logs every call to $FAKE_LOG.
echo "kubectl $*" >> "$FAKE_LOG"
args="$*"
case "$args" in
  "version --client -o json") echo '{"clientVersion":{"gitVersion":"v1.37.0"}}' ;;
  "version -o json")
    [ "${FAKE_REACHABLE:-1}" = 1 ] || exit 1
    printf '{"clientVersion":{"gitVersion":"v1.37.0"},\n "serverVersion": {"gitVersion": "%s"}}\n' "${FAKE_SERVER:-v1.37.2}" ;;
  "api-resources --api-group=admissionregistration.k8s.io")
    echo "NAME SHORTNAMES APIVERSION NAMESPACED KIND"
    [ "${FAKE_VAP:-1}" = 1 ] && echo "validatingadmissionpolicies v1 false ValidatingAdmissionPolicy" ;;
  "get daemonsets -A -o jsonpath="*)
    if [[ "$args" == *"{.spec.template"* ]]; then
      [ -n "${FAKE_CNI:-}" ] && printf '%s\n' "$FAKE_CNI"
    else
      [ -n "${FAKE_CNI:-}" ] && printf '%s\n' "$FAKE_CNI" | sed 's#^[^/]*/##; s#=.*##'
    fi ;;
  "get crd scaledjobs.keda.sh") [ -n "${FAKE_KEDA:-}" ] || exit 1 ;;
  "get deploy -n keda keda-operator -o jsonpath="*) printf '%s' "${FAKE_KEDA:-}" ;;
  "get ns nanoclaw") [ "${FAKE_NS:-0}" = 1 ] || exit 1 ;;
  "-n nanoclaw get secret "*) [ "${FAKE_SECRETS:-0}" = 1 ] || exit 1 ;;
  "get runtimeclass gvisor") exit 1 ;;
  "create secret generic "*) shift 3; name="$1"; echo "apiVersion: v1"; echo "kind: Secret"; echo "metadata: {name: $name}" ;;
  "apply -f -") cat > /dev/null; echo applied ;;
  *) echo "unexpected kubectl call: $args" >&2; exit 99 ;;
esac
'''

FAKE_HELM = r'''#!/usr/bin/env bash
echo "helm $*" >> "$FAKE_LOG"
case "$1" in version) echo "v3.19.0" ;; *) exit 0 ;; esac
'''


@pytest.fixture
def fakebin(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("kubectl", FAKE_KUBECTL), ("helm", FAKE_HELM)):
        p = bindir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.touch()

    def run(script, *args, **env):
        e = {"PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_LOG": str(log), "HOME": str(tmp_path)}
        e.update({k: str(v) for k, v in env.items()})
        r = subprocess.run(["bash", str(ROOT / "scripts" / script), *args], env=e, capture_output=True,
                           text=True, cwd=tmp_path, timeout=60)
        return r, log.read_text()
    return run


CILIUM = "kube-system/cilium=quay.io/cilium/cilium:v1.20.5"
CALICO = "calico-system/calico-node=quay.io/calico/node:v3.33.1"
FLANNEL = "kube-flannel/kube-flannel-ds=ghcr.io/flannel-io/flannel:v0.27.0"
KEDA_OK = "ghcr.io/kedacore/keda:2.21.1"
KEDA_OLD = "ghcr.io/kedacore/keda:2.20.2"


class TestPreflight:
    def test_ready_cluster_passes(self, fakebin):
        r, calls = fakebin("preflight.sh", FAKE_CNI=CILIUM, FAKE_KEDA=KEDA_OK, FAKE_NS=1, FAKE_SECRETS=1)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Result: ready" in r.stdout and "Cilium v1.20.5" in r.stdout

    def test_calico_in_calico_system_is_detected(self, fakebin):
        """Regression: the old preflight only looked in kube-system and missed operator installs."""
        r, _ = fakebin("preflight.sh", FAKE_CNI=CALICO, FAKE_KEDA=KEDA_OK)
        assert r.returncode == 0, r.stdout
        assert "Calico v3.33.1 found" in r.stdout

    def test_flannel_alone_fails(self, fakebin):
        r, _ = fakebin("preflight.sh", FAKE_CNI=FLANNEL, FAKE_KEDA=KEDA_OK)
        assert r.returncode == 1 and "Flannel alone" in r.stdout

    def test_old_keda_fails_with_cve(self, fakebin):
        r, _ = fakebin("preflight.sh", FAKE_CNI=CILIUM, FAKE_KEDA=KEDA_OLD)
        assert r.returncode == 1 and "CVE-2026-77524" in r.stdout

    def test_old_kubernetes_fails(self, fakebin):
        r, _ = fakebin("preflight.sh", FAKE_CNI=CILIUM, FAKE_KEDA=KEDA_OK, FAKE_SERVER="v1.29.4")
        assert r.returncode == 1 and "older than" in r.stdout

    def test_missing_vap_api_fails(self, fakebin):
        r, _ = fakebin("preflight.sh", FAKE_CNI=CILIUM, FAKE_KEDA=KEDA_OK, FAKE_VAP=0)
        assert r.returncode == 1 and "ValidatingAdmissionPolicy API missing" in r.stdout

    def test_unreachable_cluster(self, fakebin):
        r, _ = fakebin("preflight.sh", FAKE_REACHABLE=0)
        assert r.returncode == 1 and "cannot reach" in r.stdout

    def test_preflight_never_mutates(self, fakebin):
        _, calls = fakebin("preflight.sh", FAKE_CNI=CILIUM, FAKE_KEDA=KEDA_OK, FAKE_NS=1, FAKE_SECRETS=1)
        for verb in (" apply", " create", " delete", " patch", "helm install", "helm upgrade"):
            assert verb not in calls


class TestInstall:
    def test_existing_cni_is_never_replaced(self, fakebin):
        r, calls = fakebin("install.sh", "--cni", "calico", "--yes", FAKE_CNI=CILIUM, FAKE_KEDA=KEDA_OK)
        assert r.returncode == 0, r.stderr
        assert "Leaving the CNI alone" in r.stdout
        assert "helm install calico" not in calls and "helm install cilium" not in calls

    def test_cilium_requires_api_host(self, fakebin):
        r, calls = fakebin("install.sh", "--cni", "cilium", "--yes")
        assert r.returncode == 2 and "--k8s-api-host" in r.stderr
        assert "helm install" not in calls

    def test_cilium_install_pins_version_and_api(self, fakebin):
        r, calls = fakebin("install.sh", "--cni", "cilium", "--k8s-api-host", "10.0.0.10", "--yes")
        assert r.returncode == 0, r.stderr
        assert "helm install cilium cilium/cilium --version 1.20.5" in calls
        assert "k8sServiceHost=10.0.0.10" in calls
        assert "helm install keda kedacore/keda --version 2.21.1" in calls

    def test_cilium_wireguard_is_opt_in(self, fakebin):
        _, calls = fakebin("install.sh", "--cni", "cilium", "--k8s-api-host", "10.0.0.10", "--yes")
        assert "encryption.enabled" not in calls
        r, calls = fakebin("install.sh", "--cni", "cilium", "--k8s-api-host", "10.0.0.10", "--encrypt", "--yes")
        assert r.returncode == 0, r.stderr
        assert "encryption.enabled=true --set encryption.type=wireguard" in calls

    def test_calico_install_pins_version(self, fakebin):
        r, calls = fakebin("install.sh", "--cni", "calico", "--yes")
        assert r.returncode == 0, r.stderr
        assert "projectcalico/tigera-operator --version v3.33.1" in calls

    def test_no_cni_and_none_requested_stops(self, fakebin):
        r, calls = fakebin("install.sh", "--yes")
        assert r.returncode == 1 and "helm install" not in calls

    def test_old_keda_blocks_until_upgrade_flag(self, fakebin):
        r, calls = fakebin("install.sh", "--yes", FAKE_CNI=CILIUM, FAKE_KEDA=KEDA_OLD)
        assert r.returncode == 1 and "--upgrade-keda" in r.stderr and "helm upgrade" not in calls
        r, calls = fakebin("install.sh", "--yes", "--upgrade-keda", FAKE_CNI=CILIUM, FAKE_KEDA=KEDA_OLD)
        assert r.returncode == 0 and "helm upgrade keda kedacore/keda --version 2.21.1" in calls

    def test_current_keda_is_left_alone(self, fakebin):
        r, calls = fakebin("install.sh", "--yes", FAKE_CNI=CILIUM, FAKE_KEDA=KEDA_OK)
        assert r.returncode == 0 and "helm" not in calls.replace("helm version", "")

    def test_dry_run_changes_nothing(self, fakebin):
        r, calls = fakebin("install.sh", "--cni", "calico", "--dry-run")
        assert r.returncode == 0
        assert "+ helm install calico" in r.stdout
        assert "helm install" not in calls and "helm repo" not in calls

    def test_prompt_declined_changes_nothing(self, fakebin, tmp_path):
        e = {"FAKE_LOG": str(tmp_path / "calls.log")}
        r = subprocess.run(["bash", str(ROOT / "scripts/install.sh"), "--cni", "calico"], input="n\nn\n",
                           capture_output=True, text=True,
                           env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", **e})
        assert r.returncode == 0
        assert "helm install" not in (tmp_path / "calls.log").read_text()


class TestGenSecrets:
    def test_output_mode_renders_all_secrets_and_a_loadable_acl(self, fakebin, tmp_path):
        key = tmp_path / "api.key"
        key.write_text("sk-test-123\n")
        out = tmp_path / "secrets"
        # No kubectl on PATH for this run: exercise the built in renderer.
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
        r = subprocess.run(["bash", str(ROOT / "scripts/gen-secrets.sh"), "--api-key-file", str(key), "--output",
                            str(out), "--client-pass-file", str(tmp_path / "client.pass")],
                           capture_output=True, text=True, env=env, cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        files = sorted(p.name for p in out.iterdir())
        assert files == sorted(f"{n}.yaml" for n in ("nanoclaw-redis-acl", "nanoclaw-redis-bootstrap",
                               "nanoclaw-redis-controller", "nanoclaw-redis-agent", "nanoclaw-redis-vault",
                               "nanoclaw-redis-keda", "nanoclaw-upstream"))
        assert oct((tmp_path / "client.pass").stat().st_mode & 0o777) == "0o600"
        import base64
        import yaml
        acl = base64.b64decode(yaml.safe_load((out / "nanoclaw-redis-acl.yaml").read_text())["data"]["users.acl"]).decode()
        assert "{{" not in acl and ">" not in acl
        key_b64 = yaml.safe_load((out / "nanoclaw-upstream.yaml").read_text())["data"]["api-key"]
        assert base64.b64decode(key_b64).decode() == "sk-test-123", "trailing newline must be stripped"
        assert "sk-test-123" not in r.stdout + r.stderr, "the key must never be echoed"

        # The rendered ACL loads in a real Redis and the client password authenticates.
        binary = os.environ.get("REDIS_SERVER") or shutil.which("redis-server")
        if not binary:
            pytest.skip("redis-server not installed")
        (tmp_path / "users.acl").write_text(acl)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        proc = subprocess.Popen([binary, "--port", str(port), "--bind", "127.0.0.1", "--aclfile",
                                 str(tmp_path / "users.acl"), "--save", ""], stdout=subprocess.DEVNULL)
        try:
            import redis
            for _ in range(100):
                try:
                    socket.create_connection(("127.0.0.1", port), 0.2).close()
                    break
                except OSError:
                    time.sleep(0.05)
            client = redis.Redis(port=port, username="client", password=(tmp_path / "client.pass").read_text())
            assert client.ping()
        finally:
            proc.terminate()

    def test_requires_key_file(self, fakebin):
        r, calls = fakebin("gen-secrets.sh")
        assert r.returncode == 2 and "--api-key-file" in r.stderr and "apply" not in calls

    def test_apply_mode_uses_kubectl(self, fakebin, tmp_path):
        key = tmp_path / "k"
        key.write_text("abc")
        r, calls = fakebin("gen-secrets.sh", "--api-key-file", str(key))
        assert r.returncode == 0, r.stderr
        assert calls.count("kubectl apply -f -") == 7
