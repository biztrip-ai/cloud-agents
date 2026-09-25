"""Run with: uv run --with pytest -m pytest tests"""

import asyncio

from bizzybot_agent_wrapper.scheduled import PROMPT_PREFIX, run_scheduled_prompt
from bizzybot_agent_wrapper.session_manager import Chunk


class FakeSession:
    def __init__(self, chunks, boom=None):
        self.chunks, self.boom, self.prompts = chunks, boom, []

    async def send(self, prompt):
        self.prompts.append(prompt)
        for c in self.chunks:
            yield c
        if self.boom:
            raise self.boom


class FakeSessions:
    def __init__(self, session):
        self.session, self.created, self.dropped = session, [], []

    async def get_or_create(self, key):
        self.created.append(key)
        return self.session

    async def drop(self, key):
        self.dropped.append(key)
        return True


def run(payload, session):
    sessions, reports = FakeSessions(session), []

    async def report(r):
        reports.append(r)

    asyncio.run(run_scheduled_prompt(payload, sessions, report))
    return sessions, reports


PAYLOAD = {"task_id": "t1", "run_id": "r1", "text": "check Moderna news"}


def test_reply_and_cost_are_reported_and_session_forgotten():
    session = FakeSession([
        Chunk("turn_start", ""),
        Chunk("text", "Nothing new."),
        Chunk("text", "sub-agent chatter", subagent=True),
        Chunk("result", "cost=$0.12", cost_usd=0.12, duration_ms=9000),
    ])
    sessions, reports = run(PAYLOAD, session)
    assert session.prompts == [PROMPT_PREFIX + "check Moderna news"]
    assert sessions.created == ["scheduled:t1:r1"]
    assert sessions.dropped == ["scheduled:t1:r1"]  # fresh each run
    assert reports == [{
        "task_id": "t1", "run_id": "r1", "ok": True, "error": None,
        "text": "Nothing new.", "cost_usd": 0.12, "duration_ms": 9000,
    }]


def test_failed_turn_is_reported_not_raised():
    sessions, reports = run(PAYLOAD, FakeSession([Chunk("text", "partial")], boom=RuntimeError("cli died")))
    assert sessions.dropped == ["scheduled:t1:r1"]
    assert reports[0]["ok"] is False
    assert reports[0]["error"] == "cli died"
    assert reports[0]["text"] == "partial"


def test_error_result_is_a_failed_run():
    _, reports = run(PAYLOAD, FakeSession([Chunk("result", "ERROR: overloaded", is_error=True)]))
    assert reports[0]["ok"] is False
    assert reports[0]["error"] == "ERROR: overloaded"


def test_malformed_payload_is_dropped():
    sessions, reports = run({"task_id": "t1"}, FakeSession([]))
    assert sessions.created == [] and reports == []
