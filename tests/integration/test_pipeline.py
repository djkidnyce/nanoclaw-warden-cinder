"""End to end behavior of controller, agent and vault against a real Redis with production ACLs."""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest
import redis

from nanoclaw import agent, common as c, controller, vault
from nanoclaw.testing import mock_upstream
from tests.conftest import connect, free_port

pytestmark = pytest.mark.usefixtures("fresh")


def submit(fresh, task):
    fresh["client"].xadd(c.STREAM_TASKS, {"data": json.dumps(task)})


def drain_controller(ctl: controller.Controller, rounds: int = 3):
    for _ in range(rounds):
        batch = ctl.r.xreadgroup(c.GROUP_CONTROLLER, ctl.cfg.consumer,
                                 {c.STREAM_TASKS: ">", c.STREAM_RESULTS: ">"}, count=100, block=50)
        for stream, messages in batch or []:
            for msg_id, fields in messages:
                (ctl.handle_task if stream == c.STREAM_TASKS else ctl.handle_result)(msg_id, fields)


def lag(client, stream, group):
    """Exactly what KEDA reads for lagCount: XINFO GROUPS, as the keda user."""
    for g in client.xinfo_groups(stream):
        if g["name"] == group:
            return g["lag"]
    raise AssertionError("group missing")


@pytest.fixture
def ctl(fresh):
    cfg = controller.ControllerConfig(consumer="ctl-0", allowed_tools=["create_ticket", "send_email"])
    return controller.Controller(fresh["controller"], cfg)


@pytest.fixture
def stack(redis_server, fresh):
    """Mock upstream plus a live vault server connected as the vault role."""
    upstream, upstate = mock_upstream.start()
    port = free_port()
    cfg = vault.VaultConfig(upstream_url=f"http://127.0.0.1:{upstream.server_port}", api_key="test-upstream-key",
                            model_allowlist=["claude-sonnet-5-5"], max_tokens_cap=512, rate_per_min=1000,
                            listen_port=port)
    v = vault.Vault(connect(redis_server, "vault"), cfg)
    server = vault.serve(v, threading.Event(), host="127.0.0.1")
    yield {"vault_url": f"http://127.0.0.1:{port}", "upstate": upstate, "vault": v}
    server.shutdown()
    upstream.shutdown()


def run_agent(redis_server, stack, consumer="job-1"):
    vc = agent.VaultClient(stack["vault_url"], wait_seconds=10, request_timeout=10)
    return agent.run_once(connect(redis_server, "agent"), vc, consumer,
                          system_prompt="sys", max_tokens=256, claim_wait_ms=100)


class TestStreams:
    def test_bootstrap_creates_all_groups(self, fresh):
        for stream, group in c.CONSUMER_GROUPS:
            names = [g["name"] for g in fresh["keda"].xinfo_groups(stream)]
            assert group in names

    def test_controller_never_rereads_its_own_subtasks(self, fresh, ctl):
        submit(fresh, {"id": "t1", "goal": "g", "subtasks": ["a", "b", "c"]})
        drain_controller(ctl, rounds=5)
        assert fresh["testadmin"].xlen(c.STREAM_SUBTASKS) == 3
        assert fresh["testadmin"].xlen(c.STREAM_TASKS) == 1
        assert lag(fresh["keda"], c.STREAM_TASKS, c.GROUP_CONTROLLER) == 0

    def test_subtask_message_carries_body_and_token_only(self, fresh, ctl):
        submit(fresh, {"id": "t1", "goal": "g", "subtasks": [{"goal": "a", "input": "customer text"}]})
        drain_controller(ctl)
        (_, fields), = fresh["testadmin"].xrange(c.STREAM_SUBTASKS)
        assert set(fields) == {"id", "token", "body"}
        assert json.loads(fields["body"]) == {"task": "t1", "goal": "a", "input": "customer text",
                                              "tools": [], "model": "claude-sonnet-5-5"}

    def test_agents_cannot_see_each_others_tokens(self, redis_server, fresh, ctl):
        """Review finding: agents used to read every subtask record, tokens included."""
        submit(fresh, {"id": "t1", "goal": "g", "subtasks": ["a", "b"]})
        drain_controller(ctl)
        mine = connect(redis_server, "agent")
        other = connect(redis_server, "agent")
        msg_a, fields_a = agent.claim(mine, "job-a", 10)
        assert fields_a["token"]
        # The other agent cannot read records, cannot list the stream, and cannot see
        # pending entries that belong to job-a.
        with pytest.raises(redis.exceptions.NoPermissionError):
            other.hgetall(c.record_key("t1.0"))
        with pytest.raises(redis.exceptions.NoPermissionError):
            other.xrange(c.STREAM_SUBTASKS)
        peek = other.xreadgroup(c.GROUP_AGENTS, "job-b", {c.STREAM_SUBTASKS: "0"}, count=10)
        assert all(not messages for _stream, messages in peek or [])
        _, fields_b = agent.claim(other, "job-b", 10)
        assert fields_b["token"] != fields_a["token"]

    def test_lag_counts_unclaimed_work_for_keda(self, redis_server, fresh, ctl):
        submit(fresh, {"id": "t1", "goal": "g", "subtasks": ["a", "b", "c"]})
        drain_controller(ctl)
        assert lag(fresh["keda"], c.STREAM_SUBTASKS, c.GROUP_AGENTS) == 3
        msg_id, _ = agent.claim(connect(redis_server, "agent"), "job-a", 10)
        assert msg_id
        assert lag(fresh["keda"], c.STREAM_SUBTASKS, c.GROUP_AGENTS) == 2
        assert fresh["keda"].xpending(c.STREAM_SUBTASKS, c.GROUP_AGENTS)["pending"] == 1

    def test_duplicate_task_message_does_not_double_enqueue(self, fresh, ctl):
        task = {"id": "dup", "goal": "g", "subtasks": ["a", "b"]}
        submit(fresh, task)
        submit(fresh, task)
        drain_controller(ctl)
        assert fresh["testadmin"].xlen(c.STREAM_SUBTASKS) == 2

    @pytest.mark.parametrize("bad", [
        {"id": "x1", "goal": "g", "subtasks": [{"goal": "a", "tools": ["shell_exec"]}]},
        {"id": "bad id", "goal": "g"},
        {"id": "x2", "goal": "g", "subtasks": ["s"] * 51},
        "not json at all",
    ])
    def test_invalid_tasks_rejected_without_work(self, fresh, ctl, bad):
        fresh["client"].xadd(c.STREAM_TASKS, {"data": bad if isinstance(bad, str) else json.dumps(bad)})
        drain_controller(ctl)
        assert fresh["testadmin"].xlen(c.STREAM_SUBTASKS) == 0
        done = fresh["testadmin"].xrange(c.STREAM_COMPLETED)
        assert done and done[-1][1]["status"] == "rejected"


