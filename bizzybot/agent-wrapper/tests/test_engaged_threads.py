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
