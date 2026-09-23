# Agents

Role protocols for agents that aren't part of the btdash factory pipeline
(that one lives in `factory/`). Each folder holds the `PROTOCOL.md` the bridge
appends to the agent's system prompt (`AGENT_PROMPT_FILE`) and the
`agent.env.example` its box's `/workspaces/env/agent.env` is built from.

| Agent | Folder | Role |
|---|---|---|
| **BizzyBrain** (`@bizzybrain`) | `bizzybrain/` | Follows the channels it's invited to, remembers durable non-sensitive facts, and answers questions from that memory. Runs with `PASSIVE_LISTEN_ALL=1`. |

Each box clones this repo to `/workspaces/projects/cloud-agents`, so a
protocol change is a `git pull` plus `sudo systemctl restart agent.service`.
