# Private-message listening (design)

**Status:** design, nothing built. Written 2026-09-22.

An agent that follows group DMs it has been invited into by consent, and
remembers what matters from them. Off by default: it is enabled per agent, and
each person who wants the agent to see their conversations authorizes it
themselves.

The companion feature — periodically reviewing **public** channels the bot is a
member of — needs none of this machinery and is described at the end.

## Why it needs user tokens

A bot can only read a conversation it belongs to, and an app cannot be added to
a group DM. So the agent reads with a **user token**, granted by a person in
that conversation. That token can reach everything that person can see, which
is why most of this document is about limiting what the agent may actually
read, and about making the listening visible to everyone in the room.

## Model

| Thing | Meaning |
|---|---|
| **Authorizing user** | Someone who has granted the agent a user token. There can be several per agent. |
| **Watched conversation** | A group DM approved for recording. |
| **Approval** | Two ✅ reactions from distinct participants, at least one an authorizing user. |
| **Discovery** | Noticing a group DM exists. Metadata only: id, members, last activity. Never content. |
| **Recording** | Reading new messages from a watched conversation and handing them to the agent. |

## Enabling

1. The agent's row carries a flag (`private_messages_enabled`). Only agents
   with it see any of this; BzPM, Builder and Merger do not.
2. The agent's card on the Central-Dispatch dashboard shows **"Authorize
   private messages"**. It starts a Slack OAuth with `user_scope` only —
   `mpim:history`, `users:read` — and **no bot scopes**, so it doesn't disturb
   the app's existing install. Group DMs only: see "Scopes" below.
3. The callback stores `authed_user.access_token` in a new table, one row per
   (agent, slack_user_id): token, scopes, granted_at, revoked_at.
4. The card lists who has authorized, each with a **Remove** that deletes the
   token and stops all reading done through it.

Several people authorize the same agent independently. A conversation is
readable if *any* authorizing user is in it.

## Discovery (metadata only)

Every 2 minutes, for each authorizing user: `users.conversations`, `types=mpim`.
For each group DM the bridge records id, member ids and last activity.

**Content is gated in the bridge, not by instruction.** The read tool takes a
conversation id and checks the allowlist:

- not approved → returns metadata only (id, members, "new activity since X")
- approved → returns messages

So an agent that decides to read an unapproved conversation still cannot: the
content never enters its context. The protocol says the same thing in prose,
but the tool is what enforces it.

Every read is logged (which conversation, which token, how many messages), so
"what has it seen?" has an answer.

## Approval

A Block Kit button posted with a *user* token can't deliver clicks back to the
app, so approval is expressed as **reactions**, which the bridge can read.

1. On discovering an unapproved group DM, the bridge posts, with the
   authorizing user's token:

   > Allow **<agent>** to listen to this conversation and remember what's
   > useful? React ✅ to approve — two approvals needed, including one
   > authorized member. React ❌ to decline.

2. The bridge polls that message's reactions. **Approved** when two distinct
   people have reacted ✅ and at least one is an authorizing user. Any ❌ marks
   it declined, and it isn't asked again.
3. Approvals are stored per conversation: who approved, when, and the message
   they reacted to.

Until approval, the bridge sees only that the conversation exists.

### Membership changes

The bridge keeps the member list from discovery. When someone new appears in a
watched conversation, it posts, with the authorizing user's token:

> _<agent> is listening_

A notice, not a re-approval: recording continues. It exists so a person who
joins isn't recorded without knowing.

If every authorizing user leaves the conversation, recording stops.

## Recording

Per watched conversation, every 2 minutes:

1. `conversations.history` since the stored `last_ts`, with an authorizing
   user's token.
2. If there is anything new, start a **silent turn** with a fixed payload:

   > Latest messages from group DM `<id>` (participants: …):
   > `<messages>`

3. Advance `last_ts` only when the turn completes, so a crash re-reads rather
   than skipping.

The agent's instructions (to be written) tell it to scan for durable,
**non-sensitive** facts worth keeping and write them to its memory, and to say
nothing otherwise. "Non-sensitive" needs an explicit list in the protocol —
never record credentials or tokens, anything about health, employment, pay,
performance or legal matters, or personal life — otherwise it means nothing in
practice.

### Silent turns

Today every turn posts a "thinking…" placeholder and edits it as the agent
works. A polling turn has nowhere sensible to post: the agent isn't a member of
the conversation, and the person didn't ask a question. The bridge needs a mode
where a turn renders nothing in Slack and simply runs, with its output logged.
It reuses the empty-reply deletion added for agent hand-offs.

## Revocation

- **A user removes their authorization** → the token is deleted, and anything
  read through it stops. Other authorizing users of the same conversation are
  unaffected.
- **Someone declines** → the conversation is marked declined and never polled.
- **Past memories are unaffected.** Revoking stops future reading; it does not
  erase what the agent already learned.

## Rate limits

Slack's 2025 limits for non-Marketplace apps put `conversations.history` at
roughly 1 request/minute with ~15 messages per response. **Measured on our app
(2026-09-22): six back-to-back calls all returned 200 with 20 messages each, no
throttling** — the strict regime isn't being applied to us today.

Design for it anyway, since that can change:

- One discovery call per authorizing user per cycle.
- One history call per watched conversation per cycle.
- If a cycle would exceed a budget (say 20 calls/minute), poll conversations
  **round-robin**, one per cycle. A busy conversation then lags, but the app's
  call rate stays flat as the number of watched conversations grows.
