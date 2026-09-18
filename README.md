# cloud-agents

Agent skills for working with [Flow](https://github.com/freeflow-community/flow).
Each directory under `skills/` is one skill in the standard Claude Code layout
(`<name>/SKILL.md`, plus optional `references/` and `scripts/`). The repo's
`.claude/skills` is a symlink to `skills/`, so every skill is available when
running Claude Code inside this repo.

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
git clone git@github.com:freeflow-community/cloud-agents.git
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
