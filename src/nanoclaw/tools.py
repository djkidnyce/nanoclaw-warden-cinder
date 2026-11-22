"""Tool definitions an agent may offer the model.

Nothing here executes. When the model asks to use a tool, the agent records the
request as a proposed action and the controller routes it to the human approval
stream. A task can only request tools listed in ALLOWED_TOOLS, and the vault
checks the same list again before forwarding anything upstream.
"""

from __future__ import annotations

REGISTRY: dict[str, dict] = {
    "create_ticket": {
        "name": "create_ticket",
        "description": "Propose a ticket in the team tracker. A human reviews it before it is created.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 200},
                "body": {"type": "string", "maxLength": 4000},
                "priority": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["title", "body"],
        },
    },
    "send_email": {
        "name": "send_email",
        "description": "Propose an email. A human reviews it before anything is sent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "maxLength": 320},
                "subject": {"type": "string", "maxLength": 200},
                "body": {"type": "string", "maxLength": 8000},
            },
            "required": ["to", "subject", "body"],
        },
    },
    "http_get": {
        "name": "http_get",
        "description": "Propose fetching a URL. A human reviews the request before it runs.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "maxLength": 2000}},
            "required": ["url"],
        },
    },
}


def definitions(names: list[str]) -> list[dict]:
    return [REGISTRY[n] for n in names if n in REGISTRY]
