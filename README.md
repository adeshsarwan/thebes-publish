# Finance Hub — Pipeline, Newsroom & Site Contract

Three systems, one PostgreSQL database, three schemas:

```
 PIPELINE (python -m pipeline.cli)     NEWSROOM (python -m newsroom)              PUBLIC WEBSITE (later, Next.js)
 schema: pipeline                      schema: newsroom                           reads schema: site — nothing else
 ─────────────────────────────         ──────────────────────────────────         ──────────────────────────────
 RSS ingest → cluster → AI desk  ──▶   AI draft → (copywriter) → editor     ──▶   site.published_stories
 scoring → AI draft + guardrails       approves → publisher publishes             site.authors
                                       (every edit = a new version + audit)
```

| DB role | Can do | Used by |
|---|---|---|
| `story_pipeline` | read/write `pipeline`; create AI drafts in `newsroom` | `python -m pipeline.cli` |
| `story_newsroom` | read `pipeline`; read/write `newsroom`; write `site` (publish) | `python -m newsroom` |
| `story_site` | **read-only** `site` | the public website |

Postgres itself enforces this: the website login cannot see drafts, and the pipeline cannot publish.

## Setup (macOS)

```bash
cd ~/Projects/finance-ingest && bash setup_mac.sh
```

The script installs Homebrew PostgreSQL if needed and creates the `financehub` database, the schemas and the three logins. It writes their connection URLs into `.env`, seeds placeholder staff, imports the first-version SQLite data once, and runs the tests on SQLite and Postgres. It is safe to re-run.

**On a VPS:** install PostgreSQL, create the database, set `ADMIN_DATABASE_URL` in `.env`, then run `python -m pipeline.cli db-setup --write-env` and `python -m newsroom seed-users`. Set `APP_ENV=production` there: publishing under a placeholder profile is then refused.

## Daily use

```bash
source .venv/bin/activate
python -m newsroom serve                # newsroom desk at http://localhost:8765 (sign in)
python -m newsroom pull                 # fetch, score, draft and auto-publish now (same as the desk button)
```

### Signing in and the team
Run `python -m newsroom create-admin` once; setup_mac.sh asks for it. In the desk, admins use **Team** to add copy editors, editors, publishers and other admins. Each new person gets a temporary password, shown once, and must change it at first sign-in. Admins can also change roles, reset passwords and deactivate accounts, and the last admin can't be removed. Passwords are hashed with scrypt, sessions last 7 days, and repeated failed sign-ins are blocked for 15 minutes.

### Auto-publish
After each pull, AI drafts are published automatically when they meet all of these conditions:
- the desk impact score is **above `autopublish.score_above`** (60);
- the draft passed every guardrail;
- no one has touched it yet.

These stories carry an **Auto-published** badge. On the site they show "Finance Hub newsroom · Responsible editor: …" (`autopublish.responsible_editor`), with a disclosure that no editor reviewed them. To roll one back, an editor clicks **Unpublish**, then **Assign to copy editor**. After it's edited, approved and published again, it shows the approving editor's byline and the "reviewed and edited by" disclosure. Switch auto-publish off with `autopublish.enabled: false` in `config/editorial.yaml`.

### Sources & pulls page
- **Editors add RSS feeds** with "Test feed" or "Test and add". The feed is fetched and parsed before it's saved. Addresses on private or local networks are refused.
- Editors **switch sources on or off**. `config/feeds.yaml` only seeds the starting list; after that the database is the source of truth.
- **Pull all feeds now**, or pull a single country. The pull runs in the background and the page updates every 10 seconds. A lock stops a button pull and a scheduled pull from overlapping.

### Daily schedule (macOS)
`python -m newsroom schedule install` adds a launchd agent that runs `python -m newsroom tick` every 10 minutes. Each region is pulled once a day at **12:00 in its own time zone** (`config/schedule.yaml`):

| Region | Countries |
|---|---|
| New York | US, CA |
| London | UK |
| Sydney | AU |

Daylight-saving changes are handled automatically. If the Mac was asleep at noon, the region runs when it wakes, up to 6 hours late; after that it waits for the next day. Use `python -m newsroom schedule status` to see the next pulls and `python -m newsroom schedule uninstall` to stop. On a Linux VPS, run `python -m newsroom tick` every 10 minutes from cron instead.

## Editorial workflow

