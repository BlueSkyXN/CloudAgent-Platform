from __future__ import annotations

HIGH_RISK_TOOLS = {
    "shell.exec",
    "external.http",
    "secret.read",
    "deploy.publish",
    "file.write",
    "file.delete",
    "integration.dify.chat",
    "integration.feishu.message",
}

BUILTIN_TOOLS = [
    {
        "name": "artifact.create",
        "source": "platform",
        "description": "Create a session artifact from tool arguments.",
        "default_policy": "always_allow",
        "schema": {
            "type": "object",
            "required": ["name", "content"],
            "properties": {"name": {"type": "string"}, "content": {"type": "string"}},
        },
    },
    {
        "name": "file.read",
        "source": "platform",
        "description": "Read metadata or content for an uploaded file.",
        "default_policy": "always_allow",
        "schema": {"type": "object", "properties": {"file_id": {"type": "string"}}},
    },
    {
        "name": "external.http",
        "source": "platform",
        "description": "Placeholder for outbound HTTP calls; requires approval in P0.",
        "default_policy": "always_ask",
        "schema": {"type": "object", "properties": {"url": {"type": "string"}, "method": {"type": "string"}}},
    },
    {
        "name": "integration.dify.chat",
        "source": "connector",
        "description": "Send a Dify chat message through a configured Dify integration.",
        "default_policy": "always_ask",
        "schema": {
            "type": "object",
            "required": ["integration_id", "query", "user"],
            "properties": {
                "integration_id": {"type": "string"},
                "query": {"type": "string"},
                "inputs": {"type": "object"},
                "user": {"type": "string"},
                "response_mode": {"type": "string", "enum": ["blocking", "streaming"]},
                "conversation_id": {"type": "string"},
            },
        },
    },
    {
        "name": "integration.feishu.message",
        "source": "connector",
        "description": "Send a Feishu message through a configured Feishu integration.",
        "default_policy": "always_ask",
        "schema": {
            "type": "object",
            "required": ["integration_id", "receive_id", "content"],
            "properties": {
                "integration_id": {"type": "string"},
                "receive_id_type": {"type": "string"},
                "receive_id": {"type": "string"},
                "msg_type": {"type": "string"},
                "content": {"oneOf": [{"type": "string"}, {"type": "object"}]},
            },
        },
    },
    {
        "name": "shell.exec",
        "source": "platform",
        "description": "Placeholder for shell execution; requires approval in P0.",
        "default_policy": "always_ask",
        "schema": {"type": "object", "properties": {"command": {"type": "string"}}},
    },
]
