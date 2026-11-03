"""Human approval for production Sabre writes.

An agent reads production Sabre freely. A *write* to production stops, names
itself in the Slack thread it came from, and waits for a human to react. Held
up or declined, the call is rewritten to run against the cert environment
instead, so the agent gets a result rather than a wall.

Three things shape the implementation:

- It is a `PreToolUse` hook, not `can_use_tool`. These agents run under
  `bypassPermissions`, where no tool call reaches a permission prompt and
  `can_use_tool` is therefore never called; a `PreToolUse` hook still fires.
- It runs in-process. The Slack client and bot token live in the wrapper, and
  a subprocess hook would have neither.
- Approval is a reaction, never a typed reply. The bot posts as itself, so it
  could type "yes" into its own thread; it cannot react as somebody else, so a
  reaction carries a real user id. Reactions are read back off
  `conversations.replies`, because `reactions.get` would need a
  `reactions:read` scope these apps don't have.

A reaction alone is not enough, either: BzPM, Builder and Merger are separate
Slack apps, so "the reacting user is not a bot" would let one agent approve
another's write. The reacting id must be on the allowlist.

Configuration (agent.env or ~/.bizzybot/settings.env):

    SABRE_WRITE_APPROVERS=U0123,U0456   who may approve (the sponsor always may)
    SABRE_ENV=certmoderna               the env the MCP server defaults to
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable, Optional

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

log = logging.getLogger("agent-wrapper.sabre-approval")

# The Sabre MCP server's write tools. Everything else it exposes is a read.
WRITE_TOOLS = frozenset({
    "create_booking",
    "cancel_booking",
    "void_flight_tickets",
    "flight_exchange_book",
    "flight_exchange_queue_for_ticketing",
    "update_reservation_add_product",
    "update_reservation_delete_product",
    "update_reservation_swap_product",
})

# Flips the MCP server's process-level SABRE_ENV — the one thing that can move
# the default out from under this gate without the wrapper seeing it.
ENV_TOOL = "set_sabre_env"

# btagent/mcp_server/bookings.py:_PROD_ENV_NAMES. moderna_testprod is a test EPR
# against production endpoints, so its writes hit real inventory too.
PROD_ENVS = frozenset({"moderna", "moderna_testprod", "biztrip_prod", "cain_prod"})

# btagent/mcp_server/_env.py:resolve_active_sabre_env falls back to this when
# SABRE_ENV is unset. It is a production env, so an unconfigured box is gated
# rather than waved through.
DEFAULT_ENV = "moderna"
CERT_ENV = "certmoderna"

# The claude CLI config entry whose `env` block, when it pins SABRE_ENV, is what
# the MCP server actually starts with.
SERVER_NAME = "sabre-search"

# Hook matchers are regexes — a bare prefix matches nothing. Matched on the tool
# name under any MCP server name, so renaming the server cannot silently switch
# the gate off.
TOOL_MATCHER = r"mcp__[\w-]+__(" + "|".join(sorted(WRITE_TOOLS | {ENV_TOOL})) + r")$"

APPROVE = "white_check_mark"
THUMBS_UP = "+1"
APPROVE_NAMES = frozenset({APPROVE, THUMBS_UP})
DECLINE = "x"

TIMEOUT_S = 600.0
POLL_S = 5.0

# Arguments worth naming in the prompt: what is being written, and to what.
_SUMMARY_KEYS = (
    "locator",
    "pnr_locator",
    "confirmation_id",
    "original_ticket",
    "ticket_numbers",
    "cancel_segment_numbers",
    "segment_element_id",
    "old_segment_id",
    "name_number",
    "amount_specified",
    "queue_number",
    "pcc",
    "target_pcc",
    "skip_void",
)
_PRODUCT_KEYS = ("flights", "hotel", "car", "vehicle", "air", "fop")


def tool_basename(tool_name: str) -> str:
    """`mcp__sabre-search__create_booking` -> `create_booking`."""
    return tool_name.rsplit("__", 1)[-1]


def parse_approvers(raw: Optional[str], sponsor_id: Optional[str]) -> frozenset[str]:
    """The allowlist: the sponsor, plus whoever SABRE_WRITE_APPROVERS names."""
    ids = {p.strip() for p in (raw or "").split(",")}
    if sponsor_id:
        ids.add(sponsor_id.strip())
    return frozenset(i for i in ids if i)


def _clip(value: Any, n: int = 80) -> str:
    s = str(value)
    return s if len(s) <= n else s[:n] + "…"


def summarize(tool_input: dict[str, Any]) -> str:
    """One line naming the identifying arguments this call actually carries."""
    parts = [
        f"{k}=`{_clip(tool_input[k])}`"
        for k in _SUMMARY_KEYS
        if tool_input.get(k) not in (None, "", [], {})
    ]
    products = [k for k in _PRODUCT_KEYS if tool_input.get(k)]
    if products:
        parts.append("products: " + ", ".join(products))
    travelers = tool_input.get("travelers")
    if isinstance(travelers, list) and travelers:
        parts.append(f"{len(travelers)} traveler(s)")
    return " · ".join(parts) or "_no identifying arguments_"


class SabreWriteApprover:
    """Decides, per tool call, whether a Sabre write may hit production."""

    def __init__(
        self,
        slack: Optional[AsyncWebClient],
        *,
        default_env: str,
        approvers: Iterable[str],
        cert_env: str = CERT_ENV,
        timeout_s: float = TIMEOUT_S,
        poll_s: float = POLL_S,
    ):
        self._slack = slack
        self._default_env = default_env or DEFAULT_ENV
        self._approvers = frozenset(approvers)
        self._cert_env = cert_env
        self._timeout_s = timeout_s
        self._poll_s = poll_s
        # thread key -> the env a set_sabre_env call in that thread moved the
        # MCP server to. One claude subprocess, and so one MCP server, per key.
        self._switched: dict[str, str] = {}
        if not self._approvers:
            log.warning(
                "no Sabre write approvers configured (SABRE_WRITE_APPROVERS, sponsor) "
                "— every production write will fall back to %s",
                self._cert_env,
            )

    def note_env_switch(self, key: str, name: str) -> None:
        """Follow `set_sabre_env`, which moves the server's default.

        The hook runs before the call and never sees its result, and a rejected
        name leaves the server where it was. So only the cert env this module
        falls back to is trusted to lower the gate; any other name is held as
        production, which at worst asks when it needn't.
        """
        self._switched[key] = (
            name if name in PROD_ENVS or name == self._cert_env else DEFAULT_ENV
        )

    def resolve_env(self, key: str, tool_input: dict[str, Any]) -> str:
        """Mirrors btagent/mcp_server/_env.py:resolve_active_sabre_env — the
        per-call kwarg wins, otherwise the process-level default."""
        override = str(tool_input.get("sabre_env") or "").strip()
        return override or self._switched.get(key) or self._default_env

    async def decide(
        self, key: str, tool_name: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        """The PreToolUse `hookSpecificOutput` body for one call; `{}` = no opinion."""
        name = tool_basename(tool_name)
        if name == ENV_TOOL:
            self.note_env_switch(key, str(tool_input.get("name") or ""))
            return {}
        if name not in WRITE_TOOLS:
            return {}
        env = self.resolve_env(key, tool_input)
        if env not in PROD_ENVS:
            return {}
        approver = await self._ask(key, name, env, tool_input)
        if approver:
            return {
                "permissionDecision": "allow",
                "permissionDecisionReason": (
                    f"production Sabre write approved in Slack by <@{approver}>"
                ),
            }
        return self._to_cert(name, env, tool_input)

    def _to_cert(
        self, tool: str, env: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Every write tool takes a one-shot `sabre_env=`, so an unapproved write
        is rewritten rather than refused."""
        note = (
            f"`{tool}` was not approved to run against production Sabre ({env}), "
            f"so it ran against `{self._cert_env}` instead. That is a different "
            f"Sabre environment: PNRs, tickets and locators from {env} do not "
            f"exist there. Hotel `create_booking` in particular fails on "
            f"`{self._cert_env}` with \"Index is out of "
            "CreateBookingRequest.travelers[] range\" — its agency profile "
            "hydrates no travelers, which is a known gap, not something to work "
            "around. Do not retry against production; ask the human in this thread."
        )
        return {
            "permissionDecision": "allow",
            "updatedInput": {**tool_input, "sabre_env": self._cert_env},
            "permissionDecisionReason": note,
            "additionalContext": note,
        }

    def _prompt(self, tool: str, env: str, tool_input: dict[str, Any]) -> str:
        who = " ".join(f"<@{u}>" for u in sorted(self._approvers))
        return (
            f":lock: *Production Sabre write* — `{tool}` against `{env}`\n"
            f"{summarize(tool_input)}\n"
            f":{APPROVE}: run it against production · :{DECLINE}: run it against "
            f"`{self._cert_env}` instead. No answer in "
            f"{int(self._timeout_s // 60)} minutes is a :{DECLINE}:. {who}"
        ).strip()

    async def _ask(
        self, key: str, tool: str, env: str, tool_input: dict[str, Any]
    ) -> Optional[str]:
        """Post the prompt into the thread and poll its reactions.

        Returns the approving user id. Declined, timed out and "could not ask"
        all return None and so all end up at cert — the gate never fails open.
        """
        channel, _, thread_ts = key.partition(":")
        if not (self._slack and channel and thread_ts):
            log.warning("no Slack thread for %s; %s falls back to %s", key, tool, self._cert_env)
            return None
        try:
            resp = await self._slack.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=self._prompt(tool, env, tool_input),
            )
            prompt_ts = resp.get("ts")
        except SlackApiError as e:
            log.warning("could not ask %s for approval of %s: %s", key, tool, e)
            return None
        if not prompt_ts:
            return None

        log.info("%s: waiting for approval of %s against %s", key, tool, env)
        deadline = time.monotonic() + self._timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(self._poll_s)
            verdict, who = await self._verdict(channel, thread_ts, prompt_ts)
            if verdict == "approved":
                log.info("%s: %s approved by %s", key, tool, who)
                return who
            if verdict == "declined":
                log.info("%s: %s declined", key, tool)
                return None
        log.info("%s: %s approval timed out after %.0fs", key, tool, self._timeout_s)
        return None

    async def _verdict(
        self, channel: str, thread_ts: str, prompt_ts: str
    ) -> tuple[Optional[str], Optional[str]]:
        try:
            # Re-read the prompt message, which carries its own reactions. The
            # obvious call, reactions.get, would need a reactions:read scope the
            # apps don't have.
            resp = await self._slack.conversations_replies(
                channel=channel,
                ts=thread_ts,
                oldest=prompt_ts,
                latest=prompt_ts,
                inclusive=True,
                limit=1,
            )
        except SlackApiError as e:
            log.warning("could not read approval reactions: %s", e)
            return None, None
        msgs = [m for m in (resp.get("messages") or []) if m.get("ts") == prompt_ts]
        by_name: dict[str, set[str]] = {}
        for r in (msgs[0].get("reactions") if msgs else None) or []:
            # A skin-toned thumbs up arrives as "+1::skin-tone-3".
            base = (r.get("name") or "").split("::", 1)[0]
            by_name.setdefault(base, set()).update(r.get("users") or [])
        # A decline only ever makes the write safer, so it needs no allowlist.
        if by_name.get(DECLINE):
            return "declined", None
        for emoji in sorted(APPROVE_NAMES):
            allowed = sorted(by_name.get(emoji, set()) & self._approvers)
            if allowed:
                return "approved", allowed[0]
        return None, None