```
ai_draft ──approve──────────────────────────────▶ approved ──publish──▶ published ──unpublish──▶ unpublished
    └──assign──▶ in_copy ──submit──▶ in_review ──approve──┘                 └──reopen──▶ in_review (corrections)
                    ▲                     │
                    └───── send_back ─────┘                 kill: any state that isn't live
```

| Role | Can |
|---|---|
| Editor | approve AI drafts directly, assign to a copy editor (also after unpublishing), send back, edit any unpublished version, unpublish, kill, reopen for corrections, add sources, pull now |
| Copy editor | edit and submit stories **assigned to them** |
| Publisher | publish approved stories, unpublish |
| Admin | everything, plus the team |

- **Byline** is the editor who approved the story. The site disclosure says it was AI-assisted and reviewed by that editor.
- **Every edit creates a new version.** Guardrails re-run against the original sources, and the History page shows the diff and the full audit trail.
- **Editing an approved story resets approval.**
- **Approval is blocked while the current version has guardrail errors**, unless the editor overrides it with a written reason, which is logged.
- **Corrections:** reopen, edit, approve and publish again. The site row moves to the next version and keeps its first-published date; the optional correction note is shown to readers.

The seeded placeholder staff (Maya Fernandes, editor; Daniel Okafor, copy editor; Sara Lindqvist, publisher; Desk Admin) have no passwords. To try a role, an admin can give one a password with **Reset password** on the Team page. Placeholders can't be a byline when `APP_ENV=production`.

## The website contract (`site` schema)

`site.published_stories`, one row per live or retracted story:
`slug, country, headline, dek, sections{what_happened, money_impact, who_is_affected, illustrative_example}, action_steps[], takeaways[], topics[], sources[{name,title,url}], meta_description, body_md, author_id → site.authors, ai_assisted, human_reviewed, disclosure, version, status (live|retracted), correction_note, first_published_at, updated_at`

When `human_reviewed` is false (auto-published), show the newsroom as the author and the editor as "responsible editor", not as "By …".

The website should:
- show `status = 'live'` rows, and return 410 for `retracted`;
- render the byline from `site.authors`;
- refresh when `updated_at` changes.

The desk's **Site feed** page renders these rows through the read-only `site` login, so you can see exactly what the website will receive.

## Pipeline commands

```bash
python -m pipeline.cli ingest | cluster | evaluate | write | run | desk
python -m pipeline.cli db-setup [--write-env]
```
Add `--mock` to use the offline stub model instead of Gemini.

## Newsroom commands

```bash
python -m newsroom serve [--port 8765]
python -m newsroom list [--status ai_draft]
python -m newsroom show 12
python -m newsroom act 12 approve --as maya
python -m newsroom act 12 assign  --as maya --to daniel --note "Lead with the deadline"
python -m newsroom act 12 publish --as sara
python -m newsroom site-feed
python -m newsroom users | seed-users
```

## Tests

```bash
python -m unittest discover -s tests -v                                             # SQLite
TEST_PG_URL=postgresql://localhost/postgres python -m unittest discover -s tests -v  # Postgres
```

The Postgres run creates a throwaway database per test, with the real schema and grants. It also checks role isolation and never changes the real logins' passwords.

## Configuration

| File | What to tune |
|---|---|
| `config/feeds.yaml` | sources per country, licence (`reuse`), `full_text`, `active` |
| `config/taxonomy.yaml` | topic keywords, locale, glossary, market-exclusive terms |
| `config/editorial.yaml` | desk weights and thresholds, style guide prompt, guardrail limits |
| `.env` | Gemini key and models, `APP_ENV`, database URLs |

## Categories

Stories come in categories: **Finance, Sports, Entertainment, Fitness & health**. They're defined in `config/categories.yaml`.

- **Where the category comes from:** every RSS source belongs to one category. You pick it when you add a feed on the Sources page.
- **Each category has its own approach:**
  - the scoring desk's rubric (finance: wallet impact; sports: fan interest and stakes; and so on);
  - run and hold thresholds;
  - the title pre-filter;
  - the writer's style and section titles (sports: "Why it matters", "What to watch next");
  - extra checks (sports: no betting tips; fitness: no medical advice);
  - the disclosure printed under the story;
  - auto-publishing. Fitness & health is always reviewed by a person.
- **Staff:** admins tick the categories each person works on (Team page). People only see and act on stories in their categories. Nothing ticked means all categories, and admins always see everything.
- **Moving a story:** editors can move a story to another category from the story page.
- **Adding a category:** add a block to `categories.yaml` with its desk, writer, guardrails and disclosure, then add feeds for it.
