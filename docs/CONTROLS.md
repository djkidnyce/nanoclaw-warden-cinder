# NIST SP 800-53 Rev 5 mapping

How the design lines up with the controls an assessor will ask about. This maps implementation to control intent; it is not an authorization package. Inherited controls (physical, personnel, the cluster platform itself) stay with whoever operates the cluster.

| Control | Name | How it is met | Evidence |
|---|---|---|---|
| AC-3 | Access enforcement | Redis ACLs per workload, ValidatingAdmissionPolicy for Jobs and pods, optional pod creator lock, vault token checks | `users.acl.tmpl`, `admission-policy.yaml`, `TestAcl`, `TestJobGuard`, `TestPodCreatorLock` |
| AC-4 | Information flow enforcement | Default deny NetworkPolicy with seven allowed flows; vault is the only egress, limited by FQDN or CIDR | `networkpolicies.yaml`, overlays, `test_network_reachability.py` |
| AC-6 | Least privilege | No Roles or tokens for any workload; Redis users limited by command and key; tools off by default | `serviceaccounts.yaml`, `test_no_workload_has_kubernetes_api_permissions` |
| AC-6(9) | Log use of privileged functions | Admission bindings use `Audit` alongside `Deny`; Redis keeps an ACL denial log | `admission-policy.yaml`, `redis.conf` |
| AU-2, AU-3 | Event logging, content of records | JSON logs with component, task, subtask, status, model, latency and token counts; no prompt content | `common.py`, `vault.py` |
| AU-12 | Audit record generation | Every vault call and denial produces a record | `vault.py` |
| CM-2 | Baseline configuration | Kustomize base and overlays in version control; versions in one file | `deploy/`, `versions.env` |
| CM-3 | Configuration change control | CI blocks merges on any test or scanner failure; Renovate PRs for dependency changes | `.github/workflows/ci.yml`, `renovate.json` |
| CM-6 | Configuration settings | Restricted PSA, read only root, dropped capabilities, seccomp; enforced by tests and checkov | `test_pods_meet_restricted_and_more`, `.checkov.yaml` |
| CM-7 | Least functionality | One image with three roles, debug containers refused, Redis debug and module commands off | `Dockerfile`, `redis.conf`, `TestPodGuard` |
| CM-7(5) | Authorized software, allow by exception | Image allow list in admission, optional digest requirement | `admission.env`, `components/digest-pinning` |
| IA-2, IA-9 | Identification and authentication (users, services) | Separate Redis identity per workload; per subtask bearer tokens at the vault | `gen-secrets.sh`, `vault.py` |
| IA-5 | Authenticator management | Random 256 bit passwords, hashes only in the ACL file, rotation by rerunning the generator, tokens expire and are revoked | `gen-secrets.sh`, `CONFIGURATION.md` |
| SC-5 | Denial of service protection | Rate limits, call budgets, token caps, quota, Job deadlines, Redis value and buffer caps | `quota.yaml`, `vault.py`, `redis.conf` |
| SC-7 | Boundary protection | Namespace boundary with default deny, single egress point, no exposed Services | `networkpolicies.yaml`, `test_no_service_exposes_the_cluster` |
| SC-7(5) | Deny by default, allow by exception | `default-deny-all` plus explicit allows | `networkpolicies.yaml` |
| SC-8 | Transmission confidentiality and integrity | TLS to the upstream API; node to node WireGuard through `install.sh --encrypt` on Cilium; Redis TLS through `REDIS_TLS` | `nanoclaw.env`, `install.sh` |
| SC-12, SC-28 | Key management, protection at rest | API key in a Kubernetes Secret mounted read only by one pod; enable etcd encryption at rest on the cluster | `vault.yaml` (etcd encryption is inherited) |
| SC-39 | Process isolation | One pod per subtask, optional gVisor sandbox | `agent-scaledjob.yaml`, `components/gvisor` |
| SI-2 | Flaw remediation | Pinned versions with Renovate; preflight refuses KEDA below the CVE fix | `versions.env`, `preflight.sh` |
| SI-3, SI-4 | Malicious code protection, monitoring | Tool metadata screening, denial logs, Hubble flow logs with Cilium | `vault.py`, `HOW_IT_WORKS.md` |
| SI-7 | Software and information integrity | Hash locked Python dependencies, SHA pinned Actions, signed images with SBOM and provenance | `requirements.txt`, `release.yml` |
| SI-10 | Information input validation | Strict task and result schemas, size limits, fence handling | `common.py`, unit tests |
| SR-3, SR-11 | Supply chain controls, component authenticity | cosign signing, digest pinning, Renovate minimum release age | `release.yml`, `renovate.json` |

## DISA Kubernetes STIG alignment

Checked against the Kubernetes STIG Version 2 Release 6 (01 Apr 2026). Rules this project meets directly:

| Rule | Severity | Title | How |
|---|---|---|---|
| V-242415 | High | Secrets must not be stored as environment variables | every secret is mounted as a file; a test fails on any `secretKeyRef` env var |
| V-274883 | High | Sensitive information stored in Secrets or an external store | API key and passwords live only in Secrets; External Secrets Operator supported |
| V-274884 | Medium | Limit Secret access on a need to know basis | each pod mounts only its own Secret; no workload can read Secrets through the API |
| V-242417 | Medium | Separate user functionality | all workloads run in their own `nanoclaw` namespace, none in system namespaces |
| V-254800, V-254801 | High | Pod Security Admission configured | namespace enforces the `restricted` profile |

Rules that belong to the cluster and are inherited: Secrets encrypted at rest (V-274882), API server token authentication (V-245543), and the control plane, node and etcd items. The pod level settings also match the rest of the STIG's intent: non root, no privilege escalation, no host namespaces, read only root, dropped capabilities, resource limits. For hardened base images, the Dockerfile accepts a different base through `PYTHON_VERSION` and the `FROM` line; swap in an Iron Bank Python image if your environment requires it.
