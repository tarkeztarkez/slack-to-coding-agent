from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .config import BackendConfig


@dataclass(frozen=True)
class AgentRequest:
    message: str
    session_id: str
    user_id: str
    channel_id: str
    thread_ts: str
    thread_messages: list[dict[str, Any]]
    attachments: list[dict[str, Any]]


class AgentBackend(Protocol):
    def send(self, request: AgentRequest) -> str: ...


class HttpJsonBackend:
    """Generic JSON backend, used for the local Codex app-server by default."""

    def __init__(self, config: BackendConfig):
        self.config = config

    def send(self, request: AgentRequest) -> str:
        url = f"{self.config.base_url}{self.config.path}"
        headers = {"Content-Type": "application/json", **self.config.headers}
        if self.config.token:
            headers.setdefault("Authorization", f"Bearer {self.config.token}")

        payload = {
            "message": request.message,
            "prompt": request.message,
            "session_id": request.session_id,
            "thread_id": request.session_id,
            "thread_messages": request.thread_messages,
            "attachments": request.attachments,
            "metadata": {
                "source": "slack",
                "user_id": request.user_id,
                "channel_id": request.channel_id,
                "thread_ts": request.thread_ts,
            },
        }

        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.request(self.config.method, url, headers=headers, json=payload)
            response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type.lower():
            return response.text.strip()

        data = response.json()
        extracted = _extract_response_text(data, self.config.response_json_paths)
        if extracted:
            return extracted
        return str(data)


def create_backend(config: BackendConfig) -> AgentBackend:
    if config.type == "http_json":
        return HttpJsonBackend(config)
    raise ValueError(f"Unsupported backend type {config.type!r} for backend {config.name!r}")


def _extract_response_text(data: Any, paths: list[str]) -> str:
    for path in paths:
        value = _get_path(data, path)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            text = "\n".join(str(item) for item in value if item is not None).strip()
            if text:
                return text
    if isinstance(data, str):
        return data.strip()
    return ""


def _get_path(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current
