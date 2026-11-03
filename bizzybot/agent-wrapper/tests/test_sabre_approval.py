"""Run with: uv run --with pytest -m pytest tests

The gate's job is to never let a production Sabre write through unasked, so
most of these are about the ways it must refuse: no reaction, a reaction from
an agent, a reaction from a bystander, a thread it cannot post to, a
set_sabre_env it cannot verify.
"""

import asyncio
import re

import pytest

from slack_sdk.errors import SlackApiError

from bizzybot_agent_wrapper import sabre_approval
from bizzybot_agent_wrapper.agent_wrapper import sabre_approval_config
from bizzybot_agent_wrapper.session_manager import SessionManager
from bizzybot_agent_wrapper.sabre_approval import (
    APPROVE,
    CERT_ENV,
    DECLINE,
    THUMBS_UP,
    SabreWriteApprover,
)

SCOTT = "U_scott"
DANA = "U_dana"
BUILDER = "U_builder_bot"
KEY = "C_chan:1700000000.000100"
PROMPT_TS = "1700000000.000200"


class FakeSlack:
    """Enough of AsyncWebClient to post one prompt and read its reactions.

    `reactions` is mutated by a test (or by `react_after`) to play out what the
    humans do while the gate polls.
    """

    def __init__(self, reactions=None, react_after=0, post_error=None):
        self.reactions = dict(reactions or {})
        self.react_after = react_after
        self.post_error = post_error
        self.posts = []
        self.polls = 0

    async def chat_postMessage(self, channel, thread_ts, text):
        if self.post_error:
            raise self.post_error
        self.posts.append({"channel": channel, "thread_ts": thread_ts, "text": text})
        return {"ts": PROMPT_TS}

    async def conversations_replies(self, channel, ts, oldest, latest, inclusive, limit):
        self.polls += 1
        reactions = self.reactions if self.polls > self.react_after else {}
        return {
            "messages": [
                {
                    "ts": PROMPT_TS,
                    "reactions": [
                        {"name": name, "users": list(users)}
                        for name, users in reactions.items()
                    ],
                }
            ]
        }


def approver(slack=None, *, default_env="moderna", approvers=(SCOTT,), timeout_s=0.3):
    return SabreWriteApprover(
        slack,
        default_env=default_env,
        approvers=approvers,
        timeout_s=timeout_s,
        poll_s=0.01,
    )


def run(coro):
    return asyncio.run(coro)


# --- what the gate even looks at ---------------------------------------------


def test_matcher_covers_every_write_tool_and_the_env_switch():
    pattern = re.compile(sabre_approval.TOOL_MATCHER)
    for tool in sabre_approval.WRITE_TOOLS | {sabre_approval.ENV_TOOL}:
        assert pattern.match(f"mcp__sabre-search__{tool}"), tool
    assert not pattern.match("mcp__sabre-search__search_flights")
    assert not pattern.match("mcp__sabre-search__get_booking_rest")
    # A renamed MCP server must not silently switch the gate off.
    assert pattern.match("mcp__sabre_prod__create_booking")


def test_reads_are_not_gated():
    a = approver()
    out = run(a.decide(KEY, "mcp__sabre-search__get_booking_rest", {"confirmation_id": "ABC123"}))
    assert out == {}


# --- env resolution (mirrors _env.py:resolve_active_sabre_env) ---------------


def test_per_call_override_beats_the_default():
    a = approver(default_env="moderna")
    assert a.resolve_env(KEY, {"sabre_env": "certmoderna"}) == "certmoderna"
    assert a.resolve_env(KEY, {"sabre_env": "biztrip_prod"}) == "biztrip_prod"


def test_default_applies_when_no_override():
    assert approver(default_env="certmoderna").resolve_env(KEY, {}) == "certmoderna"
    assert approver(default_env="moderna").resolve_env(KEY, {"sabre_env": None}) == "moderna"


