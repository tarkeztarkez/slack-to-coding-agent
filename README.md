# slack-to-coding-agent

A cross-platform Slack socket-mode bot that forwards:

- `@bot` mentions in channels
- direct messages to the bot
- Slack assistant thread starts/messages where Slack exposes them as assistant/DM events

…to a local coding-agent backend. The default backend is a configurable HTTP JSON target intended for a local Codex app-server, and the config shape is ready for additional backends later.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- A Slack app created from `manifests/slack-app-manifest.yaml`
- A Slack bot token (`xoxb-...`)
- A Slack app-level socket-mode token (`xapp-...`) with `connections:write`

## Setup

```bash
uv sync
uv run slack-to-coding-agent --init-config
```

Edit `~/.slack-to-coding-agent/config.yaml`:

```yaml
slack:
  bot_token: "xoxb-your-bot-token"
  app_token: "xapp-your-app-level-token"
  signing_secret: ""
  allowed_user_id: "" # optional: set to a Slack user ID like U012ABC to only answer that user
backend:
  active: codex
backends:
  codex:
    type: http_json
    base_url: "http://127.0.0.1:1455"
    path: "/api/chat"
    method: POST
    timeout_seconds: 300
    # Optional: command to auto-start a local backend before connecting to Slack.
    start_command: ""
    startup_cwd: ""
    startup_env: {}
    startup_timeout_seconds: 30
    health_url: ""
    startup_log_file: ""
    token: ""
    headers: {}
```

Then run:

```bash
uv run slack-to-coding-agent
```

### Install as an autostart service

On Linux (systemd user service) or macOS (LaunchAgent), install and start the bot with:

```bash
uv run slack-to-coding-agent --install-service
```

The service runs the current Python environment with the selected config file and starts
automatically on login. Pass `--config /path/to/config.yaml` or `--log-level DEBUG` with
`--install-service` to bake those values into the service.

Linux logs are written to `~/.slack-to-coding-agent/service.log`. macOS logs are written to
`~/.slack-to-coding-agent/service.out.log` and `~/.slack-to-coding-agent/service.err.log`.

### Auto-starting a Codex backend

This package includes a small HTTP adapter for Codex CLI. It exposes the `/api/chat` endpoint
expected by the default `http_json` backend and runs `codex exec` for each Slack message.

Example backend config:

```yaml
backends:
  codex:
    type: http_json
    base_url: "http://127.0.0.1:1455"
    path: "/api/chat"
    method: POST
    timeout_seconds: 300
    start_command: "slack-to-coding-agent-codex-server --host 127.0.0.1 --port 1455 --codex-cwd /path/to/repo"
    health_url: "http://127.0.0.1:1455/healthz"
    startup_timeout_seconds: 30
```

Backend process output is written to `~/.slack-to-coding-agent/<backend-name>-backend.log` unless
`startup_log_file` is set.

## Slack app creation

1. Open <https://api.slack.com/apps>.
2. Create an app from `manifests/slack-app-manifest.yaml`.
3. Install the app to your workspace and copy the bot token into `~/.slack-to-coding-agent/config.yaml`.
4. Create an app-level token with `connections:write`, then copy it into `slack.app_token`.
5. Invite the bot to channels where you want to mention it.

## Backend request

The built-in `http_json` backend sends a POST like this to `base_url + path`:

```json
{
  "message": "user text",
  "prompt": "user text",
  "session_id": "slack:C123:1710000000.000000",
  "thread_id": "slack:C123:1710000000.000000",
  "thread_messages": [
    {
      "ts": "1710000000.000000",
      "user_id": "U123",
      "text": "previous thread message",
      "attachments": []
    }
  ],
  "attachments": [
    {
      "type": "file",
      "name": "example.py",
      "mimetype": "text/x-python",
      "content": "print('hello')"
    }
  ],
  "metadata": {
    "source": "slack",
    "user_id": "U123",
    "channel_id": "C123",
    "thread_ts": "1710000000.000000"
  }
}
```

Thread history is fetched with `conversations.replies` when Slack permissions allow it. File
attachments include metadata, and text-like Slack files include downloaded content up to 50 KB when
the app has `files:read`.

For JSON responses, it reads the first non-empty value from `response_json_paths` in the config. For non-JSON responses, it posts the response body as Slack text.
