# Threat Model — NanoClaw Warden Cinder

Defender's reference. Prevention is not promised; defense in depth is the goal.

## Structural reality (the anchor)

[VERIFIED — OWASP GenAI LLM01:2025] Prompt injection is the #1 risk on the OWASP Top 10
for LLM Applications. Root cause: models process instructions and data in the same channel
with no reliable separation, and injected content affects the model even when imperceptible
to a human. OWASP states it is unclear whether any fool-proof prevention exists.

The chain worth breaking, in OWASP's framing: injection (LLM01) feeds unsanitized output
(LLM05) into a system with too much autonomy (LLM06). This architecture breaks all three
links structurally.

## Why an agent swarm raises the stakes

A multi-agent system with tool access is the highest-risk surface in the taxonomy
(family H). Two compounding risks specific to this design:

1. **Agent-to-agent propagation & memory poisoning.** [REPORTED — described in agent-security
   surveys] One compromised sub-agent can inject into others; poisoned shared memory persists
   across sessions. Mitigation here: ephemeral Pods (no persistent agent state), default-deny
   NetworkPolicies (sub-agents cannot reach each other), and the controller as the only
   message router — peers never talk directly.

2. **Tool / implicit tool poisoning.** [REPORTED] Malicious instructions in a tool description
   the model reads but the user does not; or a poisoned-but-uninvoked tool whose metadata
   induces a call to a legitimate high-privilege tool. Mitigation: screen MCP tool metadata
   at the protocol boundary (the Vault sidecar), scoped per-agent tool allowlists, no shared
   tool registry across trust levels.

## Families that apply, with detection + mitigation

### Family F — Indirect injection via content
[VERIFIED — OWASP treats indirect injection as first-class.] Any sub-agent that reads a
document, web page, RAG record, or email can receive instructions planted in that data.
- **Detect:** provenance tracking on every ingested source; flag instruction-like text in
  retrieved content; OWASP RAG-triad output checks.
- **Mitigate:** treat all retrieved/external content as data, never instructions. Strip or
  neutralize instruction patterns before the content enters a prompt. The canonical OWASP
  example — summarizing a page with hidden instructions that trigger image-link exfiltration
  — is blocked at the network layer: sub-agents have egress allowlists, so an injected
  outbound image URL has nowhere to go.

### Family H — Agentic and protocol exploitation
[VERIFIED as a class — OWASP LLM06 covers manipulated output triggering unintended tool
actions, e.g. redirecting a send_email call.]
- **Detect:** log every tool call with its triggering content; anomaly-detect calls that
  don't match the task; alert on any high-impact action (send, delete, transfer, modify ACL).
- **Mitigate:** least-privilege ServiceAccount per agent; high-impact actions gated behind
  explicit human approval, never auto-executed by an agent; the controller cannot execute
  tools at all, only orchestrate.

### Family A / B — Direct override & persona
[VERIFIED — OWASP LLM01 lists instruction override; OWASP LLM07 covers system-prompt leakage.]
Relevant if any sub-agent takes input that originates from an end user.
- **Detect:** output classification on responses; refusal-consistency checks.
- **Mitigate:** do not rely on the system prompt as a security control (OWASP LLM07). The
  real controls are RBAC, network policy, and the Vault — none of which the model can talk
  its way past.

### Family D / E — Composition & automated generation
[VERIFIED — many-shot jailbreaking is published Anthropic research; payload splitting is an
OWASP LLM01 example.]
- **Detect:** conversation-level (not just turn-level) monitoring; rate-limit and
  anomaly-detect structured/repeated probing.
- **Mitigate:** per-agent rate limits enforced at the Vault; context-length caps on
  sub-agent prompts to blunt many-shot stuffing.

## What this design does NOT defend against (be honest)

- A jailbreak that produces *prohibited text* (not a tool action) from a sub-agent whose only
  job is text generation. The Pod isolation limits blast radius but does not prevent the
  model from being talked into bad output. Pair with output classification.
- A supply-chain compromise of the agent container image. Mitigate with image signing
  (cosign), admission control (e.g. Kyverno/Gatekeeper verifying signatures), and pinned
  digests — out of scope for the manifests below but noted.
- A compromised controller. It is the crown jewel. Its RBAC is the one place where
  Job-creation authority lives. Audit it hard (see rbac-controller.yaml comments).

## Cross-cutting

[REPORTED for the phenomenon; specific transfer % are [UNVERIFIED] and often tied to
outdated model versions.] Jailbreaks transfer across models, so apply the same guardrail
layer regardless of which provider a given sub-agent uses. Multi-provider routing is not
itself a defense.