class TestFullFlow:
    def test_task_completes_and_tokens_are_revoked(self, redis_server, fresh, ctl, stack):
        submit(fresh, {"id": "t9", "goal": "g", "subtasks": ["a", "b"]})
        drain_controller(ctl)
        tokens = [fresh["testadmin"].hget(c.record_key(f"t9.{i}"), "token") for i in range(2)]
        assert run_agent(redis_server, stack, "job-1") == 0
        assert run_agent(redis_server, stack, "job-2") == 0
        assert run_agent(redis_server, stack, "job-3") == 0  # nothing left, exits cleanly
        drain_controller(ctl)
        job = fresh["client"].hgetall(c.job_key("t9"))
        assert job["status"] == "completed" and job["done"] == "2"
        results = fresh["client"].hgetall(c.job_results_key("t9"))
        assert {json.loads(v)["text"] for v in results.values()} == {"done: claude-sonnet-5-5"}
        # Upstream saw the real key, a clamped request, and never the agent token.
        for seen in stack["upstate"].requests:
            assert seen["headers"]["x-api-key"] == "test-upstream-key"
            assert "authorization" not in seen["headers"]
            assert seen["body"]["max_tokens"] <= 512
        # Tokens are gone, so a replay is refused.
        for token in tokens:
            req = urllib.request.Request(f"{stack['vault_url']}/v1/messages", method="POST",
                                         data=b'{"model":"claude-sonnet-5-5","max_tokens":5,"messages":[]}',
                                         headers={"authorization": f"Bearer {token}", "content-type": "application/json"})
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            assert e.value.code == 401

    def test_tool_requests_become_approvals_not_actions(self, redis_server, fresh, ctl, stack):
        submit(fresh, {"id": "t2", "goal": "g",
                       "subtasks": [{"goal": "file it", "input": "TOOLCALL please", "tools": ["create_ticket"]}]})
        drain_controller(ctl)
        assert run_agent(redis_server, stack) == 0
        drain_controller(ctl)
        approvals = fresh["client"].xrange(c.STREAM_APPROVALS)
        assert len(approvals) == 1
        assert json.loads(approvals[0][1]["action"])["type"] == "create_ticket"

    def test_spoofed_results_are_dropped(self, fresh, ctl):
        submit(fresh, {"id": "t3", "goal": "g"})
        drain_controller(ctl)
        fake = {"subtask": "t3.0", "task": "other", "status": "ok", "text": "pwned", "proposed_actions": []}
        fresh["agent"].xadd(c.STREAM_RESULTS, {"data": json.dumps(fake)})
        fresh["agent"].xadd(c.STREAM_RESULTS, {"data": json.dumps({**fake, "subtask": "nope.9", "task": "nope"})})
        drain_controller(ctl)
        assert fresh["client"].hgetall(c.job_results_key("t3")) == {}

    def test_retried_pod_resumes_its_own_message(self, redis_server, fresh, ctl, stack):
        submit(fresh, {"id": "t4", "goal": "g"})
        drain_controller(ctl)
        first, _ = agent.claim(connect(redis_server, "agent"), "job-r", 10)   # pod dies before ack
        assert run_agent(redis_server, stack, "job-r") == 0                  # retry pod, same Job name
        drain_controller(ctl)
        assert fresh["client"].hget(c.job_key("t4"), "status") == "completed"
        assert fresh["keda"].xpending(c.STREAM_SUBTASKS, c.GROUP_AGENTS)["pending"] == 0

    def test_cold_vault_is_awaited(self, redis_server, fresh, ctl, stack):
        """Agent pods start before the vault finishes scaling up. The agent must wait, not fail."""
        submit(fresh, {"id": "t5", "goal": "g"})
        drain_controller(ctl)
        vc = agent.VaultClient("http://127.0.0.1:9", wait_seconds=2, request_timeout=2)  # nothing listening
        rc = agent.run_once(connect(redis_server, "agent"), vc, "job-cold", system_prompt="s",
                            max_tokens=10, claim_wait_ms=10)
        assert rc == agent.EXIT_RETRY
        assert fresh["keda"].xpending(c.STREAM_SUBTASKS, c.GROUP_AGENTS)["pending"] == 1
        assert run_agent(redis_server, stack, "job-cold") == 0


    def test_agent_with_revoked_token_acks_without_a_second_result(self, redis_server, fresh, ctl, stack):
        """A requeued copy can race a slow original. The loser must not double report."""
        submit(fresh, {"id": "t11", "goal": "g"})
        drain_controller(ctl)
        (_, fields), = fresh["testadmin"].xrange(c.STREAM_SUBTASKS)
        assert run_agent(redis_server, stack, "job-first") == 0
        drain_controller(ctl)
        before = len(stack["upstate"].requests)
        fresh["controller"].xadd(c.STREAM_SUBTASKS, fields)          # stale duplicate
        assert run_agent(redis_server, stack, "job-late") == 0
        assert len(stack["upstate"].requests) == before
        assert fresh["testadmin"].xlen(c.STREAM_RESULTS) == 1
        assert fresh["keda"].xpending(c.STREAM_SUBTASKS, c.GROUP_AGENTS)["pending"] == 0

    def test_huge_writes_are_refused_by_redis(self, fresh):
        """Review finding: one oversized XADD could fill maxmemory and stall every queue."""
        with pytest.raises(redis.exceptions.RedisError):
            fresh["agent"].xadd(c.STREAM_RESULTS, {"data": "x" * (2 * 1024 * 1024)})
        assert fresh["testadmin"].xlen(c.STREAM_RESULTS) == 0


