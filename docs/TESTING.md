# Testing

Run everything locally the same way CI does:

```bash
./scripts/fetch-tools.sh && . .tools/env.sh
./scripts/test-all.sh
```

You need Python 3.13, `redis-server` 7.2 or newer on PATH (or `REDIS_SERVER` pointing at one) and `openssl`. `fetch-tools.sh` pulls the pinned kustomize, kubeconform, kube-linter, kind, a Kubernetes 1.37 API server with etcd, and the KEDA and Cilium CRDs.

## What runs

| Stage | Tool | What it proves |
|---|---|---|
| Shell lint | shellcheck, style level | every script and the Redis bootstrap sidecar |
| YAML lint | yamllint | manifests and workflows |
| Schema | kubeconform, Kubernetes 1.37 | every built in object in all three overlays |
| Workload policy | kube-linter, all built in checks | exclusions are listed with reasons in `.kube-linter.yaml` |
| Security policy | checkov, offline | two accepted findings, listed with compensating controls in `.checkov.yaml` |
| Unit | pytest, 34 cases | task and result validation, prompt fencing, vault policy, tool screening |
| Integration | pytest, 35 cases, real Redis 8.10 with the production ACLs and limits | stream flow, no controller loop, lag for KEDA, token isolation between agents, token revocation, retries, recovery, dead letters, trimming, Redis data loss, oversized writes, every ACL boundary |
| Policy | pytest, 105 cases on rendered manifests | pod hardening, no API permissions, config references, image pins, KEDA trigger shape, a full network reachability matrix for both CNIs, version drift |
| Scripts | pytest, 21 cases with fake kubectl and helm | every preflight and installer branch, secret generation, the rendered ACL loading in real Redis |
| API server | pytest, 24 cases on a real `kube-apiserver` and etcd | all objects accepted, KEDA CRD validation, Pod Security restricted, all three admission policies, fail closed, no API access for any workload |
| Scale | pytest, 4 cases with real agent processes | zero to N to zero, a second wave, crash recovery, a cold vault |
| Cluster | `tests/e2e/kind_e2e.sh`, CI only | kind with Cilium and KEDA and the pod creator lock: idle at zero, a 12 subtask burst, people locked out of pod creation, network isolation from an agent shaped pod, back to zero |

## Latest results, 2026-11-22

| Stage | Result |
|---|---|
| shellcheck | clean |
| yamllint | clean |
| kubeconform | 85 valid, 0 invalid. The 14 skipped are KEDA and Cilium custom resources, which the API server stage validates against their real CRDs |
| kube-linter | no errors on cilium, calico and e2e-kind |
| checkov | 274 passed, 0 failed on cilium and calico |
| pytest | 223 passed in 88 seconds |

The kind stage needs container registries and runs in GitHub Actions. It was not part of this local run.

## Bugs the suite caught during the rebuild

These are the reason each test exists.

1. Agents shared the vault's egress because NetworkPolicy works per pod. Caught by the reachability matrix (`agent -> api`).
2. Default deny blocked the controller, agents and KEDA from Redis. Caught by the reachability matrix.
3. The controller split its own subtasks forever. Caught by `test_controller_never_rereads_its_own_subtasks`.
4. Kustomize left the ScaledJob pointing at an unhashed ConfigMap, so agent pods would never start. Caught by `test_every_configmap_reference_resolves`.
5. The pod admission policy read `request.subResource`, which is absent on a plain create, so it denied every pod in the namespace. Caught only by the real API server.
6. The vault's Redis user lacked `INCRBY`, which the client library actually sends for `INCR`. Caught by the HTTP tests against real Redis.
7. Preflight missed Flannel's real DaemonSet name. Caught by the script tests.
8. The controller crashed when Redis came back empty. Caught by `test_controller_survives_redis_data_loss`.
9. Found in the security review: any agent could read every other subtask's vault token. Now locked by `test_agents_cannot_see_each_others_tokens`.
10. Found in the security review: a single 512 MB write could exhaust Redis memory. Now locked by `test_huge_writes_are_refused_by_redis`.

## How the scale test models KEDA

`tests/e2e/keda_emulator.py` reads the same metric KEDA reads (`XINFO GROUPS` lag, as the `keda` Redis user) and applies the configured decisions: the `accurate` job strategy with a cap, the vault waking on agent pods or lag, the controller waking on task or result lag plus the sweep window, and cooldowns. Agents run as real `python -m nanoclaw agent` processes, so exit codes, retries with the same Job name and cold starts behave like pods. It proves the logic; the kind stage proves it on Kubernetes.

Timeline from the last run (12 subtasks, cap 5, 1.5 s simulated vault cold start, 2 s cooldowns): five agents running 0.5 s after submit, vault serving at 2 s, last subtask done at 4.3 s, everything at zero at 7 s. `test-all.sh` writes the full timeline to `.pytest_cache/e2e-timeline.json`, and CI uploads it as an artifact.
