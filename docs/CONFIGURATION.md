# Configuration

Everything you can tune, where it lives, and what it protects. Change values in an overlay rather than the base, so upgrades stay clean.

## Where configuration lives

| File | What it sets |
|---|---|
| `versions.env` | every pinned version. Scripts and tests read it |
| `deploy/base/nanoclaw.env` | runtime settings for controller, agents and vault (the `nanoclaw-config` ConfigMap) |
| `deploy/base/admission.env` | admission policy parameters (the `nanoclaw-admission` ConfigMap) |
| `deploy/base/agent-scaledjob.yaml` | agent Job limits and KEDA job scaling |
| `deploy/base/scaling.yaml` | controller and vault scaling |
| `deploy/base/quota.yaml` | namespace ceilings |
| `deploy/overlays/*` | the vault's egress rule for each CNI |
| `deploy/components/*` | optional gVisor, digest pinning and pod creator lock |

To override a runtime value without touching the base, merge it from your overlay:

```yaml
configMapGenerator:
  - name: nanoclaw-config
    behavior: merge
    literals:
      - MAX_TOKENS_CAP=8192
      - ALLOWED_TOOLS=create_ticket
```

The ConfigMap name carries a content hash, so changing a value rolls the controller and vault automatically. New agent Jobs pick it up immediately.

## Runtime settings (`nanoclaw.env`)

### Model and upstream

| Key | Default | Meaning |
|---|---|---|
| `MODEL_DEFAULT` | `claude-sonnet-5-5` | model the controller assigns to new subtasks |
| `MODEL_ALLOWLIST` | `claude-sonnet-5-5,claude-haiku-5-5` | models the vault will forward. Anything else gets 403 |
| `UPSTREAM_URL` | `https://api.anthropic.com` | where the vault sends requests. Change the vault egress rule with it |
| `UPSTREAM_API_VERSION` | `2023-06-01` | `anthropic-version` header |
| `UPSTREAM_TIMEOUT_SECONDS` | `170` | per call timeout at the vault |
| `MAX_TOKENS_CAP` | `4096` | the vault clamps `max_tokens` to this |
| `AGENT_MAX_TOKENS` | `2048` | what agents ask for |

### Vault guardrails

| Key | Default | Meaning |
|---|---|---|
| `RATE_LIMIT_PER_MIN` | `30` | calls per token per minute |
| `MAX_CALLS_PER_TOKEN` | `20` | lifetime calls per subtask |
| `MAX_REQUEST_BYTES` | `262144` | request body cap |
| `MAX_RESPONSE_BYTES` | `2097152` | upstream reply cap |
| `MAX_TOOL_DESCRIPTION_CHARS` | `1000` | longer tool descriptions are refused |

### Tasks

| Key | Default | Meaning |
|---|---|---|
| `ALLOWED_TOOLS` | empty | comma list from `src/nanoclaw/tools.py` (`create_ticket`, `send_email`, `http_get`). Empty means no tools |
| `MAX_SUBTASKS` | `50` | per task |
| `MAX_INPUT_CHARS` | `32000` | per goal and per input |
| `MAX_RESULT_CHARS` | `64000` | longer agent output is rejected |
| `MAX_PROPOSED_ACTIONS` | `20` | per result; extra proposals are dropped |

### Recovery and retention

| Key | Default | Meaning |
|---|---|---|
| `MAX_ATTEMPTS` | `3` | requeues before a subtask goes to the dead letter queue |
| `VISIBILITY_TIMEOUT_SECONDS` | `1200` | how long a claimed subtask may sit before the sweep requeues it. Keep it above the agent `activeDeadlineSeconds` (900) so a live agent never loses its work. A test enforces this |
| `CONTROLLER_CLAIM_IDLE_SECONDS` | `120` | when a new controller takes over a dead one's messages |
| `SWEEP_INTERVAL_SECONDS` | `60` | sweep cadence while the controller runs |
| `TOKEN_TTL_SECONDS` | `1800` | token lifetime; also kept above the agent deadline |
| `RECORD_TTL_SECONDS` | `86400` | how long task records and results stay readable |
| `STREAM_MAXLEN` | `10000` | cap on completed, approvals and dead letter streams |

### Agent timing

| Key | Default | Meaning |
|---|---|---|
| `VAULT_WAIT_SECONDS` | `180` | how long an agent waits for a cold vault |
| `VAULT_REQUEST_TIMEOUT_SECONDS` | `180` | per call timeout at the agent |
| `CLAIM_WAIT_SECONDS` | `5` | how long an agent waits for a message before exiting cleanly |

## Scaling knobs

