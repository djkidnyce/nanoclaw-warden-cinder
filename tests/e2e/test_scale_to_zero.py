"""Scale from zero to N and back to zero, with real agent processes, the real vault,
the real controller loop and a real Redis using the production ACLs."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from nanoclaw import common as c
from nanoclaw.testing import mock_upstream
from tests.conftest import PASSWORDS, connect, free_port
from tests.e2e.keda_emulator import Emulator

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("fresh")]
REPORT = Path(__file__).resolve().parents[2] / ".pytest_cache" / "e2e-timeline.json"


@pytest.fixture
def upstream():
    server, state = mock_upstream.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()


def submit(fresh, task_id, n):
    fresh["client"].xadd(c.STREAM_TASKS, {"data": json.dumps(
        {"id": task_id, "goal": "g", "subtasks": [f"part {i}" for i in range(n)]})})


def all_done(fresh, ids):
    return all(fresh["client"].hget(c.job_key(t), "status") in ("completed", "partial", "failed") for t in ids)


def test_zero_to_n_to_zero(redis_server, fresh, upstream):
    url, upstate = upstream
    emu = Emulator(redis_server["port"], PASSWORDS, url, free_port(), max_jobs=5)
    try:
        # Idle: nothing runs.
        emu.run_until(lambda e: False, timeout=1.0)
        assert all(s.agents == 0 and s.vault == 0 and s.controller == 0 for s in emu.timeline)

        ids = ["burst-a", "burst-b", "burst-c"]
        for t in ids:
            submit(fresh, t, 4)  # 12 subtasks
        assert emu.run_until(lambda e: all_done(fresh, ids), timeout=90), "tasks did not finish"
        peak = max(s.agents for s in emu.timeline)
        assert peak == 5, f"expected to hit maxReplicaCount 5, peaked at {peak}"
        assert all(s.agents <= 5 for s in emu.timeline)
        assert any(s.vault for s in emu.timeline) and any(s.controller for s in emu.timeline)
        for t in ids:
            assert fresh["client"].hget(c.job_key(t), "status") == "completed"
        assert len(upstate.requests) == 12, "each subtask calls the model exactly once"

        # Back to zero once the cooldowns pass.
        assert emu.run_until(lambda e: e.idle() and e.state().lag == 0, timeout=20), "did not scale back to zero"
        assert all(rc == 0 for rc in emu.exit_codes), emu.exit_codes
        assert fresh["keda"].xpending(c.STREAM_SUBTASKS, c.GROUP_AGENTS)["pending"] == 0
        REPORT.parent.mkdir(exist_ok=True)
        REPORT.write_text(json.dumps([s.__dict__ for s in emu.timeline]))
    finally:
        emu.shutdown()


def test_second_wave_scales_up_again(redis_server, fresh, upstream):
    url, _ = upstream
    emu = Emulator(redis_server["port"], PASSWORDS, url, free_port(), max_jobs=3)
    try:
        submit(fresh, "wave-1", 2)
        assert emu.run_until(lambda e: all_done(fresh, ["wave-1"]), timeout=60)
        assert emu.run_until(lambda e: e.idle(), timeout=20)
        mark = len(emu.timeline)
        submit(fresh, "wave-2", 6)
        assert emu.run_until(lambda e: all_done(fresh, ["wave-2"]), timeout=60)
        assert max(s.agents for s in emu.timeline[mark:]) == 3
        assert emu.run_until(lambda e: e.idle(), timeout=20)
    finally:
        emu.shutdown()


def test_crashed_agent_work_is_recovered(redis_server, fresh, upstream):
    """An agent dies after claiming. The controller's sweep window requeues the work,
    lag reappears, and a fresh agent finishes it."""
    url, _ = upstream
    emu = Emulator(redis_server["port"], PASSWORDS, url, free_port(), max_jobs=2, visibility_ms=1500)
    try:
        submit(fresh, "crash", 1)
        # Let the controller fan out, but grab the work before any agent spawns: a pod that dies.
        emu._reconcile_controller(False)
        deadline = time.time() + 10
        while emu.lag(c.STREAM_SUBTASKS, c.GROUP_AGENTS) == 0 and time.time() < deadline:
            time.sleep(0.1)
        connect(redis_server, "agent").xreadgroup(c.GROUP_AGENTS, "dead-pod", {c.STREAM_SUBTASKS: ">"}, count=1)
        assert emu.lag(c.STREAM_SUBTASKS, c.GROUP_AGENTS) == 0
        assert emu.run_until(lambda e: all_done(fresh, ["crash"]), timeout=60, sweep_every=2.0)
        assert fresh["client"].hget(c.job_key("crash"), "status") == "completed"
    finally:
        emu.shutdown()


def test_cold_vault_does_not_fail_agents(redis_server, fresh, upstream):
    url, _ = upstream
    emu = Emulator(redis_server["port"], PASSWORDS, url, free_port(), max_jobs=4, vault_cold_start=4.0)
    try:
        submit(fresh, "cold", 4)
        assert emu.run_until(lambda e: all_done(fresh, ["cold"]), timeout=60)
        assert fresh["client"].hget(c.job_key("cold"), "status") == "completed"
        assert all(rc == 0 for rc in emu.exit_codes), emu.exit_codes
    finally:
        emu.shutdown()
