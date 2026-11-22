"""Vault gateway: the only workload that holds the model API key.

Agents call it with a per subtask token the controller minted. The vault checks
the token, enforces rate and call budgets, allows only approved models and
tools, screens tool metadata, caps request and response size, then forwards to
the upstream API with the real key. It runs as its own Deployment so the
network policy can give it, and only it, a path to the internet. KEDA scales it
from zero when the first agent pod appears.
"""

from __future__ import annotations

import json
import logging
import re
import signal
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import redis

from . import common as c

LOG = c.get_logger("vault")

ALLOWED_FIELDS = {"model", "max_tokens", "system", "messages", "tools", "tool_choice",
                  "temperature", "top_p", "top_k", "stop_sequences"}

# Phrases that have no business inside a tool description. A hit means the tool
# metadata is trying to steer the model, which is the tool poisoning pattern.
TOOL_POISON_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"ignore (all |any )?(previous|prior|above) (instructions|rules)",
        r"disregard (the |all )?(system|previous)",
        r"system prompt",
        r"you (must|should) (always|now|first)",
        r"<\s*/?\s*(system|instructions?|important)\s*>",
        r"do not (tell|inform|mention)",
        r"https?://",
        r"[A-Za-z0-9+/]{120,}={0,2}",  # long base64 style blobs
        r"[​-‏‪-‮⁠-⁤﻿]",  # invisible and bidi controls
    )
]


@dataclass
class VaultConfig:
    upstream_url: str = field(default_factory=lambda: c.env_str("UPSTREAM_URL", "https://api.anthropic.com"))
    api_version: str = field(default_factory=lambda: c.env_str("UPSTREAM_API_VERSION", "2023-06-01"))
    api_key: str = field(default_factory=lambda: c.read_secret("UPSTREAM_API_KEY_FILE", "UPSTREAM_API_KEY"))
    model_allowlist: list[str] = field(default_factory=lambda: c.env_list(
        "MODEL_ALLOWLIST", "claude-sonnet-5-5,claude-haiku-5-5"))
    max_tokens_cap: int = field(default_factory=lambda: c.env_int("MAX_TOKENS_CAP", 4096, 1, 128000))
    rate_per_min: int = field(default_factory=lambda: c.env_int("RATE_LIMIT_PER_MIN", 30, 1, 10000))
    max_body: int = field(default_factory=lambda: c.env_int("MAX_REQUEST_BYTES", 262144, 1024, 16_777_216))
    max_response: int = field(default_factory=lambda: c.env_int("MAX_RESPONSE_BYTES", 2_097_152, 1024, 67_108_864))
    max_tool_desc: int = field(default_factory=lambda: c.env_int("MAX_TOOL_DESCRIPTION_CHARS", 1000, 50, 20000))
    upstream_timeout: int = field(default_factory=lambda: c.env_int("UPSTREAM_TIMEOUT_SECONDS", 170, 5, 900))
    listen_port: int = field(default_factory=lambda: c.env_int("VAULT_PORT", 8200, 1, 65535))


class PolicyError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def screen_tool(tool: dict, allowed: list[str], max_desc: int) -> dict:
    if not isinstance(tool, dict):
        raise PolicyError(400, "tool must be an object")
    name = tool.get("name")
    if not isinstance(name, str) or not c.TOOL_NAME_RE.match(name):
        raise PolicyError(400, "tool name invalid")
    if name not in allowed:
        raise PolicyError(403, f"tool {name} not allowed for this subtask")
    desc = tool.get("description", "")
    if not isinstance(desc, str) or len(desc) > max_desc:
        raise PolicyError(400, f"tool {name} description too long")
    blob = json.dumps(tool, ensure_ascii=False)
    for pattern in TOOL_POISON_PATTERNS:
        if pattern.search(blob):
            raise PolicyError(403, f"tool {name} metadata failed screening")
    return {k: tool[k] for k in ("name", "description", "input_schema") if k in tool}


def sanitize_request(body: dict, grant: dict, cfg: VaultConfig) -> dict:
    if not isinstance(body, dict):
        raise PolicyError(400, "body must be a JSON object")
    if body.get("stream"):
        raise PolicyError(400, "streaming is not supported")
    unknown = set(body) - ALLOWED_FIELDS
    if unknown:
        raise PolicyError(400, f"fields not allowed: {sorted(unknown)}")
    model = body.get("model")
    if model not in cfg.model_allowlist:
        raise PolicyError(403, f"model {model!r} not allowed")
    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or max_tokens < 1:
        raise PolicyError(400, "max_tokens must be a positive integer")
    clean = {k: body[k] for k in ALLOWED_FIELDS if k in body}
    clean["max_tokens"] = min(max_tokens, cfg.max_tokens_cap)
    allowed_tools = json.loads(grant.get("tools") or "[]")
    if "tools" in clean:
        if not isinstance(clean["tools"], list) or len(clean["tools"]) > 16:
            raise PolicyError(400, "tools must be a list of at most 16 entries")
        clean["tools"] = [screen_tool(t, allowed_tools, cfg.max_tool_desc) for t in clean["tools"]]
    return clean


