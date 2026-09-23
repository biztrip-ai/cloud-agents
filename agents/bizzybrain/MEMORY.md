# BizzyBrain: memory

BizzyBrain's memory is an **Obsidian vault**: a folder of plain Markdown
files with YAML frontmatter, `[[wikilinks]]` and `#tags`, laid out so that
anyone can open it in Obsidian and read, search and graph what the agent
knows. The agent reads and writes the files directly; the format is plain
text, so the notes never depend on Obsidian. Obsidian also runs on the box,
headless, so the agent can query the vault with `obsidian-cli` (see "Using
obsidian-cli").

The vault is `/workspaces/bizzybrain/vault/`. Everything BizzyBrain remembers
lives there. The raw Slack log does **not**: it stays in
`/workspaces/bizzybrain/log/` (see the protocol), because the vault may be
copied off the box and the log may hold things from the "never record" list.

## Layout

```
vault/
  Home.md               map of content: a link to every note, grouped by folder
  reflections.md        running journal of what is going on (see the protocol)
  People.base           Obsidian table views over people/ (see "People and companies")
  Companies.base        Obsidian table views over companies/
  topics/               one note per subject: pricing.md, staging.md, btdash-architecture.md
  people/               one note per person BizTrip works with, any group: Freddy Shim.md
  companies/            one note per outside company: customer, prospect, partner, vendor, investor
  projects/             one note per initiative: Investor Deck.md, Sabre Migration.md
  .obsidian/            Obsidian's settings; app.json must keep "alwaysUpdateLinks": true; otherwise never edit
```

Rules for the layout:

- **One note per real thing**, named the way people say it. `people/Freddy
  Shim.md`, not `people/fs.md`. Topic and project notes are lowercase-hyphen
  or Title Case, whichever reads better; be consistent within a folder.
- **Don't invent folders.** If something fits none of these, it is a topic.
- **`Home.md` lists every note.** When you create a note, add a `[[link]]`
  under the right heading in `Home.md`. This is the first thing you read when
  answering a question, so keep it current and keep it to links with a
  few-word gloss each.
- **Don't rename or move notes** without a reason. When you must, use
  `obsidian-cli rename` or `move`, which update every `[[link]]` to the
  note (the vault's `.obsidian/app.json` must have
  `"alwaysUpdateLinks": true`, or they won't), then check `obsidian-cli unresolved` reports nothing. Never rename
  with `mv`.

## Note format

```markdown
---
type: company             # topic | person | company | project
relationship: [customer]  # companies only; see "People and companies"
created: 2026-09-14
updated: 2026-09-23
aliases: [Acme Corp, ACME]
tags: [customer, enterprise]
---

Acme is a 200-seat enterprise prospect, owned by [[Scott Persinger]].

## Facts

- Renewal moved to Q1 2027. — #sales, 2026-09-23
- Pilot uses the [[btdash]] dashboard with SSO. — mail from Scott, 2026-09-20
- Contract summary is in doc "Acme MSA summary". — doc "Acme MSA summary", 2026-09-18
```

- **Frontmatter** carries `type`, `created`, `updated`, optional `aliases`
  (other names people use) and `tags`, plus the fields for people and
  companies below. Bump `updated` whenever the body changes.
- **First line is a one-sentence summary** of what the thing is, so a note
  answers "what is this?" before anyone reads the bullets.
- **Facts are bullets under `## Facts`**, one fact each, with the source and
  date at the end after an em dash, exactly as the protocol specifies. A note
  may have further headings (`## Timeline`, `## Open questions`) when they
  help; bullets under them carry citations too.
- **Wikilinks** connect notes: `[[Freddy Shim]]`, `[[Acme]]`,
  `[[btdash-architecture]]`. Link a name the first time it appears in a note.
  Only link to a note that exists or that you are creating in the same turn
  with at least one cited fact; a dangling link is a promise you didn't keep.
- **Tags** use a small vocabulary in frontmatter, lowercase: `customer`,
  `prospect`, `partner`, `vendor`, `investor`, `product`, `infra`, `sales`,
  `finance`, `hiring`, `process`, `decision`. Add a new tag only when none of these fit, and use
  it consistently after that. Inline `#tags` in the body are fine but the
  frontmatter list is the one you maintain.
- **Dates are ISO** (`2026-09-23`) everywhere.

## People and companies

The people inventory is **one list**: every person is a note in `people/`,
whatever their relationship to BizTrip, and a `group` field says which kind
they are. Moving from prospect to customer, or contractor to employee, is a
one-field edit, and links to the note never break. `People.base` shows the
list as tables (active people by group, the team, the board, customers and
prospects by company, partners/vendors/investors, former); it reads the
frontmatter, so keep the fields exact.

```markdown
---
type: person
group: customer           # employee | contractor | board | partner | vendor | investor | customer | prospect
company: "[[Acme]]"
title: VP Travel
email: jane@acme.com
phone: +1 415 555 0100
aliases: [Jane]
created: 2026-09-23
updated: 2026-09-23
tags: [customer]
---

Jane Doe runs corporate travel at [[Acme]] and is our main contact there.

## Facts

- Email, title and phone from her mail signature. — mail from Scott, 2026-09-23
```

- **`group`** is BizTrip's relationship with the person, one value:
  `employee`, `contractor`, `board` (an outside director with no other
  role), `partner`, `vendor`, `investor`, `customer` or `prospect`. For
  people at another company it matches the company's `relationship`; when
  the company has several, use the one this person deals with us on.
- **`board: true`** marks a director, whatever their group: a founder on the
  board is `group: employee` with `board: true`. The board's membership is
  also listed in `topics/Board of Directors.md`.
- **`status: former`** marks someone who no longer works with BizTrip. Keep
  the note and its history; add a dated fact saying so. Omit `status` for
  anyone active. Record a departure only once it has been announced or Scott
  says so (the protocol's employment rule).
- **`company`** is a `[[link]]` to the company's note for partners, customers
  and prospects. Employees have `company: BizTrip AI` as plain text;
  contractors have their own firm as plain text, or nothing.
- **`title`, `email`, `phone`** are work details only (the protocol's
  "Never record" list). Leave a field out rather than guess, and cite where
  each value came from in a `## Facts` bullet, like any other fact.
- **File people under the company BizTrip deals with.** Someone from a
  third firm who works on a partner's behalf (a partner's IT vendor, say)
  gets that partner as `company`; their own firm shows only in their email
  and summary line. Don't create a company note for a firm BizTrip has no
  relationship with.
- **One note per person across sources.** Before creating one, grep
  `people/` for their email and name; Slack display names, mail senders and
  Jira users are often the same person.

Every company is a note in `companies/`, named the way people say it:

```markdown
---
type: company
relationship: [prospect]  # prospect | customer | partner | vendor | investor
domain: acme.com
created: 2026-09-23
updated: 2026-09-23
aliases: [Acme Corp]
tags: [prospect]
---

Acme is a 200-seat enterprise travel buyer we are piloting with.
```

- **`relationship`** is a list, since a company can be more than one:
  `prospect`, `customer`, `partner` (works with us on the product or
  channel, e.g. a TMC or GDS), `vendor` (we pay them for a service, e.g. a PR
  agency) and `investor`. A company that both invests and partners is
  `[investor, partner]`. When a prospect signs, change the list, add a dated
  fact saying so, and update the `group` of its people. Say what kind of
  partner or vendor in the summary line ("BizTrip's TMC partner", "our PR
  agency").
- **`domain`** is the company's mail domain, so a sender can be matched to
  a company.
- BizTrip itself has no company note. The vault is about BizTrip, so
  everything that isn't another company is BizTrip's.
- `Companies.base` has a view per relationship (customers, prospects,
  partners and vendors, investors), with a count of the people linked to
  each.

## Reading

On a direct question:

1. Read `Home.md` to see what notes exist.
2. Open the notes the question touches, and follow their `[[links]]` one hop
   when needed; `obsidian-cli backlinks` shows what links *to* a note.
3. `obsidian-cli search` (or `grep -ril`) the vault for names or terms that
   `Home.md` didn't surface.
4. For list questions ("who's on the board?", "which prospects do we
   have?"), query a Base view instead of reading every note.
5. Read `reflections.md` for the pattern behind the facts.

Answer from what you found, with citations; if the vault has nothing, say so.
The boundary rule from the protocol applies to every note: a fact from a
private channel is only used where the asker could have seen it.

## Writing

On a passive turn, or when a direct conversation surfaces something durable:

1. Find the note the fact belongs to. Prefer updating an existing note over
   creating one; a new note needs a real thing behind it and at least one
   cited fact.
2. Add or correct the bullet. Corrections edit in place and note the change
   date: `— #eng, 2026-09-14; corrected 2026-09-23`.
3. Bump `updated`. If the note is new, add it to `Home.md`.
4. Reflections go in `reflections.md` per the protocol.

The protocol's "Never record" list applies to every file in the vault,
frontmatter included. A person's note holds their work contact details, their
role, what they own and what they said about the work, and nothing about them
as a private individual.

## Using obsidian-cli

`obsidian-cli` talks to the Obsidian app running on the box
(`obsidian.service`). Run it from the vault directory so it picks this
vault. The useful commands:

```sh
cd /workspaces/bizzybrain/vault
obsidian-cli search query="Sabre queue" [path=people] [limit=20]
obsidian-cli backlinks file="Cain Travel" [counts]
obsidian-cli links file="Moderna"
obsidian-cli base:query path=People.base view="Board" format=md
obsidian-cli base:query path=Companies.base view="Prospects" format=md
obsidian-cli unresolved                 # dangling [[links]]: should be none
obsidian-cli orphans                    # notes nothing links to
obsidian-cli rename path="people/Jane.md" name="Jane Doe"
```

- **Write notes with ordinary file edits**, as above. The CLI is for
  querying, and for renames and moves so links stay intact.
- After a batch of writes, `obsidian-cli unresolved` is a quick check that
  every link you added points at a note.
- If the CLI prints "unable to find Obsidian", run
  `systemctl is-active obsidian.service`; the vault files are still fine
  to read and write directly, so fall back to `grep`.

## Operating the vault (for humans)

- **Open it in Obsidian.** Copy the folder to your machine and open it as a
  vault:

  ```sh
  bin/agents ssh biztrip BizzyBrain 'tar -C /workspaces/bizzybrain -cz vault' | tar -C ~/BizzyBrain -xz
  ```

  Obsidian creates `.obsidian/` on first open; that folder is harmless if it
  gets copied back.
- **Bootstrapping.** On a fresh box, create the vault with `Home.md` holding
  one heading per folder (Topics, People, Companies, Projects), the two
  `.base` files embedded under People and Companies, and an empty
  `reflections.md`. Moving an older `memory/` directory in: topic files go to
  `vault/topics/`, `reflections.md` to the vault root, and each moved file
  gets frontmatter and a `Home.md` entry the next time the agent touches it.
- **Obsidian on the box.** The app runs headless so the agent can use the
  CLI. To set it up on a fresh Ubuntu box, as `ubuntu`:

  ```sh
  curl -fsSL -o /tmp/obsidian.deb https://github.com/obsidianmd/obsidian-releases/releases/download/v1.13.7/obsidian_1.13.7_amd64.deb
  sudo apt-get install -y xvfb /tmp/obsidian.deb
  # Register the vault and enable the CLI (Settings > General > Command line interface).
  mkdir -p ~/.config/obsidian
  echo '{"vaults":{"'$(openssl rand -hex 8)'":{"path":"/workspaces/bizzybrain/vault","ts":'$(date +%s000)',"open":true}},"cli":true}' > ~/.config/obsidian/obsidian.json
  # Let renames rewrite links (Settings > Files and links > Automatically update internal links).
  mkdir -p /workspaces/bizzybrain/vault/.obsidian
  echo '{"alwaysUpdateLinks":true}' > /workspaces/bizzybrain/vault/.obsidian/app.json
  ```

  Then add `/etc/systemd/system/obsidian.service` (`User=ubuntu`,
  `Environment=HOME=/home/ubuntu`, `ExecStart=/usr/bin/xvfb-run
  --server-num=99 --server-args="-screen 0 1280x800x24 -nolisten tcp"
  /opt/Obsidian/obsidian --disable-gpu`, `Restart=on-failure`) and enable
  it. Last, install `/usr/local/bin/obsidian-cli` as a wrapper that runs
  `env -u XDG_RUNTIME_DIR /opt/Obsidian/obsidian-cli "$@"`. The service has
  no `XDG_RUNTIME_DIR`, so Obsidian listens on `~/.obsidian-cli.sock`, and
  an SSH shell would otherwise look in `/run/user/<uid>`.
- **Sync is a later decision.** The vault is a plain folder, so a git repo
  with a deploy key, Syncthing or Obsidian Sync would all work. The box has no
  GitHub access on purpose, so a git-backed vault means a deploy key scoped
  to one private repo, not the agent's own credentials.
