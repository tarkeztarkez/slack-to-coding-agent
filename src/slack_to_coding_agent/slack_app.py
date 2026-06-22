from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from typing import Any

import httpx
from slack_bolt import App, Assistant
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from .backends import AgentBackend, AgentRequest, create_backend
from .config import AppConfig
from .process import ensure_backend_started

LOGGER = logging.getLogger(__name__)
MAX_SLACK_CHUNK = 3500
MAX_THREAD_MESSAGES = 40
MAX_CHANNEL_CONTEXT_MESSAGES = 10
MAX_ATTACHMENT_BYTES = 50_000
TEXT_FILETYPES = {
    "c",
    "cpp",
    "css",
    "csv",
    "go",
    "html",
    "java",
    "javascript",
    "json",
    "kotlin",
    "markdown",
    "php",
    "plaintext",
    "python",
    "ruby",
    "rust",
    "shell",
    "swift",
    "typescript",
    "xml",
    "yaml",
}


def run(config: AppConfig) -> None:
    ensure_backend_started(config.backend)
    backend = create_backend(config.backend)
    app = App(token=config.slack.bot_token)
    assistant = Assistant(app_name="slack-to-coding-agent", logger=LOGGER)
    bot_user_id = _bot_user_id(app.client)

    bridge = SlackBridge(
        config=config,
        backend=backend,
        bot_user_id=bot_user_id,
        logger=LOGGER,
    )

    @assistant.thread_started
    def assistant_thread_started(
        payload: dict[str, Any],
        say: Callable[..., Any],
        set_suggested_prompts: Callable[..., Any] | None = None,
    ):
        bridge.handle_assistant_thread_started_payload(
            payload=payload,
            say=say,
            set_suggested_prompts=set_suggested_prompts,
        )

    @assistant.user_message
    def assistant_user_message(
        payload: dict[str, Any],
        say: Callable[..., Any],
        set_status: Callable[..., Any] | None = None,
    ):
        bridge.handle_assistant_user_message(
            payload=payload,
            say=say,
            set_status=set_status,
            client=app.client,
        )

    app.use(assistant)

    @app.event("app_mention")
    def handle_app_mention(ack: Callable[[], None], event: dict[str, Any], client: WebClient):
        ack()
        bridge.handle_message_event(event=event, client=client, strip_bot_mention=True)

    @app.event("message")
    def handle_direct_message(ack: Callable[[], None], event: dict[str, Any], client: WebClient):
        ack()
        if event.get("channel_type") != "im":
            return
        bridge.handle_message_event(event=event, client=client, strip_bot_mention=True)

    LOGGER.info("Starting Slack socket-mode bridge with config %s", config.path)
    SocketModeHandler(app, config.slack.app_token).start()