class Vault:
    def __init__(self, client: redis.Redis, cfg: VaultConfig | None = None):
        self.r = client
        self.cfg = cfg or VaultConfig()

    def ready(self) -> bool:
        if not self.cfg.api_key:
            return False
        try:
            return bool(self.r.ping())
        except redis.RedisError:
            return False

    def authorize(self, auth_header: str) -> tuple[str, dict]:
        if not auth_header.startswith("Bearer "):
            raise PolicyError(401, "missing bearer token")
        token = auth_header[7:].strip()
        if not 20 <= len(token) <= 200:
            raise PolicyError(401, "invalid token")
        digest = c.token_digest(token)
        grant = self.r.hgetall(c.token_key(digest))
        if not grant:
            raise PolicyError(401, "unknown or expired token")
        window = int(time.time() // 60)
        rkey = c.ratelimit_key(digest, window)
        if self.r.incr(rkey) > self.cfg.rate_per_min:
            raise PolicyError(429, "rate limit")
        self.r.expire(rkey, 120)
        ckey = c.calls_key(digest)
        calls = self.r.incr(ckey)
        self.r.expire(ckey, 86400)
        if calls > int(grant.get("max_calls") or 1):
            raise PolicyError(429, "call budget for this subtask exhausted")
        return digest, grant

    def forward(self, clean: dict) -> tuple[int, bytes, dict]:
        req = urllib.request.Request(
            self.cfg.upstream_url.rstrip("/") + "/v1/messages",
            data=json.dumps(clean).encode("utf-8"), method="POST",
            headers={"content-type": "application/json", "x-api-key": self.cfg.api_key,
                     "anthropic-version": self.cfg.api_version})
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.upstream_timeout) as resp:
                data = resp.read(self.cfg.max_response + 1)
                status = resp.status
        except urllib.error.HTTPError as exc:
            data = exc.read(self.cfg.max_response + 1)
            status = exc.code
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise PolicyError(502, f"upstream unreachable: {type(exc).__name__}") from exc
        if len(data) > self.cfg.max_response:
            raise PolicyError(502, "upstream response too large")
        usage = {}
        try:
            usage = json.loads(data).get("usage", {}) or {}
        except (ValueError, AttributeError):
            pass
        return status, data, usage


def make_handler(vault: Vault):
    class Handler(BaseHTTPRequestHandler):
        server_version = "nanoclaw-vault"
        sys_version = ""

        def log_message(self, *args):  # access logs go through the JSON logger instead
            return

        def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, reason: str) -> None:
            self._send(status, json.dumps({"type": "error", "error": {"type": "policy", "message": reason}}).encode())

        def do_GET(self):
            if self.path == "/healthz":
                self._send(200, b'{"ok":true}')
            elif self.path == "/readyz":
                ok = vault.ready()
                self._send(200 if ok else 503, json.dumps({"ready": ok}).encode())
            else:
                self._error(404, "not found")

        def do_POST(self):
            started = time.time()
            if self.path != "/v1/messages":
                return self._error(404, "not found")
            digest, grant, status = "", {}, 500
            try:
                length = int(self.headers.get("content-length") or 0)
                if length <= 0:
                    raise PolicyError(411, "content-length required")
                if length > vault.cfg.max_body:
                    raise PolicyError(413, "request too large")
                digest, grant = vault.authorize(self.headers.get("authorization", ""))
                try:
                    body = json.loads(self.rfile.read(length))
                except ValueError as exc:
                    raise PolicyError(400, "body is not valid JSON") from exc
                clean = sanitize_request(body, grant, vault.cfg)
                status, data, usage = vault.forward(clean)
                self._send(status, data)
                c.log(LOG, logging.INFO, "forwarded", subtask=grant.get("subtask"), task=grant.get("task"),
                      model=clean.get("model"), status=status, ms=int(1000 * (time.time() - started)),
                      input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"))
            except PolicyError as exc:
                status = exc.status
                self._error(exc.status, exc.reason)
                c.log(LOG, logging.WARNING, "denied", subtask=grant.get("subtask"), status=exc.status,
                      reason=exc.reason, token=digest[:12])
            except redis.RedisError:
                self._error(503, "state store unavailable")

    return Handler


def serve(vault: Vault, stop: threading.Event, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, vault.cfg.listen_port), make_handler(vault))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    c.log(LOG, logging.INFO, "vault listening", port=vault.cfg.listen_port,
          models=vault.cfg.model_allowlist, key_loaded=bool(vault.cfg.api_key))
    return server


def main() -> None:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    vault = Vault(c.connect_redis())
    server = serve(vault, stop)
    stop.wait()
    server.shutdown()


if __name__ == "__main__":
    main()
