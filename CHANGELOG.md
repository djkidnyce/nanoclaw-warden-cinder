# Changelog

## 0.2.0 (2026-11-23)

### Fixed after the first CI run
- CI could not install its tools: checkov 3.4.6 pins `packaging<24` and pytest 9.2.0 needs `packaging>=24`. checkov now lives in its own virtualenv built by `scripts/fetch-tools.sh`.
- Redis pinned to 8.12.0, which has a git tag but no official Docker image yet, so Redis would never have started. Pinned to 8.10.3, the newest published release. CI now checks that every pinned image exists.

A rebuild after a full review. The first version looked right on paper but would not have worked on a cluster, and one of its security claims was overstated.

### Fixed
- Agents could reach the internet. NetworkPolicy applies to a whole pod and the vault was a sidecar, so the agent container shared the vault's egress. The vault is now its own Deployment and the only pod with a way out.
- Default deny blocked the system itself. The controller couldn't reach Redis or DNS, agents couldn't reach Redis, and KEDA couldn't read the stream, so nothing would ever scale. Every needed flow is now listed and a test proves the full matrix.
- The controller read its own subtasks back off the task stream and split them forever. Tasks, subtasks and results now use separate streams.
- KEDA used `pendingEntriesCount`, which never counts new work and cannot scale to zero. It now uses `lagCount` with the `accurate` job strategy.
- Agents never claimed work in KEDA mode. They now claim one message, resume their own pending message on retry, and ack atomically with their result.
- Subtask content travelled in a Job environment variable, visible to anyone who could read pods. It now rides on the stream message, delivered only to the agent that claims it.
- Preflight missed Calico installed by the operator and Flannel's real DaemonSet name. Install would set up Cilium without the API server address it needs and could try to replace a running CNI.
- Kustomize left the agent ScaledJob pointing at an unhashed ConfigMap name, so every agent pod would have failed to start.

### Security review fixes
- Agents could read every subtask record, including other subtasks' vault tokens. The token and body now ride on the stream message, which Redis delivers only to the claiming agent, and agents lost read access to records.
- One oversized write could fill Redis memory and stall every queue. Values are now capped at 1 MB and client buffers at 4 MB.
- Redis accepted connections from every pod in the `keda` namespace. Now only the operator pod.
- New optional pod creator lock so people with pod create rights can't impersonate a workload.
- New `--encrypt` installer option for Cilium WireGuard between nodes.

### Added
- Vault gateway with per subtask tokens, rate limits, call budgets, model allow list, token cap, size limits and tool metadata screening.
- Redis ACLs with a separate user per workload and the default user off.
- ValidatingAdmissionPolicies: only KEDA creates Jobs, only approved images run, no tokens, no debug containers, fail closed.
- Recovery: requeue of stranded work, dead letter queue after three attempts, controller takeover, survival of Redis data loss.
- ResourceQuota and LimitRange. Optional gVisor and digest pinning components.
- Operator CLI, secret generator, digest pinning script, toolchain fetcher.
- Regression suite: unit, integration on real Redis, network reachability, scripts, real Kubernetes API server, scale emulation, and a kind job in CI.
- Docs: how it works, quick start, configuration, NIST 800-53 mapping, test report.

### Changed
- KEDA 2.21.1 (minimum 2.21.0 for CVE-2026-77524), Cilium 1.20.5, Calico 3.33.1, Redis 8.10.3, Python 3.13.18.
- Every GitHub Action pinned to a commit SHA, harden-runner on every job, Renovate for updates, cosign signing on release.
- Default model is set by config instead of code.
- No workload has any Kubernetes API permission. The controller no longer needs one.

## 0.1.0 (2026-06-22)
First version.
