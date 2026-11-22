"""versions.env is the single source of truth. Anything that repeats a version must agree with it."""

from __future__ import annotations

import re

import yaml

import nanoclaw
from tests.policy.render import ROOT, versions

V = versions()


def test_dockerfile_python_matches():
    assert f"ARG PYTHON_VERSION={V['PYTHON_VERSION']}" in (ROOT / "Dockerfile").read_text()


def test_python_dependency_matches():
    assert f"redis=={V['REDIS_PY_VERSION']}" in (ROOT / "requirements.txt").read_text()
    assert f'"redis=={V["REDIS_PY_VERSION"]}"' in (ROOT / "pyproject.toml").read_text()


def test_runtime_version_is_consistent():
    assert nanoclaw.__version__ == V["RUNTIME_VERSION"]
    assert f'version = "{V["RUNTIME_VERSION"]}"' in (ROOT / "pyproject.toml").read_text()
    k = yaml.safe_load((ROOT / "deploy/base/kustomization.yaml").read_text())
    images = {i["name"]: i for i in k["images"]}
    assert images["nanoclaw-runtime"]["newName"] == V["RUNTIME_IMAGE"]
    assert images["nanoclaw-runtime"]["newTag"] == V["RUNTIME_VERSION"]
    assert images["redis"]["newTag"] == f"{V['REDIS_VERSION']}-alpine"


def test_docs_quote_current_versions():
    for doc in ("README.md", "docs/QUICKSTART.md", "docs/HOW_IT_WORKS.md"):
        text = (ROOT / doc).read_text()
        for key in ("KEDA_VERSION", "CILIUM_VERSION", "CALICO_VERSION", "REDIS_VERSION"):
            assert V[key] in text, f"{doc} does not mention {key}={V[key]}"


def test_every_action_is_pinned_to_a_commit():
    for wf in (ROOT / ".github/workflows").glob("*.yml"):
        for line in wf.read_text().splitlines():
            m = re.search(r"uses:\s*([^\s#]+)", line)
            if m:
                ref = m.group(1).split("@")
                assert len(ref) == 2 and re.fullmatch(r"[0-9a-f]{40}", ref[1]), f"{wf.name}: {line.strip()}"


def test_scripts_do_not_hardcode_versions():
    for script in ("install.sh", "preflight.sh", "fetch-tools.sh", "pin-digests.sh"):
        text = (ROOT / "scripts" / script).read_text()
        for key in ("KEDA_VERSION", "CILIUM_VERSION", "CALICO_VERSION"):
            assert V[key] not in text, f"{script} hardcodes {key}; source versions.env instead"
