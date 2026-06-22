from __future__ import annotations

import logging
import unittest

from slack_to_coding_agent.backends import AgentRequest
from slack_to_coding_agent.config import AppConfig, BackendConfig, SlackConfig
from slack_to_coding_agent.slack_app import SlackBridge


class FakeBackend:
    def send(self, request: AgentRequest) -> str:
        return "ok"


class FakeSlackClient:
    token = "xoxb-test"

    def __init__(self, *, history_messages=None, reply_messages=None):
        self.history_messages = history_messages or []
        self.reply_messages = reply_messages or []
        self.history_calls: list[dict] = []
        self.reply_calls: list[dict] = []

    def conversations_history(self, **kwargs):
        self.history_calls.append(kwargs)
        return {"messages": self.history_messages}

    def conversations_replies(self, **kwargs):
        self.reply_calls.append(kwargs)
        return {"messages": self.reply_messages}


class SlackBridgeContextTest(unittest.TestCase):
    def setUp(self) -> None:
        config = AppConfig(
            slack=SlackConfig(bot_token="xoxb-test", app_token="xapp-test"),
            backend=BackendConfig(
                name="test", type="http_json", base_url="http://127.0.0.1", path="/api/chat"
            ),
        )
        self.bridge = SlackBridge(
            config=config,
            backend=FakeBackend(),
            bot_user_id="B123",
            logger=logging.getLogger("test"),
        )

    def test_unthreaded_channel_mention_includes_recent_channel_context(self) -> None:
        current_event = {
            "type": "app_mention",
            "channel": "C123",
            "ts": "2000.000000",
            "user": "U2",
            "text": "<@B123> can you answer this?",
        }
        previous_message = {
            "channel": "C123",
            "ts": "1999.000000",
            "user": "U1",
            "text": "What broke in production?",
        }
        client = FakeSlackClient(history_messages=[current_event, previous_message])
        request = AgentRequest(
            message="can you answer this?",
            session_id="slack:C123:2000.000000",
            user_id="U2",
            channel_id="C123",
            thread_ts="2000.000000",
            thread_messages=[],
            attachments=[],
        )

        result = self.bridge._with_thread_context(  # noqa: SLF001
            client=client,
            request=request,
            fallback_event=current_event,
        )

        self.assertEqual(
            [m["text"] for m in result.thread_messages],
            [
                "What broke in production?",
                "<@B123> can you answer this?",
            ],
        )
        self.assertEqual(client.history_calls[0]["channel"], "C123")
        self.assertEqual(client.history_calls[0]["latest"], "2000.000000")
        self.assertTrue(client.history_calls[0]["inclusive"])
        self.assertEqual(client.reply_calls, [])

    def test_threaded_channel_mention_includes_parent_thread_message(self) -> None:
        parent_message = {
            "channel": "C123",
            "ts": "1999.000000",
            "user": "U1",
            "text": "Please look at this traceback",
        }
        current_event = {
            "type": "app_mention",
            "channel": "C123",
            "thread_ts": "1999.000000",
            "ts": "2000.000000",
            "user": "U2",
            "text": "<@B123> can you debug it?",
        }
        client = FakeSlackClient(reply_messages=[parent_message, current_event])
        request = AgentRequest(
            message="can you debug it?",
            session_id="slack:C123:1999.000000",
            user_id="U2",
            channel_id="C123",
            thread_ts="1999.000000",
            thread_messages=[],
            attachments=[],
        )

        result = self.bridge._with_thread_context(  # noqa: SLF001
            client=client,
            request=request,
            fallback_event=current_event,
        )

        self.assertEqual(
            [m["text"] for m in result.thread_messages],
            [
                "Please look at this traceback",
                "<@B123> can you debug it?",
            ],
        )
        self.assertEqual(client.reply_calls[0]["channel"], "C123")
        self.assertEqual(client.reply_calls[0]["ts"], "1999.000000")
        self.assertEqual(client.reply_calls[0]["latest"], "2000.000000")
        self.assertTrue(client.reply_calls[0]["inclusive"])
        self.assertEqual(client.history_calls, [])


if __name__ == "__main__":
    unittest.main()
