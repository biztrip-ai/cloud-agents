# Agents

Role protocols for agents that aren't part of the btdash factory pipeline
(that one lives in `factory/`). Each folder holds the `PROTOCOL.md` the bridge
appends to the agent's system prompt (`AGENT_PROMPT_FILE`) and the
`agent.env.example` its box's `/workspaces/env/agent.env` is built from.

| Agent | Folder | Role |
|---|---|---|
| **BizzyBrain** (`@bizzybrain`) | `bizzybrain/` | The company brain. Follows the channels it's invited to, reads its own mailbox (`bizzy@biztrip.ai`), shared Google Docs and calendars, Jira and HubSpot; remembers durable non-sensitive facts in an Obsidian vault (`bizzybrain/MEMORY.md`); answers questions and does tasks on request. Runs with `PASSIVE_LISTEN_ALL=1`. |

Each box clones this repo to `/workspaces/projects/cloud-agents`, so a
protocol change is a `git pull` plus `sudo systemctl restart agent.service`.
