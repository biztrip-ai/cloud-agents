"""Run with: uv run --with pytest -m pytest tests"""

from bizzybot_agent_wrapper.agent_wrapper import normalize_agent_event

ME = "U0C3T53LH1Q"


def event(ts, text=f"<@{ME}> what is new?", user="U0C2U8R19U5", bot_id="B0C3TGJD6SC"):
    return {"type": "app_mention", "channel": "C1", "ts": ts, "text": text,
            "user": user, "bot_id": bot_id}


def test_any_bot_may_mention_when_allowlist_unset():
    msg = normalize_agent_event(event("1.1"), ME, frozenset())
    assert msg["text"] == "what is new?"
    assert msg["from_agent"] is True


def test_allowlist_restricts_when_set():
    assert normalize_agent_event(event("1.2"), ME, frozenset({"UOTHER"})) is None
    assert normalize_agent_event(event("1.3"), ME, frozenset({"B0C3TGJD6SC"})) is not None


def test_plain_text_handle_is_not_a_mention():
    assert normalize_agent_event(event("1.4", text="@BizzyBrain what is new?"), ME, frozenset()) is None


def test_own_messages_ignored():
    assert normalize_agent_event(event("1.5", user=ME), ME, frozenset()) is None
