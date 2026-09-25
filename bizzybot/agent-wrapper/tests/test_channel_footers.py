"""Run with: uv run --with pytest -m pytest tests

A turn that reports into another channel with post_message keeps a "working"
line under its latest report there, and takes it away when the turn ends.
"""

import asyncio
import json

from bizzybot_agent_wrapper import slack_io
from bizzybot_agent_wrapper.slack_io import ChannelFooters, FooterLedger, posted_channel


class FakeSlack:
    def __init__(self, fail=()):
        self.calls = []
        self.live = {}
        self.fail = set(fail)
        self._n = 0

    async def chat_postMessage(self, channel, text, **_):
        if "post" in self.fail:
            raise RuntimeError("boom")
        self._n += 1
        ts = f"{self._n}.0"
        self.live[(channel, ts)] = text
        self.calls.append(("post", channel, text))
        return {"ts": ts}

    async def chat_update(self, channel, ts, text):
        self.live[(channel, ts)] = text
        self.calls.append(("update", channel, text))

    async def chat_delete(self, channel, ts):
        if "delete" in self.fail:
            raise RuntimeError("boom")
        self.live.pop((channel, ts), None)
        self.calls.append(("delete", channel, ts))


def run(coro):
    return asyncio.run(coro)


def test_footer_follows_each_report_and_goes_at_the_end(tmp_path, monkeypatch):
    monkeypatch.setattr(slack_io, "MIN_UPDATE_INTERVAL_S", 0)
    slack = FakeSlack()
    ledger = FooterLedger(str(tmp_path / "f.json"))
    footers = ChannelFooters(slack, skip_channel="CHOME", ledger=ledger)

    async def turn():
        await footers.after_post("CTICKET")
        assert list(slack.live.values()) == ["_🔨 working…_"]
        await footers.status("🔧 pytest · 4m")
        assert list(slack.live.values()) == ["_🔨 working · 🔧 pytest · 4m_"]
        await footers.after_post("CTICKET")  # a second report: footer moves under it
        assert len(slack.live) == 1 and ("CTICKET", "2.0") in slack.live
        assert json.load(open(tmp_path / "f.json")) == [["CTICKET", "2.0"]]
        await footers.close()

    run(turn())
    assert slack.live == {}
    assert json.load(open(tmp_path / "f.json")) == []


def test_the_turns_own_channel_gets_no_footer():
    slack = FakeSlack()
    footers = ChannelFooters(slack, skip_channel="CHOME")
    run(footers.after_post("CHOME"))
    assert slack.calls == []


def test_unchanged_label_is_not_re_edited(monkeypatch):
    monkeypatch.setattr(slack_io, "MIN_UPDATE_INTERVAL_S", 0)
    slack = FakeSlack()
    footers = ChannelFooters(slack)

    async def turn():
        await footers.after_post("C1")
        await footers.status("📄 Read a.py")
        await footers.status("📄 Read a.py")

    run(turn())
    assert [c[0] for c in slack.calls] == ["post", "update"]


def test_slack_failures_never_raise():
    slack = FakeSlack(fail={"post"})
    footers = ChannelFooters(slack)
    run(footers.after_post("C1"))
    run(footers.close())


def test_a_footer_that_wont_delete_is_swept_on_next_start(tmp_path):
    ledger = FooterLedger(str(tmp_path / "f.json"))
    slack = FakeSlack(fail={"delete"})
    footers = ChannelFooters(slack, ledger=ledger)
    run(footers.after_post("C1"))
    run(footers.close())
    assert json.load(open(tmp_path / "f.json")) == [["C1", "1.0"]]

    later = FakeSlack()
    run(ledger.sweep(later))
    assert later.calls == [("delete", "C1", "1.0")]
    assert json.load(open(tmp_path / "f.json")) == []


def test_posted_channel_reads_the_tool_result():
    ok = json.dumps({"channel": "C9", "ts": "1.2"})
    assert posted_channel({"channel": "#bp-86"}, ok) == "C9"
    assert posted_channel({"channel": "C9", "thread_ts": "1.0"}, ok) is None
    assert posted_channel({"channel": "C9"}, "No channel #nope that the bot can see") is None
    assert posted_channel({"channel": "C9"}, None) is None
