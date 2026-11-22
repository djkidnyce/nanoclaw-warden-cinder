# NanoClaw Warden Cinder Documentation

Complete operational and design reference. Pair with `ARCHITECTURE.md` (design),
`THREAT_MODEL.md` (security analysis), `REDTEAM_MATRIX.md` (self-assessment), and
`DEPLOY.md` (runbook + checklist).

Versions pinned and verified current as of 2026-06-22.

---

## 1. Concept

NanoClaw Warden Cinder ports the NanoClaw model to Kubernetes. NanoClaw runs each agent group in its
own disposable container, woken by a single host orchestrator, with strict isolation: only
explicit mounts cross the boundary and credentials are injected at request time so agents
never hold raw keys. The Kubernetes-native form (the pattern requested in nanoclaw issue
#1184) has the orchestrator call the Kubernetes API to spawn ephemeral Jobs/Pods as
sandboxes instead of relying on a local Docker socket. That is what lets it run on managed
and restricted clusters and scale horizontally.

Two roles:

- **Controller** — long-lived Deployment. Reads the task queue, decomposes work into
  subtasks, and triggers sub-agents. It decides what to do. It cannot execute tools and
  holds no credentials.
- **Sub-agent** — ephemeral Job (one Pod per task). Runs one subtask, then terminates.
  It can act, but only through a Vault sidecar that holds the credentials and enforces
  policy. No persistent state, no peer network access.

The separation between "decides" and "can act," and the further separation of "can act"
from "holds the keys," is the spine of the security design.

---

## 2. Components

### 2.1 Controller (`orchestrator/controller.py`)
- Consumes the Redis stream `nanoclaw:tasks` via a consumer group.
- `decompose()` turns a task into independent subtasks. Replace with your planning logic.
  Task **content is untrusted data**, never instructions to the controller.
- Two spawn modes:
  - **Mode A (recommended):** controller writes subtasks back to the stream; KEDA owns Job
    creation. Then the controller only watches, and you remove `jobs: create` from its RBAC.
  - **Mode B:** controller creates Jobs directly via the API (`create_job_for_subtask`) when
    you need per-subtask Pod customization KEDA cannot express.

### 2.2 Sub-agent (`agent/agent.py`)
- Loads one subtask, calls the model **through the Vault** on `localhost:8200`.
- Prompt structure fences external content in a labeled `<untrusted_data>` region so the
  model is instructed not to treat retrieved content as commands. This is the structural
  mitigation for indirect injection.
- High-impact actions are emitted as `proposed_actions` for human approval, never executed.

### 2.3 Vault sidecar
- The only holder of the API key (`secretKeyRef` injected by the kubelet).
- Injects credentials at request time, enforces per-agent rate limits, screens MCP tool
  metadata at the protocol boundary before it reaches the agent. Implementation is out of
  scope here; the contract is: agents send model/tool requests to it, it adds credentials
  and policy. Conceptually this is NanoClaw's Agent Vault.

### 2.4 Redis
- Inbound queue and the scale-to-zero signal source for KEDA. Coordination bus only;
  sub-agents are stateless by design, so Redis is not agent memory.

---

## 3. Scaling

### 3.1 Scale to zero
KEDA `ScaledJob` (`k8s/05-scaledjob-subagent.yaml`) watches the Redis stream with
`minReplicaCount: 0`. No pending messages means no Pods and no cost. A new subtask wakes
exactly the Jobs needed.

### 3.2 Scale out
- `parallelism` on the Job template caps concurrent sub-agents per scale event.
- `maxReplicaCount` caps total concurrent sub-agents cluster-wide.
- `pollingInterval` controls how fast KEDA reacts to new messages.
- The controller can also scale to zero via a KEDA `ScaledObject` if inbound is bursty.

### 3.3 Safety limits that double as cost control
- `activeDeadlineSeconds: 900` hard-kills a stuck or looping agent after 15 minutes.
- `backoffLimit: 2` bounds retries.
- `ttlSecondsAfterFinished` (Mode B) auto-cleans finished Jobs.
Tune these to your task profile. A runaway agent is both a security and a billing event.

---

## 4. Security architecture

Anchored to the OWASP structural reality: models process instructions and data in the same
channel with no reliable separation, so **prevention is not promised** — defense in depth is
the goal. The chain to break is injection → unsanitized output → excessive agency. This
design breaks all three links structurally.

| Layer | Control | What it stops |
|---|---|---|
| Identity | Controller RBAC scoped to Job/Pod read + create; no secrets, no `*` | Lateral movement, secret theft via the controller |
| Identity | Sub-agent SA has no Role and no mounted token | A jailbroken agent has no cluster credential to abuse |
| Credentials | API key only in Vault sidecar via `secretKeyRef` | Key exfiltration from agent or controller code |
| Network | Default-deny ingress + egress | Peer-to-peer agent propagation; data exfil to attacker URLs |
| Network | Vault is the only controlled egress (allowlisted) | "Tool access" silently becoming "open internet" |
| Workload | Restricted PSS, non-root, read-only rootfs, drop ALL caps | Container breakout primitives |
| Lifecycle | Ephemeral Jobs, no shared mounts | Poisoned memory persisting across tasks |
| Agent logic | Untrusted content fenced as data; high-impact actions gated | Indirect injection; unintended tool actions |
| Monitoring | Per-agent rate limits, conversation-level logging | Many-shot stuffing, structured probing |

Honest limits (also in `THREAT_MODEL.md`): isolation bounds blast radius but does not stop a
text-only sub-agent from being talked into prohibited output (pair with output
classification); a compromised container image or compromised controller are separate, higher
-severity problems addressed by image signing/admission control and hard controller auditing.

---

## 5. CNI choice

The CNI must enforce `NetworkPolicy` or the network layer of the threat model does nothing.

- **Cilium 1.19.5 (default).** eBPF dataplane, can replace kube-proxy, strong egress/L7
  policy. Best when you control the cluster and want the strongest network posture.
- **Calico 3.32.0.** Mature policy engine via the tigera-operator chart. Also the right
  choice if you already run Flannel and only need to add policy enforcement.
- **Flannel alone: not sufficient.** It does not enforce NetworkPolicy. `preflight.sh` fails
  on this and tells you to add Calico or switch to Cilium.

`install.sh --cni=cilium|calico` installs whichever you pick, only if absent, after a prompt.

---

## 6. Operations

### 6.1 Day-one
1. `./scripts/preflight.sh` — confirm dependencies.
2. `./scripts/install.sh` — install missing deps.
3. Create the credentials secret (prefer External Secrets Operator).
4. Replace image digests and the `vault-egress` CIDR placeholder.
5. Apply manifests in numeric order.

### 6.2 Verifying scale-to-zero
```bash
kubectl get pods -n nanoclaw -l app.kubernetes.io/component=sub-agent   # empty when idle
kubectl -n nanoclaw exec deploy/redis -- redis-cli XADD nanoclaw:tasks '*' data '{...}'
kubectl get jobs -n nanoclaw -w                                          # Jobs appear, run, vanish
```

### 6.3 Observability to add (not shipped)
- Tool-call audit log keyed to triggering content (for the LLM06 detection in the matrix).
- Output classifier on agent responses.
- Prometheus metrics from KEDA (`keda_scaler_*`) for scale behavior.
- Flow logs from the CNI (Cilium Hubble or Calico Whisker) to verify default-deny holds.

### 6.4 Upgrades
- KEDA: `helm upgrade keda kedacore/keda --version <new>`; update the `HTTPScaledObject`/CRDs
  per release notes first if applicable.
- Cilium/Calico: consult the compatibility table for your Kubernetes version before bumping.
- Re-run `preflight.sh` after any upgrade; it compares installed versions to the pins.

---

## 7. Self-assessment

`REDTEAM_MATRIX.md` converts the threat model into a test matrix: technique → test approach →
expected-safe behavior → pass/fail criteria. Run it as a tabletop and as live checks against
a staging deployment before production, and re-run after any change to RBAC, NetworkPolicy,
or the Vault.

---

## 8. Known gaps / out of scope

Container image build pipeline, Vault sidecar implementation, results-aggregation service,
dead-letter queue for failed tasks, and supply-chain signing (cosign + Kyverno/Gatekeeper).
Each is noted at its location in code or manifests. None changes the security model; they are
the parts you complete for your environment.