- On HTTP 429, honour `Retry-After` and back off. The `last_ts` cursor makes
  this lossless.

## Data

| Table | Holds |
|---|---|
| `agent_user_tokens` | agent_id, slack_user_id, token, scopes, granted_at, revoked_at |
| `watched_conversations` | agent_id, conversation_id, state (pending / approved / declined), prompt_ts, members, last_ts |
| `conversation_approvals` | conversation_id, slack_user_id, reaction, at |

Tokens live in Central-Dispatch, the same place as bot tokens, and are handed
to the bridge at registration. Memories live on the agent's box under
`/workspaces/<handle>/memory/`, one file per topic, each noting the
conversation and date it came from.

## Passive listening in named channels

For a channel — public or private — the simplest consent is an **invite**: a
person adds the agent to the channel, everyone sees it in the member list, and
no user token is involved. Private channels work the same way, since the bot
already holds `groups:history` and `groups:read`.

What's missing is bridge support. Today a channel message only wakes an agent
if it @-mentions it, is a reply in a thread the agent is already in, or is in a
channel the agent created. A channel the agent was merely invited to is
ignored. **Passive listening mode** changes that for named channels.

### Delivery: batch the events we already get

Central-Dispatch already fans out `message.channels` and `message.groups`
events for every channel the app is in, so passive listening needs **no extra
API calls and no polling**. The bridge:

1. Collects messages for a passively-listened channel in a buffer instead of
   dropping them.
2. Flushes the buffer as one **silent turn** when it is ~2 minutes old or
   reaches a size cap, with a payload like:

   > New messages in `#<channel>` (passive listening):
   > `<messages>`

3. Skips anything already handled as a normal turn: a message that mentions the
   agent wakes it the usual way, and is dropped from the batch by `ts` so it
   isn't seen twice.

One turn per batch, not per message — a busy channel would otherwise be
expensive and noisy.

### What's in a batch

- Human messages, with author ids resolved to names.
- Not the agent's own messages, and not other agents' messages unless they are
  in `AGENT_MENTIONS_FROM`: otherwise two listening agents can feed each other.
- Edits and deletions are ignored; the batch is what was said, when.

### Turning it on

Per agent, and per channel. Options, in the order I'd pick them:

1. **`PASSIVE_LISTEN_CHANNELS`** — explicit channel ids in `agent.env`. Boring,
   obvious, and reviewable in the manifest.
2. **`PASSIVE_LISTEN=members`** — every channel the agent is invited to.
   Simplest to operate: invite it and it listens, remove it and it stops. The
   risk is quietly listening to a channel someone invited it to for one
   question.
3. **A command in the channel** (`@agent listen here` / `stop listening`),
   stored in the agent's state dir, so it's controlled by the people in the
   channel rather than by whoever edits `agent.env`.

(1) and (3) compose well: a default list, adjustable in the room.

### Announcing itself

On joining a channel with passive listening on, the agent posts one line —
*"I'm listening in this channel and will remember what's useful"* — so
membership isn't the only signal. It says nothing after that unless mentioned.

### Why this is different from group DMs

| | Named channel | Group DM |
|---|---|---|
| How it gets in | Invited; visible in the member list | Can't be a member at all |
| Reads with | Bot token | An authorizing user's token |
| Consent | The invite, plus the joining notice | Two ✅ reactions in the conversation |
| Delivery | Events already fanned out, batched | Polling every 2 minutes |

## Public-channel review

A periodic *digest* is a different job from passive listening: rather than
reacting to what arrives, it looks back over a window and summarises. A poller
in the bridge, shaped like `pr_poller` and `sentry_poller`, reads what's new in
the channels the bot is a member of and starts a turn with a digest prompt. If
passive listening is already on for those channels, the digest can run off what
the agent remembered instead of re-reading Slack. Nothing is hidden — a bot in a public channel is visible to everyone —
so no approval flow is needed. Open choices: interval, which channels, and
whether it always posts a digest or speaks only when something crosses a bar.

## Scopes: group DMs only

The authorization asks for **`mpim:history`** (group DMs) and `users:read`
(member ids → names), and nothing else. Slack scopes are per conversation
type, so without `im:history`, `channels:history` or `groups:history` the token
**cannot** read 1:1 DMs, public channels or private channels — not by policy
but by construction. The consent screen shows the person exactly that.

If private channels are wanted later, that is a deliberate extra scope and a
re-authorization, not a quiet widening.

## Stopping

There is no "stop listening" command, and it doesn't need one: participants who
want an unrecorded conversation start a new group DM, which is unapproved by
default and silent unless someone asks for approval again. An authorizing user
can also remove their authorization on the dashboard, which stops every read
done with their token.

## Open questions

1. Where do recordings go beyond memory — does the agent report anywhere?
2. What happens to a watched conversation that goes quiet for weeks: expire the
   approval, or keep it indefinitely?

## Build order

1. Central-Dispatch: user-scope OAuth, token table, dashboard button and list.
2. Bridge: token fetch, discovery loop, the gated read tool, allowlist state.
3. Approval: prompt message, reaction polling, approval records, join notices.
4. Recording: history poller, silent turns, the memory protocol.
5. Public-channel digest poller (independent of 1–4).
