"""A faithful stand in for the KEDA behavior this system relies on.

It reads the same Redis metric KEDA reads (XINFO GROUPS lag, as the keda user) and
applies the same decisions the manifests configure:

  ScaledJob nanoclaw-agent   strategy "accurate", lagCount 1, maxReplicaCount N
  ScaledObject vault         active while agent pods exist or subtask lag > 0
  ScaledObject controller    active while task or result lag > 0, plus a sweep window

Agents run as real OS processes (python -m nanoclaw agent), one per "Job", so exit
codes, cold starts and retries behave like pods. The vault and controller run the
real server and loop code in threads that start and stop like replicas.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from nanoclaw import common as c, controller as ctl_mod, vault as vault_mod

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Sample:
    t: float
    agents: int
    vault: int
    controller: int
    lag: int


@dataclass
class Emulator:
    redis_port: int
    passwords: dict
    upstream_url: str
    vault_port: int
    max_jobs: int = 5
    cooldown: float = 2.0
    vault_cold_start: float = 1.5
    poll: float = 0.3
    visibility_ms: int = 1_200_000
    timeline: list[Sample] = field(default_factory=list)
    jobs: list[subprocess.Popen] = field(default_factory=list)
    exit_codes: list[int] = field(default_factory=list)
    retries: int = 0

    def __post_init__(self):
        import redis
        self.keda = redis.Redis(host="127.0.0.1", port=self.redis_port, username="keda",
                                password=self.passwords["keda"], decode_responses=True)
        self._vault_server = None
        self._vault_idle_since = None
        self._ctl_thread = None
        self._ctl_stop = None
        self._ctl_idle_since = None
        self._job_seq = 0
        self._job_attempts: dict[str, int] = {}
        self.start_time = time.time()

    # Metrics, exactly as KEDA reads them

    def lag(self, stream, group) -> int:
        for g in self.keda.xinfo_groups(stream):
            if g["name"] == group:
                return int(g["lag"] or 0)
        return 0

    # Agent Jobs

    def _env(self, role):
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(ROOT / "src"), "REDIS_HOST": "127.0.0.1", "REDIS_PORT": str(self.redis_port),
                    "REDIS_USERNAME": role, "REDIS_PASSWORD": self.passwords[role],
                    "VAULT_URL": f"http://127.0.0.1:{self.vault_port}", "VAULT_WAIT_SECONDS": "30",
                    "CLAIM_WAIT_SECONDS": "1", "LOG_LEVEL": "WARNING"})
        return env

    def _spawn(self, job_name):
        proc = subprocess.Popen([sys.executable, "-m", "nanoclaw", "agent"], env={**self._env("agent"),
                                "CONSUMER_NAME": job_name}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.job_name = job_name  # type: ignore[attr-defined]
        self.jobs.append(proc)

    def _reconcile_jobs(self):
        running = []
        for proc in self.jobs:
            rc = proc.poll()
            if rc is None:
                running.append(proc)
                continue
            self.exit_codes.append(rc)
            if rc == 75 and self._job_attempts.get(proc.job_name, 0) < 3:  # backoffLimit: same Job name
                self._job_attempts[proc.job_name] = self._job_attempts.get(proc.job_name, 0) + 1
                self.retries += 1
                self._spawn(proc.job_name)
                running.append(self.jobs[-1])
        self.jobs = [p for p in self.jobs if p.poll() is None]
        queue = self.lag(c.STREAM_SUBTASKS, c.GROUP_AGENTS)
        max_scale = min(self.max_jobs, math.ceil(queue / 1))
        running_count = len(self.jobs)
        pending = 0  # processes start immediately; no unscheduled pods here
        if max_scale + running_count > self.max_jobs:
            to_create = self.max_jobs - running_count
        else:
            to_create = max_scale - pending
        for _ in range(max(0, to_create)):
            self._job_seq += 1
            self._spawn(f"nanoclaw-agent-{self._job_seq:04d}")

    # Vault replica

    def _reconcile_vault(self):
        active = bool(self.jobs) or self.lag(c.STREAM_SUBTASKS, c.GROUP_AGENTS) > 0
        if active:
            self._vault_idle_since = None
            if self._vault_server is None:
                self._vault_server = "starting"
                threading.Thread(target=self._start_vault, daemon=True).start()
        elif self._vault_server not in (None, "starting"):
            self._vault_idle_since = self._vault_idle_since or time.time()
            if time.time() - self._vault_idle_since >= self.cooldown:
                self._vault_server.shutdown()
                self._vault_server.server_close()
                self._vault_server = None

    def _start_vault(self):
        import redis
        time.sleep(self.vault_cold_start)  # image pull, scheduling, process start
        client = redis.Redis(host="127.0.0.1", port=self.redis_port, username="vault",
                             password=self.passwords["vault"], decode_responses=True)
        cfg = vault_mod.VaultConfig(upstream_url=self.upstream_url, api_key="test-upstream-key",
                                    model_allowlist=["claude-sonnet-5-5"], rate_per_min=10_000,
                                    listen_port=self.vault_port)
        self._vault_server = vault_mod.serve(vault_mod.Vault(client, cfg), threading.Event(), host="127.0.0.1")

    # Controller replica

    def _reconcile_controller(self, sweep_window: bool):
        active = sweep_window or self.lag(c.STREAM_TASKS, c.GROUP_CONTROLLER) > 0 \
            or self.lag(c.STREAM_RESULTS, c.GROUP_CONTROLLER) > 0
        if active:
            self._ctl_idle_since = None
            if self._ctl_thread is None:
                import redis
                client = redis.Redis(host="127.0.0.1", port=self.redis_port, username="controller",
                                     password=self.passwords["controller"], decode_responses=True)
                cfg = ctl_mod.ControllerConfig(consumer=f"controller-{int(time.time() * 1000)}",
                                               allowed_tools=["create_ticket"], visibility_ms=self.visibility_ms,
                                               controller_claim_ms=1000, sweep_interval=1, heartbeat_file="")
                self._ctl_stop = threading.Event()
                self._ctl_thread = threading.Thread(target=ctl_mod.Controller(client, cfg).run,
                                                    args=(self._ctl_stop,), daemon=True)
                self._ctl_thread.start()
        elif self._ctl_thread is not None:
            self._ctl_idle_since = self._ctl_idle_since or time.time()
            if time.time() - self._ctl_idle_since >= self.cooldown:
                self._ctl_stop.set()
                self._ctl_thread.join(5)
                self._ctl_thread = None

    def state(self) -> Sample:
        vault_up = 1 if self._vault_server not in (None, "starting") else 0
        return Sample(round(time.time() - self.start_time, 2), len(self.jobs), vault_up,
                      1 if self._ctl_thread else 0, self.lag(c.STREAM_SUBTASKS, c.GROUP_AGENTS))

    def run_until(self, done, timeout: float, sweep_every: float | None = None):
        deadline = time.time() + timeout
        last_sweep = time.time()
        while time.time() < deadline:
            window = bool(sweep_every) and time.time() - last_sweep >= sweep_every
            if window:
                last_sweep = time.time()
            self._reconcile_controller(window)
            self._reconcile_jobs()
            self._reconcile_vault()
            self.timeline.append(self.state())
            if done(self):
                return True
            time.sleep(self.poll)
        return False

    def idle(self) -> bool:
        s = self.state()
        return s.agents == 0 and s.vault == 0 and s.controller == 0 and self._vault_server is None

    def shutdown(self):
        for p in self.jobs:
            p.kill()
        if self._vault_server not in (None, "starting"):
            self._vault_server.shutdown()
        if self._ctl_stop:
            self._ctl_stop.set()
        if self._ctl_thread:
            self._ctl_thread.join(5)
        for p in self.jobs:
            p.wait(5)
