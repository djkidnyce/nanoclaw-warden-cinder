"""
NanoClaw Warden Cinder Controller (reference implementation).

Responsibilities:
  1. Consume tasks from the Redis stream.
  2. Decompose each task into subtasks (the "main agent controlling sub-agents" role).
  3. Enqueue subtasks for KEDA to spawn as Jobs.
  4. Watch sub-agent Pod/Job status and aggregate results.

Deliberate non-responsibilities (security split):
  - The controller NEVER executes a tool call.
  - The controller NEVER holds the model API key (the Vault sidecar does).
  - The controller NEVER reads secret values.

This separation is the OWASP LLM06 (excessive agency) control: the component that
decides what to do has no authority to do anything dangerous itself.

Two operating modes (pick ONE to avoid double-spawn):
  A) KEDA-owned (recommended): controller writes subtasks to the stream; KEDA creates
     all Jobs. Controller only WATCHES. Drop "jobs: create" from its RBAC.
  B) Controller-owned: controller creates Jobs via the K8s API directly (create_job_for_subtask
     below). Use when you need per-subtask Pod customization KEDA can't express.
"""

import json
import os
import time
import uuid

import redis
from kubernetes import client, config, watch

REDIS_ADDR = os.environ["REDIS_ADDR"]
TASK_STREAM = os.environ.get("TASK_STREAM", "nanoclaw:tasks")
AGENT_NAMESPACE = os.environ.get("AGENT_NAMESPACE", "nanoclaw")
AGENT_IMAGE = os.environ["AGENT_IMAGE"]

host, port = REDIS_ADDR.split(":")
r = redis.Redis(host=host, port=int(port), decode_responses=True)


# --- Task decomposition -----------------------------------------------------
# IMPORTANT: the task CONTENT is untrusted data, not instructions to the controller.
# "Handle the items in this list" authorizes reading the list, not executing whatever
# the list contains. Decomposition produces structured subtasks the controller defines;
# it does not let task content reassign the controller's own behavior.

def decompose(task: dict) -> list[dict]:
    """Split a task into independent subtasks.

    Replace this with your real planning logic (could itself call the model via the
    Vault, but the controller's planning agent must also treat task content as data).
    Each subtask is a self-contained unit handed to one disposable sub-agent.
    """
    subtasks = task.get("subtasks")
    if subtasks:
        return [
            {"id": str(uuid.uuid4()), "parent": task["id"], "payload": s}
            for s in subtasks
        ]
    # Fallback: single subtask.
    return [{"id": str(uuid.uuid4()), "parent": task["id"], "payload": task["payload"]}]


def enqueue_subtask(subtask: dict) -> None:
    """Mode A: push to the stream; KEDA spawns the Job."""
    r.xadd(TASK_STREAM, {"data": json.dumps(subtask)})


# --- Mode B: direct Job creation via the K8s API ----------------------------
# Mirrors what KEDA's ScaledJob would build. Use only if you need per-subtask Pods.

def create_job_for_subtask(subtask: dict) -> None:
    batch = client.BatchV1Api()
    job = client.V1Job(
        metadata=client.V1ObjectMeta(
            generate_name="nanoclaw-subagent-",
            namespace=AGENT_NAMESPACE,
            labels={
                "app.kubernetes.io/name": "nanoclaw",
                "app.kubernetes.io/component": "sub-agent",
                "nanoclaw/parent-task": subtask["parent"],
            },
        ),
        spec=client.V1JobSpec(
            backoff_limit=2,
            active_deadline_seconds=900,
            ttl_seconds_after_finished=300,  # auto-clean finished Jobs
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={"app.kubernetes.io/component": "sub-agent"}
                ),
                spec=client.V1PodSpec(
                    service_account_name="nanoclaw-agent",
                    automount_service_account_token=False,
                    restart_policy="Never",
                    security_context=client.V1PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=65532,
                        seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[_agent_container(subtask), _vault_sidecar()],
                    volumes=[
                        client.V1Volume(name="tmp", empty_dir=client.V1EmptyDirVolumeSource()),
                        client.V1Volume(name="workspace", empty_dir=client.V1EmptyDirVolumeSource()),
                    ],
                ),
            ),
        ),
    )
    batch.create_namespaced_job(namespace=AGENT_NAMESPACE, body=job)


def _agent_container(subtask: dict) -> client.V1Container:
    return client.V1Container(
        name="agent",
        image=AGENT_IMAGE,
        security_context=client.V1SecurityContext(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=client.V1Capabilities(drop=["ALL"]),
        ),
        env=[
            client.V1EnvVar(name="SUBTASK", value=json.dumps(subtask)),
            client.V1EnvVar(name="VAULT_ENDPOINT", value="http://127.0.0.1:8200"),
        ],
        resources=client.V1ResourceRequirements(
            requests={"cpu": "250m", "memory": "256Mi"},
            limits={"cpu": "1", "memory": "1Gi"},
        ),
        volume_mounts=[
            client.V1VolumeMount(name="tmp", mount_path="/tmp"),
            client.V1VolumeMount(name="workspace", mount_path="/workspace"),
        ],
    )


def _vault_sidecar() -> client.V1Container:
    return client.V1Container(
        name="vault",
        image=os.environ.get("VAULT_IMAGE", AGENT_IMAGE),
        security_context=client.V1SecurityContext(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=client.V1Capabilities(drop=["ALL"]),
        ),
        env=[
            client.V1EnvVar(
                name="ANTHROPIC_API_KEY",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name="nanoclaw-credentials", key="anthropic-api-key"
                    )
                ),
            ),
        ],
        ports=[client.V1ContainerPort(container_port=8200)],
    )


# --- Main loop ---------------------------------------------------------------

def main():
    config.load_incluster_config()
    print("controller: starting, consuming", TASK_STREAM)

    # Ensure consumer group exists.
    try:
        r.xgroup_create(TASK_STREAM, "controller", id="0", mkstream=True)
    except redis.ResponseError:
        pass  # already exists

    while True:
        resp = r.xreadgroup(
            "controller", "controller-1", {TASK_STREAM: ">"}, count=1, block=5000
        )
        if not resp:
            continue
        for _stream, messages in resp:
            for msg_id, fields in messages:
                try:
                    task = json.loads(fields["data"])
                    for subtask in decompose(task):
                        # Mode A (default): enqueue for KEDA.
                        enqueue_subtask(subtask)
                    r.xack(TASK_STREAM, "controller", msg_id)
                except Exception as e:  # noqa: BLE001
                    print("controller: error handling", msg_id, e)
                    # Leave unacked for retry / move to DLQ in production.


if __name__ == "__main__":
    main()