| Setting | File | Default | Notes |
|---|---|---|---|
| `maxReplicaCount` | `agent-scaledjob.yaml` | 20 | most agents at once. The quota caps pods at 40 |
| `pollingInterval` | `agent-scaledjob.yaml` | 5 s | how fast new work is noticed |
| `activeDeadlineSeconds` | `agent-scaledjob.yaml` | 900 | hard kill for a stuck agent. The admission policy refuses anything over 3600 |
| `backoffLimit` | `agent-scaledjob.yaml` | 3 | retries per Job |
| `ttlSecondsAfterFinished` | `agent-scaledjob.yaml` | 600 | finished Jobs are deleted after this |
| controller `cooldownPeriod` | `scaling.yaml` | 120 s | idle time before the controller scales to zero |
| vault `cooldownPeriod` | `scaling.yaml` | 60 s | idle time before the vault scales to zero |
| vault `value` | `scaling.yaml` | 8 | agents per vault replica |
| cron window | `scaling.yaml` | minutes 0 to 3 of every 10 | recovery sweep for stranded work |

If you raise `maxReplicaCount`, raise the quota too and watch your upstream rate limits. Twenty agents at 30 calls a minute each is up to 600 calls a minute.

## Admission parameters (`admission.env`)

| Key | Default | Meaning |
|---|---|---|
| `jobCreators` | `system:serviceaccount:keda:keda-operator` | who may create Jobs in the namespace. Change it if KEDA lives elsewhere |
| `allowedImagePrefixes` | the runtime image and `docker.io/library/redis` | full repositories, comma separated. Matching includes the `:` or `@` after the name |
| `requireDigest` | `false` | set to `true` by the `digest-pinning` component |

Break glass: deleting a ValidatingAdmissionPolicyBinding disables that policy. Do it on purpose, log it, and put it back.

## Secrets

| Secret | Keys | Used by |
|---|---|---|
| `nanoclaw-redis-acl` | `users.acl` | Redis |
| `nanoclaw-redis-bootstrap` | `password` | Redis bootstrap sidecar |
| `nanoclaw-redis-controller` | `password` | controller |
| `nanoclaw-redis-agent` | `password` | agents |
| `nanoclaw-redis-vault` | `password` | vault |
| `nanoclaw-redis-keda` | `username`, `password` | KEDA TriggerAuthentication |
| `nanoclaw-upstream` | `api-key` | vault only |

Rotate everything by rerunning `gen-secrets.sh`, then `kubectl -n nanoclaw rollout restart deploy/redis`. The controller and vault reconnect on their next start; agents pick up new passwords on their next Job. For an external store, point External Secrets Operator at the same Secret names and keys.

## Optional hardening

### gVisor for agents
Install `runsc` on your nodes and configure containerd with a `runsc` handler (see the gVisor docs for your platform; GKE Sandbox and several managed offerings ship it). Then add the component to your overlay:

```yaml
components:
  - ../../components/gvisor
```

The component creates a RuntimeClass named `gvisor`. Remove `runtimeclass.yaml` from the component if your platform already has one.

### Pod creator lock
Only Kubernetes' Job and ReplicaSet controllers may create pods in the namespace once this is on:

```yaml
components:
  - ../../components/pod-creator-lock
```

It allows `system:serviceaccount:kube-system:job-controller`, `system:serviceaccount:kube-system:replicaset-controller` and `system:kube-controller-manager`. Those are the identities `kube-controller-manager` uses with and without `--use-service-account-credentials`. Confirm yours in the API server audit log before you turn it on in production; if the names differ, set `podCreators` in your overlay. With the lock on, people can't `kubectl run` or `kubectl debug` in the namespace.

### Encryption between nodes
With Cilium, `./scripts/install.sh --cni cilium --k8s-api-host <addr> --encrypt` turns on WireGuard (`encryption.enabled=true`, `encryption.type=wireguard`). It needs WireGuard in the node kernel (Linux 5.6 or newer). On an existing Cilium install, set the same Helm values with `helm upgrade`.

### Digest pinning
After a release is published:

```bash
./scripts/pin-digests.sh deploy/overlays/cilium
```

Then enable `../../components/digest-pinning`. From then on the admission policy refuses any image without a digest.

### Durable Redis
Replace the `data` emptyDir in `deploy/base/redis/redis.yaml` with a PersistentVolumeClaim through a patch in your overlay, or point `REDIS_HOST` at a managed Redis 7.2 or newer with TLS (`REDIS_TLS=true`, `REDIS_TLS_CA_FILE`). Keep the ACL users; managed services that support ACLs accept the same rules.

## Changing the upstream host

1. Set `UPSTREAM_URL` in your overlay.
2. Cilium: change `matchName` in `deploy/overlays/cilium/vault-egress.yaml` to the same host. A test fails if they differ.
3. Calico: replace the CIDRs in `deploy/overlays/calico/vault-egress.yaml` with the new provider's published ranges.
4. Update `MODEL_ALLOWLIST`.