class SlackBridge:
    def __init__(
        self,
        config: AppConfig,
        backend: AgentBackend,
        bot_user_id: str,
        logger: logging.Logger,
    ):
        self.config = config
        self.backend = backend
        self.bot_user_id = bot_user_id
        self.logger = logger

    def handle_message_event(
        self,
        event: dict[str, Any],
        client: WebClient,
        *,
        strip_bot_mention: bool,
    ) -> None:
        user_id = str(event.get("user") or "")
        if not self._is_allowed_user(user_id):
            return
        if self._is_from_bot(event, user_id):
            return

        channel_id = str(event.get("channel") or "")
        thread_ts = str(event.get("thread_ts") or event.get("ts") or "")
        event_ts = str(event.get("ts") or thread_ts)
        text = str(event.get("text") or "").strip()
        if strip_bot_mention:
            text = _strip_slack_mentions(text, self.bot_user_id).strip()
        if not text:
            return

        session_id = f"slack:{channel_id}:{thread_ts}"
        request = AgentRequest(
            message=text,
            session_id=session_id,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            thread_messages=[],
            attachments=[],
        )

        self._run_in_background(
            lambda: self._process_request(
                client=client,
                request=request,
                channel_id=channel_id,
                reply_thread_ts=thread_ts or event_ts,
                fallback_event=event,
            )
        )

    def handle_assistant_thread_started_payload(
        self,
        *,
        payload: dict[str, Any],
        say: Callable[..., Any],
        set_suggested_prompts: Callable[..., Any] | None,
    ) -> None:
        assistant_thread = payload.get("assistant_thread") or {}
        user_id = str(
            payload.get("user") or payload.get("user_id") or assistant_thread.get("user_id") or ""
        )
        if not self._is_allowed_user(user_id):
            return
        if set_suggested_prompts is not None:
            set_suggested_prompts(
                [
                    {
                        "title": "Ask the coding agent",
                        "message": "Help me with the current coding task.",
                    }
                ]
            )
        say("Hi! Send me a coding task and I will forward it to your local coding agent.")

    def handle_assistant_user_message(
        self,
        *,
        payload: dict[str, Any],
        say: Callable[..., Any],
        set_status: Callable[..., Any] | None,
        client: WebClient,
    ) -> None:
        user_id = str(payload.get("user") or "")
        if not self._is_allowed_user(user_id):
            return
        if self._is_from_bot(payload, user_id):
            return

        channel_id = str(payload.get("channel") or "")
        thread_ts = str(payload.get("thread_ts") or payload.get("ts") or "")
        text = str(payload.get("text") or "").strip()
        if not text:
            return

        request = AgentRequest(
            message=text,
            session_id=f"slack-assistant:{channel_id}:{thread_ts}",
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            thread_messages=self._thread_messages(
                client=client,
                channel_id=channel_id,
                thread_ts=thread_ts,
                fallback_event=payload,
            ),
            attachments=_event_attachments(payload, client=client, logger=self.logger),
        )
        try:
            if set_status is not None:
                set_status("Working on it…")
            response_text = self.backend.send(request)
        except Exception as exc:
            self.logger.exception("Backend request failed")
            response_text = f"Backend request failed: {exc}"
        if not response_text.strip():
            response_text = "Backend returned an empty response."
        for chunk in _chunk_text(response_text, MAX_SLACK_CHUNK):
            say(chunk)

    def _process_request(
        self,
        *,
        client: WebClient,
        request: AgentRequest,
        channel_id: str,
        reply_thread_ts: str,
        fallback_event: dict[str, Any],
    ) -> None:
        placeholder_ts: str | None = None
        try:
            placeholder = client.chat_postMessage(
                channel=channel_id,
                thread_ts=reply_thread_ts,
                text="Working on it…",
            )
            placeholder_ts = str(placeholder.get("ts") or "") or None
        except Exception:
            self.logger.exception("Failed to post Slack placeholder")

        try:
            request = self._with_thread_context(
                client=client,
                request=request,
                fallback_event=fallback_event,
            )
            response_text = self.backend.send(request)
        except Exception as exc:
            self.logger.exception("Backend request failed")
            response_text = f"Backend request failed: {exc}"

        if not response_text.strip():
            response_text = "Backend returned an empty response."

        _post_chunks(
            client=client,
            channel_id=channel_id,
            thread_ts=reply_thread_ts,
            text=response_text,
            update_ts=placeholder_ts,
        )

    def _is_allowed_user(self, user_id: str) -> bool:
        allowed_user_id = self.config.slack.allowed_user_id
        return not allowed_user_id or user_id == allowed_user_id

    def _is_from_bot(self, event: dict[str, Any], user_id: str) -> bool:
        if event.get("bot_id"):
            return True
        if self.bot_user_id and user_id == self.bot_user_id:
            return True
        subtype = event.get("subtype")
        return subtype not in (None, "")

    def _run_in_background(self, target: Callable[[], None]) -> None:
        thread = threading.Thread(target=target, daemon=True)
        thread.start()

    def _with_thread_context(
        self,
        *,
        client: WebClient,
        request: AgentRequest,
        fallback_event: dict[str, Any],
    ) -> AgentRequest:
        thread_messages = self._thread_messages(
            client=client,
            channel_id=request.channel_id,
            thread_ts=request.thread_ts,
            fallback_event=fallback_event,
        )
        attachments = _event_attachments(fallback_event, client=client, logger=self.logger)
        return AgentRequest(
            message=request.message,
            session_id=request.session_id,
            user_id=request.user_id,
            channel_id=request.channel_id,
            thread_ts=request.thread_ts,
            thread_messages=thread_messages,
            attachments=attachments,
        )

    def _thread_messages(
        self,
        *,
        client: WebClient,
        channel_id: str,
        thread_ts: str,
        fallback_event: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not channel_id or not thread_ts:
            return [_message_context(fallback_event, client=client, logger=self.logger)]

        event_ts = str(fallback_event.get("ts") or "")
        is_thread_reply = bool(fallback_event.get("thread_ts"))
        if not is_thread_reply and str(fallback_event.get("channel_type") or "") != "im":
            return self._channel_context_messages(
                client=client,
                channel_id=channel_id,
                latest_ts=event_ts or thread_ts,
                fallback_event=fallback_event,
            )

        try:
            kwargs: dict[str, Any] = {}
            if event_ts:
                kwargs.update({"latest": event_ts, "inclusive": True})
            response = client.conversations_replies(
                channel=channel_id,
                ts=thread_ts,
                limit=MAX_THREAD_MESSAGES,
                include_all_metadata=True,
                **kwargs,
            )
            messages = response.get("messages") or []
            if isinstance(messages, list) and messages:
                return _ensure_fallback_message(
                    [
                        _message_context(message, client=client, logger=self.logger)
                        for message in messages[-MAX_THREAD_MESSAGES:]
                        if isinstance(message, dict)
                    ],
                    fallback_event=fallback_event,
                    client=client,
                    logger=self.logger,
                )
        except Exception:
            self.logger.exception("Could not fetch Slack thread history")
        return [_message_context(fallback_event, client=client, logger=self.logger)]

    def _channel_context_messages(
        self,
        *,
        client: WebClient,
        channel_id: str,
        latest_ts: str,
        fallback_event: dict[str, Any],
    ) -> list[dict[str, Any]]:
        try:
            kwargs: dict[str, Any] = {}
            if latest_ts:
                kwargs.update({"latest": latest_ts, "inclusive": True})
            response = client.conversations_history(
                channel=channel_id,
                limit=MAX_CHANNEL_CONTEXT_MESSAGES,
                include_all_metadata=True,
                **kwargs,
            )
            messages = response.get("messages") or []
            if isinstance(messages, list) and messages:
                chronological_messages = list(reversed(messages[-MAX_CHANNEL_CONTEXT_MESSAGES:]))
                return _ensure_fallback_message(
                    [
                        _message_context(message, client=client, logger=self.logger)
                        for message in chronological_messages
                        if isinstance(message, dict)
                    ],
                    fallback_event=fallback_event,
                    client=client,
                    logger=self.logger,
                )
        except Exception:
            self.logger.exception("Could not fetch Slack channel history")
        return [_message_context(fallback_event, client=client, logger=self.logger)]


def _bot_user_id(client: WebClient) -> str:
    try:
        auth = client.auth_test()
    except Exception:
        LOGGER.exception("Could not call Slack auth.test; continuing without bot user id")
        return ""
    return str(auth.get("user_id") or "")


def _strip_slack_mentions(text: str, bot_user_id: str) -> str:
    if bot_user_id:
        text = re.sub(rf"<@{re.escape(bot_user_id)}(?:\|[^>]+)?>", "", text)
    return re.sub(r"<@[A-Z0-9]+(?:\|[^>]+)?>", "", text)


def _message_context(
    message: dict[str, Any],
    *,
    client: WebClient,
    logger: logging.Logger,
) -> dict[str, Any]:
    return {
        "ts": str(message.get("ts") or ""),
        "thread_ts": str(message.get("thread_ts") or ""),
        "user_id": str(message.get("user") or ""),
        "bot_id": str(message.get("bot_id") or ""),
        "subtype": str(message.get("subtype") or ""),
        "text": str(message.get("text") or ""),
        "attachments": _event_attachments(message, client=client, logger=logger),
    }


def _ensure_fallback_message(
    messages: list[dict[str, Any]],
    *,
    fallback_event: dict[str, Any],
    client: WebClient,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    fallback = _message_context(fallback_event, client=client, logger=logger)
    fallback_ts = fallback.get("ts")
    if fallback_ts and any(message.get("ts") == fallback_ts for message in messages):
        return messages
    return [*messages, fallback]


def _event_attachments(
    event: dict[str, Any],
    *,
    client: WebClient,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for file_info in event.get("files") or []:
        if isinstance(file_info, dict):
            attachments.append(_file_attachment(file_info, client=client, logger=logger))
    for attachment in event.get("attachments") or []:
        if isinstance(attachment, dict):
            attachments.append(_slack_attachment(attachment))
    return attachments


def _file_attachment(
    file_info: dict[str, Any],
    *,
    client: WebClient,
    logger: logging.Logger,
) -> dict[str, Any]:
    attachment: dict[str, Any] = {
        "type": "file",
        "id": str(file_info.get("id") or ""),
        "name": str(file_info.get("name") or ""),
        "title": str(file_info.get("title") or ""),
        "mimetype": str(file_info.get("mimetype") or ""),
        "filetype": str(file_info.get("filetype") or ""),
        "pretty_type": str(file_info.get("pretty_type") or ""),
        "size": file_info.get("size"),
        "url_private": str(file_info.get("url_private") or ""),
        "url_private_download": str(file_info.get("url_private_download") or ""),
        "permalink": str(file_info.get("permalink") or ""),
    }
    content = _download_text_file(file_info, client=client, logger=logger)
    if content is not None:
        attachment["content"] = content
        attachment["content_truncated"] = len(content.encode("utf-8")) >= MAX_ATTACHMENT_BYTES
    return attachment


def _slack_attachment(attachment: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "attachment",
        "title": str(attachment.get("title") or ""),
        "text": str(attachment.get("text") or ""),
        "fallback": str(attachment.get("fallback") or ""),
        "service_name": str(attachment.get("service_name") or ""),
        "from_url": str(attachment.get("from_url") or ""),
        "original_url": str(attachment.get("original_url") or ""),
    }


def _download_text_file(
    file_info: dict[str, Any],
    *,
    client: WebClient,
    logger: logging.Logger,
) -> str | None:
    mimetype = str(file_info.get("mimetype") or "").lower()
    filetype = str(file_info.get("filetype") or "").lower()
    if not (mimetype.startswith("text/") or filetype in TEXT_FILETYPES):
        return None

    url = str(file_info.get("url_private_download") or file_info.get("url_private") or "")
    token = getattr(client, "token", None)
    if not url or not token:
        return None

    try:
        response = httpx.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Range": f"bytes=0-{MAX_ATTACHMENT_BYTES - 1}",
            },
            timeout=10,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            logger.warning("Could not download Slack file; missing files:read or file access")
        else:
            logger.exception("Could not download Slack file attachment")
        return None
    except Exception:
        logger.exception("Could not download Slack file attachment")
        return None

    content = response.content[:MAX_ATTACHMENT_BYTES]
    return content.decode(response.encoding or "utf-8", errors="replace")


def _post_chunks(
    *,
    client: WebClient,
    channel_id: str,
    thread_ts: str | None,
    text: str,
    update_ts: str | None = None,
) -> None:
    chunks = _chunk_text(text, MAX_SLACK_CHUNK)
    if not chunks:
        chunks = ["(empty response)"]

    first_chunk, *remaining = chunks
    if update_ts:
        client.chat_update(channel=channel_id, ts=update_ts, text=first_chunk)
    else:
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=first_chunk)

    for chunk in remaining:
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=chunk)


def _chunk_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
