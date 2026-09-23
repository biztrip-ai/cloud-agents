"""Run with: uv run --with pytest -m pytest tests

The group-DM listener's job is to *not* read things, so most of these are
about the cases where it must stay out: no grant, no approval, one approval,
an approval by two bystanders, a conversation nobody authorized is in.
"""

import asyncio
import json

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
        chans = [
            {"id": cid}
            for cid, c in self.world.convos.items()
            if self.user in c["members"]
        ]
        return {"channels": chans}

    async def conversations_members(self, channel, **kw):
        return {"members": list(self.world.convos[channel]["members"])}

    async def chat_postMessage(self, channel, text):
        ts = f"{self.world.next_ts()}"
        self.world.convos[channel]["messages"].append(
            {"ts": ts, "user": self.user, "text": text}
        )
        self.world.posted.append((channel, self.user, text))
        return {"ts": ts}

    async def reactions_get(self, channel, timestamp, full=True):
        r = self.world.convos[channel]["reactions"]
        return {
            "message": {
                "reactions": [{"name": n, "users": sorted(u)} for n, u in r.items() if u]
            }
        }

    async def conversations_history(self, channel, oldest="0", limit=50, inclusive=False):
        msgs = [
            m for m in self.world.convos[channel]["messages"] if float(m["ts"]) > float(oldest or 0)
        ]
        self.world.history_calls += 1
        return {"messages": list(reversed(msgs))[:limit]}


class World:
    def __init__(self, members=(SCOTT, DANA, TOM)):
        self._ts = 100.0
        self.convos = {
            CONVO: {"members": list(members), "messages": [], "reactions": {}},
        }
        self.posted = []
        self.history_calls = 0
        self.grants = []

    def next_ts(self):
        self._ts += 1
        return round(self._ts, 6)

    def say(self, user, text, convo=CONVO):
        self.convos[convo]["messages"].append(
            {"ts": f"{self.next_ts()}", "user": user, "text": text}
        )

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


def test_a_dormant_conversation_is_never_asked():
    world = World()
    listener, batches = build(world)
    world.say(DANA, "something from three years ago")
    # Discovery records where the conversation stands and stays quiet: an
    # account is in hundreds of these and almost all of them are dead.
    run(listener.cycle())
    run(listener.cycle())
    assert world.posted == [], "a silent conversation is never posted into"
    assert batches == []


def test_it_asks_before_it_reads():
    world = World()
    listener, batches = build(world)
    world.say(SCOTT, "something said before anyone was asked")
    run(listener.cycle())  # baseline only
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
    run(listener.cycle())  # baseline
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
    run(listener.cycle())  # baseline
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
        world.convos[f"G{i}"] = {"members": [SCOTT, DANA], "messages": [], "reactions": {}}
    listener, _ = build(world, call_budget=4, members_every=0)

    per_cycle = []
    for _ in range(12):
        for cid in list(world.convos):
            world.say(DANA, "still talking", convo=cid)
        before = len(world.posted)
        run(listener.cycle())
        per_cycle.append(len(world.posted) - before)

    # One discovery call plus at most three conversations per cycle, never more.
    assert max(per_cycle) <= 3, per_cycle
    # The round-robin keeps moving, so the tail is reached rather than starved.
    asked = [c for c, _, _ in world.posted]
    assert len(set(asked)) == len(asked), "no conversation asked twice"
    assert len(set(asked)) == len(world.convos), f"only {len(set(asked))} of 11 reached"


@pytest.fixture(autouse=True)
def _restore_client():
    yield
    import importlib

    importlib.reload(private_dm)
