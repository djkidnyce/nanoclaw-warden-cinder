"""Shared fixtures. Integration tests run against a real redis-server loaded with the production ACL file."""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ROLES = ("bootstrap", "controller", "agent", "vault", "keda", "client")
PASSWORDS = {role: f"pw-{role}-" + "x" * 24 for role in ROLES}
ADMIN_PASSWORD = "test-admin-only-" + "y" * 16


def render_acl() -> str:
    text = (ROOT / "deploy/base/redis/users.acl.tmpl").read_text()
    for role, pw in PASSWORDS.items():
        text = text.replace("{{%s_HASH}}" % role.upper(), hashlib.sha256(pw.encode()).hexdigest())
    assert "{{" not in text, "ACL template has an unfilled placeholder"
    return text


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def redis_server():
    binary = os.environ.get("REDIS_SERVER") or shutil.which("redis-server")
    if not binary:
        pytest.skip("redis-server not installed")
    tmp = Path(tempfile.mkdtemp(prefix="nanoclaw-redis-"))
    # The production ACL plus one admin user that exists only in tests, for resets.
    (tmp / "users.acl").write_text(render_acl() + f"user testadmin on >{ADMIN_PASSWORD} ~* &* +@all\n")
    port = free_port()
    # Same limits as production, read from the shipped redis.conf.
    limits = []
    for line in (ROOT / "deploy/base/redis/redis.conf").read_text().splitlines():
        key, _, value = line.partition(" ")
        if key in ("maxmemory", "maxmemory-policy", "proto-max-bulk-len", "client-query-buffer-limit", "maxclients"):
            limits += [f"--{key}", value.strip()]
    proc = subprocess.Popen(
        [binary, "--port", str(port), "--bind", "127.0.0.1", "--aclfile", str(tmp / "users.acl"),
         "--save", "", "--appendonly", "no", "--dir", str(tmp), *limits],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(200):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("redis-server did not start")
    yield {"port": port, "proc": proc, "dir": tmp}
    proc.terminate()
    proc.wait(5)
    shutil.rmtree(tmp, ignore_errors=True)


def connect(server, role: str):
    import redis
    if role == "testadmin":
        return redis.Redis(host="127.0.0.1", port=server["port"], username="testadmin",
                           password=ADMIN_PASSWORD, decode_responses=True)
    return redis.Redis(host="127.0.0.1", port=server["port"], username=role, password=PASSWORDS[role],
                       decode_responses=True)


@pytest.fixture
def fresh(redis_server):
    """Empty Redis, consumer groups created exactly the way the bootstrap sidecar does."""
    connect(redis_server, "testadmin").flushall()
    from nanoclaw import common as c
    c.ensure_groups(connect(redis_server, "bootstrap"))
    clients = {role: connect(redis_server, role) for role in ROLES + ("testadmin",)}
    yield clients
    for cl in clients.values():
        cl.close()


@pytest.fixture
def role_env(redis_server, monkeypatch):
    """Point nanoclaw.common.connect_redis at the test server as a given role."""
    def apply(role: str):
        monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
        monkeypatch.setenv("REDIS_PORT", str(redis_server["port"]))
        monkeypatch.setenv("REDIS_USERNAME", role)
        monkeypatch.setenv("REDIS_PASSWORD", PASSWORDS[role])
        monkeypatch.delenv("REDIS_USERNAME_FILE", raising=False)
        monkeypatch.delenv("REDIS_PASSWORD_FILE", raising=False)
    return apply
