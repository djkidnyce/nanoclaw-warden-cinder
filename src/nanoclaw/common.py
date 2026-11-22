"""Shared settings, Redis keys, logging and input validation.

Every value that crosses a trust boundary passes through a validator in this
module before anything else touches it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import redis

# Streams. Each one has exactly one consumer group reading it.
STREAM_TASKS = "nanoclaw:tasks"          # inbound work, read by the controller
STREAM_SUBTASKS = "nanoclaw:subtasks"    # fan out, read by agents, drives KEDA
STREAM_RESULTS = "nanoclaw:results"      # agent output, read by the controller
STREAM_COMPLETED = "nanoclaw:completed"  # finished tasks, read by clients
STREAM_APPROVALS = "nanoclaw:approvals"  # proposed actions waiting on a human
STREAM_DLQ = "nanoclaw:dlq"              # subtasks that exhausted their attempts

GROUP_CONTROLLER = "controller"
GROUP_AGENTS = "agents"

# (stream, group) pairs that must exist before KEDA polls them.
CONSUMER_GROUPS = (
    (STREAM_TASKS, GROUP_CONTROLLER),
    (STREAM_SUBTASKS, GROUP_AGENTS),
    (STREAM_RESULTS, GROUP_CONTROLLER),
)

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,95}$")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def record_key(subtask_id: str) -> str:
    return f"nanoclaw:task:{subtask_id}"


def token_key(digest: str) -> str:
    return f"nanoclaw:tok:{digest}"


def calls_key(digest: str) -> str:
    return f"nanoclaw:calls:{digest}"


def ratelimit_key(digest: str, window: int) -> str:
    return f"nanoclaw:rl:{digest}:{window}"


def job_key(task_id: str) -> str:
    return f"nanoclaw:job:{task_id}"


def job_results_key(task_id: str) -> str:
    return f"nanoclaw:job:{task_id}:results"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Settings

def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"config error: {name} must be an integer, got {raw!r}") from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise SystemExit(f"config error: {name}={value} outside [{minimum}, {maximum}]")
    return value


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in env_str(name, default).split(",") if item.strip()]


def read_secret(file_env: str, value_env: str) -> str:
    """Prefer a mounted secret file over an environment variable.

    Files keep secrets out of the process environment and out of crash dumps.
    """
    path = env_str(file_env)
    if path:
        p = Path(path)
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
        raise SystemExit(f"config error: {file_env} points to a missing file")
    return env_str(value_env)


def connect_redis() -> redis.Redis:
    """Connect with the identity this process was given. Never the default user."""
    username = read_secret("REDIS_USERNAME_FILE", "REDIS_USERNAME")
    password = read_secret("REDIS_PASSWORD_FILE", "REDIS_PASSWORD")
    client = redis.Redis(
        host=env_str("REDIS_HOST", "redis"),
        port=env_int("REDIS_PORT", 6379, 1, 65535),
        username=username or None,
        password=password or None,
        ssl=env_str("REDIS_TLS", "false").lower() == "true",
        ssl_ca_certs=env_str("REDIS_TLS_CA_FILE") or None,
        decode_responses=True,
        socket_timeout=env_int("REDIS_SOCKET_TIMEOUT_SECONDS", 10, 1, 300),
        socket_connect_timeout=env_int("REDIS_CONNECT_TIMEOUT_SECONDS", 5, 1, 120),
        health_check_interval=30,
    )
    return client


# Logging. One JSON object per line, never message content.

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "level": record.levelname,
            "component": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


def get_logger(component: str) -> logging.Logger:
    logger = logging.getLogger(component)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(env_str("LOG_LEVEL", "INFO").upper())
    return logger


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})


# Validation of untrusted input

class ValidationError(ValueError):
    pass


def require_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ValidationError(f"{what} must match {ID_RE.pattern}")
    return value


def require_text(value: Any, what: str, max_chars: int, allow_empty: bool = False) -> str:
    if value is None and allow_empty:
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"{what} must be a string")
    if not allow_empty and not value.strip():
        raise ValidationError(f"{what} must not be empty")
    if len(value) > max_chars:
        raise ValidationError(f"{what} exceeds {max_chars} characters")
    if "\x00" in value:
        raise ValidationError(f"{what} contains a NUL byte")
    return value


def validate_task(raw: Any, allowed_tools: list[str], max_subtasks: int, max_chars: int) -> dict:
    """Normalize an inbound task. Raises ValidationError on anything unexpected."""
    if not isinstance(raw, dict):
        raise ValidationError("task must be a JSON object")
    unknown = set(raw) - {"id", "goal", "subtasks"}
    if unknown:
        raise ValidationError(f"unknown task fields: {sorted(unknown)}")
    task_id = require_id(raw.get("id"), "task id")
    goal = require_text(raw.get("goal"), "goal", max_chars)
    subtasks_raw = raw.get("subtasks")
    if subtasks_raw is None:
        subtasks_raw = [{"goal": goal}]
    if not isinstance(subtasks_raw, list) or not subtasks_raw:
        raise ValidationError("subtasks must be a non empty list")
    if len(subtasks_raw) > max_subtasks:
        raise ValidationError(f"too many subtasks ({len(subtasks_raw)} > {max_subtasks})")
    subtasks = []
    for index, item in enumerate(subtasks_raw):
        if isinstance(item, str):
            item = {"goal": item}
        if not isinstance(item, dict):
            raise ValidationError(f"subtask {index} must be an object or string")
        extra = set(item) - {"goal", "input", "tools"}
        if extra:
            raise ValidationError(f"subtask {index} has unknown fields: {sorted(extra)}")
        tools = item.get("tools", [])
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise ValidationError(f"subtask {index} tools must be a list of names")
        for name in tools:
            if not TOOL_NAME_RE.match(name):
                raise ValidationError(f"subtask {index} tool name {name!r} is invalid")
            if name not in allowed_tools:
                raise ValidationError(f"subtask {index} requests tool {name!r} which is not allowed")
        subtasks.append(
            {
                "goal": require_text(item.get("goal"), f"subtask {index} goal", max_chars),
                "input": require_text(item.get("input"), f"subtask {index} input", max_chars, allow_empty=True),
                "tools": sorted(set(tools)),
            }
        )
    return {"id": task_id, "goal": goal, "subtasks": subtasks}


ACTION_KEYS = {"type", "target", "args"}


def validate_result(raw: Any, max_chars: int, max_actions: int) -> dict:
    """Agent output is untrusted too. Keep only known fields with bounded size."""
    if not isinstance(raw, dict):
        raise ValidationError("result must be a JSON object")
    subtask_id = require_id(raw.get("subtask"), "result subtask")
    task_id = require_id(raw.get("task"), "result task")
    status = raw.get("status")
    if status not in ("ok", "error"):
        raise ValidationError("result status must be ok or error")
    text = require_text(raw.get("text", ""), "result text", max_chars, allow_empty=True)
    error = require_text(raw.get("error", ""), "result error", 2000, allow_empty=True)
    actions_raw = raw.get("proposed_actions", [])
    if not isinstance(actions_raw, list):
        raise ValidationError("proposed_actions must be a list")
    actions = []
    for action in actions_raw[:max_actions]:
        if not isinstance(action, dict):
            continue
        clean = {k: action[k] for k in ACTION_KEYS if k in action}
        if not isinstance(clean.get("type"), str) or not TOOL_NAME_RE.match(clean["type"]):
            continue
        encoded = json.dumps(clean, default=str)
        if len(encoded) > 8000:
            continue
        actions.append(json.loads(encoded))
    return {
        "subtask": subtask_id,
        "task": task_id,
        "status": status,
        "text": text,
        "error": error,
        "proposed_actions": actions,
    }


def ensure_groups(client: redis.Redis, pairs=CONSUMER_GROUPS) -> None:
    for stream, group in pairs:
        try:
            client.xgroup_create(stream, group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise


def touch_heartbeat(path: str) -> None:
    if path:
        try:
            Path(path).write_text(str(int(time.time())), encoding="utf-8")
        except OSError:
            pass
