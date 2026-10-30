-- Story Hub database: one PostgreSQL database, three schemas.
--
--   pipeline  — ingest, clustering, desk scoring (written by the Python pipeline)
--   newsroom  — stories, revisions, workflow, staff (the editorial desk)
--   site      — published snapshots + public author profiles
--               (the ONLY schema the public website can read)
--
-- Access is enforced with one database role per system (see pipeline/dbsetup.py):
--   story_pipeline : read/write pipeline; may create AI drafts in newsroom
--   story_newsroom : read pipeline; read/write newsroom; write site (publish)
--   story_site     : read-only on site
--
-- Apply with:  python -m pipeline.cli db-setup   (uses ADMIN_DATABASE_URL)
-- Statements are idempotent, so re-running is safe.

create schema if not exists pipeline;
create schema if not exists newsroom;
create schema if not exists site;

-- =========================================================================
-- pipeline
-- =========================================================================
create table if not exists pipeline.sources (
    id              text primary key,
    name            text not null,
    country         text not null check (country in ('US','UK','CA','AU')),
    url             text not null,
    kind            text not null check (kind in ('official','publisher')),
    category        text not null default 'finance',   -- config/categories.yaml
    reuse           text not null default 'link_only',
    full_text       boolean not null default false,
    active          boolean not null default true,
    etag            text,
    last_modified   text,
    last_fetched_at timestamptz,
    last_status     text,
    added_by        text,                            -- 'config' (feeds.yaml) or the newsroom user's email
    created_at      timestamptz not null default now(),
    deleted_at      timestamptz,                     -- deleted in the desk: hidden, never pulled or re-added from feeds.yaml
    deleted_by      text
);

