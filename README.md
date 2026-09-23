# cloud-agents

Everything for running fleets of always-on cloud coding agents: the Bizzybot
control plane they talk through, the skill that provisions them, the tool that
tracks them, and the role protocols they work to.

Each directory under `skills/` is one skill in the standard Claude Code layout
(`<name>/SKILL.md`, plus optional `references/` and `scripts/`). The repo's
`.claude/skills` is a symlink to `skills/`, so every skill is available when
running Claude Code inside this repo.

![How the pieces fit: a Slack workspace with the BzPM, Builder and Merger bots
talks to Central Dispatch on Railway, which reaches the three AWS cloud agents;
the bizzybot bridge runs inside each one; the cloud-agents repo supplies both
Central Dispatch and the agents' factory definitions; each agent clones the
btdash repo it works in.](docs/cloud-agent-factory.png)

## What's here

| Path | What |
|---|---|
| `skills/provision-cloud-agent/` | How to build an agent: host, coding agent, control plane, GitHub, env, verification |
| `bin/agents` | Read and update a company's encrypted agent manifest |
| `bizzybot/` | The Bizzybot control plane: `central-dispatch/` (server) and `agent-wrapper/` (the bridge each box runs) |
| `factory/` | The BizTrip pipeline's role protocols (BzPM, Builder, Merger) |
| `agents/` | Role protocols for agents outside the pipeline (BizzyBrain) |

## Running the Bizzybot bridge

The bridge (`bizzybot/agent-wrapper/`) is what makes a machine an agent: it
dials out to Central-Dispatch over a WebSocket, receives Slack events, and
drives Claude Code. Run it on any machine you want to reach from Slack — an
agent box, or your laptop.

```sh
uv tool install "git+https://github.com/biztrip-ai/cloud-agents.git#subdirectory=bizzybot/agent-wrapper"
bizzybot        # prompts once for the registration token, then caches it
```

Get the registration token from the agent's card on the Central-Dispatch
dashboard. Non-interactively (how the cloud agents run it), set it in the
environment instead:

```sh
REGISTRATION_TOKEN=<token> \
CLAUDE_CWD=/workspaces/projects/btdash \
CLAUDE_PERMISSION_MODE=bypassPermissions \
BIZZYBOT_STATE_DIR=/workspaces/<handle> \
  bizzybot
```

Useful extras: `CLAUDE_MODEL` pins the model, `AGENT_PROMPT_FILE` appends a
role protocol (see `factory/`), and `AGENT_MENTIONS_FROM` lets other agents
wake this one. `bizzybot/agent-wrapper/README.md` documents them all, plus
`bizzybot-dropbox` for getting secrets onto a box.

On a provisioned box the bridge runs under systemd rather than by hand:

```sh
sudo systemctl restart agent.service
journalctl -u agent.service -f        # preflight ✓ lines, then "connected to Central-Dispatch"
```

To upgrade it: `uv tool upgrade bizzybot-agent-wrapper && sudo systemctl restart agent.service`.

The server half (`bizzybot/central-dispatch/`) is deployed once per fleet, on
Railway; see its README to run one locally.

## Company agent manifests

What each company has provisioned is recorded in an encrypted manifest,
`companies/<company>/agents.sops.json`, encrypted with
[SOPS](https://github.com/getsops/sops) + [age](https://github.com/FiloSottile/age).
Manifests live in the company's own **private** repo, never in this one.
`bin/agents` reads and updates them without writing plaintext to disk:

```sh
brew install sops age
export CLOUD_AGENTS_HOME=<private repo>/infra/cloud-agents   # holds .sops.yaml + companies/
bin/agents list <company>
bin/agents ssh <company> <agent> 'uptime'
```

See `CLAUDE.md` for the rules and for adding people who can decrypt.

## Using these skills

Clone the repo and symlink (or copy) the skills you want into a skills
directory Claude Code reads:

```sh
git clone git@github.com:biztrip-ai/cloud-agents.git
ln -s "$PWD/cloud-agents/skills/provision-cloud-agent" ~/.claude/skills/provision-cloud-agent
```

- `~/.claude/skills/` — personal, available in every project
- `<repo>/.claude/skills/` — project-scoped, shared with everyone on the repo

## Skills

| Skill | What it does |
|---|---|
| [provision-cloud-agent](skills/provision-cloud-agent/SKILL.md) | Provision an always-on cloud coding agent: pluggable host (Railway, AWS EC2), pluggable coding agent (Claude / Codex / OpenCode), pluggable control plane (Flow, Bizzybot/Slack), GitHub access + repo checkout, env-var sync, and an agent-run bootstrap verification. |

### provision-cloud-agent layout

```
skills/provision-cloud-agent/
  SKILL.md                      # the workflow spine + plugin contracts
  references/
    hosts/{railway,aws}.md      # host plugins (add fly.md here)
    agents/{claude,codex,opencode}.md
    control-planes/{flow,bizzybot}.md   # control-plane plugins
    access/eks-readonly-and-db.md       # add-on: read-only EKS + prod DB, box outside the VPC
  scripts/
    copy-env-vars.sh            # local env → host env, values never printed
    aws-user-data.sh            # EC2 cloud-init: packages, /workspaces, agent.service
```

Adding a platform = adding one reference file that satisfies the contract in
SKILL.md; the spine doesn't change.
