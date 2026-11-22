"""Controller: splits tasks into subtasks, collects results, recovers stuck work.

The controller has no Kubernetes API access and no model credentials. It only
talks to Redis. KEDA starts it when there is inbound work or results to collect,
and on a short sweep window every few minutes, then scales it back to zero.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import socket
import threading
import time
from dataclasses import dataclass, field

import redis

from . import common as c

LOG = c.get_logger("controller")


@dataclass
class ControllerConfig:
    consumer: str = field(default_factory=lambda: c.env_str("POD_NAME") or socket.gethostname())
    allowed_tools: list[str] = field(default_factory=lambda: c.env_list("ALLOWED_TOOLS"))
    max_subtasks: int = field(default_factory=lambda: c.env_int("MAX_SUBTASKS", 50, 1, 500))
    max_chars: int = field(default_factory=lambda: c.env_int("MAX_INPUT_CHARS", 32000, 100, 1_000_000))
    max_result_chars: int = field(default_factory=lambda: c.env_int("MAX_RESULT_CHARS", 64000, 100, 2_000_000))
    max_actions: int = field(default_factory=lambda: c.env_int("MAX_PROPOSED_ACTIONS", 20, 0, 200))
    max_attempts: int = field(default_factory=lambda: c.env_int("MAX_ATTEMPTS", 3, 1, 20))
    visibility_ms: int = field(default_factory=lambda: 1000 * c.env_int("VISIBILITY_TIMEOUT_SECONDS", 1200, 30, 86400))
    controller_claim_ms: int = field(default_factory=lambda: 1000 * c.env_int("CONTROLLER_CLAIM_IDLE_SECONDS", 120, 10, 86400))
    sweep_interval: int = field(default_factory=lambda: c.env_int("SWEEP_INTERVAL_SECONDS", 60, 5, 3600))
    record_ttl: int = field(default_factory=lambda: c.env_int("RECORD_TTL_SECONDS", 86400, 600, 30 * 86400))
    token_ttl: int = field(default_factory=lambda: c.env_int("TOKEN_TTL_SECONDS", 1800, 60, 86400))
    calls_per_token: int = field(default_factory=lambda: c.env_int("MAX_CALLS_PER_TOKEN", 20, 1, 1000))
    model: str = field(default_factory=lambda: c.env_str("MODEL_DEFAULT", "claude-sonnet-5-5"))
    stream_maxlen: int = field(default_factory=lambda: c.env_int("STREAM_MAXLEN", 10000, 100, 10_000_000))
    heartbeat_file: str = field(default_factory=lambda: c.env_str("HEARTBEAT_FILE", "/tmp/heartbeat"))


class Controller:
    def __init__(self, client: redis.Redis, cfg: ControllerConfig | None = None):
        self.r = client
        self.cfg = cfg or ControllerConfig()
        self._last_sweep = 0.0

    # Tasks in

    def handle_task(self, msg_id: str, fields: dict) -> None:
        raw = None
        try:
            raw = json.loads(fields.get("data", ""))
            task = c.validate_task(raw, self.cfg.allowed_tools, self.cfg.max_subtasks, self.cfg.max_chars)
        except (json.JSONDecodeError, c.ValidationError) as exc:
            task_id = raw.get("id") if isinstance(raw, dict) else None
            safe_id = task_id if isinstance(task_id, str) and c.ID_RE.match(task_id) else "unknown"
            self.r.xadd(c.STREAM_COMPLETED, {"task": safe_id, "status": "rejected", "error": str(exc)[:500]},
                        maxlen=self.cfg.stream_maxlen, approximate=True)
            self.r.xack(c.STREAM_TASKS, c.GROUP_CONTROLLER, msg_id)
            c.log(LOG, logging.WARNING, "task rejected", task=safe_id, reason=str(exc)[:200])
            return

        task_id = task["id"]
        jkey = c.job_key(task_id)
        total = len(task["subtasks"])
        # HSETNX makes redelivery of the same task message idempotent.
        if not self.r.hsetnx(jkey, "status", "running"):
            existing = self.r.hget(jkey, "status")
            if existing != "running" or self.r.hget(jkey, "msg") not in (None, msg_id):
                self.r.xack(c.STREAM_TASKS, c.GROUP_CONTROLLER, msg_id)
                c.log(LOG, logging.WARNING, "duplicate task id ignored", task=task_id)
                return
        self.r.hset(jkey, mapping={"total": total, "goal": task["goal"][:500], "created": int(time.time()), "msg": msg_id})
        self.r.hsetnx(jkey, "done", 0)
        self.r.expire(jkey, self.cfg.record_ttl)

        for index, sub in enumerate(task["subtasks"]):
            sub_id = f"{task_id}.{index}"
            rkey = c.record_key(sub_id)
            if self.r.exists(rkey):
                continue  # already enqueued on an earlier delivery
            token = secrets.token_urlsafe(32)
            digest = c.token_digest(token)
            pipe = self.r.pipeline(transaction=True)
            pipe.hset(rkey, mapping={
                "id": sub_id,
                "task": task_id,
                "goal": sub["goal"],
                "input": sub["input"],
                "tools": json.dumps(sub["tools"]),
                "model": self.cfg.model,
                "token": token,
                "attempts": 1,
                "status": "queued",
                "created": int(time.time()),
            })
            pipe.expire(rkey, self.cfg.record_ttl)
            pipe.hset(c.token_key(digest), mapping={
                "subtask": sub_id,
                "task": task_id,
                "tools": json.dumps(sub["tools"]),
                "max_calls": self.cfg.calls_per_token,
            })
            pipe.expire(c.token_key(digest), self.cfg.token_ttl)
            # The token and body ride on the stream message. Redis hands a message only to
            # the consumer that claims it, so an agent never sees another subtask's token,
            # and agents need no read access to the records at all.
            pipe.xadd(c.STREAM_SUBTASKS, subtask_message({
                "id": sub_id, "task": task_id, "goal": sub["goal"], "input": sub["input"],
                "tools": json.dumps(sub["tools"]), "model": self.cfg.model, "token": token}))
            pipe.execute()
        self.r.xack(c.STREAM_TASKS, c.GROUP_CONTROLLER, msg_id)
        c.log(LOG, logging.INFO, "task accepted", task=task_id, subtasks=total)

    # Results in

    def handle_result(self, msg_id: str, fields: dict) -> None:
        try:
            result = c.validate_result(json.loads(fields.get("data", "")), self.cfg.max_result_chars, self.cfg.max_actions)
        except (json.JSONDecodeError, c.ValidationError) as exc:
            self.r.xack(c.STREAM_RESULTS, c.GROUP_CONTROLLER, msg_id)
            c.log(LOG, logging.WARNING, "malformed result dropped", reason=str(exc)[:200])
            return
        record = self.r.hgetall(c.record_key(result["subtask"]))
        if not record or record.get("task") != result["task"]:
            # A result for a subtask we never issued. Possible spoofing; never trust it.
            self.r.xack(c.STREAM_RESULTS, c.GROUP_CONTROLLER, msg_id)
            c.log(LOG, logging.WARNING, "result for unknown subtask dropped", subtask=result["subtask"])
            return
        self.finalize_subtask(record, result)
        self.r.xack(c.STREAM_RESULTS, c.GROUP_CONTROLLER, msg_id)

    def finalize_subtask(self, record: dict, result: dict) -> None:
        task_id = record["task"]
        sub_id = record["id"]
        stored = {
            "status": result["status"],
            "text": result.get("text", ""),
            "error": result.get("error", ""),
            "proposed_actions": result.get("proposed_actions", []),
        }
        first_time = self.r.hsetnx(c.job_results_key(task_id), sub_id, json.dumps(stored))
        self.r.expire(c.job_results_key(task_id), self.cfg.record_ttl)
        if record.get("token"):
            # Revoke the vault token the moment its subtask is done.
            self.r.delete(c.token_key(c.token_digest(record["token"])))
        self.r.hset(c.record_key(sub_id), mapping={"status": "done" if result["status"] == "ok" else "failed", "token": ""})
        if not first_time:
            return
        for action in stored["proposed_actions"]:
            # Proposals are never executed by this system. A human approves them elsewhere.
            self.r.xadd(c.STREAM_APPROVALS, {"task": task_id, "subtask": sub_id, "action": json.dumps(action)},
                        maxlen=self.cfg.stream_maxlen, approximate=True)
        jkey = c.job_key(task_id)
        done = self.r.hincrby(jkey, "done", 1)
        if result["status"] != "ok":
            self.r.hincrby(jkey, "failed", 1)
        total = int(self.r.hget(jkey, "total") or 0)
        if total and done >= total:
            failed = int(self.r.hget(jkey, "failed") or 0)
            status = "completed" if failed == 0 else ("failed" if failed == total else "partial")
            self.r.hset(jkey, mapping={"status": status, "finished": int(time.time())})
            self.r.xadd(c.STREAM_COMPLETED, {"task": task_id, "status": status},
                        maxlen=self.cfg.stream_maxlen, approximate=True)
            c.log(LOG, logging.INFO, "task finished", task=task_id, status=status, subtasks=total, failed=failed)

    # Recovery

    def sweep(self) -> dict:
        """Recover work stranded by crashed pods and keep streams bounded."""
        stats = {"requeued": 0, "dead_lettered": 0, "reclaimed_controller": 0}
        # Subtasks whose agent died after claiming them.
        start = "0-0"
        while True:
            start, claimed, _deleted = self.r.xautoclaim(
                c.STREAM_SUBTASKS, c.GROUP_AGENTS, self.cfg.consumer,
                min_idle_time=self.cfg.visibility_ms, start_id=start, count=100)
            for msg_id, fields in claimed:
                self._requeue_or_dead_letter(msg_id, fields, stats)
            if start == "0-0" or not claimed:
                break
        # Tasks or results a previous controller pod claimed but never finished.
        for stream, handler in ((c.STREAM_TASKS, self.handle_task), (c.STREAM_RESULTS, self.handle_result)):
            _next, claimed, _deleted = self.r.xautoclaim(
                stream, c.GROUP_CONTROLLER, self.cfg.consumer,
                min_idle_time=self.cfg.controller_claim_ms, start_id="0-0", count=100)
            for msg_id, fields in claimed:
                stats["reclaimed_controller"] += 1
                handler(msg_id, fields)
        for stream in (c.STREAM_TASKS, c.STREAM_SUBTASKS, c.STREAM_RESULTS):
            self._trim_consumed(stream)
        for stream in (c.STREAM_COMPLETED, c.STREAM_APPROVALS, c.STREAM_DLQ):
            self.r.xtrim(stream, maxlen=self.cfg.stream_maxlen, approximate=True)
        self._last_sweep = time.time()
        if any(stats.values()):
            c.log(LOG, logging.INFO, "sweep", **stats)
        return stats

    def _requeue_or_dead_letter(self, msg_id: str, fields: dict, stats: dict) -> None:
        sub_id = fields.get("id", "")
        rkey = c.record_key(sub_id) if c.ID_RE.match(sub_id or "") else None
        record = self.r.hgetall(rkey) if rkey else {}
        if not record or record.get("status") in ("done", "failed"):
            self.r.xack(c.STREAM_SUBTASKS, c.GROUP_AGENTS, msg_id)
            return
        attempts = int(record.get("attempts") or 1)
        if attempts >= self.cfg.max_attempts:
            self.r.xadd(c.STREAM_DLQ, {"id": sub_id, "task": record.get("task", ""), "attempts": attempts},
                        maxlen=self.cfg.stream_maxlen, approximate=True)
            self.finalize_subtask(record, {"status": "error", "text": "",
                                           "error": f"gave up after {attempts} attempts", "proposed_actions": []})
            self.r.xack(c.STREAM_SUBTASKS, c.GROUP_AGENTS, msg_id)
            stats["dead_lettered"] += 1
            return
        # Requeue as a fresh entry so it shows up as lag and KEDA starts a new agent.
        pipe = self.r.pipeline(transaction=True)
        pipe.hincrby(rkey, "attempts", 1)
        pipe.xadd(c.STREAM_SUBTASKS, subtask_message(record))
        pipe.xack(c.STREAM_SUBTASKS, c.GROUP_AGENTS, msg_id)
        if record.get("token"):
            pipe.expire(c.token_key(c.token_digest(record["token"])), self.cfg.token_ttl)
        pipe.execute()
        stats["requeued"] += 1

    def _trim_consumed(self, stream: str) -> None:
        """Drop entries every group has already acknowledged.

        Trimming only below the oldest unacknowledged entry and the group's last
        delivered id keeps XINFO GROUPS lag valid, which KEDA depends on.
        """
        try:
            groups = self.r.xinfo_groups(stream)
        except redis.ResponseError:
            return
        floor = None
        for g in groups:
            candidate = g.get("last-delivered-id") or "0-0"
            if g.get("pending"):
                summary = self.r.xpending(stream, g["name"])
                if summary and summary.get("min"):
                    candidate = summary["min"]
            if floor is None or _id_lt(candidate, floor):
                floor = candidate
        if floor and floor != "0-0":
            self.r.xtrim(stream, minid=floor, approximate=False)

    # Main loop

    def run(self, stop: threading.Event) -> None:
        c.ensure_groups(self.r)
        self.sweep()
        streams = {c.STREAM_TASKS: ">", c.STREAM_RESULTS: ">"}
        handlers = {c.STREAM_TASKS: self.handle_task, c.STREAM_RESULTS: self.handle_result}
        c.log(LOG, logging.INFO, "controller ready", consumer=self.cfg.consumer)
        while not stop.is_set():
            c.touch_heartbeat(self.cfg.heartbeat_file)
            try:
                batch = self.r.xreadgroup(c.GROUP_CONTROLLER, self.cfg.consumer, streams, count=20, block=2000)
                for stream, messages in batch or []:
                    for msg_id, fields in messages:
                        handlers[stream](msg_id, fields)
                if time.time() - self._last_sweep >= self.cfg.sweep_interval:
                    self.sweep()
            except redis.ConnectionError as exc:
                c.log(LOG, logging.WARNING, "redis unavailable, retrying", error=str(exc)[:200])
                stop.wait(2)
            except redis.ResponseError as exc:
                if "NOGROUP" not in str(exc):
                    raise
                # Redis restarted without its data. Recreate the groups and carry on.
                c.log(LOG, logging.WARNING, "consumer groups missing, recreating")
                c.ensure_groups(self.r)
        c.log(LOG, logging.INFO, "controller stopped")


def subtask_message(record: dict) -> dict:
    """Fields of a subtask stream entry, built from the controller's record."""
    body = {
        "task": record["task"],
        "goal": record.get("goal", ""),
        "input": record.get("input", ""),
        "tools": json.loads(record.get("tools") or "[]"),
        "model": record.get("model", ""),
    }
    return {"id": record["id"], "token": record["token"], "body": json.dumps(body)}


def _id_lt(a: str, b: str) -> bool:
    am, _, aseq = a.partition("-")
    bm, _, bseq = b.partition("-")
    return (int(am), int(aseq or 0)) < (int(bm), int(bseq or 0))


def main() -> None:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    Controller(c.connect_redis()).run(stop)


if __name__ == "__main__":
    os.umask(0o077)
    main()
