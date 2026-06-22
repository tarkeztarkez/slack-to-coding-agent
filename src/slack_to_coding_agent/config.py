from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path.home() / ".slack-to-coding-agent"
CONFIG_FILE = CONFIG_DIR / "config.yaml"

DEFAULT_CONFIG: dict[str, Any] = {
    "slack": {
        "bot_token": "xoxb-your-bot-token",
        "app_token": "xapp-your-app-level-token",
        "signing_secret": "",  # Not required for socket mode, kept here if HTTP mode is added.
        "allowed_user_id": "",  # Set to a Slack user ID like U012ABC to allow only that user.
    },
    "backend": {
        "active": "codex",
    },
    "backends": {
        "codex": {
            "type": "http_json",
            "base_url": "http://127.0.0.1:1455",
            "path": "/api/chat",
            "method": "POST",
            "timeout_seconds": 300,
            "token": "",  # Optional bearer token for a protected local backend.
            "headers": {},
            "response_json_paths": [
                "message",
                "response",
                "content",
                "output",
                "choices.0.message.content",
            ],
        }
    },
}


@dataclass(frozen=True)
class SlackConfig:
    bot_token: str
    app_token: str
    signing_secret: str = ""
    allowed_user_id: str = ""


@dataclass(frozen=True)
class BackendConfig:
    name: str
    type: str
    base_url: str
    path: str
    method: str = "POST"
    timeout_seconds: float = 300
    token: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    response_json_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AppConfig:
    slack: SlackConfig
    backend: BackendConfig
    path: Path = CONFIG_FILE


def ensure_config_file(path: Path = CONFIG_FILE) -> None:
    if path.exists():
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        # Best effort: chmod is not meaningful on every Windows filesystem.
        pass


def load_config(path: Path = CONFIG_FILE) -> AppConfig:
    ensure_config_file(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    slack_raw = raw.get("slack", {})
    slack = SlackConfig(
        bot_token=str(slack_raw.get("bot_token", "")).strip(),
        app_token=str(slack_raw.get("app_token", "")).strip(),
        signing_secret=str(slack_raw.get("signing_secret", "")).strip(),
        allowed_user_id=str(slack_raw.get("allowed_user_id", "")).strip(),
    )

    active_backend = str(raw.get("backend", {}).get("active", "codex"))
    backends = raw.get("backends", {})
    if active_backend not in backends:
        raise ValueError(f"Active backend {active_backend!r} not found in {path}")
    backend_raw = backends[active_backend]

    backend = BackendConfig(
        name=active_backend,
        type=str(backend_raw.get("type", "http_json")),
        base_url=str(backend_raw.get("base_url", "")).rstrip("/"),
        path="/" + str(backend_raw.get("path", "")).lstrip("/"),
        method=str(backend_raw.get("method", "POST")).upper(),
        timeout_seconds=float(backend_raw.get("timeout_seconds", 300)),
        token=str(backend_raw.get("token", "")).strip(),
        headers={str(k): str(v) for k, v in (backend_raw.get("headers") or {}).items()},
        response_json_paths=[str(p) for p in backend_raw.get("response_json_paths", [])],
    )

    _validate_tokens(slack, path)
    if not backend.base_url:
        raise ValueError(f"Set backends.{active_backend}.base_url in {path}")

    return AppConfig(slack=slack, backend=backend, path=path)


def _validate_tokens(slack: SlackConfig, path: Path) -> None:
    missing: list[str] = []
    if not slack.bot_token or slack.bot_token.startswith("xoxb-your"):
        missing.append("slack.bot_token")
    if not slack.app_token or slack.app_token.startswith("xapp-your"):
        missing.append("slack.app_token")
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Set {joined} in {path}")
