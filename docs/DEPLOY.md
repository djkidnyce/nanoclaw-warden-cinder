# Deploy

## Prerequisites
- A cluster with a NetworkPolicy-enforcing CNI: **Calico or Cilium**. Flannel alone does
  not enforce the policies in `03-networkpolicy.yaml` — pair Flannel with Calico, or use
  Cilium. This matters: the network isolation is load-bearing for the threat model.
- KEDA installed: `helm install keda kedacore/keda -n keda --create-namespace`
- Images built and pushed for controller, agent, and vault (pin by digest).

## Order
```bash
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/01-rbac-controller.yaml
kubectl apply -f k8s/02-rbac-agent.yaml
kubectl apply -f k8s/03-networkpolicy.yaml
kubectl apply -f k8s/06-redis-and-secrets.yaml   # replace the secret first
kubectl apply -f k8s/04-controller-deployment.yaml
kubectl apply -f k8s/05-scaledjob-subagent.yaml
```

## Verify scale-to-zero
```bash
# No tasks -> no sub-agent pods
kubectl get pods -n nanoclaw -l app.kubernetes.io/component=sub-agent
# (empty)

# Enqueue a task
kubectl exec -n nanoclaw deploy/redis -- redis-cli XADD nanoclaw:tasks '*' \
  data '{"id":"t1","payload":{"goal":"demo"},"subtasks":[{"goal":"a"},{"goal":"b"}]}'

# Watch Jobs appear, run, and disappear
kubectl get jobs -n nanoclaw -w
```

## Security checklist before production

Identity & privilege
- [ ] Controller RBAC reviewed; no `*` verbs; no secrets read.
- [ ] Sub-agent SA has no Role and `automountServiceAccountToken: false`.
- [ ] If using KEDA-owned Job creation (Mode A), remove `jobs: create` from controller Role.

Network
- [ ] CNI enforces NetworkPolicy (Calico/Cilium confirmed, not bare Flannel).
- [ ] `vault-egress` CIDR replaced with real API endpoints, NOT 0.0.0.0/0.
- [ ] Default-deny confirmed: a sub-agent cannot reach a peer or the open internet.

Credentials
- [ ] API key only in the `nanoclaw-credentials` Secret, referenced only by the Vault.
- [ ] Controller confirmed unable to read the Secret (RBAC test).
- [ ] Use External Secrets Operator for the real key; do not commit values.

Workload hardening
- [ ] Namespace `restricted` PSS enforced.
- [ ] All containers: non-root, read-only rootfs, drop ALL caps, RuntimeDefault seccomp.
- [ ] `activeDeadlineSeconds` and `backoffLimit` set so a stuck/looping agent self-terminates.
- [ ] Image digests pinned; consider cosign + Kyverno signature verification.

Agent-layer (the model can't be RBAC'd, so defend in depth)
- [ ] Sub-agent prompts fence untrusted content in a labeled data region.
- [ ] Vault screens MCP tool metadata at the protocol boundary.
- [ ] High-impact actions return as PROPOSED for human approval, never auto-executed.
- [ ] Output classification on agent responses (catches jailbroken text output that
      isolation alone won't stop).
- [ ] Conversation-level monitoring + per-agent rate limits at the Vault.

## What's intentionally out of scope here
Image supply-chain signing, the Vault's internal implementation, a results-aggregation
service, and DLQ handling for failed tasks. Each is noted where it belongs in the code.
