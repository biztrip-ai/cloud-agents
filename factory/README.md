# Factory: btdash's agent pipeline

Three always-on cloud agents turn Jira tickets into merged, deployed btdash
code, supervised by one human in Slack. It's adapted from Flow's
[factory](https://github.com/freeflow-community/factory) and runs on the
Bizzybot Slack bridge.

| Agent | Folder | GitHub identity | Role |
|---|---|---|---|
| **BzPM** (`@bzpm`) | `pm/` | bizzy-btbot | Product manager. Writes tickets, moves approved ones to Ready and dispatches Builder, reviews and approves PRs, hands them to Merger, and escalates to Human Review. |
| **Builder** (`@builder`) | `builder/` | Scott (author "Builder") | Takes a Ready ticket and works it in its own Slack channel. Opens the PR, runs the change locally and checks it in a browser, moves the ticket to In Review and reports to BzPM. |
| **Merger** (`@merger`) | `merger/` | Scott (author "Merger") | Merges approved PRs, watches the staging/dev (and mobile) deploys go green, and moves the ticket to Done. Never tags production. |

BzPM reviews as a different GitHub account from the one Builder opens PRs
with, so its approval satisfies `main`'s one-review rule.

## The board: Jira BP (board 168)

https://biztrip-team.atlassian.net/jira/software/projects/BP/boards/168

| Status | Who moves it there | Meaning |
|---|---|---|
| **To Do** | BzPM (new tickets) | Written, not yet approved to build |
| **Ready** | BzPM, on Scott's go | Approved and dispatched, or queued for Scott to pick next |
| **In Progress** | Builder | Being built, in `#bp-<n>` |
| **In Review** | Builder | PR open and linked to the ticket; BzPM reviewing |
| **Done** | Merger | Merged, and the staging/dev deploy is green |
| **Human Review** | BzPM | Needs Scott: a risky change, a product decision, or something BzPM can't judge |

```
#factory:  Scott ─▶ BzPM: ticket (To Do) ─▶ "Build it?" ─▶ Ready
                                                   │
                                      "@builder build BP-n"
                                                   ▼
#bp-n:     Builder opens the channel (Scott, BzPM, Merger invited)
           In Progress ─▶ plan ─▶ work ─▶ PR ─▶ In Review
                     └─▶ "@bzpm BP-n is done: PR #x"
                                  │ BzPM reviews here
                                  ├─ approve ─▶ "@merger merge PR #x"
                                  │             └─▶ merge ─▶ deploy green ─▶ Done ─▶ ✅
                                  ├─ changes ─▶ In Progress, "@builder PR #x needs changes"
                                  └─ needs a human ─▶ Human Review (BzPM tells Scott in #factory)

#factory:  BzPM relays the result to Scott
```

Agents use the Atlassian MCP (`mcp__atlassian__*`, site
`biztrip-team.atlassian.net`, cloudId `cd3fe82c-6a18-40a7-8767-844d7fa7721d`).

## Slack

- **`#factory`**: Scott and BzPM. Requests, "Build it?", the dispatch to
  Builder, status back to Scott, and anything needing Human Review.
- **`#bp-<n>`**: one per ticket, created by Builder, with Scott, BzPM and
  Merger invited. Everything about that ticket happens here: the plan, a
  message per finished step, screenshots, the PR, BzPM's review verdict, the
  merge hand-off, and Merger's result. Post there to steer Builder. Merger
  adds a ✅ when it's merged.

Every hand-off is one line opening with the target's Slack handle: `@bzpm`,
`@builder`, `@merger` or `@scottp`. The bridge turns a known handle into a
real `<@U…>` mention, so that is what wakes the target; a handle in backticks,
a real name with spaces (`@Scott Persinger`) or a hand-typed, HTML-escaped
token (`&lt;@U…&gt;`) wakes nobody.

Loop guards (enforced by the bridge, restated in each protocol): an agent only
wakes on a message that mentions it (from Scott or an allowlisted agent). A
reply that mentions nobody ends the exchange. A chain of agent-to-agent turns
stops at `AGENT_CHAIN_LIMIT`.

## How the protocols load

Each folder's `PROTOCOL.md` is that agent's whole job description. The bridge
appends it to every session's system prompt (`AGENT_PROMPT_FILE` in
`<role>/agent.env.example`) and reads it at session start, so a merged change
takes effect after the agent's **cloud-agents** checkout
(`/workspaces/projects/cloud-agents`) pulls `main`. The protocols point at
btdash's own skills in its `.claude/skills/` (`dev-workflow`, `test-pr`,
`backend-review-pr`) rather than restating them, so each agent also keeps a
btdash checkout at `/workspaces/projects/btdash` — that's where the work
happens.

## Setup (cloud agents)

Each agent is a Bizzybot cloud agent (see
[cloud-agents](https://github.com/biztrip-ai/cloud-agents)), with this repo at
`/workspaces/projects/cloud-agents`, btdash at `/workspaces/projects/btdash`,
the chrome-devtools browser MCP, and
the Atlassian MCP registered as `atlassian`. Per agent:

1. Merge `<role>/agent.env.example` into the box's `/workspaces/env/agent.env`.
   These settings aren't secret; secrets come through `bizzybot-dropbox`.
2. Fill in the Slack user IDs of the other two agents (`AGENT_MENTIONS_FROM`).
3. Restart the agent service.

Create `#factory` once and invite the three agents.

**Requires Bizzybot with:** agent-to-agent mentions (`AGENT_MENTIONS_FROM`,
`AGENT_CHAIN_LIMIT`), `AGENT_PROMPT_FILE`, `@handle` mention resolution
(bizzybot PR #28), and the `post_message`, `read_messages` and
`add_reaction` Slack tools. Until the boxes run a bridge with those, the
agents can't hear each other's hand-offs.

## Not done yet

- A CI watcher like Flow's `ci-watch`, which wakes an agent when checks
  finish. Until then, Builder and Merger wait inside their session, for at
  most 30 minutes (`gh pr checks --watch`).
- The browser-driving skills still call `claude-in-chrome`; on the boxes, use
  the chrome-devtools equivalents. Verification is **local**: the box runs the
  data plane (postgres/redis/minio) plus the worktree's own servers on leased
  ports, per btdash's `local-stack` skill. There is no PR preview environment.
- `EVAL_BUNDLE_TOKEN` and Sentry credentials for the review skills.
