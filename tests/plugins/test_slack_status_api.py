"""Slack free-text progress and clearing must survive a newer installed SDK."""

from types import SimpleNamespace

import pytest

from slack_sdk.web.async_client import AsyncWebClient

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter


class CapturingClient(AsyncWebClient):
    """Keep the real SDK's endpoint/argument serialization, without network I/O."""

    def __init__(self):
        super().__init__(token="synthetic-test-token")
        self.calls = []

    async def api_call(self, api_method, **kwargs):
        self.calls.append((api_method, kwargs.get("json") or kwargs.get("params")))
        return {"ok": True}


@pytest.fixture
def adapter():
    client = CapturingClient()
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="synthetic-test-token"))
    adapter._app = SimpleNamespace(client=client)
    return adapter, client


@pytest.mark.asyncio
async def test_progress_preserves_free_text_with_agent_sessions_sdk(adapter):
    adapter, client = adapter
    metadata = {"thread_id": "123.456"}
    statuses = ("is thinking...", "Searching files...", "still working... (2m03s)")

    for status in statuses:
        adapter.set_status_text("C_TEST", status)
        await adapter.send_typing("C_TEST", metadata=metadata)

    assert client.calls == [
        ("assistant.threads.setStatus", {
            "channel_id": "C_TEST", "thread_ts": "123.456", "status": status,
        })
        for status in statuses
    ]


@pytest.mark.asyncio
async def test_completion_clears_the_same_thread_with_agent_sessions_sdk(adapter):
    adapter, client = adapter
    metadata = {"thread_id": "123.456"}

    await adapter.send_typing("C_TEST", metadata=metadata)
    await adapter.stop_typing("C_TEST", metadata=metadata)

    assert len(client.calls) == 2
    assert client.calls[0][0] == client.calls[1][0] == "assistant.threads.setStatus"
    assert client.calls[1][1] == {
        "channel_id": "C_TEST", "thread_ts": "123.456", "status": "",
    }
    assert not adapter._active_status_threads
