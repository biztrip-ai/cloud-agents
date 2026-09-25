"""Silent scheduled prompts: Central-Dispatch's scheduled tasks whose delivery
is "prompt the agent" rather than a post in a channel.

Central-Dispatch queues a `scheduled_prompt` event ({task_id, run_id, text})
when a task is due. Each run is a fresh conversation, closed afterwards, so a
frequent task neither grows one context forever nor writes anything to Slack.
Anything the agent should remember between runs belongs in its own memory.
Nothing is posted: the reply, cost and duration go back to Central-Dispatch
(POST /api/task-result), which shows them on the task's dashboard card. If the
task calls for telling someone, the agent posts with its own Slack tools.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .session_manager import SessionManager

log = logging.getLogger("agent-wrapper.scheduled")

PROMPT_PREFIX = (
    "[Scheduled task — not a message from a person, and nothing you write here "
    "is posted anywhere. Your reply is shown on the Bizzybot dashboard, so keep "
    "it to a short summary of what you did or found. If the task says to tell "
    "someone, post it yourself with your Slack tools.]\n\n"
)

# What Central-Dispatch keeps of a reply; longer replies are cut here.
MAX_RESULT_CHARS = 4000

Report = Callable[[dict[str, Any]], Awaitable[None]]


def session_key(task_id: str, run_id: str) -> str:
    return f"scheduled:{task_id}:{run_id}"


async def run_scheduled_prompt(payload: Any, sessions: SessionManager, report: Report) -> None:
    """Run one scheduled prompt as a silent, throwaway turn and report how it
    went. Never raises: a failed turn is reported as a failed run."""
    if not isinstance(payload, dict):
        return
    task_id, run_id, text = payload.get("task_id"), payload.get("run_id"), payload.get("text")
    if not task_id or not run_id or not text:
        log.warning("scheduled prompt missing task_id/run_id/text; dropping")
        return

    key = session_key(task_id, run_id)
    parts: list[str] = []
    result: dict[str, Any] = {"task_id": task_id, "run_id": run_id, "ok": True, "error": None}
    log.info("scheduled task %s run %s: starting", task_id, run_id)
    try:
        session = await sessions.get_or_create(key)
        async for chunk in session.send(PROMPT_PREFIX + text):
            if chunk.subagent:
                continue
            if chunk.kind == "text":
                parts.append(chunk.text)
            elif chunk.kind == "result":
                result["cost_usd"] = chunk.cost_usd
                result["duration_ms"] = chunk.duration_ms
                if chunk.is_error:
                    result.update(ok=False, error=chunk.text[:500])
    except Exception as e:  # noqa: BLE001 — reported as a failed run
        log.exception("scheduled task %s run %s failed", task_id, run_id)
        result.update(ok=False, error=str(e)[:500])
    finally:
        # Fresh each run: forget the conversation, resume id included.
        await sessions.drop(key)

    reply = "\n".join(parts).strip()
    result["text"] = reply[:MAX_RESULT_CHARS]
    log.info(
        "scheduled task %s run %s: ok=%s cost_usd=%s reply=%s",
        task_id, run_id, result["ok"], result.get("cost_usd"), reply[:300] or "(no output)",
    )
    try:
        await report(result)
    except Exception:  # noqa: BLE001 — the run happened; only the report is lost
        log.exception("could not report scheduled task %s run %s", task_id, run_id)
