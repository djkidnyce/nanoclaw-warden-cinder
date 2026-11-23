# Quick start

About 15 minutes on a cluster you already have. If you just want to see it work, skip to "Try it on your laptop" at the bottom.

Versions this guide installs: KEDA 2.21.1, Cilium 1.20.5 or Calico 3.33.1, Redis 8.10.3.

## What you need

1. A Kubernetes cluster, version 1.32 or newer, with a CNI that enforces NetworkPolicy. Cilium is the best fit. Calico works. Flannel alone does not.
2. `kubectl`, `helm`, `openssl` and `python3` on your workstation.
3. An Anthropic API key saved in a file, for example `~/.secrets/anthropic.key`. Never paste it on the command line.

## 1. Get the code

```bash
git clone https://github.com/djkidnyce/nanoclaw-warden-cinder.git
cd nanoclaw-warden-cinder
```

## 2. Check the cluster

```bash
./scripts/preflight.sh
```

It only reads. It tells you your Kubernetes version, which CNI you run and which overlay fits it, whether KEDA is there and new enough, and what is missing. Fix anything marked `[fail]` before moving on.

## 3. Install what is missing

```bash
./scripts/install.sh                     # installs KEDA only, leaves your CNI alone
./scripts/install.sh --upgrade-keda      # if preflight said KEDA is older than 2.21.0
```

If the cluster has no CNI at all (a fresh kubeadm build, say):

```bash
./scripts/install.sh --cni cilium --k8s-api-host <control plane address> --k8s-api-port 6443
```

Add `--encrypt` to that Cilium command to encrypt pod traffic between nodes with WireGuard. The installer asks before every change and never replaces a CNI that is already running. Add `--dry-run` to see the commands without running them.

Run `./scripts/preflight.sh` again. You want `Result: ready`.

## 4. Create the namespace and secrets

```bash
kubectl apply -f deploy/base/namespace.yaml
./scripts/gen-secrets.sh --api-key-file ~/.secrets/anthropic.key
```

This creates seven Secrets with fresh random passwords and writes the operator password to `./nanoclaw-client.pass` (mode 600, already in `.gitignore`). Prefer GitOps? Add `--output ./secrets` to get YAML you can encrypt with SOPS or Sealed Secrets instead.

## 5. Deploy

Pick the overlay that matches your CNI:

```bash
kubectl apply -k deploy/overlays/cilium     # hostname based egress for the vault
# or
kubectl apply -k deploy/overlays/calico     # address range egress for the vault
```

The runtime image is `ghcr.io/djkidnyce/nanoclaw-warden-cinder/runtime:0.2.0`. If you build it yourself, push it and change the `images` entry in `deploy/base/kustomization.yaml` and the allow list in `deploy/base/admission.env`.

Wait for Redis:

```bash
kubectl -n nanoclaw rollout status deploy/redis
```

## 6. Watch it scale to zero

Right after deploy the controller and vault start with one replica each. Within a few minutes KEDA takes them to zero. The controller also wakes for three minutes at the top of every ten for its recovery sweep, so don't be surprised to see it blink on:

```bash
kubectl -n nanoclaw get deploy,scaledjob,scaledobject -w
```

When it settles, `controller` and `vault` show `0/0` and there are no agent pods. Only Redis runs.

## 7. Send work

Open a tunnel to Redis in another terminal:

```bash
kubectl -n nanoclaw port-forward svc/redis 6379:6379
```

Then submit a task as the restricted `client` user:

```bash
python3 -m pip install --require-hashes -r requirements.txt
export PYTHONPATH=src REDIS_HOST=127.0.0.1 REDIS_USERNAME=client REDIS_PASSWORD_FILE=./nanoclaw-client.pass

python3 -m nanoclaw cli submit \
  --goal "Review last night's failed deploy" \
  --subtask "Build a timeline from the log excerpt" \
  --subtask "List likely root causes" \
  --subtask "Draft three follow up actions" \
  --input "$(cat deploy.log)" \
  --wait 600
```

In the watch window you will see the controller go to 1, three agent Jobs appear, the vault go to 1, then everything drain back to zero.

## 8. Read the results

```bash
python3 -m nanoclaw cli status  <task id>
python3 -m nanoclaw cli results <task id>
python3 -m nanoclaw cli approvals        # actions agents proposed; nothing ran
python3 -m nanoclaw cli dlq              # subtasks that failed three times
```

## 9. Let agents propose actions (optional)

Tools are off by default. To let agents propose tickets, add the name to `ALLOWED_TOOLS` in `deploy/base/nanoclaw.env`, reapply, and ask for it per subtask:

```bash
python3 -m nanoclaw cli submit --goal "Triage" --subtask "File a ticket if this needs one" --tool create_ticket
```

The proposal lands on the approvals stream. Nothing is created until a person acts on it.

## Tear down

```bash
kubectl delete -k deploy/overlays/cilium
kubectl delete -f deploy/base/namespace.yaml
```

The admission policies are cluster scoped and are removed by the first command.

## Try it on your laptop

With Docker, kind, kubectl, helm and python3 installed:

```bash
./scripts/fetch-tools.sh && . .tools/env.sh     # Linux; on macOS install kind with brew
KEEP=1 ./tests/e2e/kind_e2e.sh
```

That builds a two node kind cluster with Cilium and KEDA, builds the image, deploys the `e2e-kind` overlay with a mock model API, runs a 12 subtask burst, checks that an agent shaped pod can reach Redis but not the internet, and waits for everything to return to zero. `KEEP=1` leaves the cluster up so you can poke at it. Delete it with `kind delete cluster --name nanoclaw-e2e`.

## Next

Read [HOW_IT_WORKS.md](HOW_IT_WORKS.md) for the full design, and [CONFIGURATION.md](CONFIGURATION.md) before you change limits or turn on gVisor and digest pinning.
