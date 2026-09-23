"""Run with: uv run --with pytest -m pytest tests

The group-DM listener's job is to *not* read things, so most of these are
about the cases where it must stay out: no grant, no approval, one approval,
an approval by two bystanders, a conversation nobody authorized is in.
"""

import asyncio
import json
import time

import pytest

from bizzybot_agent_wrapper import private_dm
from bizzybot_agent_wrapper.private_dm import APPROVE, DECLINE, PrivateDMListener

SCOTT = "U_scott"
DANA = "U_dana"
TOM = "U_tom"
CONVO = "G_group"

USERS = {
    SCOTT: "Scott Persinger",
    DANA: "Dana Scully",
    TOM: "Tom Romary",
}


class FakeBot:
    """The bot token: used only to turn user ids into names."""

    token = "xoxb-test"

    async def users_info(self, user):
        return {"user": {"id": user, "real_name": USERS.get(user, user), "name": user}}


class FakeUserClient:
    """One authorizing user's token."""

    def __init__(self, world, user):
        self.world = world
        self.user = user
        self.token = f"xoxp-{user}"

    async def users_conversations(self, **kw):
        assert kw.get("types") == "mpim", "group DMs only"
        # Slack's listing carries `updated` — for a group DM, its last message
        # in milliseconds. That one field is what drives the whole design.
        chans = [
            {"id": cid, "updated": int(float(c["updated"]) * 1000)}
            for cid, c in self.world.convos.items()
            if self.user in c["members"]
        ]
        self.world.list_calls += 1
        return {"channels": chans}

    async def conversations_members(self, channel, **kw):
        return {"members": list(self.world.convos[channel]["members"])}

    async def chat_postMessage(self, channel, text):
        ts = f"{self.world.next_ts()}"
        c = self.world.convos[channel]
        c["messages"].append({"ts": ts, "user": self.user, "text": text})
        c["updated"] = float(ts)
        self.world.posted.append((channel, self.user, text))
        return {"ts": ts}

    # No reactions_get: the approval check reads the prompt message itself, so
    # a call to it here would be an AttributeError — which is the point.

    async def conversations_history(
        self, channel, oldest="0", latest=None, limit=50, inclusive=False
    ):
        c = self.world.convos[channel]
        self.world.history_calls += 1
        if latest is not None and inclusive:
            # Exactly one message, by ts, carrying its reactions — how Slack
            # answers oldest == latest == ts.
            m = next((m for m in c["messages"] if m["ts"] == str(latest)), None)
            if not m:
                return {"messages": []}
            live = [{"name": n, "users": sorted(u)} for n, u in c["reactions"].items() if u]
            return {"messages": [dict(m, **({"reactions": live} if live else {}))]}
        msgs = [m for m in c["messages"] if float(m["ts"]) > float(oldest or 0)]
        return {"messages": list(reversed(msgs))[:limit]}


class World:
    def __init__(self, members=(SCOTT, DANA, TOM)):
        # Real epoch seconds: the listener compares message timestamps against
        # the moment it started listening, so a toy clock would put every
        # message in the distant past and nothing would ever fire.
        self._ts = time.time() - 3600
        self.convos = {CONVO: self.blank(members)}
        self.posted = []
        self.history_calls = 0
        self.list_calls = 0
        self.grants = []

    def blank(self, members=(SCOTT, DANA)):
        # `updated` starts at the conversation's creation, as Slack's does —
        # and, as we found the hard way, stays there even when messages arrive.
        return {"members": list(members), "messages": [], "reactions": {}, "updated": 1.0}

    def next_ts(self):
        # Messages happen "now": what matters to the listener is whether a
        # message is newer than the moment it started.
        self._ts = max(time.time(), self._ts + 0.001)
        return round(self._ts, 6)

    def say(self, user, text, convo=CONVO):
        ts = self.next_ts()
        self.convos[convo]["messages"].append({"ts": f"{ts}", "user": user, "text": text})
        self.convos[convo]["updated"] = ts

    def housekeeping_bump(self, convo=CONVO):
        """Slack bumps `updated` about a month after a conversation dies."""
        month = 30 * 86400
        self._ts += month
        self.convos[convo]["updated"] = float(self.convos[convo]["messages"][-1]["ts"]) + month

    def react(self, name, user, convo=CONVO):
        self.convos[convo]["reactions"].setdefault(name, set()).add(user)