create table if not exists pipeline.story_clusters (
    id           bigserial primary key,
    country      text not null,
    category     text not null default 'finance',
    headline     text not null,
    lead_item_id bigint,
    item_count   integer not null default 1,
    status       text not null default 'pending'
                 check (status in ('pending','evaluated','commissioned','drafted','skipped')),
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create table if not exists pipeline.raw_items (
    id            bigserial primary key,
    source_id     text not null references pipeline.sources(id),
    country       text not null,
    guid          text,
    url           text not null,
    title         text not null,
    summary       text,
    author        text,
    published_at  timestamptz,
    fetched_at    timestamptz not null default now(),
    fingerprint   text not null unique,
    title_key     text not null,
    topics        jsonb not null default '[]',
    feed_tags     jsonb not null default '[]',    -- the feed's category/keyword tags (SEO signal)
    cluster_id    bigint references pipeline.story_clusters(id),
    status        text not null default 'new' check (status in ('new','filtered','clustered')),
    filter_reason text
);
create index if not exists raw_items_country_pub on pipeline.raw_items (country, published_at);
create index if not exists raw_items_cluster on pipeline.raw_items (cluster_id);

create table if not exists pipeline.evaluations (
    id            bigserial primary key,
    cluster_id    bigint not null references pipeline.story_clusters(id),
    model         text not null,
    wallet_impact smallint, breadth smallint, actionability smallint, urgency smallint, novelty smallint,
    scores        jsonb,                      -- the category's dimension scores {key: 1-5}
    flags         jsonb not null default '[]',
    impact_score  numeric(5,1) not null,
    decision      text not null check (decision in ('run','hold','reject')),
    angle         text,
    audience      jsonb not null default '[]',
    rationale     text,
    image_query   text,                       -- the desk's stock-photo search words for this story
    image_prompt  text,                       -- the desk's suggested illustration prompt
    created_at    timestamptz not null default now()
);
create index if not exists evaluations_cluster on pipeline.evaluations (cluster_id, created_at);

create table if not exists pipeline.pipeline_runs (
    id          bigserial primary key,
    stage       text not null,
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    stats       jsonb not null default '{}',
    error       text
);

-- One row per region per local day: the daily schedule claims it before running,
-- so a region is pulled at most once a day even if the scheduler fires repeatedly.
create table if not exists pipeline.scheduled_runs (
    id          bigserial primary key,
    region      text not null,
    local_date  text not null,                       -- YYYY-MM-DD in the region's time zone
    countries   jsonb not null default '[]',
    trigger     text not null default 'schedule',    -- schedule | manual
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    stats       jsonb not null default '{}',
    error       text
);
create unique index if not exists scheduled_runs_once on pipeline.scheduled_runs (region, local_date);

-- =========================================================================
-- newsroom
-- =========================================================================
create table if not exists newsroom.users (
    id             bigserial primary key,
    email          text not null unique,
    display_name   text not null,
    slug           text not null unique,
    role           text not null check (role in ('copywriter','editor','publisher','admin')),
    categories     jsonb not null default '[]',      -- categories this person works in; [] = all
    job_title      text,
    bio            text,
    credentials    text,
    avatar_url     text,
    is_placeholder boolean not null default false,  -- seeded test persona; never publish under it in production
    active         boolean not null default true,
    password_hash  text,                             -- scrypt; null = cannot log in
    must_change_password boolean not null default false,
    last_login_at  timestamptz,
    created_at     timestamptz not null default now()
);

create table if not exists newsroom.sessions (
    token_hash  text primary key,                    -- sha256 of the cookie token; the token itself is never stored
    user_id     bigint not null references newsroom.users(id),
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null
);

-- Workflow: ai_draft -> (in_copy -> in_review ->) approved -> published -> unpublished
--           killed from any unpublished state.
create table if not exists newsroom.stories (
    id                bigserial primary key,
    cluster_id        bigint unique,             -- pipeline.story_clusters(id); null for human-originated stories
    country           text not null check (country in ('US','UK','CA','AU')),
    category          text not null default 'finance',
    slug              text not null unique,
    status            text not null default 'ai_draft'
                      check (status in ('ai_draft','in_copy','in_review','approved','published','unpublished','killed')),
    current_version   integer not null default 1,
    published_version integer,
    assigned_to       bigint references newsroom.users(id),   -- copywriter working on it
    editor_id         bigint references newsroom.users(id),   -- approving editor = byline
    impact_score      numeric(5,1),                           -- copied from the desk for queue ordering
    angle             text,
    auto_published    boolean not null default false,         -- went live without a human approval
    origin            text not null default 'pipeline',       -- pipeline | editor_pick (an editor overrode the desk)
    picked_by         bigint references newsroom.users(id),   -- editor who picked it up
    research          jsonb,                                  -- AI research brief {text, sources, queries, notes}
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);
create index if not exists stories_status on newsroom.stories (status, updated_at);

-- Editor pick-ups: an editor overrides the desk's decision and asks the AI to
-- research and draft a story. Runs in the background; the row tracks progress.
-- Also "Write from a link": an editor pastes another publication's story URL; then cluster_id is
-- null and source_url/country/category say what to write about.
create table if not exists newsroom.pick_requests (
    id           bigserial primary key,
    cluster_id   bigint,                          -- pipeline.story_clusters(id); null for a story written from a link
    source_url   text,                            -- the story an editor pasted (Write from a link)
    country      text,                            -- link requests: the edition to write for
    category     text,                            -- link requests: the category to write in
    requested_by bigint not null references newsroom.users(id),
    angle        text not null,
    audience     jsonb not null default '[]',
    notes        text,
    extra_urls   jsonb not null default '[]',
    research     boolean not null default true,
    image_prompts jsonb not null default '[]',      -- [{prompt, aspect, role}] written at pick-up, generated on request
    status       text not null default 'queued' check (status in ('queued','running','done','failed')),
    story_id     bigint references newsroom.stories(id),
    error        text,
    created_at   timestamptz not null default now(),
    finished_at  timestamptz
);
create index if not exists pick_requests_cluster on newsroom.pick_requests (cluster_id, id);

create table if not exists newsroom.story_revisions (
    id               bigserial primary key,
    story_id         bigint not null references newsroom.stories(id),
    version          integer not null,
    author_kind      text not null check (author_kind in ('ai','human')),
    author_id        bigint references newsroom.users(id),
    headline         text not null,
    dek              text,
    sections         jsonb not null,
    takeaways        jsonb not null default '[]',
    action_steps     jsonb not null default '[]',
    topics           jsonb not null default '[]',
    meta_description text,
    sources          jsonb not null default '[]',
    body_md          text not null,
    blocks           jsonb,                      -- set when a person edits in the block editor
    seo              jsonb,                      -- {title, focus_keyword, keywords, source_keywords}
    word_count       integer,
    guardrails       jsonb,
    model            text,
    note             text,
    created_at       timestamptz not null default now(),
    unique (story_id, version)
);

create table if not exists newsroom.story_events (
    id          bigserial primary key,
    story_id    bigint not null references newsroom.stories(id),
    actor_id    bigint references newsroom.users(id),          -- null = pipeline / system
    action      text not null,
    from_status text,
    to_status   text,
    note        text,
    created_at  timestamptz not null default now()
);
create index if not exists story_events_story on newsroom.story_events (story_id, id);

-- =========================================================================
-- site  (read-only for the public website)
-- =========================================================================
create table if not exists site.authors (
    id           bigint primary key,      -- same id as newsroom.users
    slug         text not null unique,
    display_name text not null,
    job_title    text,
    bio          text,
    credentials  text,
    avatar_url   text,
    updated_at   timestamptz not null default now()
);

create table if not exists site.published_stories (
    id                 bigserial primary key,
    story_id           bigint not null unique,      -- newsroom.stories(id)
    slug               text not null unique,
    country            text not null,
    category           text not null default 'finance',
    headline           text not null,
    dek                text,
    sections           jsonb not null,             -- what_happened, money_impact, who_is_affected, illustrative_example
    takeaways          jsonb not null default '[]',
    action_steps       jsonb not null default '[]',
    topics             jsonb not null default '[]',
    sources            jsonb not null default '[]', -- [{name,title,url}]
    meta_description   text,
    body_md            text not null,
    blocks             jsonb,                       -- block body for stories edited by people; null for AI-format stories
    seo                jsonb,                       -- {title, description, focus_keyword, keywords}
    images             jsonb not null default '[]', -- hero and in-story images with sizes, alt, caption, credit
    author_id          bigint not null references site.authors(id),
    ai_assisted        boolean not null default true,
    human_reviewed     boolean not null default true,       -- false = published automatically, no editor review yet
    disclosure         text not null,
    version            integer not null,
    status             text not null default 'live' check (status in ('live','retracted')),
    correction_note    text,
    first_published_at timestamptz not null,
    updated_at         timestamptz not null
);
create index if not exists published_country on site.published_stories (country, status, first_published_at);

-- =========================================================================
-- Migrations for databases created by earlier versions (Postgres only; no-ops when present)
-- =========================================================================
alter table pipeline.sources add column if not exists added_by text;
alter table pipeline.sources add column if not exists created_at timestamptz not null default now();
alter table newsroom.users add column if not exists password_hash text;
alter table newsroom.users add column if not exists must_change_password boolean not null default false;
alter table newsroom.users add column if not exists last_login_at timestamptz;
alter table newsroom.stories add column if not exists auto_published boolean not null default false;
alter table newsroom.stories add column if not exists origin text not null default 'pipeline';
alter table newsroom.stories add column if not exists picked_by bigint;
alter table newsroom.stories add column if not exists research jsonb;
alter table site.published_stories add column if not exists human_reviewed boolean not null default true;
alter table newsroom.story_revisions add column if not exists blocks jsonb;
alter table site.published_stories add column if not exists blocks jsonb;
alter table pipeline.sources add column if not exists category text not null default 'finance';
alter table pipeline.story_clusters add column if not exists category text not null default 'finance';
alter table pipeline.evaluations add column if not exists scores jsonb;
alter table newsroom.users add column if not exists categories jsonb not null default '[]';
alter table newsroom.stories add column if not exists category text not null default 'finance';
alter table site.published_stories add column if not exists category text not null default 'finance';
create index if not exists stories_category on newsroom.stories (category, status);
create index if not exists clusters_category on pipeline.story_clusters (category, status);
create index if not exists site_category on site.published_stories (category, status);
alter table pipeline.raw_items add column if not exists feed_tags jsonb not null default '[]';
alter table newsroom.story_revisions add column if not exists seo jsonb;
alter table site.published_stories add column if not exists seo jsonb;
create index if not exists site_updated on site.published_stories (updated_at);
alter table newsroom.pick_requests add column if not exists image_prompts jsonb not null default '[]';
alter table pipeline.evaluations add column if not exists image_query text;
alter table pipeline.sources add column if not exists deleted_at timestamptz;
alter table pipeline.sources add column if not exists deleted_by text;
alter table pipeline.evaluations add column if not exists image_prompt text;
alter table site.published_stories add column if not exists images jsonb not null default '[]';
alter table newsroom.pick_requests alter column cluster_id drop not null;
alter table newsroom.pick_requests add column if not exists source_url text;
alter table newsroom.pick_requests add column if not exists country text;
alter table newsroom.pick_requests add column if not exists category text;
create index if not exists pick_requests_source_url on newsroom.pick_requests (source_url);

-- Images attached to newsroom stories: stock photos (hotlinked from Unsplash/Pexels with
-- credit), AI illustrations (Nano Banana, stored in Cloudflare R2) and uploads (R2).
-- status 'draft' = a prompt written at pick-up, not generated yet.
create table if not exists newsroom.story_images (
    id            bigserial primary key,
    story_id      bigint not null references newsroom.stories(id),
    kind          text not null check (kind in ('stock','generated','upload')),
    provider      text not null,                 -- unsplash | pexels | gemini | upload
    status        text not null default 'ready' check (status in ('draft','queued','generating','ready','failed','removed')),
    role          text not null default 'gallery' check (role in ('hero','inline','gallery')),
    position      text,                          -- inline images in fixed-section stories: the section they follow
    prompt        text,
    style         text,
    aspect        text,
    url           text,                          -- main display address
    sizes         jsonb not null default '{}',   -- {"1600": url, "800": url, ...}
    width         integer,
    height        integer,
    alt           text,
    caption       text,
    credit        text,                          -- e.g. "Photo by Jane Doe on Unsplash"
    credit_url    text,
    photographer  text,
    photographer_url text,
    source_url    text,                          -- the photo's page on Unsplash/Pexels
    provider_id   text,
    storage_keys  jsonb not null default '[]',   -- R2 object keys we own
    model         text,
    error         text,
    created_by    bigint references newsroom.users(id),
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create index if not exists story_images_story on newsroom.story_images (story_id, status);

-- Keys for the read-only Stories API (storyapi). Only a SHA-256 hash of each key is kept;
-- the key itself is shown once, when an admin creates it on the desk's API page.
-- Desk settings admins change on the Team page (e.g. who is named on auto-published stories).
create table if not exists newsroom.settings (
    key         text primary key,
    value       text,
    updated_by  text,
    updated_at  timestamptz not null default now()
);

-- Pages editors add as sources while editing: read once, used as source material by the checks.
create table if not exists newsroom.source_pages (
    url         text primary key,
    title       text,
    text        text,
    fetched_at  timestamptz not null default now()
);

-- Editor autosave: the work in progress of one person on one story, kept every minute while they
-- edit. Not a version: no status change, no checks. Cleared when they save a real version.
create table if not exists newsroom.story_autosaves (
    story_id      bigint not null references newsroom.stories(id),
    user_id       bigint not null references newsroom.users(id),
    base_version  integer not null,            -- the version they started from
    state         jsonb not null,              -- {headline, dek, blocks, side}
    updated_at    timestamptz not null default now(),
    primary key (story_id, user_id)
);

-- The editor's AI assistant: one row per question and answer, shared by everyone editing the story.
create table if not exists newsroom.story_chats (
    id           bigserial primary key,
    story_id     bigint not null references newsroom.stories(id),
    user_id      bigint not null references newsroom.users(id),
    question     text not null,
    research     boolean not null default true,       -- answered with Google Search
    story_state  jsonb,                                -- the story on screen when asked {headline, dek, blocks}
    status       text not null default 'queued' check (status in ('queued','running','done','failed')),
    reply        text,                                 -- markdown answer
    blocks       jsonb not null default '[]',          -- text to add, as editor blocks
    sources      jsonb not null default '[]',          -- [{name, title, url}]
    queries      jsonb not null default '[]',
    model        text,
    error        text,
    created_at   timestamptz not null default now(),
    finished_at  timestamptz
);
create index if not exists story_chats_story on newsroom.story_chats (story_id, id);

create table if not exists site.api_clients (
    id          bigserial primary key,
    name        text not null,                 -- who it's for, e.g. "Acme frontend (staging)"
    key_prefix  text not null,                 -- first characters, to recognise a key in lists
    key_hash    text not null unique,
    notes       text,
    created_by  text,
    created_at  timestamptz not null default now(),
    revoked_at  timestamptz
);
