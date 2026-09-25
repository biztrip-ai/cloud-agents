// Scheduled tasks: once, or every 5/10/15/30/60 minutes, one of the signed-in
// workspace's agents either
//
//   - posts a message to a Slack channel (mode 'post'). A task that mentions
//     an agent wakes it like any bot mention would, so `@handle`s in the text
//     are turned into real <@U…> mentions when the task is saved: Slack treats
//     a bot's plain-text "@name" as text, not a mention; or
//   - is prompted silently (mode 'prompt'). The prompt goes to the agent as a
//     `scheduled_prompt` event on its queue, nothing is posted, and the agent's
//     bridge reports the reply, cost and duration back (POST /api/task-result)
//     for the task's card. See agent-wrapper/…/scheduled.py.
//
// Managed at /dashboard/scheduled. A loop here runs whatever is due.
import crypto from 'node:crypto';
import express from 'express';
import { config } from './config.js';
import { get, all, run, isPg } from './db.js';
import { getSession } from './session.js';
import { listAgentsByTeam, getAgentById, getAgentByToken, appendEvent } from './store.js';
import { postSlackMessage, botChannels, workspaceUsers, userName } from './slack.js';
import { onlineIds, pushEvent } from './wsHub.js';

const TASKS = isPg ? `${config.dbSchema}.scheduled_tasks` : 'scheduled_tasks';

export const INTERVALS = [5, 10, 15, 30, 60]; // minutes; 0 means once
const MIN = 60 * 1000;
const TICK_MS = 30 * 1000;
const MAX_TEXT = 4000;
// A silent run that hasn't reported back within this long is taken as lost
// (the agent restarted mid-run, say), and the next run may start.
const RUN_TIMEOUT_MS = 60 * MIN;
// Slack errors that won't fix themselves: the task is paused rather than
// retried every interval. Anything else (rate limits, outages) just retries.
const FATAL = new Set([
  'channel_not_found', 'not_in_channel', 'is_archived', 'invalid_auth',
  'account_inactive', 'token_revoked', 'no_permission', 'missing_scope', 'agent_missing',
]);

export async function initScheduledTasksTable() {
  const big = isPg ? 'BIGINT' : 'INTEGER';
  await run(`
    CREATE TABLE IF NOT EXISTS ${TASKS} (
      id            TEXT PRIMARY KEY,
      team_id       TEXT NOT NULL,
      agent_id      TEXT NOT NULL,
      channel_id    TEXT NOT NULL,
      channel_name  TEXT,
      text          TEXT NOT NULL,
      interval_min  INTEGER NOT NULL,
      next_run_at   ${big} NOT NULL,
      enabled       INTEGER NOT NULL DEFAULT 1,
      created_by    TEXT,
      created_at    ${big} NOT NULL,
      last_run_at   ${big},
      last_ts       TEXT,
      last_error    TEXT
    )`);
  // Silent prompts (added after the table first shipped).
  const cols = {
    mode: `TEXT NOT NULL DEFAULT 'post'`,
    run_id: 'TEXT',
    running_since: big,
    last_result: 'TEXT',
    last_cost_usd: isPg ? 'DOUBLE PRECISION' : 'REAL',
    last_duration_ms: big,
  };
  const have = isPg
    ? null
    : new Set((await all(`PRAGMA table_info(${TASKS})`)).map((c) => c.name));
  for (const [name, type] of Object.entries(cols)) {
    if (isPg) await run(`ALTER TABLE ${TASKS} ADD COLUMN IF NOT EXISTS ${name} ${type}`);
    else if (!have.has(name)) await run(`ALTER TABLE ${TASKS} ADD COLUMN ${name} ${type}`);
  }
}

// --- Pure helpers (exported for tests) ---------------------------------------

// The next run of a recurring task after `now`. Keeps to the task's grid
// (start + k × interval), and a stretch of downtime is skipped rather than
// replayed as a burst of posts.
export function nextRunAfter(nextRunAt, intervalMin, now) {
  const step = intervalMin * MIN;
  if (nextRunAt > now) return nextRunAt;
  return nextRunAt + (Math.floor((now - nextRunAt) / step) + 1) * step;
}