def build(world, grant_users=(SCOTT,), tmp_path=None, **kw):
    batches = []

    async def fetch_grants():
        return [{"slackUserId": u, "token": f"xoxp-{u}"} for u in world.grants]

    async def on_batch(cid, text):
        batches.append((cid, text))

    world.grants = list(grant_users)
    listener = PrivateDMListener(
        FakeBot(),
        fetch_grants,
        on_batch,
        agent_label="BizzyBrain",
        state_path=(tmp_path / "private_dms.json") if tmp_path else None,
        **{"members_every": 1, **kw},
    )
    # The real thing builds an AsyncWebClient per token; swap in fakes.
    orig = private_dm.AsyncWebClient
    private_dm.AsyncWebClient = lambda token: FakeUserClient(  # noqa: E731
        world, token.removeprefix("xoxp-")
    )
    listener._restore = lambda: setattr(private_dm, "AsyncWebClient", orig)
    return listener, batches


def run(coro):
    return asyncio.run(coro)


def test_nothing_happens_without_a_grant():
    world = World()
    listener, batches = build(world, grant_users=())
    world.say(SCOTT, "something private")
    run(listener.cycle())
    assert world.posted == [], "must not post into a conversation nobody authorized"
    assert batches == []
    assert world.history_calls == 0, "must not read a single message"


def test_a_dormant_conversation_is_never_tracked():
    world = World()
    listener, batches = build(world)
    world.say(DANA, "something from three years ago")
    # It gets looked at — there is no way to learn a last message without
    # asking — but a conversation that has not spoken since we started is
    # never tracked and never posted into.
    run(listener.cycle())
    run(listener.cycle())
    assert world.posted == [], "a silent conversation is never posted into"
    assert batches == []
    assert listener._convos == {}, "nothing is tracked until it speaks"


def test_slacks_updated_field_is_not_trusted():
    """A group DM with a message today can report `updated` from months ago,
    which is how the second version of this got it wrong."""
    world = World()
    listener, _ = build(world)
    run(listener.cycle())
    world.say(DANA, "something new")
    world.convos[CONVO]["updated"] = 1.0  # Slack's listing, stale as ever
    run(listener.cycle())
    assert len(world.posted) == 1, "the ask must not depend on `updated`"


def test_the_sweep_stays_inside_its_budget():
    world = World()
    for i in range(50):
        world.convos[f"G{i}"] = world.blank()
    listener, _ = build(world, call_budget=20)
    for _ in range(3):
        before = world.history_calls + world.list_calls
        run(listener.cycle())
        assert world.history_calls + world.list_calls - before <= 20
    assert world.posted == [], "51 quiet conversations, nobody asked anything"


def test_it_asks_before_it_reads():
    world = World()
    listener, batches = build(world)
    world.say(SCOTT, "something said before anyone was asked")
    run(listener.cycle())  # watermark only
    assert world.posted == []

    world.say(DANA, "someone is talking again")
    run(listener.cycle())
    assert len(world.posted) == 1
    channel, who, text = world.posted[0]
    assert channel == CONVO and who == SCOTT, "the prompt is posted as an authorizing user"
    assert "BizzyBrain" in text and APPROVE in text and DECLINE in text
    assert batches == [], "nothing is read before approval"

    # One ✅ is not enough.
    world.react(APPROVE, DANA)
    run(listener.cycle())
    assert listener._convos[CONVO]["state"] == "pending"
    assert batches == []

    # Two, but neither has authorized the agent: still not enough.
    world.react(APPROVE, TOM)
    run(listener.cycle())
    assert listener._convos[CONVO]["state"] == "pending", (
        "two bystanders must not be able to opt a conversation in"
    )

    # The authorizing user joins in: approved.
    world.react(APPROVE, SCOTT)
    run(listener.cycle())
    assert listener._convos[CONVO]["state"] == "approved"


def test_approval_does_not_reach_backwards():
    world = World()
    listener, batches = build(world)
    run(listener.cycle())  # watermark
    world.say(DANA, "said in private, before the ask")
    run(listener.cycle())  # the activity triggers the prompt
    world.react(APPROVE, SCOTT)
    world.react(APPROVE, DANA)
    run(listener.cycle())  # approves
    world.say(DANA, "said after approval")
    run(listener.cycle())  # records

    assert len(batches) == 1
    _, text = batches[0]
    assert "said after approval" in text
    assert "before the ask" not in text, "must not read what was said before consent"
    assert "Dana Scully" in text, "authors are named, not raw ids"
    assert "reply with nothing" in text.lower()


