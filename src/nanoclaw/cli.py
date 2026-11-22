"""Operator CLI: submit tasks and read results, approvals and the dead letter queue.

Connects as the restricted "client" Redis user, which can only append to the
task stream and read job state. Typical use is through a port forward:

  kubectl -n nanoclaw port-forward svc/redis 6379:6379
  REDIS_HOST=127.0.0.1 REDIS_USERNAME=client REDIS_PASSWORD_FILE=./client.pass \\
    python -m nanoclaw.cli submit --goal "Summarize the incident" --subtask "Draft a timeline"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

from . import common as c


def cmd_submit(args, r) -> int:
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            task = json.load(fh)
    else:
        task = {"goal": args.goal}
        subs = []
        for s in args.subtask or []:
            subs.append({"goal": s, "input": args.input or "", "tools": args.tool or []})
        if subs:
            task["subtasks"] = subs
    task.setdefault("id", args.id or f"t{time.strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:6]}")
    r.xadd(c.STREAM_TASKS, {"data": json.dumps(task)})
    print(task["id"])
    if args.wait:
        return cmd_wait(argparse.Namespace(task=task["id"], timeout=args.wait), r)
    return 0


def cmd_status(args, r) -> int:
    print(json.dumps(r.hgetall(c.job_key(args.task)) or {"status": "unknown"}, indent=2))
    return 0


def cmd_results(args, r) -> int:
    raw = r.hgetall(c.job_results_key(args.task))
    print(json.dumps({k: json.loads(v) for k, v in sorted(raw.items())}, indent=2))
    return 0


def cmd_wait(args, r) -> int:
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        status = r.hget(c.job_key(args.task), "status")
        if status in ("completed", "partial", "failed"):
            print(status)
            return 0 if status == "completed" else 2
        time.sleep(2)
    print("timeout", file=sys.stderr)
    return 3


def cmd_stream(stream):
    def run(args, r) -> int:
        for msg_id, fields in r.xrevrange(stream, count=args.count):
            print(msg_id, json.dumps(fields))
        return 0
    return run


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="nanoclaw")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit", help="queue a task")
    s.add_argument("--goal", help="overall goal")
    s.add_argument("--subtask", action="append", help="repeat for each subtask")
    s.add_argument("--input", help="untrusted input text passed to every subtask")
    s.add_argument("--tool", action="append", help="tool name to offer (must be in ALLOWED_TOOLS)")
    s.add_argument("--file", help="JSON task file instead of flags")
    s.add_argument("--id", help="task id (default generated)")
    s.add_argument("--wait", type=int, default=0, help="seconds to wait for completion")
    s.set_defaults(fn=cmd_submit)
    for name, fn in (("status", cmd_status), ("results", cmd_results)):
        x = sub.add_parser(name)
        x.add_argument("task")
        x.set_defaults(fn=fn)
    w = sub.add_parser("wait")
    w.add_argument("task")
    w.add_argument("--timeout", type=int, default=600)
    w.set_defaults(fn=cmd_wait)
    for name, stream in (("completed", c.STREAM_COMPLETED), ("approvals", c.STREAM_APPROVALS), ("dlq", c.STREAM_DLQ)):
        x = sub.add_parser(name)
        x.add_argument("--count", type=int, default=20)
        x.set_defaults(fn=cmd_stream(stream))
    args = p.parse_args(argv)
    if args.cmd == "submit" and not (args.goal or args.file):
        p.error("submit needs --goal or --file")
    return args.fn(args, c.connect_redis())


if __name__ == "__main__":
    sys.exit(main())