const SPECIAL = { here: '<!here>', channel: '<!channel>', everyone: '<!everyone>' };
const norm = (s) => String(s || '').toLowerCase().replace(/\s+/g, '');

// `@handle` -> `<@U…>` for every handle that names someone in `users` (a
// users.list). Matches the username, display name or real name, ignoring case
// and spaces; a username wins over someone else's display name. Unknown
// handles are left as typed.
export function resolveMentions(text, users) {
  const byName = new Map();
  const live = (users || []).filter((u) => !u.deleted);
  for (const u of live) {
    for (const n of [u.real_name, u.profile?.real_name, u.profile?.display_name]) {
      if (norm(n) && !byName.has(norm(n))) byName.set(norm(n), u.id);
    }
  }
  for (const u of live) if (u.name) byName.set(norm(u.name), u.id);
  return String(text).replace(/(^|[^<\w])@([A-Za-z0-9][\w.-]*)/g, (m, pre, raw) => {
    const handle = raw.replace(/[.-]+$/, ''); // "@bob." keeps its full stop
    const rest = raw.slice(handle.length);
    const key = handle.toLowerCase();
    if (SPECIAL[key]) return `${pre}${SPECIAL[key]}${rest}`;
    const id = byName.get(key);
    return id ? `${pre}<@${id}>${rest}` : m;
  });
}

// The reverse, for showing a stored task: `<@U…>` -> `@Name`.
export function displayMentions(text, users) {
  const names = new Map((users || []).map((u) => [u.id, u.profile?.display_name || u.real_name || u.name]));
  return String(text)
    .replace(/<@([UW][A-Z0-9]+)>/g, (m, id) => (names.get(id) ? `@${names.get(id)}` : m))
    .replace(/<!(here|channel|everyone)>/g, '@$1');
}

// --- Posting -------------------------------------------------------------------

