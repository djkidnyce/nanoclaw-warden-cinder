# Threat model

Prevention isn't on offer. Containment is. This document says what an attacker can try, what stops them, how you'd notice, and what still gets through.

Source tags: [V] verified against a primary source, [R] reported by reputable secondary sources, [U] unverified.

## Starting assumptions

1. [V] OWASP ranks prompt injection first in the 2025 Top 10 for LLM applications (LLM01). Models read instructions and data in the same channel, and OWASP says it is unclear whether any foolproof prevention exists.
2. Any text an agent reads can carry instructions: the task input, a retrieved page, a tool description, a reply from another system.
3. So we assume any agent can be fully controlled by an attacker, and design so that a controlled agent can't do much.

OWASP describes the dangerous chain as injection (LLM01) feeding unchecked output (LLM05) into a system with too much autonomy (LLM06). Each link is cut here by something the model can't argue with.

## Assets

| Asset | Where it lives | Worst case if lost |
|---|---|---|
| Model API key | Secret `nanoclaw-upstream`, mounted only by the vault | spend, data exposure through the provider account |
| Task content | Redis records, readable by agents and the controller | disclosure of whatever users submitted |
| Results and approvals | Redis, readable by the client user | tampered output acted on by a person |
| The cluster | everything outside the namespace | lateral movement |

## Trust boundaries

| Boundary | Trusted side | Untrusted side |
|---|---|---|
| Client to Redis | the `client` ACL (append tasks, read status) | the task body |
| Controller to agent | the subtask record the controller wrote | the input text inside it |
| Agent to vault | the vault's policy and token store | everything in the request |
| Vault to upstream | the vault's allow lists | the model's reply |
| Agent to controller | the record the controller issued | the result body |
| Namespace to cluster | admission policies, PSA, NetworkPolicy | anything a pod tries |

## Threats and controls

### 1. Indirect injection through content
[V] OWASP treats instructions hidden in documents, pages and data as a first class form of LLM01.

Controls: input is fenced in `<untrusted_data>` and fence breaking text is removed; the system prompt says to ignore instructions inside the fence; agents can't act, only propose; agents have no internet route, so the classic "hide an instruction that makes the model fetch an attacker URL with your data" exfiltration path has nowhere to go.

Detect: vault denial logs, unusual proposal volume on `nanoclaw:approvals`, results that quote the fence.

Tests: `test_fence_cannot_be_closed_by_input`, reachability rows `agent -> api` and `agent -> evil`, kind e2e `INTERNET False`.

### 2. Excessive agency and tool misuse
[V] OWASP LLM06 covers manipulated output that triggers unintended actions, such as a redirected `send_email`.

Controls: tools come from a fixed registry in the image; a task can only grant tools in `ALLOWED_TOOLS`; the vault checks the grant again; every tool use becomes a proposal on the approvals stream; nothing in the system executes it.

Tests: `test_tool_requests_become_approvals_not_actions`, `test_tool_outside_grant_rejected`.

### 3. Tool and metadata poisoning
[R] Security research on MCP and agent tooling describes instructions hidden in tool descriptions that the model reads and the user never sees, including tools that are never called but steer the model toward a privileged one.

Controls: the vault screens every tool definition for override phrases, system prompt references, hidden tags, URLs, long base64 runs and invisible Unicode, refuses descriptions over 1000 characters, and refuses tools outside the token's grant. Screening is pattern based and will miss clever variants, which is why threat 2's controls exist.

Tests: `test_poisoned_tool_metadata_blocked` (six variants), `test_policy_errors_do_not_reach_upstream`.

### 4. Credential theft
Controls: one Secret holds the key and one Deployment mounts it; agents get a random token scoped to one subtask, delivered only on the stream message the agent claims, capped at 20 calls and 30 a minute, expiring in 30 minutes and revoked on completion; Redis passwords are files, not environment variables; no workload has a Kubernetes token; the optional pod creator lock stops a person with pod create rights from mounting a workload password.

Tests: `test_only_the_vault_holds_the_upstream_key`, `test_agents_cannot_see_each_others_tokens`, `test_task_completes_and_tokens_are_revoked`, `test_call_budget`, `test_rate_limit`, `test_workload_identities_have_no_api_access`, `test_only_controllers_may_create_pods`.

### 5. Agent to agent spread and poisoned memory
[R] Agent security surveys describe one compromised agent injecting into others and poisoned shared memory persisting across sessions.

Controls: agents can't reach each other on the network; they can't read records, results, tokens, job state or unclaimed stream entries in Redis; they only append to the results stream; the controller drops any result that doesn't match a record it issued; pods are destroyed after one subtask.

Tests: reachability `agent -> agent2`, `TestAcl::test_agent_boundaries`, `test_spoofed_results_are_dropped`.

### 6. Breaking out of the pod
Controls: PSA `restricted`, non root, read only root, no capabilities, RuntimeDefault seccomp, no service links, optional gVisor; admission refuses unapproved images, other identities, token mounts and ephemeral containers; quota limits blast radius.

Tests: `TestPodGuard` (real API server), `test_pods_meet_restricted_and_more`.

### 7. Abusing the scaler
Controls: the `keda` Redis user can only read stream metadata; only the KEDA operator may create Jobs, and only agent shaped ones; agent Jobs are capped at 20 and killed after 15 minutes; the namespace quota holds regardless.

Tests: `TestJobGuard`, `test_keda_is_read_only_metrics`, `test_agent_job_limits`.

### 8. Denial of wallet
Controls: `MAX_TOKENS_CAP`, call budgets and rate limits per token, `MAX_SUBTASKS`, input size limits, model allow list, Job deadline, quota, and Redis caps of 1 MB per value and 4 MB per client buffer so one bad write can't exhaust memory.

Tests: `test_clamps_max_tokens`, `test_body_size_limit`, `test_huge_writes_are_refused_by_redis`, the validation tests.

### 9. Supply chain
Controls: SHA pinned GitHub Actions, hash locked Python dependencies, release images signed with cosign and shipped with SBOM and provenance, Renovate for updates, the digest pinning component for enforcement.

Tests: `test_every_action_is_pinned_to_a_commit`, `test_digest_pinning_component_enforces_digests`.

## What this design does not stop

1. A hijacked agent writing misleading text into its own result. Treat results as untrusted when a person acts on them.
2. A hijacked agent hoarding work. It can claim extra subtasks during its 15 minute life; they sit until the sweep requeues them, and each requeue counts toward the three attempt limit. It can also append junk results, which the controller drops. Agents share one Redis identity, so run sensitive tenants in separate namespaces.
3. A compromised KEDA operator. It can create agent Jobs, though only agent shaped ones.
4. A compromised Redis or a stolen Redis password for the controller. Keep Redis inside the namespace, or managed with TLS. Traffic to Redis is plaintext unless you turn on Cilium WireGuard (`install.sh --encrypt`) or Redis TLS.
5. Anything the upstream provider does with data you send.
6. Pattern based screening misses. Tool metadata from third parties should be reviewed before it's added to the registry.
7. Unsigned images. Releases are signed with cosign, but nothing verifies signatures at admission. Add Sigstore policy controller or Kyverno for that.

## Cross model note
[R] Jailbreaks transfer between models, so passing these checks with one model says little about another. [U] Published transfer rates are often tied to old model versions; don't plan around a specific number. Keep the controls identical for every model in `MODEL_ALLOWLIST`.
