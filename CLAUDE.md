# cloud-agents

This repo creates and manages fleets of always-on cloud coding agents, one fleet
per company. It is **public**: it holds the tooling, and each company keeps its
own records in a private repo.

- **`skills/provision-cloud-agent/`**: how to build an agent. That covers a host
  (AWS EC2, Railway), a coding agent (Claude Code, Codex, OpenCode), a control
  plane (Bizzybot/Slack, Flow), GitHub access, env vars and a bootstrap check.
  `.claude/skills` symlinks to `skills/`.
- **`bin/agents`**: reads and updates a company's encrypted agent manifest.
- **Company data** is *not* in this repo. `CLOUD_AGENTS_HOME` points to a
  directory in the company's private repo that holds `.sops.yaml` and
  `companies/<company>/agents.sops.json`, the record of every agent the company
  runs, how to reach it, and its SSH key.

**Never commit company data here.** That means manifests, Slack app IDs,
hostnames and IPs, account IDs, and company-specific runbooks. They go in the
company's private repo, next to its manifest.

## The agent manifest

Treat the manifest as the source of truth for "what agents exist". Before
answering any question about an agent, or acting on one, look it up:

```sh
export CLOUD_AGENTS_HOME=<private repo>/infra/cloud-agents   # usually set in the shell rc
bin/agents list <company>
bin/agents show <company> <agent>          # names and aliases both match
bin/agents ssh <company> <agent> 'systemctl is-active agent.service'
bin/agents railway <company> <agent> 'ls /workspaces'
```

If `CLOUD_AGENTS_HOME` isn't set, ask the user where their company's cloud-agents
directory is. Don't create a manifest inside this repo.

Rules:

- **Only touch the manifest through `bin/agents`.** It decrypts with `sops` in
  memory and re-encrypts on every save. Never write a decrypted copy to disk,
  and never print private keys or tokens into the conversation.
- **Record every change as it happens**: new agent (stub at the start of
  provisioning, details after each stage), discovered agent (`unverified`),
  bridge upgrade (new commit), token renewal, teardown (`deleted`). The
  provisioning skill's "Agent manifest" section has the lifecycle and the entry
  schema.
- **One SSH key per agent**, created with `bin/agents keygen` and stored only in
  the manifest. Don't point agents at a personal `~/.ssh` key.
- **Deliver secrets the standard way** (the provisioning skill's "Delivering
  secrets" section). The Claude token is created on the box with
  `scripts/claude-login.sh`, and the user only pastes the short sign-in code.
  Everything else (registration tokens, `GH_TOKEN`, API keys, SSH keys) goes
  through `bizzybot-dropbox`: give the user the link and the check code. Never
  ask for a secret in chat, and don't have users paste into silent terminal
  prompts.
- **Runtime secrets stay on the box.** The manifest lists env var *names*;
  values like `CLAUDE_CODE_OAUTH_TOKEN` and `GH_TOKEN` live in
  `/workspaces/env/agent.env` on the agent.
- **Commit manifest changes in the private repo** once they're made.

## Encryption (SOPS + age)

- The private repo's `.sops.yaml` encrypts every value in
  `companies/*/agents.sops.json` to the age public keys it lists. Field names
  stay readable, so diffs make sense.
- Decrypting needs a matching age private key. On macOS sops reads
  `~/Library/Application Support/sops/age/keys.txt`, or you can set
  `SOPS_AGE_KEY_FILE`. Back that key up. If every recipient key is lost, the
  manifest is lost too.
- To add a person or machine, append their `age1…` public key to `.sops.yaml`,
  then run `sops updatekeys companies/<co>/agents.sops.json` from that directory.
- Tools: `brew install sops age`.

## Operating agents (Bizzybot control plane)

- **Upgrading the bridge:**
  1. Run `export UV_TOOL_DIR=/workspaces/.uv/tools UV_TOOL_BIN_DIR=/workspaces/.uv/bin; uv tool upgrade bizzybot-agent-wrapper`.
  2. Check that no `claude` session is mid-turn (see `/workspaces/<state>/logs/`).
  3. Run `sudo systemctl restart agent.service`.
  4. In `journalctl -u agent.service`, look for ✓ on the `preflight` lines and
     for `connected to Central-Dispatch`.
  5. Record the new commit in the manifest.
- **Central-Dispatch** usually auto-deploys from the bizzybot repo. Restarting an
  agent while it redeploys produces 502s, so check that the deploy has finished first.
- **Claude auth expired** ("Not logged in"): run
  `bin/agents ssh <co> <agent> 'bash -s start' < skills/provision-cloud-agent/scripts/claude-login.sh`,
  have the user open the URL and paste back the short code, run the same script
  with `finish <code>`, then restart the agent service.
- **Slack apps:** each Bizzybot agent needs its own Slack app. Keep a Slack CLI
  project per app in the company's private repo (`manifest.json` plus a
  `.slack/hooks.json` whose `get-manifest` hook is `sh -c "cat manifest.json"`).
  `slack app install --team <team> -E deployed` creates the app, and
  `slack manifest sync` updates it. Each app's secrets go into Central-Dispatch's
  `SLACK_APPS`; the user sets that, so never read or echo it.
