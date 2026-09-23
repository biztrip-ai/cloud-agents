"""Group-DM listening: read conversations the bot cannot be a member of.

An app can't be added to a group DM, so the agent reads one with a **user
token** granted by somebody in it (Central-Dispatch holds the grants; see
`docs/private-message-listening.md`). That token could reach everything its
owner can see, so almost all of this module is about narrowing that down:

- The token is asked for with `mpim:history` and no other history scope, so it
  **cannot** read 1:1 DMs, public channels or private channels. Not a policy —
  Slack won't serve them.
- A conversation is read only after its participants approve it in the
  conversation itself: two ✅, at least one from an authorizing user.
- The agent never sees the token. It only ever receives text this module hands
  it, from approved conversations. An agent that decides to go read something
  has nothing to read it with.

Every cycle (default 2 minutes) the listener:

1. Fetches the current grants from Central-Dispatch, so a dashboard change
   takes effect without a restart.
2. Sweeps group DMs for ones that have said something since we started,
   spending its call budget on recently-active conversations first.
3. Asks those, and only those, for approval.
4. Polls an asked conversation's reactions, and reads what's new in approved
   ones, handing each batch over as one silent turn.

**The tracking list starts empty and only grows when a conversation speaks.**
An account accumulates hundreds of group DMs, nearly all dormant for years.
The first version of this asked every one of them for approval on discovery,
which posted into 19 dead conversations before it was stopped; the second
tried to spot live ones from the `updated` field in Slack's conversation
listing, which turned out not to be the last-message time at all (a group DM
with a message from today reported `updated` from three months earlier). So
the last message is asked for per conversation, one call each, and the budget
goes to conversations that spoke recently before the long dormant tail.

There is no push to replace this. An app cannot join a group DM, so Slack
sends it no events for one, and the only user-scoped event stream (RTM) needs
a legacy scope that would hand over every conversation the person is in.

Each sweep call reads one **timestamp** — the text is never looked at, stored
or handed to the agent. Content still requires approval.

Configuration (agent.env):

    PRIVATE_DM_POLL_S=120         seconds between cycles
    PRIVATE_DM_CALL_BUDGET=20     max Slack calls per cycle (round-robin above it)
    PRIVATE_DM_MAX_MESSAGES=50    messages per batch handed to the agent
    PRIVATE_DM_MEMBERS_EVERY=5    re-check membership every Nth cycle

Off unless Central-Dispatch says the agent is enabled *and* somebody has
authorized it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from .slack_io import user_label

log = logging.getLogger("agent-wrapper.private-dm")

APPROVE = "white_check_mark"
DECLINE = "x"

# Approval needs this many ✅ from distinct people, one of whom must have
# authorized the agent (otherwise two bystanders could opt a conversation in).
APPROVALS_NEEDED = 2

# Subtypes that aren't somebody saying something.
_SKIP_SUBTYPES = {
    "message_changed",
    "message_deleted",
    "group_join",
    "group_leave",
    "channel_join",
    "channel_leave",
    "bot_message",
}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


class PrivateDMListener:
    """Discovery, approval and recording for group DMs, on one timer."""

    def __init__(
        self,
        bot: AsyncWebClient,
        fetch_grants: Callable[[], Awaitable[list[dict[str, Any]]]],
        on_batch: Callable[[str, str], Awaitable[None]],
        *,
        agent_label: str = "the agent",
        state_path: Optional[Path] = None,
        interval_s: Optional[float] = None,
        call_budget: Optional[int] = None,
        max_messages: Optional[int] = None,
        members_every: Optional[int] = None,
    ) -> None:
        self._bot = bot  # bot token: only for resolving user ids to names
        self._fetch_grants = fetch_grants
        self._on_batch = on_batch
        self._label = agent_label
        self._state_path = state_path
        self._interval = interval_s if interval_s is not None else _float_env("PRIVATE_DM_POLL_S", 120)
        self._budget = call_budget if call_budget is not None else _int_env("PRIVATE_DM_CALL_BUDGET", 20)
        self._max = max_messages if max_messages is not None else _int_env("PRIVATE_DM_MAX_MESSAGES", 50)
        self._members_every = (
            members_every if members_every is not None else _int_env("PRIVATE_DM_MEMBERS_EVERY", 5)
        )
        # Only conversations we have engaged with: asked, approved or declined.
        # A group DM nobody has spoken in since we started is not in here.
        # id -> {state, members, prompt_ts, prompt_user, last_ts, approvals}
        self._convos: dict[str, dict[str, Any]] = {}
        # When we started listening. Anything said before it is none of our
        # business, and this is the only thing besides tracked conversations
        # that has to survive a restart.
        self._started_at: Optional[float] = None
        # Last message time and last poll time per conversation, for spending
        # the call budget where something is likely to have happened. In
        # memory only: losing it costs one sweep to relearn.
        self._seen_ts: dict[str, float] = {}
        self._checked: dict[str, float] = {}
        self._hot_s = _float_env("PRIVATE_DM_HOT_S", 24 * 3600)
        self._clients: dict[str, AsyncWebClient] = {}  # slack user id -> their client
        self._cursor = 0  # round-robin position when the budget runs out
        self._cycle = 0
        self._task: Optional[asyncio.Task] = None
        self._load()

    # --- persistence --------------------------------------------------------
    # Approvals and read cursors outlive restarts: losing them would re-ask
    # every conversation, and re-read from the beginning.

    def _load(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text())
            convos = data.get("conversations")
            if isinstance(convos, dict):
                # Older builds tracked every group DM that existed. Keep only
                # the ones actually engaged with — asked, approved or declined
                # — and let the watermark find the rest if they ever speak.
                self._convos = {
                    k: v
                    for k, v in convos.items()
                    if v.get("prompt_ts") or v.get("state") in ("approved", "declined")
                }
                dropped = len(convos) - len(self._convos)
                if dropped:
                    log.info("private DM: dropped %d untouched conversation(s)", dropped)
            self._started_at = data.get("started_at") or data.get("watermark")
            log.info("private DM state: %d tracked", len(self._convos))
        except Exception:  # noqa: BLE001 — corrupt state just starts over
            log.warning("could not read %s; starting fresh", self._state_path, exc_info=True)

    def _save(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"started_at": self._started_at, "conversations": self._convos},
                    indent=1,
                )
            )
            tmp.replace(self._state_path)
        except Exception:  # noqa: BLE001
            log.warning("could not write %s", self._state_path, exc_info=True)

    # --- helpers ------------------------------------------------------------

    def _authorizers_in(self, convo: dict[str, Any]) -> list[str]:
        """Authorizing users who are in this conversation — whose tokens may
        read it. Empty means nobody with a grant is in the room any more, and
        reading stops."""
        members = set(convo.get("members") or [])
        # Before the first membership fetch we only know who discovered it.
        if not members:
            found_by = convo.get("found_by")
            return [found_by] if found_by in self._clients else []
        return [u for u in self._clients if u in members]

    def _client_for(self, convo: dict[str, Any]) -> Optional[tuple[str, AsyncWebClient]]:
        for user in self._authorizers_in(convo):
            client = self._clients.get(user)
            if client:
                return user, client
        return None

    async def _names(self, ids: Iterable[str]) -> dict[str, str]:
        out = {}
        for uid in ids:
            out[uid] = await user_label(self._bot, uid) or uid
        return out

    # --- the cycle ----------------------------------------------------------

    async def _refresh_grants(self) -> bool:
        """Rebuild the per-user clients from Central-Dispatch. Returns False
        when there is nothing to do this cycle."""
        try:
            grants = await self._fetch_grants()
        except Exception:  # noqa: BLE001 — Central-Dispatch hiccup: keep what we have
            log.warning("could not fetch private-message grants", exc_info=True)
            return bool(self._clients)
        fresh: dict[str, AsyncWebClient] = {}
        for g in grants or []:
            uid, token = g.get("slackUserId"), g.get("token")
            if not uid or not token:
                continue
            existing = self._clients.get(uid)
            # Reuse the client unless the token changed (re-authorization).
            fresh[uid] = existing if existing and existing.token == token else AsyncWebClient(token=token)
        gone = set(self._clients) - set(fresh)
        if gone:
            log.info("private DM: %d authorization(s) removed", len(gone))
        added = set(fresh) - set(self._clients)
        if added:
            log.info("private DM: %d new authorization(s)", len(added))
        self._clients = fresh
        return bool(fresh)

    async def _sweep(self, budget: int) -> int:
        """Find conversations that have spoken since we started listening.

        There is no push for this. An app cannot be a member of a group DM, so
        Slack sends it no events for one; the only user-scoped event stream is
        legacy RTM, which needs a `client` scope that would hand over every
        conversation the person is in. So we poll, and the only question is how
        few calls it takes.

        Slack's listing does not help: a conversation's `updated` field is NOT
        its last message (measured — a group DM with a message today still
        reported `updated` from June). So the last message has to be asked for
        per conversation, one call each, and the budget is spent on the ones
        most likely to have moved: conversations that spoke recently, then
        everything else in rotation.

        Each call reads one **timestamp**. The text is never looked at, stored
        or handed to the agent; a conversation only becomes readable after its
        participants approve it.
        """
        spent = 0
        owner: dict[str, str] = {}
        for uid, client in list(self._clients.items()):
            if spent >= budget:
                break
            spent += 1
            try:
                resp = await client.users_conversations(
                    types="mpim", exclude_archived=True, limit=200
                )
            except SlackApiError as e:
                self._on_api_error(uid, e, "users.conversations")
                continue
            for ch in resp.get("channels") or []:
                cid = ch.get("id")
                if cid and cid not in self._convos:
                    owner.setdefault(cid, uid)

        if self._started_at is None:
            # Nothing said before we existed is our business. From here on,
            # anything newer than this is a conversation speaking to us.
            self._started_at = time.time()
            log.info(
                "private DM: listening from now; %d conversation(s) exist and none "
                "are tracked until one of them speaks",
                len(owner),
            )

        now = time.time()
        hot = [c for c in owner if now - self._seen_ts.get(c, 0.0) < self._hot_s]
        cold = [c for c in owner if c not in set(hot)]
        order = sorted(hot, key=lambda c: self._checked.get(c, 0.0)) + sorted(
            cold, key=lambda c: self._checked.get(c, 0.0)
        )
        checked = 0
        for cid in order:
            if spent >= budget:
                break
            spent += 1
            checked += 1
            # An ask costs one more call than the check that found it, which
            # can take a tight budget one over. Asking is the rare case and the
            # whole point; the next cycle simply does that much less.
            spent += await self._check_activity(cid, owner[cid])
        # One line a cycle, so "is it getting anywhere?" has an answer without
        # a debug build: a full pass over a big account takes many cycles.
        log.info(
            "private DM sweep: %d checked this cycle, %d/%d conversations seen, %d tracked",
            checked,
            len(self._seen_ts),
            len(owner) + len(self._convos),
            len(self._convos),
        )
        return spent

    async def _check_activity(self, cid: str, uid: str) -> int:
        """One conversation: has anything been said since we started? Returns
        the extra calls spent (the ask, if it happened)."""
        convo = {"state": "pending", "found_by": uid, "members": []}
        self._checked[cid] = time.time()
        ts = await self._latest_ts(cid, convo)
        if ts is None:
            return 0
        latest = float(ts)
        previous = self._seen_ts.get(cid, 0.0)
        self._seen_ts[cid] = latest
        if latest <= max(self._started_at or 0.0, previous):
            return 0  # quiet since we started, or nothing new since last look
        self._convos[cid] = convo
        log.info("private DM: %s has spoken; asking", cid)
        await self._ask(cid, convo)
        return 1

    def _on_api_error(self, uid: str, e: SlackApiError, method: str) -> None:
        data = (e.response.data or {}) if hasattr(e, "response") else {}
        code = data.get("error")
        if code in ("invalid_auth", "token_revoked", "account_inactive"):
            # The grant is gone on Slack's side; drop it until Central-Dispatch
            # agrees (it will, on the next fetch, once the dashboard catches up).
            log.warning("private DM: token for %s is no longer valid (%s)", uid, code)
            self._clients.pop(uid, None)
        elif code == "missing_scope":
            # Name the scope: this is what a stale grant looks like after the
            # Slack app's manifest gained a scope the token predates, and the
            # fix is for that person to re-authorize on the dashboard.
            log.warning(
                "private DM: %s needs scope %s but the grant from %s has %s — "
                "that person should re-authorize on the dashboard",
                method,
                data.get("needed"),
                uid,
                data.get("provided"),
            )
        else:
            log.warning("private DM: %s failed for %s: %s", method, uid, code or e)

    async def _refresh_members(self, cid: str, convo: dict[str, Any]) -> None:
        """Who is in the conversation. A new face gets a visible notice — a
        person who joins shouldn't be recorded without knowing."""
        picked = self._client_for(convo)
        if not picked:
            return
        uid, client = picked
        try:
            resp = await client.conversations_members(channel=cid, limit=100)
        except SlackApiError as e:
            self._on_api_error(uid, e, "conversations.members")
            return
        members = [m for m in (resp.get("members") or []) if m]
        if not members:
            return
        known = set(convo.get("members") or [])
        convo["members"] = members
        new = [m for m in members if m not in known]
        if known and new and convo.get("state") == "approved":
            await self._post(cid, convo, f"_{self._label} is listening_")

    async def _post(self, cid: str, convo: dict[str, Any], text: str) -> Optional[str]:
        """Post as an authorizing user — the only way to write into a group DM
        the app isn't in."""
        picked = self._client_for(convo)
        if not picked:
            return None
        uid, client = picked
        try:
            resp = await client.chat_postMessage(channel=cid, text=text)
            return resp.get("ts")
        except SlackApiError as e:
            self._on_api_error(uid, e, "chat.postMessage")
            return None

    async def _latest_ts(self, cid: str, convo: dict[str, Any]) -> Optional[str]:
        """The timestamp of the most recent message, and nothing else.

        Deliberately does not return, log or keep the message: before approval
        the only thing we are allowed to know about a conversation is that
        somebody said *something*.
        """
        picked = self._client_for(convo)
        if not picked:
            return None
        uid, client = picked
        try:
            resp = await client.conversations_history(channel=cid, limit=1)
        except SlackApiError as e:
            self._on_api_error(uid, e, "conversations.history")
            return None
        msgs = resp.get("messages") or []
        return msgs[0].get("ts") if msgs else None

    async def _ask(self, cid: str, convo: dict[str, Any]) -> None:
        picked = self._client_for(convo)
        if not picked:
            return
        text = (
            f"Allow *{self._label}* to listen to this conversation and remember what's "
            f"useful? React :{APPROVE}: to approve — two approvals needed, including one "
            f"authorized member. React :{DECLINE}: to decline. Nothing is read until then."
        )
        ts = await self._post(cid, convo, text)
        if ts:
            convo["prompt_ts"] = ts
            convo["prompt_user"] = picked[0]
            convo["asked_at"] = time.time()
            log.info("private DM: asked %s for approval", cid)

    async def _check_approval(self, cid: str, convo: dict[str, Any]) -> None:
        picked = self._client_for(convo)
        if not picked:
            return
        uid, client = picked
        try:
            # Fetch the prompt message itself — oldest == latest == its ts,
            # inclusive — because a message carries its own reactions. The
            # obvious call, reactions.get, would need a reactions:read scope on
            # everyone's token, and a scope we can do without is a scope we
            # don't ask for.
            resp = await client.conversations_history(
                channel=cid,
                oldest=convo["prompt_ts"],
                latest=convo["prompt_ts"],
                inclusive=True,
                limit=1,
            )
        except SlackApiError as e:
            self._on_api_error(uid, e, "conversations.history (approval)")
            return
        msgs = resp.get("messages") or []
        # Slack truncates a long `users` list, so treat it as a lower bound: an
        # approval needs an authorizing user we can actually see in it.
        reactions = (msgs[0].get("reactions") if msgs else None) or []
        by_name = {r.get("name"): set(r.get("users") or []) for r in reactions}
        if by_name.get(DECLINE):
            convo["state"] = "declined"
            log.info("private DM: %s declined", cid)
            return
        approvers = by_name.get(APPROVE) or set()
        authorized = approvers & set(self._clients)
        if len(approvers) >= APPROVALS_NEEDED and authorized:
            convo["state"] = "approved"
            convo["approvals"] = sorted(approvers)
            # Record from the approval forward, never backwards: messages sent
            # before anyone was asked were sent in private.
            convo["last_ts"] = convo.get("prompt_ts")
            log.info("private DM: %s approved by %d participant(s)", cid, len(approvers))

    async def _record(self, cid: str, convo: dict[str, Any]) -> None:
        picked = self._client_for(convo)
        if not picked:
            return
        uid, client = picked
        try:
            resp = await client.conversations_history(
                channel=cid, oldest=convo.get("last_ts") or "0", limit=self._max, inclusive=False
            )
        except SlackApiError as e:
            self._on_api_error(uid, e, "conversations.history")
            return
        msgs = [
            m
            for m in reversed(resp.get("messages") or [])
            if m.get("subtype") not in _SKIP_SUBTYPES
            and (m.get("text") or "").strip()
            and m.get("ts") != convo.get("prompt_ts")
            and not m.get("bot_id")
        ]
        newest = max((m.get("ts") or "0" for m in resp.get("messages") or []), default=None)
        if not msgs:
            if newest:
                convo["last_ts"] = newest
            return
        names = await self._names({m.get("user") for m in msgs if m.get("user")})
        body = "\n".join(f"{names.get(m.get('user'), m.get('user') or '?')}: {m['text']}" for m in msgs)
        who = ", ".join(sorted(names.values()))
        log.info("private DM batch %s: %d message(s) (read as %s)", cid, len(msgs), uid)
        await self._on_batch(
            cid,
            f"[Private group DM — nobody addressed you. This conversation's participants "
            f"approved recording. Participants: {who}. Read these messages, keep anything "
            f"worth remembering that the rules allow, and reply with nothing.]\n\n{body}",
        )
        # Only after the turn completed: a crash re-reads rather than skipping.
        convo["last_ts"] = msgs[-1]["ts"]

    async def cycle(self) -> None:
        """One pass. Separate from the loop so tests can drive it directly."""
        self._cycle += 1
        if not await self._refresh_grants():
            return
        budget = self._budget
        # Split the budget between noticing new conversations and serving the
        # ones we track. Tracked conversations get what they need where the
        # budget allows it, but never more than half when that would leave the
        # sweep unable to afford a single check — a few busy rooms must not
        # blind the agent to every other one.
        sweep_budget = max(budget // 2, budget - len(self._convos))
        budget -= await self._sweep(min(budget, sweep_budget))

        # The ones we already track: poll an unanswered ask, or read what
        # is new in an approved conversation. Round-robin from where the last
        # cycle stopped, so a tight budget doesn't starve the tail.
        ids = [c for c in self._convos if self._convos[c].get("state") != "declined"]
        if not ids:
            self._save()
            return
        start = self._cursor % len(ids)
        order = ids[start:] + ids[:start]
        done = 0
        for cid in order:
            if budget <= 0:
                break
            convo = self._convos[cid]
            if not self._authorizers_in(convo):
                continue  # nobody with a grant is in the room
            if self._members_every and self._cycle % self._members_every == 0:
                budget -= 1
                await self._refresh_members(cid, convo)
            if budget <= 0:
                break
            state = convo.get("state")
            if state == "pending":
                budget -= 1
                await self._check_approval(cid, convo)
            elif state == "approved":
                budget -= 1
                await self._record(cid, convo)
            done += 1
        self._cursor = (start + done) % len(ids)
        self._save()

    async def _loop(self) -> None:
        while True:
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — one bad cycle mustn't kill the loop
                log.exception("private DM cycle failed")
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            log.info(
                "private DM listening armed (every %.0fs, budget %d calls/cycle)",
                self._interval,
                self._budget,
            )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
