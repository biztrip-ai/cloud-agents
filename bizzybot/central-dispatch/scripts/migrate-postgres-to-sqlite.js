// One-time copy of Central-Dispatch's tables from Postgres (Neon) into a fresh
// SQLite file. The Postgres URL comes on stdin so it never lands in argv,
// `ps` or shell history:
//
//   <print DATABASE_URL> | DB_PATH=/var/lib/central-dispatch/central-dispatch.db \
//     DB_SCHEMA=claudebot node scripts/migrate-postgres-to-sqlite.js
//
// The SQLite tables are created by the app's own init code, then every row is
// copied, column by column where both sides have the column. Refuses to touch
// a table that already has rows. Prints row counts per table.
import pg from 'pg';

const sourceUrl = (await new Promise((resolve) => {
  let s = '';
  process.stdin.on('data', (d) => (s += d)).on('end', () => resolve(s));
})).trim();
if (!sourceUrl) {
  console.error('expected the Postgres URL on stdin');
  process.exit(1);
}
const schema = process.env.DB_SCHEMA || 'claudebot';

// The app picks its backend at import time: make sure it chooses SQLite.
delete process.env.DATABASE_URL;
const { init, all, run } = await import('../src/db.js');
const { initDropboxTable } = await import('../src/dropbox.js');
const { initScheduledTasksTable } = await import('../src/scheduled_tasks.js');
await init();
await initDropboxTable();
await initScheduledTasksTable();

pg.types.setTypeParser(20, (v) => parseInt(v, 10)); // bigint -> number, as the app does
const ssl = /neon\.tech|sslmode=require/.test(sourceUrl) ? { rejectUnauthorized: false } : undefined;
const source = new pg.Client({ connectionString: sourceUrl, ssl });
await source.connect();

const tables = (await all(
  `SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name`,
)).map((r) => r.name);

let mismatch = false;
for (const table of tables) {
  const pgCols = (await source.query(
    `SELECT column_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2`,
    [schema, table],
  )).rows.map((r) => r.column_name);
  if (!pgCols.length) {
    console.log(`${table}: not in Postgres, skipped`);
    continue;
  }
  const liteCols = (await all(`PRAGMA table_info(${table})`)).map((c) => c.name);
  const cols = liteCols.filter((c) => pgCols.includes(c));
  const dropped = pgCols.filter((c) => !liteCols.includes(c));

  const [{ n: existing }] = await all(`SELECT COUNT(*) AS n FROM ${table}`);
  if (existing > 0) {
    console.error(`${table}: already has ${existing} rows in SQLite; refusing to copy into it`);
    process.exit(1);
  }

  const rows = (await source.query(`SELECT ${cols.join(', ')} FROM ${schema}.${table}`)).rows;
  const insert = `INSERT INTO ${table} (${cols.join(', ')}) VALUES (${cols.map(() => '?').join(', ')})`;
  await run('BEGIN');
  for (const row of rows) await run(insert, cols.map((c) => row[c] ?? null));
  await run('COMMIT');

  const [{ n: copied }] = await all(`SELECT COUNT(*) AS n FROM ${table}`);
  if (copied !== rows.length) mismatch = true;
  console.log(
    `${table}: ${rows.length} in Postgres, ${copied} in SQLite` +
      (dropped.length ? ` (columns not in SQLite, dropped: ${dropped.join(', ')})` : ''),
  );
}

await source.end();
process.exit(mismatch ? 1 : 0);
