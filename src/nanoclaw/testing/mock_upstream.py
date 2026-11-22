"""Stand in for the model API, used by tests and the kind end to end overlay.

It checks that requests carry the expected API key and the vault's sanitized
shape, records what it saw, and answers with a small canned message. When the
input contains the word TOOLCALL it answers with a tool_use block so the
approval path gets exercised.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EXPECTED_KEY = os.environ.get("MOCK_EXPECTED_KEY", "test-upstream-key")


class State:
    def __init__(self):
        self.requests: list[dict] = []
        self.lock = threading.Lock()


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def _send(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._send(200, {"ok": True, "seen": len(state.requests)})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            with state.lock:
                state.requests.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            if self.headers.get("x-api-key") != EXPECTED_KEY:
                return self._send(401, {"type": "error", "error": {"type": "authentication_error"}})
            text = json.dumps(body.get("messages", []))
            content = [{"type": "text", "text": f"done: {body.get('model')}"}]
            if "TOOLCALL" in text and body.get("tools"):
                tool = body["tools"][0]
                content.append({"type": "tool_use", "id": "tu_1", "name": tool["name"],
                                "input": {"title": "proposed", "body": "from mock", "to": "ops@example.com",
                                          "subject": "s", "url": "https://example.com"}})
            self._send(200, {"id": "msg_mock", "type": "message", "role": "assistant", "content": content,
                             "usage": {"input_tokens": 11, "output_tokens": 7}})

    return Handler


def start(port: int = 0, host: str = "127.0.0.1"):
    state = State()
    server = ThreadingHTTPServer((host, port), make_handler(state))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


if __name__ == "__main__":
    srv, _ = start(int(os.environ.get("PORT", "8080")), "0.0.0.0")
    threading.Event().wait()
