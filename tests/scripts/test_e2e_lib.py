"""The kind e2e helpers, driven by a fake kubectl that mimics the real one.

Regression: at zero replicas Kubernetes drops .status.replicas, kubectl prints
nothing (not even a newline), and the old helper turned that into "" instead of
"0", so the scale to zero check could never pass on a real cluster."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

FAKE = r'''#!/usr/bin/env bash
# args: -n ns get deploy NAME -o jsonpath=...  |  -n ns get pods -l LABEL --field-selector=... -o name
args="$*"
case "$args" in
  *"get deploy controller"*) printf '%s' "${FAKE_CONTROLLER_SPEC-}" ;;
  *"get deploy vault"*) printf '%s' "${FAKE_VAULT_SPEC-}" ;;
  *"component=controller"*) [ -n "${FAKE_CONTROLLER_PODS-}" ] && printf '%s\n' $FAKE_CONTROLLER_PODS ;;
  *"component=vault"*) [ -n "${FAKE_VAULT_PODS-}" ] && printf '%s\n' $FAKE_VAULT_PODS ;;
  *"component=agent"*) [ -n "${FAKE_AGENT_PODS-}" ] && printf '%s\n' $FAKE_AGENT_PODS ;;
  *) exit 1 ;;
esac
exit 0
'''


@pytest.fixture
def run(tmp_path):
    k = tmp_path / "kubectl"
    k.write_text(FAKE)
    k.chmod(k.stat().st_mode | stat.S_IEXEC)

    def call(expr: str, **env) -> subprocess.CompletedProcess:
        script = f'set -euo pipefail; NS=nanoclaw; . "{ROOT}/tests/e2e/lib.sh"; {expr}'
        e = {"PATH": f"{tmp_path}:{os.environ['PATH']}", "WAIT_INTERVAL": "0"}
        e.update(env)
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e, timeout=30)
    return call


def test_empty_output_counts_as_zero(run):
    """Exactly what the real kubectl does when KEDA has scaled a Deployment away."""
    r = run('echo "[$(replicas controller)]"', FAKE_CONTROLLER_SPEC="")
    assert r.stdout.strip() == "[0]"


def test_scaled_to_zero_is_detected(run):
    r = run("is_zero && echo yes || echo no", FAKE_CONTROLLER_SPEC="0", FAKE_VAULT_SPEC="")
    assert r.stdout.strip() == "yes", r.stderr


@pytest.mark.parametrize("env", [
    {"FAKE_CONTROLLER_SPEC": "1"},
    {"FAKE_VAULT_SPEC": "2"},
    {"FAKE_AGENT_PODS": "pod/agent-a pod/agent-b"},
    {"FAKE_VAULT_PODS": "pod/vault-x"},          # spec says 0 but a pod is still running
    {"FAKE_CONTROLLER_PODS": "pod/controller-x"},
])
def test_anything_still_up_is_not_zero(run, env):
    r = run("is_zero && echo yes || echo no", **env)
    assert r.stdout.strip() == "no"


def test_agent_count(run):
    assert run("agents", FAKE_AGENT_PODS="pod/a pod/b pod/c").stdout.strip() == "3"
    assert run("agents").stdout.strip() == "0"


def test_wait_for_passes_and_times_out(run):
    ok = run('wait_for 5 "zero" is_zero')
    assert ok.returncode == 0 and "ok: zero" in ok.stdout
    bad = run('wait_for 0 "zero" is_zero', FAKE_CONTROLLER_SPEC="1")
    assert bad.returncode == 1 and "timed out waiting for: zero" in bad.stderr


def test_e2e_script_uses_the_library():
    text = (ROOT / "tests/e2e/kind_e2e.sh").read_text()
    assert '. "$ROOT/tests/e2e/lib.sh"' in text
    assert "replicas() {" not in text and "is_zero() {" not in text
