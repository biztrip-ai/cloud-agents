# Host: Railway

Satisfies the host contract with a Railway service inside an existing project.
Assumes the `railway` CLI is authenticated (`railway login`); ids come from
`railway status --json` in a linked repo, or `railway list --json`.

## Provision

```sh
railway add --service <SVC> --json          # ALWAYS pass --json (silent no-JSON success trap)
railway volume -s <service-id> -e <env-id> add -m /workspaces --json   # durable storage
```

Image: `mcr.microsoft.com/devcontainers/universal:2` is a good default dev
image (~10 GB; first deploy is slow; Node + Python + gh preinstalled). It has
no long-running process, so the start command below is mandatory.

## Set image + start command — GraphQL, not the CLI

**Trap**: `railway environment edit --service-config …` silently no-ops ("No
changes to apply") on current CLIs. Use the GraphQL API
(`https://backboard.railway.com/graphql/v2`):

```graphql
mutation update($serviceId: String!, $environmentId: String, $input: ServiceInstanceUpdateInput!) {
  serviceInstanceUpdate(serviceId: $serviceId, environmentId: $environmentId, input: $input)
}
mutation deploy($serviceId: String!, $environmentId: String!) {
  serviceInstanceDeployV2(serviceId: $serviceId, environmentId: $environmentId)
}
```

`input` takes `{"source": {"image": "…"}, "startCommand": "…"}`. Build the
JSON with a script rather than hand-escaping. Bearer token: the `use-railway`
skill's `railway-api.sh` helper works but reads `.user.token` from
`~/.railway/config.json`; newer CLIs store `.user.accessToken` — use a copy
with `.user.accessToken // .user.token`. "Not Authorized" from GraphQL means
the access token expired: run any CLI command (`railway whoami`) to refresh,
then retry.

## Start command template

Railway runs containers **as root** regardless of the image's USER, so the
final process must drop privileges (Claude Code refuses its
permission-bypass flag as root — agents error on first message otherwise).
`<AGENT_MAIN>` is the control plane's daemon command; for a control-plane-less
box use `sleep infinity`.

```sh
bash -c '
mkdir -p /workspaces/.npm-global /workspaces/.python /workspaces/projects;
grep -q "# persist-path" /home/codespace/.bashrc 2>/dev/null || \
  printf "\n# persist-path\nexport PATH=/workspaces/.npm-global/bin:/workspaces/.python/bin:\$PATH\n" >> /home/codespace/.bashrc;
export PATH=/workspaces/.npm-global/bin:$PATH;
<INSTALL_IF_MISSING_LINES>;
chown -R codespace:codespace /workspaces;
if [ <AGENT_CONFIGURED_TEST> ]; then
  exec sudo -E -u codespace env HOME=/home/codespace PATH=$PATH <AGENT_MAIN>;
else sleep infinity; fi'
```

`<INSTALL_IF_MISSING_LINES>` example:
`command -v claude >/dev/null 2>&1 || npm install -g --prefix /workspaces/.npm-global @anthropic-ai/claude-code`

## Environment variables

```sh
railway variable set --skip-deploys --service <SVC> KEY=value …
railway variable set --service <SVC> KEY=value        # without --skip-deploys: triggers a redeploy
```

Variable changes only reach the container via a redeploy — and a redeploy
**wipes everything outside `/workspaces`**. Vars are visible to every member
of the Railway project; say so before the user copies secrets in.

## Shell, logs, verification

```sh
railway ssh --service <SVC> -- bash -c '<commands>'   # non-interactive shell
railway logs --service <SVC> --lines 50
railway deployment list --service <SVC> --json        # poll [0].status
```

Never report a deploy as done before `deployment list` shows `SUCCESS`
(`FAILED`/`CRASHED` → read logs). After the control plane is the main process,
also verify with `ps -o user,cmd -C node` over SSH that it runs as
`codespace`, not root.

## Quirks summary

- Runs as root; drop to `codespace` (passwordless sudo available).
- Ephemeral filesystem outside the volume; every redeploy is a fresh container.
- `npm install -g` without `--prefix /workspaces/.npm-global` lands in the
  image's nvm dir (ephemeral). Don't set `NPM_CONFIG_PREFIX` globally — it
  breaks the image's nvm shell init; always pass `--prefix` explicitly.
- `railway environment edit` broken (above); `railway add` needs `--json`.
- One volume per service, one mount path.
