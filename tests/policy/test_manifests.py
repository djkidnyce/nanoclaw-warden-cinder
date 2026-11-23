"""Static guarantees about the rendered manifests. Each test pins down a property the
threat model relies on, so a careless edit fails CI instead of weakening the design."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

from nanoclaw import common as c
from tests.policy.render import OVERLAYS, ROOT, by_kind, pod_specs, render, versions

COMPONENT_SETS = [(), ("gvisor",), ("digest-pinning",), ("pod-creator-lock",),
                  ("gvisor", "digest-pinning", "pod-creator-lock")]
IDENTITIES = {"nanoclaw-agent", "nanoclaw-controller", "nanoclaw-vault", "nanoclaw-redis"}
SECRETS = {"nanoclaw-redis-acl", "nanoclaw-redis-bootstrap", "nanoclaw-redis-controller", "nanoclaw-redis-agent",
           "nanoclaw-redis-vault", "nanoclaw-redis-keda", "nanoclaw-upstream"}


@pytest.fixture(params=OVERLAYS)
def docs(request):
    return render(request.param)


def all_containers(spec):
    return spec.get("initContainers", []) + spec["containers"]


def test_every_overlay_and_component_combination_builds():
    for overlay in ("cilium", "calico"):
        for comps in COMPONENT_SETS:
            assert render(overlay, comps)


def test_pods_meet_restricted_and_more(docs):
    for owner, spec in pod_specs(docs):
        where = f"{owner['kind']}/{owner['metadata']['name']}"
        sc = spec.get("securityContext", {})
        assert sc.get("runAsNonRoot") is True, where
        assert sc.get("seccompProfile", {}).get("type") == "RuntimeDefault", where
        assert spec.get("automountServiceAccountToken") is False, where
        assert spec.get("enableServiceLinks") is False, where
        assert spec.get("serviceAccountName") in IDENTITIES, where
        assert not spec.get("hostNetwork") and not spec.get("hostPID") and not spec.get("hostIPC"), where
        for ctr in all_containers(spec):
            csc = ctr.get("securityContext", {})
            assert csc.get("allowPrivilegeEscalation") is False, f"{where}/{ctr['name']}"
            assert csc.get("readOnlyRootFilesystem") is True, f"{where}/{ctr['name']}"
            assert csc.get("capabilities", {}).get("drop") == ["ALL"], f"{where}/{ctr['name']}"
            assert not csc.get("privileged"), f"{where}/{ctr['name']}"
            res = ctr.get("resources", {})
            for kind in ("requests", "limits"):
                assert {"cpu", "memory"} <= set(res.get(kind, {})), f"{where}/{ctr['name']} {kind}"


def test_no_workload_has_kubernetes_api_permissions(docs):
    kinds = {d["kind"] for d in docs}
    assert not kinds & {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}
    for sa in by_kind(docs, "ServiceAccount"):
        assert sa.get("automountServiceAccountToken") is False, sa["metadata"]["name"]


def test_every_configmap_reference_resolves(docs):
    """Regression: kustomize once left the ScaledJob pointing at an unhashed ConfigMap name."""
    names = {d["metadata"]["name"] for d in by_kind(docs, "ConfigMap")}
    for owner, spec in pod_specs(docs):
        for ctr in all_containers(spec):
            for ref in ctr.get("envFrom", []):
                if "configMapRef" in ref:
                    assert ref["configMapRef"]["name"] in names, owner["metadata"]["name"]
        for vol in spec.get("volumes", []):
            if "configMap" in vol:
                assert vol["configMap"]["name"] in names, owner["metadata"]["name"]


def test_non_root_pods_can_read_their_secrets(docs):
    """Regression from the third kind run: secret files mount as root:<fsGroup> with the volume's mode.
    With 0440 and no fsGroup the group is root, so the app user gets permission denied and crash loops.
    Unit tests never mount secrets, so only the kind cluster caught it."""
    checked = 0
    for owner, spec in pod_specs(docs):
        ctx = spec.get("securityContext", {})
        for vol in spec.get("volumes", []):
            if "secret" not in vol:
                continue
            mode = vol["secret"].get("defaultMode", 0o644)
            if mode & 0o004:
                continue
            checked += 1
            name = f'{owner["metadata"]["name"]}/{vol["name"]}'
            assert mode & 0o040, f"{name}: group can't read"
            assert ctx.get("fsGroup") is not None, f"{name}: mode {oct(mode)} but no fsGroup"
            assert ctx["fsGroup"] == ctx.get("runAsGroup"), f"{name}: fsGroup must match runAsGroup"
    assert checked >= 4


def test_secrets_referenced_are_the_documented_set(docs):
    used = set()
    for _owner, spec in pod_specs(docs):
        used |= {v["secret"]["secretName"] for v in spec.get("volumes", []) if "secret" in v}
        for ctr in all_containers(spec):
            assert not any("secretKeyRef" in e.get("valueFrom", {}) for e in ctr.get("env", [])), \
                "secrets are mounted as files, never injected as environment variables"
    for ta in by_kind(docs, "TriggerAuthentication"):
        used |= {r["name"] for r in ta["spec"]["secretTargetRef"]}
    assert used <= SECRETS
    script = (ROOT / "scripts/gen-secrets.sh").read_text()
    for name in SECRETS:
        assert name in script, f"gen-secrets.sh does not create {name}"


def test_only_the_vault_holds_the_upstream_key(docs):
    for owner, spec in pod_specs(docs):
        mounted = {v["secret"]["secretName"] for v in spec.get("volumes", []) if "secret" in v}
        if "nanoclaw-upstream" in mounted:
            assert owner["metadata"]["name"] == "vault"


def test_images_match_pinned_versions():
    v = versions()
    for overlay in ("cilium", "calico"):
        images = {ctr["image"] for _o, spec in pod_specs(render(overlay)) for ctr in all_containers(spec)}
        assert images == {f"{v['RUNTIME_IMAGE']}:{v['RUNTIME_VERSION']}",
                          f"docker.io/library/redis:{v['REDIS_VERSION']}-alpine"}


def test_admission_allowlist_covers_every_image(docs):
    cm = next(d for d in by_kind(docs, "ConfigMap") if d["metadata"]["name"] == "nanoclaw-admission")
    prefixes = cm["data"]["allowedImagePrefixes"].split(",")
    for _o, spec in pod_specs(docs):
        for ctr in all_containers(spec):
            assert any(ctr["image"].startswith(p + ":") or ctr["image"].startswith(p + "@") for p in prefixes), ctr["image"]


def test_keda_triggers_track_the_code(docs):
    expected = {(s, g) for s, g in c.CONSUMER_GROUPS}
    seen = set()
    for obj in by_kind(docs, "ScaledJob") + by_kind(docs, "ScaledObject"):
        assert obj["spec"].get("minReplicaCount") == 0, obj["metadata"]["name"]
        for trig in obj["spec"]["triggers"]:
            if trig["type"] != "redis-streams":
                continue
            md = trig["metadata"]
            assert "lagCount" in md and md.get("activationLagCount") == "0", \
                "only lagCount lets KEDA scale to zero; it needs activationLagCount"
            assert "pendingEntriesCount" not in md and "streamLength" not in md
            assert trig["authenticationRef"]["name"] == "nanoclaw-redis-keda"
            seen.add((md["stream"], md["consumerGroup"]))
    assert seen == expected


def test_scaled_targets_exist_and_are_not_pinned(docs):
    deployments = {d["metadata"]["name"]: d for d in by_kind(docs, "Deployment")}
    for so in by_kind(docs, "ScaledObject"):
        target = deployments[so["spec"]["scaleTargetRef"]["name"]]
        assert "replicas" not in target["spec"], "a fixed replica count fights KEDA on every apply"


def test_agent_job_limits(docs):
    sj = by_kind(docs, "ScaledJob")[0]["spec"]
    job = sj["jobTargetRef"]
    assert job["activeDeadlineSeconds"] <= 3600
    assert job["backoffLimit"] <= 5
    assert job["ttlSecondsAfterFinished"] > 0
    assert sj["maxReplicaCount"] <= 40
    assert sj["scalingStrategy"]["strategy"] == "accurate", "lag excludes claimed work; default would undercount"
    env = {e["name"]: e for e in job["template"]["spec"]["containers"][0]["env"]}
    assert env["CONSUMER_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == \
        "metadata.labels['batch.kubernetes.io/job-name']", "retried pods must reuse the Job's consumer name"


def test_visibility_timeout_outlives_agent_deadline(docs):
    cfg = next(d for d in by_kind(docs, "ConfigMap") if d["metadata"]["name"].startswith("nanoclaw-config"))["data"]
    deadline = by_kind(docs, "ScaledJob")[0]["spec"]["jobTargetRef"]["activeDeadlineSeconds"]
    if cfg.get("UPSTREAM_URL", "").startswith("http://mock"):
        return  # e2e shortens the timeout on purpose
    assert int(cfg["VISIBILITY_TIMEOUT_SECONDS"]) > deadline, "a live agent must never have its work requeued"
    assert int(cfg["TOKEN_TTL_SECONDS"]) > deadline


def test_cilium_fqdn_matches_upstream_host():
    docs = render("cilium")
    cfg = next(d for d in by_kind(docs, "ConfigMap") if d["metadata"]["name"].startswith("nanoclaw-config"))["data"]
    host = urlparse(cfg["UPSTREAM_URL"]).hostname
    cnp = by_kind(docs, "CiliumNetworkPolicy")[0]
    fqdns = [f["matchName"] for rule in cnp["spec"]["egress"] for f in rule.get("toFQDNs", [])]
    assert fqdns == [host]


def test_bootstrap_script_creates_the_code_groups():
    script = (ROOT / "deploy/base/redis/bootstrap.sh").read_text()
    for stream, group in c.CONSUMER_GROUPS:
        assert f"{stream} {group}" in script


def test_acl_users_match_workload_identities(docs):
    acl = (ROOT / "deploy/base/redis/users.acl.tmpl").read_text()
    users = set(re.findall(r"^user (\w+)", acl, re.M))
    assert users == {"default", "bootstrap", "controller", "agent", "vault", "keda", "client"}
    assert re.search(r"^user default off", acl, re.M)
    assert ">" not in acl, "plaintext passwords must never appear in the ACL file"
    names = set()
    for _o, spec in pod_specs(docs):
        for ctr in all_containers(spec):
            names |= {e["value"] for e in ctr.get("env", []) if e["name"] == "REDIS_USERNAME"}
    assert names <= users


def test_no_service_exposes_the_cluster(docs):
    for svc in by_kind(docs, "Service"):
        assert svc["spec"].get("type", "ClusterIP") == "ClusterIP"


def test_gvisor_component_targets_agents_only():
    docs = render("cilium", ("gvisor",))
    sj = by_kind(docs, "ScaledJob")[0]
    assert sj["spec"]["jobTargetRef"]["template"]["spec"]["runtimeClassName"] == "gvisor"
    assert by_kind(docs, "RuntimeClass")[0]["handler"] == "runsc"


def test_digest_component_turns_on_enforcement():
    docs = render("cilium", ("digest-pinning",))
    cm = next(d for d in by_kind(docs, "ConfigMap") if d["metadata"]["name"] == "nanoclaw-admission")
    assert cm["data"]["requireDigest"] == "true"


def test_pod_creator_lock_component():
    docs = render("cilium", ("pod-creator-lock",))
    names = {d["metadata"]["name"] for d in by_kind(docs, "ValidatingAdmissionPolicy")}
    assert "nanoclaw-pod-creator-lock" in names
    cm = next(d for d in by_kind(docs, "ConfigMap") if d["metadata"]["name"] == "nanoclaw-admission")
    assert "system:serviceaccount:kube-system:job-controller" in cm["data"]["podCreators"].split(",")
    assert "nanoclaw-pod-creator-lock" in {d["metadata"]["name"] for d in by_kind(render("e2e-kind"), "ValidatingAdmissionPolicy")}
