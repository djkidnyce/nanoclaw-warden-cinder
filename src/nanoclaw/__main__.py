"""Single image, three roles: python -m nanoclaw controller|agent|vault|cli."""

import sys


def main() -> int:
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.argv = [f"nanoclaw {role}"] + sys.argv[2:]
    if role == "controller":
        from .controller import main as run
        run()
        return 0
    if role == "agent":
        from .agent import main as run
        return run()
    if role == "vault":
        from .vault import main as run
        run()
        return 0
    if role == "cli":
        from .cli import main as run
        return run()
    if role == "mock-upstream":
        from .testing import mock_upstream
        import threading
        import os
        mock_upstream.start(int(os.environ.get("PORT", "8080")), "0.0.0.0")
        threading.Event().wait()
        return 0
    print("usage: python -m nanoclaw controller|agent|vault|cli|mock-upstream", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
