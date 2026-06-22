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
    token: ""
    headers: {}
```

Then run:

```bash
uv run slack-to-coding-agent
```

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
  "metadata": {
    "source": "slack",
    "user_id": "U123",
    "channel_id": "C123",
    "thread_ts": "1710000000.000000"
  }
}
```

For JSON responses, it reads the first non-empty value from `response_json_paths` in the config. For non-JSON responses, it posts the response body as Slack text.