def test_set_sabre_env_moves_the_default_for_that_thread_only():
    a = approver(default_env="moderna")
    run(a.decide(KEY, "mcp__sabre-search__set_sabre_env", {"name": CERT_ENV}))
    assert a.resolve_env(KEY, {}) == CERT_ENV
    assert a.resolve_env("C_other:1.2", {}) == "moderna"


def test_unverifiable_set_sabre_env_is_held_as_production():
    # The hook runs before the call, so a name the server would reject must not
    # be allowed to lower the gate.
    a = approver(default_env=CERT_ENV)
    run(a.decide(KEY, "mcp__sabre-search__set_sabre_env", {"name": "certmodernaX"}))
    assert a.resolve_env(KEY, {}) in sabre_approval.PROD_ENVS


def test_set_sabre_env_to_prod_raises_the_gate():
    a = approver(default_env=CERT_ENV)
    run(a.decide(KEY, "mcp__sabre-search__set_sabre_env", {"name": "moderna"}))
    assert a.resolve_env(KEY, {}) == "moderna"


# --- non-production writes go straight through -------------------------------


def test_cert_write_is_allowed_without_asking():
    slack = FakeSlack()
    a = approver(slack, default_env=CERT_ENV)
    out = run(a.decide(KEY, "mcp__sabre-search__create_booking", {"travelers": [{}]}))
    assert out == {}
    assert slack.posts == []


def test_per_call_cert_override_on_a_prod_box_is_allowed_without_asking():
    slack = FakeSlack()
    a = approver(slack, default_env="moderna")
    out = run(
        a.decide(KEY, "mcp__sabre-search__cancel_booking",
                 {"confirmation_id": "ABC123", "sabre_env": CERT_ENV})
    )
    assert out == {}
    assert slack.posts == []


# --- production writes ask ----------------------------------------------------


def test_prod_write_asks_and_names_the_tool_and_its_arguments():
    slack = FakeSlack(reactions={APPROVE: [SCOTT]})
    a = approver(slack)
    run(a.decide(KEY, "mcp__sabre-search__cancel_booking", {"confirmation_id": "ABC123"}))
    assert len(slack.posts) == 1
    post = slack.posts[0]
    assert post["channel"] == "C_chan"
    assert post["thread_ts"] == "1700000000.000100"
    assert "cancel_booking" in post["text"]
    assert "moderna" in post["text"]
    assert "ABC123" in post["text"]


def test_approval_by_an_allowlisted_human_allows_the_call_unchanged():
    slack = FakeSlack(reactions={APPROVE: [SCOTT]})
    out = run(approver(slack).decide(
        KEY, "mcp__sabre-search__create_booking", {"travelers": [{}, {}]}
    ))
    assert out["permissionDecision"] == "allow"
    assert "updatedInput" not in out
    assert SCOTT in out["permissionDecisionReason"]


def test_thumbs_up_counts_even_with_a_skin_tone():
    slack = FakeSlack(reactions={f"{THUMBS_UP}::skin-tone-3": [SCOTT]})
    out = run(approver(slack).decide(
        KEY, "mcp__sabre-search__void_flight_tickets", {"confirmation_id": "ABC123"}
    ))
    assert out["permissionDecision"] == "allow"
    assert "updatedInput" not in out


def test_a_late_approval_still_counts():
    slack = FakeSlack(reactions={APPROVE: [SCOTT]}, react_after=3)
    out = run(approver(slack).decide(
        KEY, "mcp__sabre-search__flight_exchange_book", {"pnr_locator": "ABC123"}
    ))
    assert out["permissionDecision"] == "allow"
    assert "updatedInput" not in out


# --- who may approve ----------------------------------------------------------


def test_another_agent_cannot_approve():
    slack = FakeSlack(reactions={APPROVE: [BUILDER]})
    out = run(approver(slack).decide(
        KEY, "mcp__sabre-search__create_booking", {"travelers": [{}]}
    ))
    assert out["updatedInput"]["sabre_env"] == CERT_ENV


def test_a_bystander_cannot_approve():
    slack = FakeSlack(reactions={APPROVE: [DANA]})
    out = run(approver(slack).decide(
        KEY, "mcp__sabre-search__create_booking", {"travelers": [{}]}
    ))
    assert out["updatedInput"]["sabre_env"] == CERT_ENV


