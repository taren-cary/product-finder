-- =====================================================================
-- Product Gap Finder: initial schema
--
-- Everything lives in a private "gapfinder" schema. It is NOT exposed to
-- Supabase's public Data API, so nothing here is reachable from the
-- internet with the anon/publishable key. Only our Python scripts and the
-- Streamlit dashboard (which connect with the database password) can
-- read or write it. RLS is also switched on for every table as a second
-- layer of protection.
--
-- Data flow:
--   raw_responses  -> exact API responses, one row per request per day
--   items          -> one row per unique product per source
--   item_snapshots -> that product's numbers on a given day (the time series)
--   concepts       -> canonical "product concepts" (e.g. heatless hair curler)
--   item_concept_map -> which concept each item belongs to
--   concept_signals  -> per-concept daily numbers from enrichment queries
--   pipeline_runs / source_runs -> log of every daily run and its cost
--
-- Feature and score tables are added in a later migration (Milestone 3),
-- once the metric definitions are settled.
-- =====================================================================

create schema if not exists gapfinder;

-- Make sure the public API roles can never see this schema.
revoke all on schema gapfinder from anon, authenticated;


-- ---------------------------------------------------------------------
-- Run log
-- ---------------------------------------------------------------------

-- One row per run of run_daily.py.
create table gapfinder.pipeline_runs (
  id           bigint generated always as identity primary key,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  status       text not null default 'running'
               check (status in ('running', 'success', 'partial', 'failed')),
  notes        text
);

-- One row per step (collector, clustering, scoring...) inside a run.
-- A failed step is logged here and the pipeline moves on.
create table gapfinder.source_runs (
  id                  bigint generated always as identity primary key,
  pipeline_run_id     bigint references gapfinder.pipeline_runs (id) on delete cascade,
  source              text not null,
  snapshot_date       date not null,
  started_at          timestamptz not null default now(),
  finished_at         timestamptz,
  status              text not null default 'running'
                      -- partial = stopped early at its spending cap, kept what it fetched
                      check (status in ('running', 'success', 'partial', 'failed')),
  records_saved       integer not null default 0,
  estimated_cost_usd  numeric(10, 4) not null default 0,
  error_message       text
);
create index source_runs_pipeline_run_id_idx on gapfinder.source_runs (pipeline_run_id);
create index source_runs_source_date_idx on gapfinder.source_runs (source, snapshot_date);


-- ---------------------------------------------------------------------
-- Raw data (append-only)
-- ---------------------------------------------------------------------

-- The exact response from each API call. Kept so metrics can be
-- recomputed later without paying to fetch the data again.
-- request_key describes the request (e.g. "movers:beauty" or
-- "hashtag:heatlesscurls"). The unique constraint is our cache:
-- the same request is never stored twice on the same day.
create table gapfinder.raw_responses (
  id               bigint generated always as identity primary key,
  source           text not null,
  snapshot_date    date not null,
  request_key      text not null,
  fetched_at       timestamptz not null default now(),
  source_run_id    bigint references gapfinder.source_runs (id) on delete set null,
  payload          jsonb not null,
  unique (source, snapshot_date, request_key)
);
create index raw_responses_source_run_id_idx on gapfinder.raw_responses (source_run_id);


-- ---------------------------------------------------------------------
-- Items: individual products as each source lists them
-- ---------------------------------------------------------------------

-- One row per unique product per source (an Amazon ASIN, a Kalodata
-- product id, a Reddit post...). Filled by the normalize step.
create table gapfinder.items (
  id                      bigint generated always as identity primary key,
  source                  text not null,
  source_item_id          text not null,
  title                   text not null,
  normalized_description  text,          -- short cleaned-up description, used for clustering
  category                text,          -- source's own category label
  url                     text,
  first_seen              date not null,
  last_seen               date not null,
  unique (source, source_item_id)
);

-- That item's numbers on a given day. metrics holds source-specific
-- fields (sales, GMV, seller count, upvotes...) so we don't need a new
-- column for every source.
create table gapfinder.item_snapshots (
  item_id           bigint not null references gapfinder.items (id) on delete cascade,
  snapshot_date     date not null,
  raw_response_id   bigint references gapfinder.raw_responses (id) on delete set null,
  price             numeric(12, 2),
  rank              integer,
  metrics           jsonb not null default '{}'::jsonb,
  primary key (item_id, snapshot_date)
);
create index item_snapshots_raw_response_id_idx on gapfinder.item_snapshots (raw_response_id);
create index item_snapshots_snapshot_date_idx on gapfinder.item_snapshots (snapshot_date);


-- ---------------------------------------------------------------------
-- Product concepts
-- ---------------------------------------------------------------------

create table gapfinder.concepts (
  id              bigint generated always as identity primary key,
  name            text not null unique,           -- e.g. "heatless hair curler"
  description     text,
  category        text,                           -- broad category, e.g. "Beauty > Hair tools"
  keywords        text[] not null default '{}',   -- search terms used to enrich from other sources
  -- Manual review status set from the dashboard.
  review_status   text not null default 'new'
                  check (review_status in ('new', 'reviewed', 'shortlisted', 'rejected')),
  -- When two concepts are merged, the old one points at the one it was merged into.
  merged_into_id  bigint references gapfinder.concepts (id) on delete set null,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index concepts_merged_into_id_idx on gapfinder.concepts (merged_into_id);

-- Each item belongs to at most one concept, so it is only classified once.
create table gapfinder.item_concept_map (
  item_id      bigint primary key references gapfinder.items (id) on delete cascade,
  concept_id   bigint not null references gapfinder.concepts (id) on delete cascade,
  assigned_by  text not null check (assigned_by in ('claude', 'manual')),
  model        text,               -- which Claude model made the call, if any
  assigned_at  timestamptz not null default now()
);
create index item_concept_map_concept_id_idx on gapfinder.item_concept_map (concept_id);

-- Enrichment results: when we look a concept up on another source by its
-- keywords (TikTok video count, Google Trends interest, Reddit mentions...),
-- each number is stored here, one row per concept/source/metric/day.
create table gapfinder.concept_signals (
  concept_id        bigint not null references gapfinder.concepts (id) on delete cascade,
  source            text not null,
  metric            text not null,        -- e.g. "video_count", "trend_interest"
  snapshot_date     date not null,
  value             numeric,
  raw_response_id   bigint references gapfinder.raw_responses (id) on delete set null,
  primary key (concept_id, source, metric, snapshot_date)
);
create index concept_signals_raw_response_id_idx on gapfinder.concept_signals (raw_response_id);


-- ---------------------------------------------------------------------
-- Security: RLS on every table. No policies are created, which means
-- the anon/authenticated roles get nothing. Our scripts connect as the
-- table owner, which is not affected by RLS.
-- ---------------------------------------------------------------------

alter table gapfinder.pipeline_runs     enable row level security;
alter table gapfinder.source_runs       enable row level security;
alter table gapfinder.raw_responses     enable row level security;
alter table gapfinder.items             enable row level security;
alter table gapfinder.item_snapshots    enable row level security;
alter table gapfinder.concepts          enable row level security;
alter table gapfinder.item_concept_map  enable row level security;
alter table gapfinder.concept_signals   enable row level security;
