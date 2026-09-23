// Private-message listening, server side: the per-agent flag, the user-token
// grant, what the bridge is told, and who may take a grant away.
//
//   npm test
//
// Runs against SQLite in a temp dir, with a real Express app on a real port —
// the dashboard is HTML forms, so the interesting bugs are in the routing and
// the permission checks, not in any unit.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cd-test-'));
process.env.DB_PATH = path.join(dir, 'test.db');
process.env.SESSION_SECRET = 'test-secret';
process.env.PUBLIC_URL = 'http://localhost:0';
process.env.SLACK_APPS = JSON.stringify([
  { appId: 'A1', name: 'Listener', clientId: 'cid', clientSecret: 'csec', signingSecret: 'ssec' },
]);

const express = (await import('express')).default;
const { init } = await import('../src/db.js');
const { router } = await import('../src/routes.js');
const store = await import('../src/store.js');

const TEAM = 'T1';
const SCOTT = 'U_scott';
const DANA = 'U_dana';

let server;
let base;

// The session cookie is a signed blob; mint one rather than doing OAuth.
function cookieFor(userId) {
  const payload = { teamId: TEAM, userId, name: userId, exp: Date.now() + 3600_000 };
  const body = Buffer.from(JSON.stringify(payload)).toString('base64url');
  const mac = crypto
    .createHmac('sha256', process.env.SESSION_SECRET)
    .update(body)
    .digest('base64url');
  return `cb_session=${body}.${mac}`;
}

async function post(url, body, { cookie, json } = {}) {
  return fetch(base + url, {
    method: 'POST',
    redirect: 'manual',
    headers: {
      'Content-Type': json ? 'application/json' : 'application/x-www-form-urlencoded',
      ...(cookie ? { Cookie: cookie } : {}),
    },
    body: json ? JSON.stringify(body) : new URLSearchParams(body),
  });
}

before(async () => {
  await init();
  const app = express();
  app.use(express.json());
  app.use(express.urlencoded({ extended: false }));
  app.use(router);
  server = http.createServer(app);
  await new Promise((r) => server.listen(0, r));
  base = `http://127.0.0.1:${server.address().port}`;
});

after(() => {
  server?.close();
  fs.rmSync(dir, { recursive: true, force: true });
});

async function newAgent(name) {
  const agent = await store.createAgent(name);
  await store.setAgentSlack(agent.id, { teamId: TEAM, appId: 'A1', botToken: 'xoxb-test' });
  await store.claimAgentSponsor(agent.id, SCOTT);
  return agent;
}

test('private messages are off until someone turns them on', async () => {
  const agent = await newAgent('Listener');

  // The authorize link refuses while the flag is off, so a stray URL can't
  // start a grant for an agent nobody enabled.
  const blocked = await fetch(`${base}/slack/authorize-private?agent=${agent.id}`, {
    redirect: 'manual',
    headers: { Cookie: cookieFor(SCOTT) },
  });
  assert.equal(blocked.status, 403);

  // And the bridge is told there is nothing to read with.
  const before = await (await post('/api/user-tokens', { token: agent.registrationToken }, { json: true })).json();
  assert.equal(before.enabled, false);
  assert.deepEqual(before.tokens, []);

  const on = await post(
    '/dashboard/agent-private-messages',
    { agentId: agent.id, enabled: '1' },
    { cookie: cookieFor(SCOTT) },
  );
  assert.equal(on.status, 302);

  const started = await fetch(`${base}/slack/authorize-private?agent=${agent.id}`, {
    redirect: 'manual',
    headers: { Cookie: cookieFor(SCOTT) },
  });
  assert.equal(started.status, 302);
  const to = new URL(started.headers.get('location'));
  assert.equal(to.host, 'slack.com');
  // User scopes only: asking for bot scopes here would re-install the app.
  assert.equal(to.searchParams.get('scope'), '');
  const userScopes = to.searchParams.get('user_scope').split(',');
  assert.ok(userScopes.includes('mpim:history'));
  // Group DMs only — these would widen the token to other conversation types.
  for (const forbidden of ['im:history', 'channels:history', 'groups:history']) {
    assert.ok(!userScopes.includes(forbidden), `must not request ${forbidden}`);
  }
});

