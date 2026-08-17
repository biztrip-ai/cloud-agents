# flow-skills

Agent skills for working with [Flow](https://github.com/freeflow-community/flow).
Each directory is one skill in the standard Claude Code layout
(`<name>/SKILL.md`, plus optional `references/` and `scripts/`).

## Using these skills

Clone the repo and symlink (or copy) the skills you want into a skills
directory Claude Code reads:

```sh
git clone git@github.com:freeflow-community/flow-skills.git
ln -s "$PWD/flow-skills/provision-cloud-agent" ~/.claude/skills/provision-cloud-agent
```

- `~/.claude/skills/` — personal, available in every project
- `<repo>/.claude/skills/` — project-scoped, shared with everyone on the repo

## Skills

| Skill | What it does |
|---|---|
| [provision-cloud-agent](provision-cloud-agent/SKILL.md) | Provision an always-on cloud coding agent: pluggable host (Railway today), pluggable coding agent (Claude / Codex / OpenCode), pluggable control plane (Flow today), GitHub access + repo checkout, env-var sync, and an agent-run bootstrap verification. |

### provision-cloud-agent layout

```
provision-cloud-agent/
  SKILL.md                      # the workflow spine + plugin contracts
  references/
    hosts/railway.md            # host plugins (add fly.md, aws.md here)
    agents/{claude,codex,opencode}.md
    control-planes/flow.md      # control-plane plugins
  scripts/
    copy-env-vars.sh            # local env → host env, values never printed
```

Adding a platform = adding one reference file that satisfies the contract in
SKILL.md; the spine doesn't change.
