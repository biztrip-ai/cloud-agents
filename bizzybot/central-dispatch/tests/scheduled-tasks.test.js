// Scheduled tasks: scheduling math, @mention resolution, the dashboard forms
// and the posting loop.
//
//   npm test
//
// Runs against SQLite in a temp dir, with a real Express app on a real port.
// Slack is a fake: fetches to slack.com are answered here and recorded.
import { test, before, after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cd-sched-test-'));
process.env.DB_PATH = path.join(dir, 'test.db');
process.env.SESSION_SECRET = 'test-secret';
process.env.PUBLIC_URL = 'http://localhost:0';
process.env.SLACK_APPS = JSON.stringify([
  { appId: 'A1', name: 'Poster', clientId: 'cid', clientSecret: 'csec', signingSecret: 'ssec' },
]);

const express = (await import('express')).default;
const { init, all } = await import('../src/db.js');
const store = await import('../src/store.js');
const sched = await import('../src/scheduled_tasks.js');

const TEAM = 'T1';
const MIN = 60_000;

// --- Fake Slack ----------------------------------------------------------------
const USERS = [
  { id: 'U0BRAIN', name: 'bizzybrain', profile: { display_name: 'BizzyBrain' } },
  { id: 'U0SCOTT', name: 'scottp', real_name: 'Scott Persinger', profile: { display_name: 'Scott P' } },
  { id: 'U0GONE', name: 'gone', deleted: true },
];
const CHANNELS = [
  { id: 'C_brain', name: 'braincenter', is_member: true },
  { id: 'C_other', name: 'random', is_member: false },
  { id: 'C_secret', name: 'secret', is_member: true, is_private: true },
];
let posts = [];
let postError = null;
const realFetch = globalThis.fetch;
globalThis.fetch = async (url, opts = {}) => {
  const u = new URL(String(url));
  if (u.host !== 'slack.com') return realFetch(url, opts);
  const reply = (body) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
  switch (u.pathname) {
    case '/api/conversations.list': {
      // An app installed before groups:read was added can't list private channels.
      const old = opts.headers?.Authorization === 'Bearer xoxb-old';
      if (old && u.searchParams.get('types').includes('private_channel')) {
        return reply({ ok: false, error: 'missing_scope', needed: 'groups:read' });
      }
      const types = u.searchParams.get('types').split(',');
      return reply({ ok: true, channels: CHANNELS.filter((c) => types.includes(c.is_private ? 'private_channel' : 'public_channel')) });
    }
    case '/api/users.list': return reply({ ok: true, members: USERS });
    case '/api/users.info': return reply({ ok: true, user: { real_name: 'Scott Persinger' } });
    case '/api/chat.postMessage': {
      const body = JSON.parse(opts.body);
      posts.push({ ...body, token: opts.headers.Authorization });
      return reply(postError ? { ok: false, error: postError } : { ok: true, ts: `${posts.length}.0` });
    }
    default: return reply({ ok: false, error: 'unknown_method' });
  }
};

// --- App -------------------------------------------------------------------------
let server;
let base;

function cookieFor(userId, teamId = TEAM) {
  const payload = { teamId, userId, name: userId, exp: Date.now() + 3600_000 };
  const body = Buffer.from(JSON.stringify(payload)).toString('base64url');
  const mac = crypto.createHmac('sha256', process.env.SESSION_SECRET).update(body).digest('base64url');
  return `cb_session=${body}.${mac}`;
}

async function post(url, body, cookie = cookieFor('U0SCOTT')) {
  return fetch(base + url, {
    method: 'POST',
    redirect: 'manual',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', Cookie: cookie },
    body: new URLSearchParams(body),
  });
}

const tasks = () => all('SELECT * FROM scheduled_tasks ORDER BY created_at');

let agent;
before(async () => {
  await init();
  await sched.initScheduledTasksTable();
  const app = express();
  app.use(express.urlencoded({ extended: false }));
  app.use(sched.scheduledRouter);
  server = http.createServer(app);
  await new Promise((r) => server.listen(0, r));
  base = `http://127.0.0.1:${server.address().port}`;
  agent = await store.createAgent('BzPM');
  await store.setAgentSlack(agent.id, { teamId: TEAM, appId: 'A1', botToken: 'xoxb-bzpm' });
});

beforeEach(async () => {
  posts = [];
  postError = null;
  const { run } = await import('../src/db.js');
  await run('DELETE FROM scheduled_tasks');
});

after(() => {
  server?.close();
  globalThis.fetch = realFetch;
  fs.rmSync(dir, { recursive: true, force: true });
});

// --- Pure helpers ----------------------------------------------------------------

test('nextRunAfter keeps to the grid and skips missed runs', () => {
  const t0 = 1_000_000;
  assert.equal(sched.nextRunAfter(t0 + 5, 5, t0), t0 + 5); // not due yet: unchanged
  assert.equal(sched.nextRunAfter(t0, 5, t0), t0 + 5 * MIN);
  // Down for 23 minutes on a 10-minute schedule: one post, next at +30, no burst.
  assert.equal(sched.nextRunAfter(t0, 10, t0 + 23 * MIN), t0 + 30 * MIN);
});

test('resolveMentions turns @handles into real mentions', () => {
  const r = (t) => sched.resolveMentions(t, USERS);
  assert.equal(r('@BizzyBrain what is the latest Moderna update?'), '<@U0BRAIN> what is the latest Moderna update?');
  assert.equal(r('hey @bizzybrain, and @ScottP.'), 'hey <@U0BRAIN>, and <@U0SCOTT>.');
  assert.equal(r('@ScottPersinger ping'), '<@U0SCOTT> ping'); // real name, spaces ignored
  assert.equal(r('@here standup'), '<!here> standup');
  // Left alone: unknown, deleted, emails, and mentions that are already tokens.
  assert.equal(r('@nobody @gone a@bizzybrain.com <@U0BRAIN>'), '@nobody @gone a@bizzybrain.com <@U0BRAIN>');
  assert.equal(sched.displayMentions('<@U0BRAIN> hi <!here>', USERS), '@BizzyBrain hi @here');
});

// --- Dashboard ---------------------------------------------------------------------

test('creating a task stores a real mention and the channel name', async () => {
  const res = await post('/dashboard/scheduled', {
    agentId: agent.id, channel: 'C_brain', interval: '15', runAt: '',
    text: '@BizzyBrain what is the latest Moderna update?',
  });
  assert.equal(res.headers.get('location'), '/dashboard/scheduled');
  const [t] = await tasks();
  assert.equal(t.text, '<@U0BRAIN> what is the latest Moderna update?');
  assert.equal(t.channel_name, 'braincenter');
  assert.equal(t.created_by, 'U0SCOTT');
  assert.equal(Number(t.interval_min), 15);
  assert.ok(Math.abs(Number(t.next_run_at) - (Date.now() + 15 * MIN)) < 5000);

  const page = await (await fetch(`${base}/dashboard/scheduled`, { headers: { Cookie: cookieFor('U0SCOTT') } })).text();
  assert.match(page, /@BizzyBrain what is the latest Moderna update\?/);
  assert.match(page, /#braincenter/);
  assert.doesNotMatch(page, /xoxb-/); // bot tokens never reach the page
});

test('creation is refused for bad input', async () => {
  const ok = { agentId: agent.id, channel: 'C_brain', interval: '5', text: 'hi', runAt: '' };
  const err = async (body, cookie) =>
    new URL((await post('/dashboard/scheduled', { ...ok, ...body }, cookie)).headers.get('location'), base)
      .searchParams.get('err');
  assert.equal(await err({ channel: 'C_other' }), 'channel'); // bot isn't a member
  assert.equal(await err({ interval: '7' }), 'interval');
  assert.equal(await err({ text: '  ' }), 'text');
  assert.equal(await err({ interval: '0', runAt: String(Date.now() - 10 * MIN) }), 'when');
  assert.equal(await err({}, cookieFor('U_x', 'T_other')), 'agent'); // another workspace's bot
  assert.equal((await tasks()).length, 0);
});

test('another workspace cannot touch a task', async () => {
  await post('/dashboard/scheduled', { agentId: agent.id, channel: 'C_brain', interval: '5', text: 'hi', runAt: '' });
  const [t] = await tasks();
  const outsider = cookieFor('U_x', 'T_other');
  for (const action of ['send-now', 'toggle', 'delete']) {
    assert.equal((await post(`/dashboard/scheduled/${t.id}/${action}`, {}, outsider)).status, 404);
  }
  assert.equal(posts.length, 0);
  assert.equal((await tasks()).length, 1);
});

// --- Posting loop ----------------------------------------------------------------------

test('a due recurring task posts once per slot, as the chosen bot', async () => {
  const start = Date.now() + 60 * MIN;
  await post('/dashboard/scheduled', {
    agentId: agent.id, channel: 'C_brain', interval: '10', runAt: String(start), text: '@BizzyBrain status?',
  });
  await sched.runDueTasks(start - 1); // not yet
  assert.equal(posts.length, 0);

  await sched.runDueTasks(start);
  await sched.runDueTasks(start + 1000); // same slot: already claimed
  assert.equal(posts.length, 1);
  assert.deepEqual(
    { channel: posts[0].channel, text: posts[0].text, token: posts[0].token },
    { channel: 'C_brain', text: '<@U0BRAIN> status?', token: 'Bearer xoxb-bzpm' },
  );
  const [t] = await tasks();
  assert.equal(Number(t.next_run_at), start + 10 * MIN);
  assert.equal(t.last_ts, '1.0');
  assert.equal(t.last_error, null);

  await sched.runDueTasks(start + 10 * MIN);
  assert.equal(posts.length, 2);
});

test('a one-time task posts once and switches off', async () => {
  const at = Date.now() + 5 * MIN;
  await post('/dashboard/scheduled', { agentId: agent.id, channel: 'C_brain', interval: '0', runAt: String(at), text: 'once' });
  await sched.runDueTasks(at);
  await sched.runDueTasks(at + 60 * MIN);
  assert.equal(posts.length, 1);
  const [t] = await tasks();
  assert.equal(Number(t.enabled), 0);
});

test('a fatal Slack error pauses the task; a temporary one only records it', async () => {
  const start = Date.now() + 60 * MIN;
  await post('/dashboard/scheduled', { agentId: agent.id, channel: 'C_brain', interval: '5', runAt: String(start), text: 'x' });

  postError = 'ratelimited';
  await sched.runDueTasks(start);
  let [t] = await tasks();
  assert.equal(t.last_error, 'ratelimited');
  assert.equal(Number(t.enabled), 1);

  postError = 'not_in_channel';
  await sched.runDueTasks(start + 5 * MIN);
  [t] = await tasks();
  assert.equal(t.last_error, 'not_in_channel');
  assert.equal(Number(t.enabled), 0);

  // Resuming clears the error and waits for the next slot rather than
  // posting straight away.
  postError = null;
  await post(`/dashboard/scheduled/${t.id}/toggle`, {});
  [t] = await tasks();
  assert.equal(Number(t.enabled), 1);
  assert.equal(t.last_error, null);
  assert.ok(Number(t.next_run_at) > Date.now());
});

test('send now posts off schedule and leaves the schedule alone', async () => {
  const start = Date.now() + 60 * MIN;
  await post('/dashboard/scheduled', { agentId: agent.id, channel: 'C_brain', interval: '30', runAt: String(start), text: 'now' });
  const [before] = await tasks();
  await post(`/dashboard/scheduled/${before.id}/send-now`, {});
  assert.equal(posts.length, 1);
  const [t] = await tasks();
  assert.equal(Number(t.next_run_at), Number(before.next_run_at));
  assert.ok(t.last_run_at);
});

test('a bot missing a channel scope still lists what it can, and says how to fix it', async () => {
  const old = await store.createAgent('Bizzy');
  await store.setAgentSlack(old.id, { teamId: TEAM, appId: 'A2', botToken: 'xoxb-old' });
  const page = await (await fetch(`${base}/dashboard/scheduled`, { headers: { Cookie: cookieFor('U0SCOTT') } })).text();
  const bizzy = page.match(new RegExp(`<select name="channel" data-agent="${old.id}"[\\s\\S]*?</select>`))[0];
  assert.match(bizzy, /#braincenter/); // public channels still listed
  assert.doesNotMatch(bizzy, /secret/);
  assert.match(page, /Bizzy's Slack app is missing groups:read, so\s+private channels\s+can’t be listed/);
  assert.match(page, /#braincenter[\s\S]*🔒secret/); // BzPM, with every scope, sees both
});