test('a grant reaches the bridge, and revoking takes it away', async () => {
  const agent = await newAgent('Listener2');
  await store.setAgentPrivateMessages(agent.id, true);
  await store.putUserToken(agent.id, SCOTT, 'xoxp-scott', 'mpim:history');
  await store.putUserToken(agent.id, DANA, 'xoxp-dana', 'mpim:history');

  const seen = await (await post('/api/user-tokens', { token: agent.registrationToken }, { json: true })).json();
  assert.equal(seen.enabled, true);
  assert.deepEqual(
    seen.tokens.map((t) => [t.slackUserId, t.token]).sort(),
    [[DANA, 'xoxp-dana'], [SCOTT, 'xoxp-scott']].sort(),
  );

  // Re-authorizing replaces the token rather than piling up rows.
  await store.putUserToken(agent.id, SCOTT, 'xoxp-scott-2', 'mpim:history');
  const again = await (await post('/api/user-tokens', { token: agent.registrationToken }, { json: true })).json();
  assert.equal(again.tokens.length, 2);
  assert.equal(again.tokens.find((t) => t.slackUserId === SCOTT).token, 'xoxp-scott-2');

  // Dana can remove her own.
  const mine = await post(
    '/dashboard/revoke-user-token',
    { agentId: agent.id, slackUserId: DANA },
    { cookie: cookieFor(DANA) },
  );
  assert.equal(mine.status, 302);
  const afterMine = await (await post('/api/user-tokens', { token: agent.registrationToken }, { json: true })).json();
  assert.deepEqual(afterMine.tokens.map((t) => t.slackUserId), [SCOTT]);

  // Dana cannot remove Scott's: she is neither its owner nor the sponsor.
  const notMine = await post(
    '/dashboard/revoke-user-token',
    { agentId: agent.id, slackUserId: SCOTT },
    { cookie: cookieFor(DANA) },
  );
  assert.equal(notMine.status, 403);

  // The sponsor can remove anyone's.
  await store.putUserToken(agent.id, DANA, 'xoxp-dana-2', 'mpim:history');
  const bySponsor = await post(
    '/dashboard/revoke-user-token',
    { agentId: agent.id, slackUserId: DANA },
    { cookie: cookieFor(SCOTT) },
  );
  assert.equal(bySponsor.status, 302);
  const end = await (await post('/api/user-tokens', { token: agent.registrationToken }, { json: true })).json();
  assert.deepEqual(end.tokens.map((t) => t.slackUserId), [SCOTT]);
});

test('turning the feature off revokes every grant with it', async () => {
  const agent = await newAgent('Listener3');
  await store.setAgentPrivateMessages(agent.id, true);
  await store.putUserToken(agent.id, SCOTT, 'xoxp-scott', 'mpim:history');

  await post(
    '/dashboard/agent-private-messages',
    { agentId: agent.id, enabled: '0' },
    { cookie: cookieFor(SCOTT) },
  );
  const seen = await (await post('/api/user-tokens', { token: agent.registrationToken }, { json: true })).json();
  assert.equal(seen.enabled, false);
  assert.deepEqual(seen.tokens, []);
  // Re-enabling must not resurrect the old token.
  await store.setAgentPrivateMessages(agent.id, true);
  const back = await (await post('/api/user-tokens', { token: agent.registrationToken }, { json: true })).json();
  assert.deepEqual(back.tokens, []);
});

test('another workspace cannot touch this workspace\'s agent', async () => {
  const agent = await newAgent('Listener4');
  const stranger = `cb_session=${(() => {
    const payload = { teamId: 'T_other', userId: 'U_x', name: 'x', exp: Date.now() + 3600_000 };
    const body = Buffer.from(JSON.stringify(payload)).toString('base64url');
    const mac = crypto.createHmac('sha256', 'test-secret').update(body).digest('base64url');
    return `${body}.${mac}`;
  })()}`;
  const res = await post(
    '/dashboard/agent-private-messages',
    { agentId: agent.id, enabled: '1' },
    { cookie: stranger },
  );
  assert.equal(res.status, 403);
});

test('the bridge needs a valid registration token to see any of this', async () => {
  const res = await post('/api/user-tokens', { token: 'nope' }, { json: true });
  assert.equal(res.status, 401);
});
