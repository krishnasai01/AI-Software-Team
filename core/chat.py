"""Chat adapter. Slack now; swap in Mattermost or Discord by implementing `Chat`."""

from typing import Protocol

from core.settings import get_settings


class Chat(Protocol):
    async def post(self, text: str, thread: str | None = None) -> str:
        """Post a message; return a permalink (used as the approvals.chat_message_link)."""
        ...


class SlackChat:
    def __init__(self, channel: str | None = None):
        from slack_sdk.web.async_client import AsyncWebClient

        s = get_settings()
        self.client = AsyncWebClient(token=s.slack_bot_token)
        self.channel = channel or s.slack_channel

    async def post(self, text: str, thread: str | None = None) -> str:
        r = await self.client.chat_postMessage(channel=self.channel, text=text, thread_ts=thread, unfurl_links=False)
        link = await self.client.chat_getPermalink(channel=r["channel"], message_ts=r["ts"])
        return link["permalink"]


class LogChat:
    """Fallback when Slack is not configured (local development)."""

    async def post(self, text: str, thread: str | None = None) -> str:
        print(f"[chat] {text}")
        return ""


def get_chat() -> Chat:
    return SlackChat() if get_settings().slack_bot_token else LogChat()
