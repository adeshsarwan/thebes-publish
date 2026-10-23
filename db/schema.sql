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
    reuse           text not null default 'link_only',
    full_text       boolean not null default false,
    active          boolean not null default true,
    etag            text,
    last_modified   text,
    last_fetched_at timestamptz,
    last_status     text,
    added_by        text,                            -- 'config' (feeds.yaml) or the newsroom user's email
    created_at      timestamptz not null default now()
);

create table if not exists pipeline.story_clusters (
    id           bigserial primary key,
    country      text not null,
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
    flags         jsonb not null default '[]',
    impact_score  numeric(5,1) not null,
    decision      text not null check (decision in ('run','hold','reject')),
    angle         text,
    audience      jsonb not null default '[]',
    rationale     text,
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
create table if not exists newsroom.pick_requests (
    id           bigserial primary key,
    cluster_id   bigint not null,                 -- pipeline.story_clusters(id)
    requested_by bigint not null references newsroom.users(id),
    angle        text not null,
    audience     jsonb not null default '[]',
    notes        text,
    extra_urls   jsonb not null default '[]',
    research     boolean not null default true,
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
