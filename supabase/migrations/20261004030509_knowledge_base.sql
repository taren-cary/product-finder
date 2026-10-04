-- =====================================================================
-- YouTube knowledge base about selling and affiliate marketing on TikTok Shop.
--
--   kb_channels  YouTube channels; the owner approves or rejects each one.
--                Only approved channels' videos are processed.
--   kb_videos    videos found, with their transcript once fetched
--   kb_claims    individual tips/rules/claims Claude extracted from a video,
--                each with the moment in the video it came from
--   kb_insights  claims merged across videos: one entry per piece of advice,
--                with how many videos/channels said it, and contradictions
-- =====================================================================

create table gapfinder.kb_channels (
  channel_id     text primary key,          -- YouTube channel id
  name           text not null,
  url            text,
  status         text not null default 'candidate'
                 check (status in ('candidate', 'approved', 'rejected')),
  added_by       text not null default 'discovery' check (added_by in ('discovery', 'manual')),
  videos_found   integer not null default 0,  -- matching videos found by the searches
  total_views    bigint not null default 0,
  sample_titles  jsonb not null default '[]'::jsonb,
  first_seen     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

create table gapfinder.kb_videos (
  video_id         text primary key,          -- YouTube video id
  channel_id       text not null references gapfinder.kb_channels (channel_id) on delete cascade,
  title            text not null,
  duration_s       integer,
  view_count       bigint,
  upload_date      date,
  found_by         text,                      -- the search that found it, or 'channel'
  status           text not null default 'found'
                   check (status in ('found', 'transcribed', 'no_transcript', 'extracted', 'failed')),
  transcript       jsonb,                     -- [{start, text}, ...]
  transcript_chars integer,
  error            text,
  extracted_at     timestamptz,
  first_seen       timestamptz not null default now()
);
create index kb_videos_channel_idx on gapfinder.kb_videos (channel_id);
create index kb_videos_status_idx on gapfinder.kb_videos (status);

create table gapfinder.kb_insights (
  id              bigint generated always as identity primary key,
  topic           text not null,
  audience        text not null check (audience in ('seller', 'affiliate', 'both')),
  statement       text not null,             -- the merged piece of advice
  details         text,
  video_count     integer not null default 0,
  channel_count   integer not null default 0,
  contradictions  text,                      -- where creators disagree, if they do
  latest_upload   date,                      -- newest video saying it (advice ages fast)
  search          tsvector generated always as
                  (to_tsvector('english', topic || ' ' || statement || ' ' || coalesce(details, ''))) stored,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index kb_insights_search_idx on gapfinder.kb_insights using gin (search);
create index kb_insights_topic_idx on gapfinder.kb_insights (topic);

create table gapfinder.kb_claims (
  id           bigint generated always as identity primary key,
  video_id     text not null references gapfinder.kb_videos (video_id) on delete cascade,
  topic        text not null,
  audience     text not null check (audience in ('seller', 'affiliate', 'both')),
  claim        text not null,                -- one concrete tip, rule, number or warning
  details      text,
  start_s      integer,                      -- where in the video it's said
  insight_id   bigint references gapfinder.kb_insights (id) on delete set null,
  created_at   timestamptz not null default now()
);
create index kb_claims_video_idx on gapfinder.kb_claims (video_id);
create index kb_claims_insight_idx on gapfinder.kb_claims (insight_id);
create index kb_claims_unmerged_idx on gapfinder.kb_claims (topic) where insight_id is null;

alter table gapfinder.kb_channels enable row level security;
alter table gapfinder.kb_videos   enable row level security;
alter table gapfinder.kb_insights enable row level security;
alter table gapfinder.kb_claims   enable row level security;