// Post one task now. Resolves to { ok, ts } or { ok: false, error }.
async function postTask(task) {
  const agent = await getAgentById(task.agent_id);
  if (!agent?.slack_bot_token) return { ok: false, error: 'agent_missing' };
  try {
    const r = await postSlackMessage({ token: agent.slack_bot_token, channel: task.channel_id, text: task.text });
    return r.ok ? { ok: true, ts: r.ts } : { ok: false, error: r.error || 'unknown_error' };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

async function recordResult(task, result, t) {
  const pause = !result.ok && FATAL.has(result.error);
  await run(
    `UPDATE ${TASKS} SET last_run_at = ?, last_ts = ?, last_error = ?${pause ? ', enabled = 0' : ''} WHERE id = ?`,
    [t, result.ok ? result.ts : null, result.ok ? null : result.error, task.id],
  );
  if (!result.ok) {
    console.warn(`[scheduled] task ${task.id} failed: ${result.error}${pause ? ' (paused)' : ''}`);
  }
}

// Hand a silent task's prompt to its agent. Skipped while the previous run is
// still going; an offline agent is a failed run rather than a queued one, so
// a backlog can't build up and replay when it reconnects.
async function promptTask(task, now) {
  const agent = await getAgentById(task.agent_id);
  if (!agent) return recordResult(task, { ok: false, error: 'agent_missing' }, now);
  const since = Number(task.running_since) || 0;
  if (since && now - since < RUN_TIMEOUT_MS) {
    console.log(`[scheduled] task ${task.id}: previous run still going; skipped`);
    return;
  }
  if (!onlineIds().has(agent.id)) return recordResult(task, { ok: false, error: 'agent_offline' }, now);
  const runId = crypto.randomUUID();
  await run(
    `UPDATE ${TASKS} SET run_id = ?, running_since = ?, last_run_at = ?, last_error = NULL, last_ts = NULL
      WHERE id = ?`,
    [runId, now, now, task.id],
  );
  const ev = await appendEvent(agent.id, 'scheduled_prompt', { task_id: task.id, run_id: runId, text: task.text });
  pushEvent(agent.id, ev);
}

async function deliver(task, now) {
  if (task.mode === 'prompt') return promptTask(task, now);
  return recordResult(task, await postTask(task), now);
}

// Post every task that is due. Each one is claimed first by moving its
// next_run_at (or switching a one-off task off) with a conditional UPDATE, so
// a slow tick, an overlapping tick or a second instance can't post it twice.
export async function runDueTasks(now = Date.now()) {
  const due = await all(
    `SELECT * FROM ${TASKS} WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at LIMIT 50`,
    [now],
  );
  for (const task of due) {
    const iv = Number(task.interval_min);
    const was = Number(task.next_run_at);
    const claim = iv
      ? await run(`UPDATE ${TASKS} SET next_run_at = ? WHERE id = ? AND enabled = 1 AND next_run_at = ?`,
        [nextRunAfter(was, iv, now), task.id, was])
      : await run(`UPDATE ${TASKS} SET enabled = 0 WHERE id = ? AND enabled = 1 AND next_run_at = ?`,
        [task.id, was]);
    if (!claim.changes) continue;
    await deliver(task, now);
  }
}

export function startScheduler() {
  let stopped = false;
  let timer = null;
  const tick = async () => {
    if (stopped) return;
    try {
      await runDueTasks();
    } catch (e) {
      console.error('[scheduled] tick failed:', e);
    }
    if (!stopped) timer = setTimeout(tick, TICK_MS);
  };
  timer = setTimeout(tick, TICK_MS);
  console.log(`[scheduled] scheduler started: tick=${TICK_MS / 1000}s`);
  return {
    stop() {
      stopped = true;
      clearTimeout(timer);
    },
  };
}

// --- Dashboard -----------------------------------------------------------------

export const scheduledRouter = express.Router();

// Express 4 doesn't catch async rejections; route them to the error handler.
const h = (fn) => (req, res, next) => fn(req, res).catch(next);

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

const scheduleLabel = (iv) => (iv ? `every ${iv} min` : 'once');

const ERRORS = {
  agent: 'Pick an agent.',
  channel: 'Pick a channel the bot is in.',
  text: `Write a message (up to ${MAX_TEXT} characters).`,
  interval: 'Pick a schedule.',
  when: 'A one-time task needs a time in the future.',
  slack: 'Slack didn’t answer. Try again in a moment.',
  scope: 'That bot’s Slack app is missing a permission. Reinstall it from the dashboard, then try again.',
};

// The workspace's agents that can post (installed, with a bot token), each
// with the channels it is a member of. A bot whose lists fail to load is
// still shown, with no channels, and the error. `missingScopes` are channel
// permissions its install predates; reinstalling the app grants them.
async function postingAgents(teamId) {
  const out = [];
  for (const a of await listAgentsByTeam(teamId)) {
    const full = await getAgentById(a.id);
    if (!full?.slack_bot_token) continue;
    let channels = [];
    let missingScopes = [];
    let error = null;
    try {
      const r = await botChannels(full.slack_bot_token);
      channels = r.channels.filter((c) => c.isMember);
      missingScopes = r.missingScopes;
    } catch (e) {
      error = e.message;
    }
    out.push({ id: a.id, name: a.name, token: full.slack_bot_token, channels, missingScopes, error });
  }
  return out;
}

// Load a task and check it belongs to the signed-in workspace.
async function ownTask(req) {
  const sess = getSession(req);
  if (!sess) return { sess: null };
  const task = await get(`SELECT * FROM ${TASKS} WHERE id = ?`, [req.params.id]);
  return { sess, task: task && task.team_id === sess.teamId ? task : null };
}

scheduledRouter.get('/dashboard/scheduled', h(async (req, res) => {
  const sess = getSession(req);
  if (!sess) return res.redirect('/login');
  const agents = await postingAgents(sess.teamId);
  const tasks = await all(
    `SELECT * FROM ${TASKS} WHERE team_id = ? ORDER BY created_at DESC`,
    [sess.teamId],
  );
  // Names for mentions and creators, via any bot in the workspace.
  let users = [];
  if (agents[0]) {
    try {
      users = await workspaceUsers(agents[0].token);
    } catch (e) {
      console.warn('[scheduled] users.list failed:', e.message);
    }
  }
  const agentName = new Map(agents.map((a) => [a.id, a.name]));
  const personName = async (id) =>
    (id && agents[0] && (await userName(agents[0].token, id))) || id || 'someone';

  const fieldStyle =
    'font-size:14px;padding:6px 8px;border:1px solid #ccc;border-radius:6px';
  const btnStyle =
    'padding:8px 14px;border:0;border-radius:6px;background:#4A154B;color:#fff;cursor:pointer';
  const linkBtn = (color = '#4A154B') =>
    `border:0;background:none;color:${color};text-decoration:underline;cursor:pointer;padding:0;font-size:inherit`;
  const cardStyle = 'border:1px solid #e5e5e5;border-radius:10px;padding:14px 16px;margin:12px 0';
  const time = (ts) => `<time data-ts="${Number(ts)}">${new Date(Number(ts)).toISOString()}</time>`;
  const action = (task, path, label, color) => `
    <form method="post" action="/dashboard/scheduled/${escapeHtml(task.id)}/${path}" style="display:inline;margin-right:10px">
      <button type="submit" style="${linkBtn(color)}">${label}</button>
    </form>`;

  const rows = [];
  for (const t of tasks) {
    const iv = Number(t.interval_min);
    const on = Number(t.enabled);
    const sent = !iv && !on && t.last_run_at && !t.last_error;
    const state = on
      ? `next ${time(t.next_run_at)}`
      : sent ? 'sent' : '<span style="color:#a60">paused</span>';
    const silent = t.mode === 'prompt';
    const running = silent && Number(t.running_since) && Date.now() - Number(t.running_since) < RUN_TIMEOUT_MS;
    const outcome = t.last_error
      ? `<span style="color:#a11">✗ ${escapeHtml(t.last_error)}</span>`
      : running ? '<span style="color:#666">running…</span>' : '<span style="color:#161">✓</span>';
    const stats = silent && !running && t.last_cost_usd != null
      ? ` · $${Number(t.last_cost_usd).toFixed(2)}${t.last_duration_ms ? `, ${Math.round(Number(t.last_duration_ms) / 1000)}s` : ''}`
      : '';
    const last = t.last_run_at ? `last ${time(t.last_run_at)} ${outcome}${stats}` : 'not run yet';
    const target = silent
      ? '<span style="color:#666">· silent prompt</span>'
      : `→ <b>#${escapeHtml(t.channel_name || t.channel_id)}</b>`;
    const reply = silent && !running && t.last_result
      ? `<details style="margin:0 0 6px;font-size:13px"><summary style="cursor:pointer;color:#4A154B">last reply</summary>
          <div style="white-space:pre-wrap;border-left:3px solid #e5e5e5;padding:4px 10px;margin-top:4px">${escapeHtml(t.last_result)}</div></details>`
      : '';
    rows.push(`<div style="${cardStyle}">
      <p style="margin:0 0 6px"><b>${escapeHtml(agentName.get(t.agent_id) || 'missing bot')}</b>
        ${target} · ${scheduleLabel(iv)} · ${state}</p>
      <div style="white-space:pre-wrap;background:#f6f6f6;border-radius:6px;padding:8px 10px;margin:0 0 6px">${escapeHtml(silent ? t.text : displayMentions(t.text, users))}</div>
      <p style="margin:0 0 6px;font-size:13px;color:#666">${last} · added by ${escapeHtml(await personName(t.created_by))}</p>
      ${reply}
      <div style="font-size:13px">
        ${action(t, 'send-now', silent ? 'run now' : 'send now')}
        ${sent ? '' : action(t, 'toggle', on ? 'pause' : 'resume')}
        ${action(t, 'delete', 'delete', '#a11')}
      </div>
    </div>`);
  }

  const agentOptions = agents
    .map((a) => `<option value="${escapeHtml(a.id)}">${escapeHtml(a.name)}</option>`)
    .join('');
  // One channel list per bot; the form shows (and submits) the chosen bot's.
  const channelSelects = agents
    .map((a, i) => `<select name="channel" data-agent="${escapeHtml(a.id)}" ${i ? 'disabled style="display:none;' : 'style="'}${fieldStyle};width:100%">
      ${a.channels.map((c) => `<option value="${escapeHtml(c.id)}">${c.isPrivate ? '🔒' : '#'}${escapeHtml(c.name)}</option>`).join('')
      || `<option value="">${a.error ? `couldn't list channels: ${escapeHtml(a.error)}` : 'not in any channel yet'}</option>`}
    </select>`)
    .join('');
  // Shown under the channel list for a bot whose Slack install is missing a
  // channel permission.
  const scopeNotes = agents
    .filter((a) => a.missingScopes.length)
    .map((a) => `<p data-agent-note="${escapeHtml(a.id)}" style="display:none;margin:4px 0 0;color:#a60;font-size:13px">
      ${escapeHtml(a.name)}'s Slack app is missing ${escapeHtml(a.missingScopes.join(', '))}, so
      ${a.missingScopes.length > 1 ? 'no channels' : a.missingScopes.join('').includes('groups') ? 'private channels' : 'public channels'}
      can’t be listed. <a href="/dashboard">Reinstall it from the dashboard</a> to fix this.</p>`)
    .join('');
  const intervalOptions = [0, ...INTERVALS]
    .map((iv) => `<option value="${iv}">${iv ? `Every ${iv} minutes` : 'Once'}</option>`)
    .join('');

  const form = agents.length
    ? `<form id="new" method="post" action="/dashboard/scheduled" style="${cardStyle};display:flex;flex-direction:column;gap:10px;font-size:14px">
  <label>Agent<br><select name="agentId" id="agent" style="${fieldStyle};width:100%">${agentOptions}</select></label>
  <div>
    <label style="display:block"><input type="radio" name="mode" value="post" checked> Post a message in a channel, as this agent</label>
    <label style="display:block"><input type="radio" name="mode" value="prompt"> Prompt this agent silently <span style="color:#999">— nothing is posted; its reply shows on the task below</span></label>
  </div>
  <div id="postFields">
  <label>Channel <span style="color:#999">— only channels the bot is in; /invite it to add one</span><br>${channelSelects}</label>${scopeNotes}
  </div>
  <label><span id="textLabel">Message <span style="color:#999">— @name mentions a person or agent</span></span><br>
    <textarea name="text" rows="3" maxlength="${MAX_TEXT}" required style="${fieldStyle};width:100%;box-sizing:border-box;font-family:inherit"></textarea></label>
  <div style="display:flex;gap:10px;flex-wrap:wrap">
    <label>Schedule<br><select name="interval" id="interval" style="${fieldStyle}">${intervalOptions}</select></label>
    <label><span id="whenLabel">At</span><br><input type="datetime-local" id="when" style="${fieldStyle}"></label>
  </div>
  <p id="whenHint" style="margin:0;color:#666;font-size:13px"></p>
  <p id="costHint" style="display:none;margin:0;color:#a60;font-size:13px">Each run is a full agent turn in a fresh conversation, and can cost a dollar or more if the agent does a lot. Check the cost on the task after its first run before choosing a short interval.</p>
  <input type="hidden" name="runAt" id="runAt">
  <div><button type="submit" style="${btnStyle}">Add scheduled task</button></div>
</form>`
    : '<p>No agent in this workspace can post yet. Install one from the <a href="/dashboard">dashboard</a> first.</p>';

  const err = ERRORS[req.query.err];
  res.type('html').send(`<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scheduled tasks — ${escapeHtml(sess.teamName || sess.teamId)}</title>
<body style="font-family:system-ui;max-width:680px;margin:40px auto;padding:0 16px;line-height:1.5">
<p style="color:#666"><a href="/dashboard">← dashboard</a> · Signed in as ${escapeHtml(sess.name || 'you')}</p>
<h1>⏰ Scheduled tasks</h1>
<p style="color:#666">Once or on a repeating schedule, have one of ${escapeHtml(sess.teamName || 'this workspace')}'s agents post a message to a channel (mentioning an agent wakes it), or prompt an agent silently without posting anything.</p>
${err ? `<p style="background:#fdeaea;color:#a11;border:1px solid #f3caca;border-radius:6px;padding:8px 12px">${escapeHtml(err)}</p>` : ''}
${form}
<h3>Tasks</h3>
${rows.join('') || '<p style="color:#999">None yet.</p>'}
<script>
document.querySelectorAll('time[data-ts]').forEach(function (el) {
  el.textContent = new Date(Number(el.dataset.ts)).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
});
var form = document.getElementById('new');
if (form) {
  var agent = document.getElementById('agent');
  var interval = document.getElementById('interval');
  var when = document.getElementById('when');
  var showChannels = function () {
    document.querySelectorAll('select[data-agent]').forEach(function (s) {
      var mine = s.dataset.agent === agent.value;
      s.disabled = !mine;
      s.style.display = mine ? '' : 'none';
    });
    document.querySelectorAll('[data-agent-note]').forEach(function (p) {
      p.style.display = p.dataset.agentNote === agent.value ? '' : 'none';
    });
  };
  var showWhen = function () {
    var once = interval.value === '0';
    document.getElementById('whenLabel').textContent = once ? 'At' : 'Starting';
    when.required = once;
    document.getElementById('whenHint').textContent = once
      ? 'Runs once at this time.'
      : 'Leave blank to start one interval from now. Use "send now" / "run now" on the task to go right away.';
  };
  var showMode = function () {
    var silent = form.querySelector('input[name=mode]:checked').value === 'prompt';
    document.getElementById('postFields').style.display = silent ? 'none' : '';
    document.getElementById('costHint').style.display = silent ? '' : 'none';
    document.getElementById('textLabel').textContent = silent ? 'Prompt' : 'Message — @name mentions a person or agent';
    // A silent task has no channel: don't submit (or require) one.
    document.querySelectorAll('select[data-agent]').forEach(function (s) {
      s.disabled = silent || s.dataset.agent !== agent.value;
    });
  };
  agent.addEventListener('change', function () { showChannels(); showMode(); });
  interval.addEventListener('change', showWhen);
  form.querySelectorAll('input[name=mode]').forEach(function (r) { r.addEventListener('change', showMode); });
  showChannels();
  showWhen();
  showMode();
  form.addEventListener('submit', function () {
    document.getElementById('runAt').value = when.value ? String(new Date(when.value).getTime()) : '';
  });
}
</script>
</body>`);
}));

scheduledRouter.post('/dashboard/scheduled', h(async (req, res) => {
  const sess = getSession(req);
  if (!sess) return res.redirect('/login');
  const fail = (code) => res.redirect(`/dashboard/scheduled?err=${code}`);
  const { agentId, channel } = req.body || {};
  const agent = agentId ? await getAgentById(agentId) : null;
  if (!agent || agent.slack_team_id !== sess.teamId || !agent.slack_bot_token) return fail('agent');
  const text = String(req.body?.text || '').trim();
  if (!text || text.length > MAX_TEXT) return fail('text');
  const iv = Number(req.body?.interval);
  if (!(iv === 0 || INTERVALS.includes(iv))) return fail('interval');
  const now = Date.now();
  const runAt = Number(req.body?.runAt) || 0;
  if (!iv && runAt < now - MIN) return fail('when');
  const firstRun = iv ? (runAt > now ? runAt : now + iv * MIN) : Math.max(runAt, now);

  if (req.body?.mode === 'prompt') {
    await run(
      `INSERT INTO ${TASKS} (id, team_id, agent_id, channel_id, text, interval_min,
                             next_run_at, enabled, created_by, created_at, mode)
       VALUES (?, ?, ?, '', ?, ?, ?, 1, ?, ?, 'prompt')`,
      [crypto.randomUUID(), sess.teamId, agent.id, text, iv, firstRun, sess.userId || null, now],
    );
    return res.redirect('/dashboard/scheduled');
  }

  let ch;
  let users;
  try {
    ch = (await botChannels(agent.slack_bot_token)).channels.find((c) => c.id === channel && c.isMember);
    users = await workspaceUsers(agent.slack_bot_token);
  } catch (e) {
    console.warn('[scheduled] Slack lookup failed:', e.message);
    return fail(e.slackError === 'missing_scope' ? 'scope' : 'slack');
  }
  if (!ch) return fail('channel');

  await run(
    `INSERT INTO ${TASKS} (id, team_id, agent_id, channel_id, channel_name, text, interval_min,
                           next_run_at, enabled, created_by, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)`,
    [
      crypto.randomUUID(), sess.teamId, agent.id, ch.id, ch.name, resolveMentions(text, users), iv,
      firstRun, sess.userId || null, now,
    ],
  );
  res.redirect('/dashboard/scheduled');
}));

// Post (or prompt) now, off schedule. The schedule itself is left as it was.
scheduledRouter.post('/dashboard/scheduled/:id/send-now', h(async (req, res) => {
  const { sess, task } = await ownTask(req);
  if (!sess) return res.redirect('/login');
  if (!task) return res.status(404).send('not found');
  await deliver(task, Date.now());
  res.redirect('/dashboard/scheduled');
}));

// Pause, or resume at the next slot on the task's grid (no catch-up posts).
scheduledRouter.post('/dashboard/scheduled/:id/toggle', h(async (req, res) => {
  const { sess, task } = await ownTask(req);
  if (!sess) return res.redirect('/login');
  if (!task) return res.status(404).send('not found');
  if (Number(task.enabled)) {
    await run(`UPDATE ${TASKS} SET enabled = 0 WHERE id = ?`, [task.id]);
  } else {
    const iv = Number(task.interval_min);
    const now = Date.now();
    const next = iv ? nextRunAfter(Number(task.next_run_at), iv, now) : Math.max(Number(task.next_run_at), now);
    await run(`UPDATE ${TASKS} SET enabled = 1, next_run_at = ?, last_error = NULL WHERE id = ?`, [next, task.id]);
  }
  res.redirect('/dashboard/scheduled');
}));

scheduledRouter.post('/dashboard/scheduled/:id/delete', h(async (req, res) => {
  const { sess, task } = await ownTask(req);
  if (!sess) return res.redirect('/login');
  if (!task) return res.status(404).send('not found');
  await run(`DELETE FROM ${TASKS} WHERE id = ?`, [task.id]);
  res.redirect('/dashboard/scheduled');
}));

// Agent bridge → how a silent run went. Authenticated by the agent's
// registration token; only the task's own agent may report, and only for the
// run it was handed (a late report for an older run is ignored).
scheduledRouter.post('/api/task-result', h(async (req, res) => {
  const b = req.body || {};
  const agent = b.token ? await getAgentByToken(String(b.token)) : null;
  if (!agent) return res.status(401).json({ error: 'invalid_token' });
  const task = b.task_id ? await get(`SELECT * FROM ${TASKS} WHERE id = ?`, [String(b.task_id)]) : null;
  if (!task || task.agent_id !== agent.id) return res.status(404).json({ error: 'not_found' });
  const num = (v) => (Number.isFinite(Number(v)) && v !== null && v !== '' ? Number(v) : null);
  const r = await run(
    `UPDATE ${TASKS} SET running_since = NULL, last_result = ?, last_cost_usd = ?, last_duration_ms = ?,
                         last_error = ?
      WHERE id = ? AND run_id = ?`,
    [
      String(b.text || '').slice(0, MAX_TEXT), num(b.cost_usd), num(b.duration_ms),
      b.ok === false ? String(b.error || 'failed').slice(0, 500) : null,
      task.id, String(b.run_id || ''),
    ],
  );
  if (!r.changes) return res.status(409).json({ error: 'stale_run' });
  res.json({ ok: true });
}));
