# NanoClaw Warden Cinder

Ephemeral, isolated AI agents on Kubernetes. They spark when needed and burn out clean.

![status](https://img.shields.io/badge/status-reference_implementation-blue)
![k8s](https://img.shields.io/badge/kubernetes-1.28%2B-326ce5)
![keda](https://img.shields.io/badge/KEDA-chart_2.20.1-00b3e6)
![cni](https://img.shields.io/badge/CNI-Cilium_1.19.5_%7C_Calico_3.32.0-f8c517)
![license](https://img.shields.io/badge/license-Apache_2.0-green)

> Versions pinned and verified current as of **2026-06-22**.

## What it does

NanoClaw Warden Cinder runs AI agent workloads as disposable Kubernetes Jobs. A long-lived
controller reads a task queue, decomposes work into subtasks, and spawns one sub-agent per
task. Each sub-agent lives in its own Pod, does one job, and terminates. When no work is
queued, KEDA scales the whole agent layer to zero, so idle cost is nothing. A new task wakes
exactly the agents needed.

The design treats every agent interaction as hostile until proven otherwise. The component
that *decides* what to do (the controller) is separated from the components that *can act*
(the sub-agents), and even those hold no credentials. A vault sidecar injects API keys at
request time, enforces per-agent rate limits, and screens tool metadata at the protocol
boundary. Default-deny networking stops agents from reaching each other or the open internet.
Sub-agents run with powerless service accounts, so a fully compromised agent holds no cluster
credential to abuse. High-impact actions are returned for human approval rather than
auto-executed.

The result is an agent swarm that scales horizontally, isolates blast radius at the OS level,
and leaves no persistent state for one task to poison the next. It ships with a verified
threat model and a red-team self-assessment matrix mapped to the OWASP LLM Top 10. Built on
the NanoClaw isolation model.

## The name

Three ideas in one. **NanoClaw** is the lineage: minimal, auditable, isolation-first agent
design. **Warden** is the controller stance: it watches over a set of isolated cells where
agents work under least privilege. **Cinder** is the lifecycle: agents are sparks that flare
on demand and burn out clean, leaving no embers (no persistent state).

## Architecture at a glance

```
 inbound task (Redis stream)
        |
        v
 +--------------+   creates Jobs via K8s API (not docker.sock)
 |  Controller  | ---------------+
 | (decides)    |                |
 +--------------+                v
   scales 0->1            +----------------+ +----------------+
                          | Sub-agent Pod  | | Sub-agent Pod  |  ephemeral Jobs
                          | +----+ +-----+ | | +----+ +-----+ |  restricted PSS
                          | |agt | |vault| | | |agt | |vault| |  scoped SA, no token
                          | +----+ +-----+ | | +----+ +-----+ |  default-deny netpol
                          +----------------+ +----------------+
                               scale 0->N via KEDA, back to 0 when idle
```

Full diagram: [`docs/infographic.html`](docs/infographic.html) (open in a browser) and
[`docs/infographic.svg`](docs/infographic.svg). Security analysis in
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md); self-assessment in
[`docs/REDTEAM_MATRIX.md`](docs/REDTEAM_MATRIX.md).

## Quickstart

```bash
# 1. Check dependencies (no changes made)
./scripts/preflight.sh

# 2. Install only what's missing (prompts first). Cilium is default.
./scripts/install.sh --cni=cilium      # or --cni=calico

# 3. Re-check
./scripts/preflight.sh

# 4. Set the API key (use External Secrets Operator in prod)
kubectl create namespace nanoclaw
kubectl -n nanoclaw create secret generic nanoclaw-credentials \
  --from-literal=anthropic-api-key=YOUR_KEY

# 5. Deploy (after replacing image digests and the egress CIDR placeholder)
kubectl apply -f k8s/

# 6. Watch scale-to-zero in action
kubectl get pods -n nanoclaw -w
kubectl -n nanoclaw exec deploy/redis -- redis-cli XADD nanoclaw:tasks '*' \
  data '{"id":"t1","payload":{"goal":"demo"},"subtasks":[{"goal":"a"},{"goal":"b"}]}'
```

## Dependencies

| Component | Pinned version | Role | Notes |
|---|---|---|---|
| Kubernetes | 1.28+ | Platform | Tested floor |
| KEDA | chart 2.20.1 (app 2.19.0 GA) | Scale-to-zero | `ScaledJob` on Redis stream |
| Cilium | 1.19.5 | CNI + NetworkPolicy | eBPF, default option |
| Calico | 3.32.0 (tigera-operator) | CNI + NetworkPolicy | Alternative / Flannel add-on |
| Redis | 7-alpine | Task queue + scale signal | Coordination only, not agent memory |

**Load-bearing requirement:** the CNI must enforce `NetworkPolicy`. Bare Flannel does not.
`preflight.sh` flags this. The network isolation is what blocks the indirect-injection
exfiltration chain, so it is not optional.

## Security model in one paragraph

Controller has the only RBAC that can create Jobs and cannot execute tools or read secrets.
Sub-agents get a powerless ServiceAccount with no mounted token, so a jailbroken agent holds
no cluster credential. Credentials live only in a per-Pod Vault sidecar that injects keys at
call time, rate-limits per agent, and screens MCP tool metadata at the protocol boundary.
Default-deny NetworkPolicies stop peer-to-peer and open-internet egress. High-impact actions
(send, delete, transfer, modify ACL) are returned as proposed for human approval, never
auto-executed. Pods are ephemeral, so no poisoned state survives a task.

## Repository layout

```
nanoclaw-warden-cinder/
|-- README.md
|-- LICENSE
|-- scripts/
|   |-- preflight.sh          # check-only dependency report
|   `-- install.sh            # install missing deps, prompts first
|-- k8s/                      # manifests, applied in numeric order
|-- orchestrator/controller.py
|-- agent/agent.py
`-- docs/
    |-- ARCHITECTURE.md
    |-- THREAT_MODEL.md
    |-- REDTEAM_MATRIX.md
    |-- DEPLOY.md
    |-- DOCUMENTATION.md
    |-- infographic.html
    `-- infographic.svg
```

## Disclaimer

Reference implementation. Container images, the Vault internals, results aggregation, and
supply-chain signing are stubs or out of scope and noted where they belong. Review
`docs/DEPLOY.md`'s production checklist before any real deployment. Do not commit a real API
key; the secret in `k8s/06-redis-and-secrets.yaml` ships as a placeholder.

## License

Apache-2.0. See [LICENSE](LICENSE).