class TestRecovery:
    def test_stranded_work_is_requeued_then_dead_lettered(self, redis_server, fresh):
        cfg = controller.ControllerConfig(consumer="ctl-0", visibility_ms=1, max_attempts=2, controller_claim_ms=1)
        ctl = controller.Controller(fresh["controller"], cfg)
        submit(fresh, {"id": "t6", "goal": "g"})
        drain_controller(ctl)
        agent.claim(connect(redis_server, "agent"), "dead-pod-1", 10)
        time.sleep(0.01)
        assert ctl.sweep()["requeued"] == 1
        assert lag(fresh["keda"], c.STREAM_SUBTASKS, c.GROUP_AGENTS) == 1  # KEDA will start a new agent
        agent.claim(connect(redis_server, "agent"), "dead-pod-2", 10)
        time.sleep(0.01)
        assert ctl.sweep()["dead_lettered"] == 1
        assert fresh["client"].hget(c.job_key("t6"), "status") == "failed"
        assert len(fresh["client"].xrange(c.STREAM_DLQ)) == 1

    def test_trim_keeps_lag_valid(self, redis_server, fresh, ctl, stack):
        submit(fresh, {"id": "t7", "goal": "g", "subtasks": ["a", "b", "c"]})
        drain_controller(ctl)
        for i in range(3):
            assert run_agent(redis_server, stack, f"job-{i}") == 0
        drain_controller(ctl)
        ctl.sweep()
        assert fresh["testadmin"].xlen(c.STREAM_SUBTASKS) <= 1
        assert lag(fresh["keda"], c.STREAM_SUBTASKS, c.GROUP_AGENTS) == 0
        submit(fresh, {"id": "t8", "goal": "g", "subtasks": ["a", "b"]})
        drain_controller(ctl)
        assert lag(fresh["keda"], c.STREAM_SUBTASKS, c.GROUP_AGENTS) == 2

    def test_controller_survives_redis_data_loss(self, fresh):
        cfg = controller.ControllerConfig(consumer="ctl-0", sweep_interval=3600, heartbeat_file="")
        ctl = controller.Controller(fresh["controller"], cfg)
        stop = threading.Event()
        worker = threading.Thread(target=ctl.run, args=(stop,), daemon=True)
        worker.start()
        time.sleep(0.5)
        fresh["testadmin"].flushall()          # Redis comes back empty
        time.sleep(3)
        assert worker.is_alive(), "controller must recreate its groups instead of crashing"
        submit(fresh, {"id": "after-loss", "goal": "g"})
        deadline = time.time() + 10
        while fresh["testadmin"].xlen(c.STREAM_SUBTASKS) == 0 and time.time() < deadline:
            time.sleep(0.2)
        stop.set()
        worker.join(5)
        assert fresh["testadmin"].xlen(c.STREAM_SUBTASKS) == 1

    def test_controller_restart_reclaims_unacked_tasks(self, fresh):
        cfg = controller.ControllerConfig(consumer="ctl-old", controller_claim_ms=1)
        old = controller.Controller(fresh["controller"], cfg)
        submit(fresh, {"id": "t10", "goal": "g"})
        old.r.xreadgroup(c.GROUP_CONTROLLER, "ctl-old", {c.STREAM_TASKS: ">"}, count=1)  # dies here
        time.sleep(0.01)
        new = controller.Controller(fresh["controller"], controller.ControllerConfig(consumer="ctl-new", controller_claim_ms=1))
        assert new.sweep()["reclaimed_controller"] == 1
        assert fresh["testadmin"].xlen(c.STREAM_SUBTASKS) == 1


