"""Evaluate the rendered NetworkPolicies (and the CiliumNetworkPolicy) with Kubernetes
semantics and prove the full reachability matrix. This is the regression test for the
original bugs: agents reaching the internet, and default deny blocking the system itself."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

import pytest

from tests.policy.render import by_kind, render


@dataclass(frozen=True)
class Peer:
    name: str
    namespace: str | None = None          # None means outside the cluster
    labels: tuple = ()
    ip: str | None = None
    fqdn: str | None = None

    @property
    def label_map(self):
        return dict(self.labels)


def pod(name, ns, **labels):
    return Peer(name, ns, tuple(sorted(labels.items())))


NS_LABELS = {
    "nanoclaw": {"kubernetes.io/metadata.name": "nanoclaw"},
    "keda": {"kubernetes.io/metadata.name": "keda"},
    "kube-system": {"kubernetes.io/metadata.name": "kube-system"},
    "default": {"kubernetes.io/metadata.name": "default"},
}
C = "app.kubernetes.io/component"
PEERS = {
    "controller": pod("controller", "nanoclaw", **{C: "controller"}),
    "agent": pod("agent", "nanoclaw", **{C: "agent"}),
    "agent2": pod("agent2", "nanoclaw", **{C: "agent"}),
    "vault": pod("vault", "nanoclaw", **{C: "vault"}),
    "redis": pod("redis", "nanoclaw", **{C: "redis"}),
    "keda": pod("keda-operator", "keda", app="keda-operator"),
    "keda-metrics": pod("keda-metrics", "keda", app="keda-operator-metrics-apiserver"),
    "dns": pod("coredns", "kube-system", **{"k8s-app": "kube-dns"}),
    "other": pod("intruder", "default", app="intruder"),
    "api": Peer("upstream-api", ip="160.79.104.10", fqdn="api.anthropic.com"),
    "evil": Peer("evil", ip="203.0.113.5", fqdn="exfil.example.com"),
    "metadata": Peer("cloud-metadata", ip="169.254.169.254", fqdn="metadata.internal"),
}


def match_labels(selector: dict | None, labels: dict) -> bool:
    if selector is None:
        return False
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        val = labels.get(expr["key"])
        op = expr["operator"]
        if op == "In" and val not in expr["values"]:
            return False
        if op == "NotIn" and val in expr["values"]:
            return False
        if op == "Exists" and expr["key"] not in labels:
            return False
        if op == "DoesNotExist" and expr["key"] in labels:
            return False
    return True


def peer_matches(rule_peer: dict, policy_ns: str, target: Peer) -> bool:
    if "ipBlock" in rule_peer:
        if not target.ip:
            return False
        block = rule_peer["ipBlock"]
        net = ipaddress.ip_network(block["cidr"])
        addr = ipaddress.ip_address(target.ip)
        if addr.version != net.version or addr not in net:
            return False
        return not any(addr in ipaddress.ip_network(e) for e in block.get("except", []))
    if target.namespace is None:
        return False
    ns_sel = rule_peer.get("namespaceSelector")
    pod_sel = rule_peer.get("podSelector")
    if ns_sel is not None:
        if not match_labels(ns_sel, NS_LABELS[target.namespace]):
            return False
    elif target.namespace != policy_ns:
        return False
    return pod_sel is None or match_labels(pod_sel, target.label_map)


def port_matches(ports: list | None, port: int, proto: str) -> bool:
    if not ports:
        return True
    return any(int(p.get("port", port)) == port and p.get("protocol", "TCP") in (proto, "ANY") for p in ports)


def k8s_allows(policies, direction, subject: Peer, other: Peer, port, proto):
    """Returns None when no policy selects the subject for this direction (no isolation)."""
    if subject.namespace is None:
        return None
    selecting = [p for p in policies
                 if p["metadata"]["namespace"] == subject.namespace
                 and match_labels(p["spec"]["podSelector"] or {}, subject.label_map)
                 and direction.capitalize() in p["spec"].get("policyTypes", ["Ingress"])]
    if not selecting:
        return None
    key, peer_key = ("egress", "to") if direction == "egress" else ("ingress", "from")
    for p in selecting:
        for rule in p["spec"].get(key) or []:
            peers = rule.get(peer_key)
            peer_ok = not peers or any(peer_matches(rp, p["metadata"]["namespace"], other) for rp in peers)
            if peer_ok and port_matches(rule.get("ports"), port, proto):
                return True
    return False


def cilium_allows(cnps, subject: Peer, other: Peer, port, proto):
    selecting = [p for p in cnps if p["metadata"]["namespace"] == subject.namespace
                 and match_labels(p["spec"]["endpointSelector"], subject.label_map)]
    if not selecting:
        return None
    for p in selecting:
        for rule in p["spec"].get("egress", []):
            ports = [pp for tp in rule.get("toPorts", []) for pp in tp.get("ports", [])]
            if not port_matches(ports, port, proto):
                continue
            for fq in rule.get("toFQDNs", []):
                if other.fqdn and other.fqdn == fq.get("matchName"):
                    return True
            for ep in rule.get("toEndpoints", []):
                labels = dict(ep.get("matchLabels", {}))
                ns = labels.pop("k8s:io.kubernetes.pod.namespace", p["metadata"]["namespace"])
                if other.namespace == ns and match_labels({"matchLabels": labels}, other.label_map):
                    return True
    return False


def allowed(docs, src: Peer, dst: Peer, port: int, proto: str = "TCP") -> bool:
    nps = by_kind(docs, "NetworkPolicy")
    cnps = by_kind(docs, "CiliumNetworkPolicy")
    eg = [r for r in (k8s_allows(nps, "egress", src, dst, port, proto), cilium_allows(cnps, src, dst, port, proto))
          if r is not None]
    egress_ok = True if not eg else any(eg)
    ing = k8s_allows(nps, "ingress", dst, src, port, proto)
    ingress_ok = True if ing is None else ing
    return egress_ok and ingress_ok


# (source, destination, port, expected)
MATRIX = [
    ("controller", "redis", 6379, True),
    ("controller", "vault", 8200, False),
    ("controller", "api", 443, False),
    ("controller", "dns", 53, True),
    ("agent", "redis", 6379, True),
    ("agent", "vault", 8200, True),
    ("agent", "dns", 53, True),
    ("agent", "api", 443, False),           # original bug: agents could reach the internet
    ("agent", "evil", 443, False),
    ("agent", "metadata", 80, False),
    ("agent", "agent2", 8200, False),
    ("agent", "controller", 8080, False),
    ("agent", "redis", 6380, False),
    ("vault", "redis", 6379, True),
    ("vault", "evil", 443, False),
    ("vault", "metadata", 80, False),
    ("vault", "agent", 8200, False),
    ("vault", "api", 80, False),
    ("redis", "api", 443, False),
    ("redis", "vault", 8200, False),
    ("keda", "redis", 6379, True),          # original bug: KEDA could not read the stream
    ("keda", "vault", 8200, False),
    ("keda-metrics", "redis", 6379, False),  # only the operator pod, not its neighbors
    ("other", "redis", 6379, False),
    ("other", "vault", 8200, False),
    ("other", "agent", 8200, False),
]


@pytest.mark.parametrize("overlay", ["cilium", "calico"])
@pytest.mark.parametrize("src,dst,port,expected", MATRIX)
def test_matrix(overlay, src, dst, port, expected):
    assert allowed(render(overlay), PEERS[src], PEERS[dst], port) is expected


@pytest.mark.parametrize("overlay", ["cilium", "calico"])
def test_vault_reaches_upstream_and_only_upstream(overlay):
    docs = render(overlay)
    assert allowed(docs, PEERS["vault"], PEERS["api"], 443)
    assert not allowed(docs, PEERS["vault"], PEERS["evil"], 443)


def test_dns_over_udp_allowed_for_cilium_proxy():
    docs = render("cilium")
    assert allowed(docs, PEERS["vault"], PEERS["dns"], 53, "UDP")
    assert allowed(docs, PEERS["agent"], PEERS["dns"], 53, "UDP")


def test_e2e_overlay_routes_vault_to_mock_only():
    docs = render("e2e-kind")
    mock = pod("mock", "nanoclaw", **{C: "mock-upstream"})
    assert allowed(docs, PEERS["vault"], mock, 8080)
    assert not allowed(docs, PEERS["agent"], mock, 8080)
    assert not allowed(docs, PEERS["agent"], PEERS["api"], 443)
