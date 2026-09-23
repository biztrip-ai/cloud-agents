"""Passive listening: follow named channels the agent was invited to.

Normally a channel message only reaches the agent when it @-mentions it. An
agent that is supposed to *follow* a channel — to notice decisions and remember
them — needs the rest too. Someone invites the agent to the channel (public or
private), which is the consent: it shows in the member list, and reading uses
the bot token, so no user token is involved.

Delivery costs no API calls: Central-Dispatch already fans out `message.*`
events for every channel the app is in. We buffer those and hand the agent one
**silent turn** per batch (see agent_wrapper.handle_user_message's `silent`
payload flag) rather than a turn per message, which a busy channel would make
expensive and noisy.

Configuration (agent.env):

    PASSIVE_LISTEN_CHANNELS=C0123,#eng   channel ids or names; the allow-list
    PASSIVE_LISTEN_ALL=1                 every channel the agent is in instead
    PASSIVE_FLUSH_S=120                  how long a batch may wait (default 120)
    PASSIVE_MAX_MESSAGES=50              flush early at this many (default 50)

With neither channel setting, passive listening is off and nothing changes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable, Optional

from slack_sdk.web.async_client import AsyncWebClient

from .slack_io import user_label

log = logging.getLogger("agent-wrapper.passive")

# Subtypes that are not somebody saying something.
_SKIP_SUBTYPES = {
    "message_changed",
    "message_deleted",
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "thread_broadcast_reply",
}


def _split(raw: str) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


class PassiveListener:
    """Buffers channel messages and flushes each channel's batch as one turn."""

    def __init__(
        self,
        slack: AsyncWebClient,
        on_batch: Callable[[str, str], Awaitable[None]],
        *,
        channels: Optional[list[str]] = None,
        listen_all: Optional[bool] = None,
        flush_s: Optional[float] = None,
        max_messages: Optional[int] = None,
    ) -> None:
        self._slack = slack
        self._on_batch = on_batch  # (channel_id, rendered batch text)
        self._configured = channels if channels is not None else _split(
            os.getenv("PASSIVE_LISTEN_CHANNELS", "")
        )
        self._listen_all = (
            listen_all
            if listen_all is not None
            else os.getenv("PASSIVE_LISTEN_ALL", "").lower() in ("1", "true", "yes", "on")
        )
        self._flush_s = flush_s if flush_s is not None else float(os.getenv("PASSIVE_FLUSH_S", "120"))
        self._max = max_messages if max_messages is not None else int(os.getenv("PASSIVE_MAX_MESSAGES", "50"))
        # channel id -> [{ts, user, text}], plus when the batch opened.
        self._buffers: dict[str, list[dict[str, str]]] = {}
        self._opened: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        # Names from the allow-list resolved to ids, once.
        self._resolved: Optional[set[str]] = None

    @property
    def enabled(self) -> bool:
        return bool(self._listen_all or self._configured)

    async def _allowed(self, channel: str) -> bool:
        if self._listen_all:
            return True  # every channel the app is a member of
        if self._resolved is None:
            ids = {c for c in self._configured if re.fullmatch(r"[CGD][A-Z0-9]{6,}", c)}
            names = {c.lstrip("#").lower() for c in self._configured} - {c.lower() for c in ids}
            if names:
                try:
                    cursor = ""
                    while True:
                        resp = await self._slack.conversations_list(
                            types="public_channel,private_channel", limit=200, cursor=cursor or None
                        )
                        for c in resp.get("channels") or []:
                            if (c.get("name") or "").lower() in names:
                                ids.add(c["id"])
                        cursor = (resp.get("response_metadata") or {}).get("next_cursor") or ""
                        if not cursor:
                            break
                except Exception:  # noqa: BLE001 — names just stay unresolved
                    log.warning("could not resolve passive channel names", exc_info=True)
            self._resolved = ids
            log.info("passive listening on %d channel(s)", len(ids))
        return channel in self._resolved

    async def offer(
        self, event: dict[str, Any], bot_user_id: Optional[str], agent_ids: frozenset[str]
    ) -> bool:
        """Buffer a message if it belongs to a passively-listened channel.

        Returns True when it was buffered. A message that @-mentions the agent
        is left alone: it wakes the agent through the normal path, and taking it
        here as well would have the agent see it twice.
        """
        if not self.enabled or not isinstance(event, dict):
            return False
        if event.get("type") != "message" or event.get("subtype") in _SKIP_SUBTYPES:
            return False
        if event.get("channel_type") not in ("channel", "group"):
            return False
        user = event.get("user")
        if not user or user == bot_user_id:
            return False  # never our own words
        if event.get("bot_id") and not ({user, event.get("app_id"), event.get("bot_id")} & agent_ids):
            return False  # other apps, unless they are agents we work with
        text = event.get("text") or ""
        if bot_user_id and f"<@{bot_user_id}>" in text:
            return False  # addressed to us: the normal path handles it
        if not text.strip():
            return False
        channel = event.get("channel")
        ts = event.get("ts")
        if not channel or not ts or not await self._allowed(channel):
            return False
        async with self._lock:
            buf = self._buffers.setdefault(channel, [])
            if any(m["ts"] == ts for m in buf):
                return True  # replayed event
            buf.append({"ts": ts, "user": user, "text": text})
            self._opened.setdefault(channel, time.monotonic())
            full = len(buf) >= self._max
        if full:
            await self.flush(channel)
        return True

    async def flush(self, channel: str) -> None:
        async with self._lock:
            msgs = self._buffers.pop(channel, [])
            self._opened.pop(channel, None)
        if not msgs:
            return
        name = channel
        try:
            info = await self._slack.conversations_info(channel=channel)
            name = "#" + ((info.get("channel") or {}).get("name") or channel)
        except Exception:  # noqa: BLE001 — the id is a fine fallback
            pass
        lines = []
        for m in msgs:
            who = await user_label(self._slack, m["user"]) or m["user"]
            lines.append(f"{who}: {m['text']}")
        body = "\n".join(lines)
        log.info("passive batch %s: %d message(s)", channel, len(msgs))
        await self._on_batch(
            channel,
            f"[Passive listening — nobody addressed you. Read these messages from "
            f"{name}, keep anything worth remembering, and reply with nothing.]\n\n{body}",
        )

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(min(self._flush_s, 15))
            now = time.monotonic()
            async with self._lock:
                due = [c for c, at in self._opened.items() if now - at >= self._flush_s]
            for channel in due:
                try:
                    await self.flush(channel)
                except Exception:  # noqa: BLE001 — one bad batch mustn't kill the loop
                    log.exception("passive flush failed for %s", channel)

    def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop())
            log.info(
                "passive listening enabled (%s, flush %.0fs, max %d)",
                "all channels" if self._listen_all else f"{len(self._configured)} configured",
                self._flush_s,
                self._max,
            )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