class TestAcl:
    """Each identity can do its job and nothing else."""

    def denied(self, fn):
        with pytest.raises(redis.exceptions.NoPermissionError):
            fn()

    def test_default_user_is_disabled(self, redis_server):
        r = redis.Redis(host="127.0.0.1", port=redis_server["port"])
        with pytest.raises(redis.exceptions.AuthenticationError):
            r.ping()

    def test_agent_boundaries(self, fresh, ctl):
        submit(fresh, {"id": "t1", "goal": "g"})
        drain_controller(ctl)
        a = fresh["agent"]
        self.denied(lambda: a.hget(c.record_key("t1.0"), "goal"))
        self.denied(lambda: a.xadd(c.STREAM_TASKS, {"data": "{}"}))
        self.denied(lambda: a.xadd(c.STREAM_SUBTASKS, {"id": "x"}))
        self.denied(lambda: a.hset(c.record_key("t1.0"), "status", "done"))
        self.denied(lambda: a.xrange(c.STREAM_RESULTS))
        self.denied(lambda: a.hgetall(c.token_key("0" * 64)))
        self.denied(lambda: a.hgetall(c.job_key("t1")))
        self.denied(lambda: a.flushall())
        self.denied(lambda: a.config_get("*"))
        self.denied(lambda: a.keys("*"))

    def test_vault_boundaries(self, fresh, ctl):
        submit(fresh, {"id": "t1", "goal": "g"})
        drain_controller(ctl)
        v = fresh["vault"]
        self.denied(lambda: v.hgetall(c.record_key("t1.0")))
        self.denied(lambda: v.hset(c.token_key("a"), "max_calls", 999))
        self.denied(lambda: v.xrange(c.STREAM_SUBTASKS))
        self.denied(lambda: v.xadd(c.STREAM_RESULTS, {"data": "{}"}))

    def test_keda_is_read_only_metrics(self, fresh):
        k = fresh["keda"]
        assert k.xlen(c.STREAM_SUBTASKS) == 0
        self.denied(lambda: k.xadd(c.STREAM_SUBTASKS, {"id": "x"}))
        self.denied(lambda: k.xrange(c.STREAM_SUBTASKS))
        self.denied(lambda: k.hgetall(c.record_key("t1.0")))

    def test_client_can_submit_and_read_status_only(self, fresh, ctl):
        cl = fresh["client"]
        submit(fresh, {"id": "t1", "goal": "g"})
        drain_controller(ctl)
        assert cl.hget(c.job_key("t1"), "status") == "running"
        self.denied(lambda: cl.hgetall(c.record_key("t1.0")))
        self.denied(lambda: cl.xadd(c.STREAM_SUBTASKS, {"id": "x"}))
        self.denied(lambda: cl.xrange(c.STREAM_TASKS))

    def test_controller_cannot_touch_server_config(self, fresh):
        ctl = fresh["controller"]
        self.denied(lambda: ctl.config_set("appendonly", "no"))
        self.denied(lambda: ctl.flushall())
        self.denied(lambda: ctl.set("unrelated", "1"))
