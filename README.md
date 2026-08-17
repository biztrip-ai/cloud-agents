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
| [provision-cloud-agent](provision-cloud-agent/SKILL.md) | Provision an always-on Flow agent on Railway: devcontainer image, persistent volume, flow-agent-bridge daemon running Claude Code as a workspace member. |
