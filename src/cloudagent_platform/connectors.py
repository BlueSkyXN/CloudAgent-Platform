from __future__ import annotations


def integration_capabilities(provider: str) -> dict[str, object]:
    if provider == "feishu":
        return {
            "api_family": "feishu-open-platform",
            "webhook_trigger": True,
            "bot_messages": True,
            "document_read_write": True,
            "requires_secret": True,
        }
    if provider == "dify":
        return {
            "api_family": "dify-v2-compatible",
            "workflow_run": True,
            "chat_messages": True,
            "dataset_access": True,
            "requires_secret": True,
        }
    if provider == "github_actions":
        return {
            "api_family": "github-actions",
            "workflow_dispatch": True,
            "run_status": True,
            "requires_secret": True,
        }
    return {"api_family": "generic-webhook", "webhook_trigger": True}
