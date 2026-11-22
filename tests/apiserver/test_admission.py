"""Real API server tests: the manifests install cleanly, and admission, Pod Security and
RBAC refuse what the threat model says they must refuse."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest
import yaml

from tests.apiserver.cluster import Cluster, envtest_dir
from tests.policy.render import by_kind, render

pytestmark = pytest.mark.apiserver
KEDA_SA = "system:serviceaccount:keda:keda-operator"
NS = "nanoclaw"


@pytest.fixture(scope="module")
def cluster():
    bin_dir = envtest_dir()
    crds = [os.environ.get("KEDA_CRDS"), os.environ.get("CILIUM_CRDS")]
    if not bin_dir or not all(p and Path(p).exists() for p in crds):
        pytest.skip("envtest binaries or CRD files not available (set ENVTEST_DIR, KEDA_CRDS, CILIUM_CRDS)")
    cl = Cluster(bin_dir).start()
    try:
        for path in crds:
            cl.kubectl("apply", "--server-side", "-f", path)
        cl.kubectl("wait", "--for=condition=Established", "--timeout=60s", "crd/scaledjobs.keda.sh",
                   "crd/scaledobjects.keda.sh", "crd/triggerauthentications.keda.sh",
                   "crd/ciliumnetworkpolicies.cilium.io")
        # Stand in for the RBAC the KEDA Helm chart grants its operator.
        cl.kubectl("create", "namespace", "keda")
        cl.kubectl("create", "clusterrole", "keda-jobs-test", "--verb=create,get,list,delete", "--resource=jobs.batch")
        cl.kubectl("create", "clusterrolebinding", "keda-jobs-test", "--clusterrole=keda-jobs-test",
                   "--serviceaccount=keda:keda-operator")
        yield cl
    finally:
        cl.stop()


def apply_docs(cl: Cluster, docs, *extra):
    ordered = sorted(docs, key=lambda d: 0 if d["kind"] == "Namespace" else 1)
    return cl.kubectl("apply", "--server-side", "--field-manager=test", "-f", "-", *extra,
                      stdin=yaml.safe_dump_all(ordered))


@pytest.fixture(scope="module")
def installed(cluster):
    docs = render("cilium")
    out = apply_docs(cluster, docs)
    for line in out.stdout.splitlines():
        assert "serverside-applied" in line or "unchanged" in line, line
    # A real cluster's controller manager creates this; here nothing runs, so make it by hand.
    cluster.kubectl("create", "serviceaccount", "default", "-n", NS)
    return docs


def agent_pod(docs) -> dict:
    sj = by_kind(docs, "ScaledJob")[0]
    tmpl = copy.deepcopy(sj["spec"]["jobTargetRef"]["template"])
    tmpl["metadata"]["labels"]["batch.kubernetes.io/job-name"] = "nanoclaw-agent-test"
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"generateName": "agent-", "namespace": NS, "labels": tmpl["metadata"]["labels"]},
            "spec": tmpl["spec"]}


def agent_job(docs) -> dict:
    sj = by_kind(docs, "ScaledJob")[0]
    spec = copy.deepcopy(sj["spec"]["jobTargetRef"])
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"generateName": "nanoclaw-agent-", "namespace": NS}, "spec": spec}


def create(cl, obj, as_user=None):
    return cl.kubectl("create", "-f", "-", stdin=json.dumps(obj), check=False, as_user=as_user)


def test_every_object_is_accepted_by_the_api_server(installed, cluster):
    kinds = cluster.kubectl("get", "scaledjobs,scaledobjects,triggerauthentications,ciliumnetworkpolicies,"
                            "networkpolicies,deployments,services,configmaps,serviceaccounts,resourcequotas,limitranges",
                            "-n", NS, "-o", "name").stdout.split()
    assert len(kinds) >= 25
    assert cluster.kubectl("get", "validatingadmissionpolicies", "-o", "name").stdout.count("nanoclaw-") == 2


def test_calico_overlay_validates_server_side(installed, cluster):
    apply_docs(cluster, render("calico"), "--dry-run=server")


def test_e2e_overlay_validates_server_side(installed, cluster):
    apply_docs(cluster, render("e2e-kind"), "--dry-run=server")


def test_keda_schema_rejects_a_broken_scaledjob(installed, cluster):
    sj = copy.deepcopy(by_kind(installed, "ScaledJob")[0])
    sj["spec"]["jobTargetRef"] = "not an object"
    r = cluster.kubectl("apply", "--dry-run=server", "-f", "-", stdin=json.dumps(sj), check=False)
    assert r.returncode != 0


class TestJobGuard:
    def test_keda_operator_can_create_agent_jobs(self, installed, cluster):
        r = create(cluster, agent_job(installed), as_user=KEDA_SA)
        assert r.returncode == 0, r.stderr

    def test_cluster_admin_cannot_create_jobs(self, installed, cluster):
        r = create(cluster, agent_job(installed))
        assert r.returncode != 0
        assert "may only be created by" in r.stderr

    @pytest.mark.parametrize("mutate,expect", [
        (lambda j: j["spec"]["template"]["spec"].__setitem__("serviceAccountName", "default"), "nanoclaw-agent"),
        (lambda j: j["spec"]["template"]["spec"].__setitem__("automountServiceAccountToken", True), "token"),
        (lambda j: j["spec"].pop("activeDeadlineSeconds"), "activeDeadlineSeconds"),
    ])
    def test_unsafe_agent_jobs_are_refused_even_for_keda(self, installed, cluster, mutate, expect):
        job = agent_job(installed)
        mutate(job)
        r = create(cluster, job, as_user=KEDA_SA)
        assert r.returncode != 0 and expect in r.stderr, r.stderr


class TestPodGuard:
    def test_agent_pod_from_template_is_admitted(self, installed, cluster):
        r = create(cluster, agent_pod(installed))
        assert r.returncode == 0, r.stderr

    def test_redis_controller_and_vault_pods_are_admitted(self, installed, cluster):
        for dep in by_kind(installed, "Deployment"):
            pod = {"apiVersion": "v1", "kind": "Pod",
                   "metadata": {"generateName": f"{dep['metadata']['name']}-", "namespace": NS,
                                "labels": dep["spec"]["template"]["metadata"]["labels"]},
                   "spec": dep["spec"]["template"]["spec"]}
            r = create(cluster, pod)
            assert r.returncode == 0, f"{dep['metadata']['name']}: {r.stderr}"

    @pytest.mark.parametrize("image", [
        "docker.io/library/busybox:1.37",
        "ghcr.io/djkidnyce/nanoclaw-warden-cinder/runtime-evil:0.2.0",   # prefix trick
        "evil.example/ghcr.io/djkidnyce/nanoclaw-warden-cinder/runtime:0.2.0",
    ])
    def test_unapproved_images_are_refused(self, installed, cluster, image):
        pod = agent_pod(installed)
        pod["spec"]["containers"][0]["image"] = image
        r = create(cluster, pod)
        assert r.returncode != 0 and "allow list" in r.stderr, r.stderr

    def test_identity_and_token_rules(self, installed, cluster):
        pod = agent_pod(installed)
        pod["spec"]["serviceAccountName"] = "default"
        assert "service accounts" in create(cluster, pod).stderr
        pod = agent_pod(installed)
        pod["spec"]["automountServiceAccountToken"] = True
        assert "token" in create(cluster, pod).stderr

    def test_pod_security_restricted_is_enforced(self, installed, cluster):
        pod = agent_pod(installed)
        pod["spec"]["containers"][0]["securityContext"] = {"privileged": True}
        r = create(cluster, pod)
        assert r.returncode != 0, r.stderr
        pod = agent_pod(installed)
        pod["spec"]["securityContext"].pop("runAsNonRoot")
        pod["spec"]["securityContext"].pop("runAsUser")
        pod["spec"]["containers"][0]["securityContext"].pop("capabilities")
        r = create(cluster, pod)
        assert r.returncode != 0 and "restricted" in r.stderr, r.stderr

    def test_ephemeral_debug_containers_are_refused(self, installed, cluster):
        name = cluster.kubectl("create", "-f", "-", "-o", "jsonpath={.metadata.name}",
                               stdin=json.dumps(agent_pod(installed))).stdout
        patch = {"spec": {"ephemeralContainers": [{
            "name": "debug", "image": "ghcr.io/djkidnyce/nanoclaw-warden-cinder/runtime:0.2.0",
            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
                                "runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}}]}}
        r = cluster.kubectl("patch", "pod", name, "-n", NS, "--subresource=ephemeralcontainers",
                            "--type=strategic", "-p", json.dumps(patch), check=False)
        assert r.returncode != 0 and "ephemeral" in r.stderr, r.stderr

    def test_digest_pinning_component_enforces_digests(self, installed, cluster):
        cluster.kubectl("patch", "configmap", "nanoclaw-admission", "-n", NS, "--type=merge",
                        "-p", '{"data":{"requireDigest":"true"}}')
        try:
            r = create(cluster, agent_pod(installed))
            assert r.returncode != 0 and "digest" in r.stderr, r.stderr
            pod = agent_pod(installed)
            pod["spec"]["containers"][0]["image"] = (
                "ghcr.io/djkidnyce/nanoclaw-warden-cinder/runtime@sha256:" + "a" * 64)
            r = create(cluster, pod)
            assert r.returncode == 0, r.stderr
        finally:
            cluster.kubectl("patch", "configmap", "nanoclaw-admission", "-n", NS, "--type=merge",
                            "-p", '{"data":{"requireDigest":"false"}}')

    def test_missing_parameters_fail_closed(self, installed, cluster):
        saved = cluster.kubectl("get", "configmap", "nanoclaw-admission", "-n", NS, "-o", "json").stdout
        cluster.kubectl("delete", "configmap", "nanoclaw-admission", "-n", NS)
        try:
            r = create(cluster, agent_pod(installed))
            assert r.returncode != 0, "with its parameters gone the policy must deny, not allow"
        finally:
            obj = json.loads(saved)
            for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"):
                obj["metadata"].pop(k, None)
            cluster.kubectl("create", "-f", "-", stdin=json.dumps(obj))


@pytest.mark.parametrize("sa", ["nanoclaw-agent", "nanoclaw-controller", "nanoclaw-vault", "nanoclaw-redis"])
def test_workload_identities_have_no_api_access(installed, cluster, sa):
    user = f"system:serviceaccount:{NS}:{sa}"
    for verb, res in (("get", "secrets"), ("list", "pods"), ("create", "jobs"), ("get", "configmaps"),
                      ("create", "pods/exec"), ("list", "scaledjobs.keda.sh"), ("get", "nodes")):
        r = cluster.kubectl("auth", "can-i", verb, res, "-n", NS, "--as", user, check=False)
        assert r.stdout.strip() == "no", f"{sa} can {verb} {res}"


class TestPodCreatorLock:
    """Runs last: applies the optional lock, proves it, then removes it."""

    def test_only_controllers_may_create_pods(self, installed, cluster):
        lock = render("cilium", ("pod-creator-lock",))
        apply_docs(cluster, [d for d in lock if d["kind"] in ("ValidatingAdmissionPolicy",
                    "ValidatingAdmissionPolicyBinding") or d["metadata"].get("name") == "nanoclaw-admission"])
        try:
            import time
            deadline = time.time() + 30          # new policies take a moment to activate
            while True:
                r = create(cluster, agent_pod(installed))
                if r.returncode != 0 or time.time() > deadline:
                    break
                time.sleep(1)
            assert r.returncode != 0 and "Job and ReplicaSet controllers" in r.stderr, r.stderr
            # Controllers need to read service accounts to create pods; mimic the bootstrap roles.
            for ident in ("job-controller", "replicaset-controller"):
                cluster.kubectl("create", "clusterrolebinding", f"test-{ident}", "--clusterrole=cluster-admin",
                                f"--serviceaccount=kube-system:{ident}", check=False)
                r = create(cluster, agent_pod(installed), as_user=f"system:serviceaccount:kube-system:{ident}")
                assert r.returncode == 0, f"{ident}: {r.stderr}"
        finally:
            cluster.kubectl("delete", "validatingadmissionpolicybinding", "nanoclaw-pod-creator-lock", check=False)
