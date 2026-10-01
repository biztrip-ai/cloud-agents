"""Run with: uv run --with pytest -m pytest tests

A follow-up in a thread the bot is answering in is a message *to* the bot. It
must reach the normal path even in a channel under passive listening, which
would otherwise buffer it as chatter and read it in silence.
"""

from bizzybot_agent_wrapper.agent_wrapper import in_engaged_thread


class Sessions:
    def __init__(self, live=(), resumable=()):
        self.live, self.resumable = set(live), set(resumable)

    def exists(self, key):
        return key in self.live

    def has_resume(self, key):
        return key in self.resumable


def reply(channel="C1", thread_ts="100.1", **extra):
    return {"type": "message", "channel": channel, "thread_ts": thread_ts, "ts": "100.2", **extra}


def test_a_reply_in_a_live_thread_is_ours():
    assert in_engaged_thread(reply(), Sessions(live={"C1:100.1"}))


def test_a_reply_in_a_thread_we_can_resume_is_ours():
    assert in_engaged_thread(reply(), Sessions(resumable={"C1:100.1"}))


def test_a_reply_in_some_other_thread_is_not():
    assert not in_engaged_thread(reply(thread_ts="200.1"), Sessions(live={"C1:100.1"}))
    assert not in_engaged_thread(reply(channel="C2"), Sessions(live={"C1:100.1"}))


def test_a_top_level_message_is_never_a_thread_reply():
    top = {"type": "message", "channel": "C1", "ts": "100.1"}
    assert not in_engaged_thread(top, Sessions(live={"C1:100.1"}))


def test_non_messages_are_ignored():
    assert not in_engaged_thread({"type": "reaction_added", "channel": "C1", "thread_ts": "100.1"}, Sessions(live={"C1:100.1"}))
    assert not in_engaged_thread("not a dict", Sessions(live={"C1:100.1"}))


# --- Replies under a "working" footer ------------------------------------------

import asyncio

from bizzybot_agent_wrapper import agent_wrapper, slack_io


def test_a_reply_under_a_working_footer_is_ours(monkeypatch):
    monkeypatch.setitem(slack_io._FOOTER_OWNERS, "C1:300.1", "C9:100.1")
    assert in_engaged_thread(reply(thread_ts="300.1"), Sessions())


def test_stop_under_a_working_footer_stops_the_turn_that_owns_it(monkeypatch):
    # The footer sits in #bp-212; the turn it reports for lives in another thread.
    monkeypatch.setitem(slack_io._FOOTER_OWNERS, "C1:300.1", "C1:100.1")
    seen = []

    async def fake_stop(payload, sessions, slack):
        seen.append(payload)

    async def bot_id(_slack):
        return "UBOT"

    monkeypatch.setitem(agent_wrapper.META_COMMANDS, "!stop", (fake_stop, "stop"))
    monkeypatch.setattr(agent_wrapper, "get_bot_user_id", bot_id)
    event = reply(thread_ts="300.1", channel_type="channel", user="UHUMAN", text="!stop")
    asyncio.run(agent_wrapper.dispatch_event(event, Sessions(), slack=None))
    assert len(seen) == 1
    assert seen[0]["thread_key"] == "C1:100.1"  # the owning turn's session
    assert (seen[0]["channel"], seen[0]["reply_thread_ts"]) == ("C1", "300.1")  # answered where typed