def test_a_configured_human_may_approve_alongside_the_sponsor():
    slack = FakeSlack(reactions={APPROVE: [DANA]})
    out = run(approver(slack, approvers=(SCOTT, DANA)).decide(
        KEY, "mcp__sabre-search__create_booking", {"travelers": [{}]}
    ))
    assert out["permissionDecision"] == "allow"
    assert "updatedInput" not in out


def test_parse_approvers_keeps_the_sponsor_and_drops_blanks():
    assert sabre_approval.parse_approvers(" U_a , ,U_b ", SCOTT) == frozenset(
        {"U_a", "U_b", SCOTT}
    )
    assert sabre_approval.parse_approvers(None, SCOTT) == frozenset({SCOTT})
    assert sabre_approval.parse_approvers("", None) == frozenset()


# --- everything else falls back to cert --------------------------------------


def test_decline_rewrites_the_call_to_cert():
    slack = FakeSlack(reactions={DECLINE: [SCOTT]})
    out = run(approver(slack).decide(
        KEY, "mcp__sabre-search__update_reservation_delete_product",
        {"locator": "ABC123", "segment_element_id": "81"},
    ))
    assert out["permissionDecision"] == "allow"
    assert out["updatedInput"] == {
        "locator": "ABC123", "segment_element_id": "81", "sabre_env": CERT_ENV
    }


def test_decline_tells_the_agent_what_happened_and_not_to_retry():
    slack = FakeSlack(reactions={DECLINE: [SCOTT]})
    out = run(approver(slack).decide(
        KEY, "mcp__sabre-search__create_booking", {"hotel": {"search_id": "s1"}}
    ))
    note = out["additionalContext"]
    assert out["permissionDecisionReason"] == note
    assert CERT_ENV in note
    assert "travelers[] range" in note
    assert "Do not retry against production" in note


def test_a_decline_from_anyone_counts():
    # A decline only ever makes the write safer, so it needs no allowlist.
    slack = FakeSlack(reactions={DECLINE: [DANA]})
    out = run(approver(slack).decide(
        KEY, "mcp__sabre-search__create_booking", {"travelers": [{}]}
    ))
    assert out["updatedInput"]["sabre_env"] == CERT_ENV


def test_a_decline_beats_an_approval_on_the_same_message():
    slack = FakeSlack(reactions={APPROVE: [SCOTT], DECLINE: [DANA]})
    out = run(approver(slack).decide(
        KEY, "mcp__sabre-search__create_booking", {"travelers": [{}]}
    ))
    assert out["updatedInput"]["sabre_env"] == CERT_ENV


def test_no_answer_times_out_to_cert():
    slack = FakeSlack()
    out = run(approver(slack).decide(
        KEY, "mcp__sabre-search__flight_exchange_queue_for_ticketing", {"pnr_locator": "ABC123"}
    ))
    assert out["updatedInput"]["sabre_env"] == CERT_ENV
    assert slack.polls > 1


def test_a_prod_override_is_rewritten_too_not_just_added():
    slack = FakeSlack()
    out = run(approver(slack, default_env=CERT_ENV).decide(
        KEY, "mcp__sabre-search__update_reservation_swap_product",
        {"locator": "ABC123", "old_segment_id": "81", "sabre_env": "biztrip_prod"},
    ))
    assert out["updatedInput"]["sabre_env"] == CERT_ENV


def test_an_unpostable_thread_falls_back_to_cert():
    slack = FakeSlack(post_error=SlackApiError("not_in_channel", {"error": "not_in_channel"}))
    out = run(approver(slack).decide(
        KEY, "mcp__sabre-search__create_booking", {"travelers": [{}]}
    ))
    assert out["updatedInput"]["sabre_env"] == CERT_ENV


def test_no_slack_client_falls_back_to_cert():
    out = run(approver(None).decide(
        KEY, "mcp__sabre-search__create_booking", {"travelers": [{}]}
    ))
    assert out["updatedInput"]["sabre_env"] == CERT_ENV