def test_a_decline_is_final():
    world = World()
    listener, batches = build(world)
    run(listener.cycle())
    world.say(TOM, "hello")
    run(listener.cycle())
    world.react(DECLINE, TOM)
    run(listener.cycle())
    assert listener._convos[CONVO]["state"] == "declined"

    world.say(SCOTT, "more talk")
    posts_before = len(world.posted)
    run(listener.cycle())
    assert batches == []
    assert len(world.posted) == posts_before, "a declined conversation is never asked again"


def approved(world, tmp_path=None, **kw):
    listener, batches = build(world, tmp_path=tmp_path, **kw)
    run(listener.cycle())  # watermark
    world.say(TOM, "starting a conversation")
    run(listener.cycle())  # asks
    world.react(APPROVE, SCOTT)
    world.react(APPROVE, DANA)
    run(listener.cycle())
    return listener, batches


def test_it_reads_only_what_is_new():
    world = World()
    listener, batches = approved(world)
    world.say(TOM, "first")
    run(listener.cycle())
    assert len(batches) == 1 and "first" in batches[0][1]

    run(listener.cycle())
    assert len(batches) == 1, "nothing new means no turn"

    world.say(TOM, "second")
    run(listener.cycle())
    assert len(batches) == 2 and "second" in batches[1][1]
    assert "first" not in batches[1][1], "the cursor advanced past what was handed over"


def test_revoking_the_last_grant_stops_everything():
    world = World()
    listener, batches = approved(world)
    world.grants = []  # the dashboard "remove" landed
    reads_before = world.history_calls
    world.say(TOM, "after the revocation")
    run(listener.cycle())
    assert batches == []
    assert world.history_calls == reads_before, "a revoked token reads nothing more"
    # And it comes back when re-authorized.
    world.grants = [SCOTT]
    run(listener.cycle())
    assert len(batches) == 1 and "after the revocation" in batches[0][1]


def test_a_conversation_without_an_authorizing_member_is_left_alone():
    world = World(members=(DANA, TOM))  # Scott, the only authorizer, isn't in it
    listener, batches = build(world)
    # Discovery finds nothing for Scott: he can't see a conversation he's not in.
    run(listener.cycle())
    assert listener._convos == {}
    assert world.posted == []


def test_a_new_member_is_told(tmp_path):
    world = World(members=(SCOTT, DANA))
    listener, batches = approved(world, tmp_path=tmp_path)
    world.convos[CONVO]["members"].append(TOM)
    run(listener.cycle())
    notices = [t for _, _, t in world.posted if "is listening" in t]
    assert notices == ["_BizzyBrain is listening_"]


def test_state_survives_a_restart(tmp_path):
    world = World()
    listener, _ = approved(world, tmp_path=tmp_path)
    world.say(TOM, "after approval")
    run(listener.cycle())

    saved = json.loads((tmp_path / "private_dms.json").read_text())
    assert saved["conversations"][CONVO]["state"] == "approved"

    # A fresh listener over the same state file must not re-ask or re-read.
    listener2, batches2 = build(world, tmp_path=tmp_path)
    posts_before = len(world.posted)
    run(listener2.cycle())
    assert len(world.posted) == posts_before, "no second approval prompt"
    assert batches2 == [], "no re-reading what the previous run already handed over"


def test_the_budget_caps_calls_per_cycle():
    world = World()
    # Ten live conversations, a budget that only allows a few calls each cycle.
    for i in range(10):
        world.convos[f"G{i}"] = world.blank()
    listener, _ = build(world, call_budget=4, members_every=0)
    run(listener.cycle())  # watermark; nothing is tracked yet

    for cid in list(world.convos):
        world.say(DANA, "all of them wake up at once", convo=cid)

    per_cycle = []
    for _ in range(12):
        before = len(world.posted)
        run(listener.cycle())
        per_cycle.append(len(world.posted) - before)

    # An ask costs two calls (the check, then the post), so a budget of four
    # buys at most one per cycle.
    assert max(per_cycle) <= 2, per_cycle
    # The round-robin keeps moving, so the tail is reached rather than starved.
    asked = [c for c, _, _ in world.posted]
    assert len(set(asked)) == len(asked), "no conversation asked twice"
    assert len(set(asked)) == len(world.convos), f"only {len(set(asked))} of 11 reached"


@pytest.fixture(autouse=True)
def _restore_client():
    yield
    import importlib

    importlib.reload(private_dm)
