"""Render overlays with kustomize and expose the objects to policy tests."""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
OVERLAYS = ("cilium", "calico", "e2e-kind")


def kustomize_bin() -> str | None:
    return os.environ.get("KUSTOMIZE") or shutil.which("kustomize")


@functools.lru_cache(maxsize=None)
def render(overlay: str, components: tuple[str, ...] = ()) -> list[dict]:
    binary = kustomize_bin()
    if not binary:
        pytest.skip("kustomize not installed")
    target = ROOT / "deploy" / "overlays" / overlay
    if components:
        # Build a throwaway overlay that adds the requested components.
        tmp = ROOT / ".pytest_cache" / f"overlay-{overlay}-{'-'.join(components)}"
        tmp.mkdir(parents=True, exist_ok=True)
        rel = os.path.relpath(target, tmp)
        comps = "\n".join(f"  - {os.path.relpath(ROOT / 'deploy/components' / c, tmp)}" for c in components)
        (tmp / "kustomization.yaml").write_text(
            f"apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n  - {rel}\ncomponents:\n{comps}\n")
        target = tmp
    out = subprocess.run([binary, "build", str(target)], check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def versions() -> dict:
    vals = {}
    for line in (ROOT / "versions.env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k] = v
    return vals


def by_kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def pod_specs(docs):
    """Every pod template in the build, including the one inside the KEDA ScaledJob."""
    for d in docs:
        if d["kind"] in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
            yield d, d["spec"]["template"]["spec"]
        elif d["kind"] == "ScaledJob":
            yield d, d["spec"]["jobTargetRef"]["template"]["spec"]