def test_no_approvers_configured_falls_back_to_cert():
    slack = FakeSlack(reactions={APPROVE: [SCOTT]})
    out = run(approver(slack, approvers=()).decide(
        KEY, "mcp__sabre-search__create_booking", {"travelers": [{}]}
    ))
    assert out["updatedInput"]["sabre_env"] == CERT_ENV


# --- the prompt ---------------------------------------------------------------


def test_summarize_names_products_and_traveler_count():
    text = sabre_approval.summarize(
        {"travelers": [{}, {}], "hotel": {"search_id": "s1"}, "car": {"search_id": "s2"},
         "target_pcc": "0K3L"}
    )
    assert "2 traveler(s)" in text
    assert "hotel" in text and "car" in text
    assert "0K3L" in text


def test_summarize_survives_an_argument_free_call():
    assert sabre_approval.summarize({}) == "_no identifying arguments_"


@pytest.mark.parametrize("tool", sorted(sabre_approval.WRITE_TOOLS))
def test_every_write_tool_is_gated(tool):
    slack = FakeSlack()
    out = run(approver(slack).decide(KEY, f"mcp__sabre-search__{tool}", {"locator": "ABC123"}))
    assert out["updatedInput"]["sabre_env"] == CERT_ENV
    assert len(slack.posts) == 1


# --- wiring into the session ---------------------------------------------------


def _manager(**kw):
    return SessionManager(cwd=".", permission_mode="bypassPermissions", **kw)


def test_the_gate_is_registered_as_a_pretooluse_hook():
    opts = _manager(sabre_approver=approver(FakeSlack()))._build_options(KEY)
    [matcher] = opts.hooks["PreToolUse"]
    assert matcher.matcher == sabre_approval.TOOL_MATCHER
    # The gate does its own waiting; the SDK timeout is only a backstop.
    assert matcher.timeout > sabre_approval.TIMEOUT_S
    assert "SubagentStop" in opts.hooks


def test_no_approver_means_no_hook():
    assert "PreToolUse" not in _manager()._build_options(KEY).hooks


def test_the_hook_wraps_the_decision_for_the_sdk():
    slack = FakeSlack(reactions={DECLINE: [SCOTT]})
    hook = _manager(sabre_approver=approver(slack))._make_sabre_write_hook(KEY)
    out = run(hook(
        {"tool_name": "mcp__sabre-search__create_booking", "tool_input": {"travelers": [{}]}},
        "tu_1",
        None,
    ))
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert out["hookSpecificOutput"]["updatedInput"]["sabre_env"] == CERT_ENV


def test_a_gate_that_errors_blocks_the_call():
    class Broken:
        async def decide(self, *_args):
            raise RuntimeError("slack is on fire")

    hook = _manager(sabre_approver=Broken())._make_sabre_write_hook(KEY)
    out = run(hook(
        {"tool_name": "mcp__sabre-search__create_booking", "tool_input": {}}, "tu_1", None
    ))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- how the wrapper learns the MCP server's default env ----------------------


def test_default_env_prefers_what_the_mcp_entry_pins(monkeypatch):
    monkeypatch.delenv("SABRE_ENV", raising=False)
    env, _ = sabre_approval_config(
        {"SABRE_ENV": CERT_ENV},
        {sabre_approval.SERVER_NAME: {"env": {"SABRE_ENV": "moderna"}}},
        SCOTT,
    )
    assert env == "moderna"


def test_default_env_falls_back_to_settings_then_to_production(monkeypatch):
    monkeypatch.delenv("SABRE_ENV", raising=False)
    env, approvers = sabre_approval_config(
        {"SABRE_ENV": CERT_ENV, "SABRE_WRITE_APPROVERS": DANA},
        {sabre_approval.SERVER_NAME: {"env": {}}},
        SCOTT,
    )
    assert env == CERT_ENV
    assert approvers == frozenset({SCOTT, DANA})
    assert sabre_approval_config({}, {}, SCOTT)[0] in sabre_approval.PROD_ENVS
