# NanoClaw Warden Cinder: Scalable Isolated Agent Orchestration

A Kubernetes-native port of the NanoClaw model. One controller agent spawns sub-agents,
each in its own Pod, each scaling to zero when idle and starting on demand.

## Core principle (inherited from NanoClaw)

Treat every agent interaction as hostile until proven otherwise. Isolation is OS-level,
not application-level allowlists. Nothing crosses the Pod boundary unless explicitly wired.

## Topology

```
                         inbound (queue: Redis stream)
                                   |
                                   v
                      +------------------------+
                      |  Controller (orchestrator)  |   long-lived Deployment, 1 replica
                      |  - reads task queue          |
                      |  - decomposes into subtasks  |
                      |  - creates K8s Jobs via API  |   <-- replaces docker.sock
                      |  - NEVER executes tool calls |
                      +------------------------+
                          |          |          |
              creates Job |   Job    |   Job    |  (one per sub-agent task)
                          v          v          v
                   +--------+  +--------+  +--------+
                   | Pod    |  | Pod    |  | Pod    |   ephemeral, restricted PSS
                   | agent  |  | agent  |  | agent  |   own ServiceAccount, scoped RBAC
                   +--------+  +--------+  +--------+   own NetworkPolicy (default deny)
                          \         |         /
                           credentials injected at call time
                                via Agent Vault sidecar
                                 (agents hold no raw keys)
```

Scale-to-zero: KEDA `ScaledJob` watches the Redis stream. No messages, no Pods, no cost.
A new message wakes exactly the Jobs needed, then they terminate. The controller itself
can also scale to zero via KEDA `ScaledObject` if inbound is bursty.

## Why Jobs, not long-lived Pods

NanoClaw's containers are disposable: no persistent state, no shared filesystem, no config
drift. A K8s `Job` maps to this exactly. One task = one Job = one Pod = one lifecycle.
Job completes, Pod is garbage-collected. This is the recommendation from nanoclaw issue
#1184 (K8s-native runtime calling the API to spin up ephemeral Pods/Jobs as sandboxes).

## The trust boundary, drawn explicitly

| Boundary | Trusted | Untrusted (data, never instructions) |
|---|---|---|
| Controller input | Operator-defined task schema | Task *content* from the queue |
| Sub-agent input | Its CLAUDE.md / system prompt | The subtask payload, any retrieved/tool content |
| Tool output | Nothing by default | All of it — classified before re-entering a prompt |
| Inter-agent | Nothing by default | Any message from a peer agent |

## Threat model

See THREAT_MODEL.md. This architecture is the defense for OWASP LLM06 (excessive agency)
and the agentic family (H) of the injection taxonomy: least-privilege tools, no raw
credentials in agents, default-deny networking, and a controller that orchestrates but
does not itself hold tool-execution authority.
