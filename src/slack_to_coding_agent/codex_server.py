from __future__ import annotations

import argparse
import json
import logging
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class CodexHttpServer(ThreadingHTTPServer):
    codex_bin: str
    codex_cwd: Path | None
    sandbox: str
    approval_policy: str
    timeout_seconds: float
    extra_args: list[str]


class Handler(BaseHTTPRequestHandler):
    server: CodexHttpServer

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/healthz", "/readyz"}:
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/chat":
            self._send_json(404, {"error": "not found"})
            return

        try:
            payload = self._read_json()
            prompt = str(payload.get("message") or payload.get("prompt") or "").strip()
            if not prompt:
                self._send_json(400, {"error": "missing message or prompt"})
                return
            response = self._run_codex(_build_codex_prompt(payload))
        except subprocess.TimeoutExpired:
            LOGGER.exception("Codex request timed out")
            self._send_json(504, {"error": "Codex request timed out"})
            return
        except Exception as exc:
            LOGGER.exception("Codex request failed")
            self._send_json(500, {"error": str(exc)})
            return

        self._send_json(200, {"message": response})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("content-length") or "0")
        body = self.rfile.read(content_length)
        if not body:
            return {}
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request JSON must be an object")
        return data

    def _run_codex(self, prompt: str) -> str:
        with tempfile.NamedTemporaryFile("r", encoding="utf-8") as output_file:
            cmd = [self.server.codex_bin]
            if self.server.approval_policy:
                # Codex exposes approval policy as a top-level option, not as an `exec` option.
                cmd.extend(["--ask-for-approval", self.server.approval_policy])
            cmd.extend(
                [
                    "exec",
                    "--skip-git-repo-check",
                    "--sandbox",
                    self.server.sandbox,
                    "--color",
                    "never",
                    "--output-last-message",
                    output_file.name,
                    *self.server.extra_args,
                    "-",
                ]
            )
            LOGGER.info("Running Codex command: %s", " ".join(cmd))
            completed = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                cwd=str(self.server.codex_cwd) if self.server.codex_cwd else None,
                timeout=self.server.timeout_seconds,
                check=False,
            )
            output_file.seek(0)
            final_message = output_file.read().strip()

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            stdout = completed.stdout.strip()
            detail = stderr or stdout or f"codex exited with code {completed.returncode}"
            raise RuntimeError(detail)

        return final_message or completed.stdout.strip()

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Small HTTP JSON adapter for Codex CLI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1455)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--codex-cwd", type=Path, default=None)
    parser.add_argument("--sandbox", default="workspace-write")
    parser.add_argument("--approval-policy", default="never")
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Extra argument passed to 'codex exec' before the prompt marker; repeat as needed.",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server = CodexHttpServer((args.host, args.port), Handler)
    server.codex_bin = args.codex_bin
    server.codex_cwd = args.codex_cwd.expanduser() if args.codex_cwd else None
    server.sandbox = args.sandbox
    server.approval_policy = args.approval_policy
    server.timeout_seconds = args.timeout_seconds
    server.extra_args = args.extra_arg

    LOGGER.info("Codex HTTP adapter listening on http://%s:%s", args.host, args.port)
    server.serve_forever()


def _build_codex_prompt(payload: dict[str, Any]) -> str:
    message = str(payload.get("message") or payload.get("prompt") or "").strip()
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    thread_messages = payload.get("thread_messages")
    attachments = payload.get("attachments")

    parts = [
        "You are responding to a Slack request forwarded to a local coding agent.",
        "Use the Slack thread history and attachments below as context.",
        "",
        "Current user message:",
        message,
    ]

    if metadata:
        parts.extend(["", "Slack metadata:", _format_dict(metadata)])

    if isinstance(thread_messages, list) and thread_messages:
        parts.extend(["", "Slack thread history:"])
        for index, item in enumerate(thread_messages, start=1):
            if isinstance(item, dict):
                parts.append(_format_thread_message(index, item))

    if isinstance(attachments, list) and attachments:
        parts.extend(["", "Current message attachments:"])
        for index, item in enumerate(attachments, start=1):
            if isinstance(item, dict):
                parts.append(_format_attachment(index, item))

    return "\n".join(part for part in parts if part is not None).strip()


def _format_thread_message(index: int, item: dict[str, Any]) -> str:
    user = item.get("user_id") or item.get("bot_id") or "unknown"
    ts = item.get("ts") or ""
    text = str(item.get("text") or "").strip() or "(no text)"
    lines = [f"[{index}] {user} at {ts}:", text]
    attachments = item.get("attachments")
    if isinstance(attachments, list) and attachments:
        lines.append("Attachments:")
        for attachment_index, attachment in enumerate(attachments, start=1):
            if isinstance(attachment, dict):
                lines.append(_format_attachment(attachment_index, attachment))
    return "\n".join(lines)


def _format_attachment(index: int, item: dict[str, Any]) -> str:
    attachment_type = item.get("type") or "attachment"
    title = item.get("title") or item.get("name") or "untitled"
    lines = [f"- Attachment {index} ({attachment_type}): {title}"]
    for key in (
        "id",
        "name",
        "mimetype",
        "filetype",
        "pretty_type",
        "size",
        "permalink",
        "url_private",
        "url_private_download",
        "service_name",
        "from_url",
        "original_url",
        "fallback",
        "text",
    ):
        value = item.get(key)
        if value not in (None, ""):
            lines.append(f"  {key}: {value}")
    content = item.get("content")
    if isinstance(content, str) and content:
        lines.extend(["  content:", _indent(content.rstrip(), "    ")])
        if item.get("content_truncated"):
            lines.append("  content_truncated: true")
    return "\n".join(lines)


def _format_dict(data: dict[str, Any]) -> str:
    return "\n".join(f"- {key}: {value}" for key, value in data.items())


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())


if __name__ == "__main__":
    main()
