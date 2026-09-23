-- =====================================================================
-- Milestone 3: metrics and opportunity score per concept per week.
--
-- One row per concept per week (week_start = that week's Monday). The
-- features step fills in the metrics; the scoring step fills in the score
-- and rank. Both rerun safely: they overwrite the current week's row.
--
-- Velocities are growth rates: 0.25 = +25%, -0.10 = -10%. Null means the
-- source has no data (yet) for this concept.
-- =====================================================================

create table gapfinder.concept_weekly (
  concept_id           bigint not null references gapfinder.concepts (id) on delete cascade,
  week_start           date not null,

  -- Demand velocity per source
  velocity_google      numeric,   -- Google Trends: last 4 weeks vs the 4 before
  velocity_amazon      numeric,   -- Amazon Best Sellers: rank gains / new entries vs last week
  velocity_reddit      numeric,   -- Reddit: buy-intent mentions, last 4 weeks vs the 4 before
  velocity_tiktok      numeric,   -- TikTok hashtag views, week over week
  velocity_tiktokshop  numeric,   -- TikTok Shop revenue for the keyword (Kalodata), week over week

  demand_breadth       integer,   -- how many sources show rising demand
  outside_velocity     numeric,   -- weighted demand growth outside TikTok (Google, Amazon, Reddit)

  -- TikTok Shop saturation, 0 (empty) to 1 (crowded), and its parts
  tiktok_saturation    numeric,
  sellers              integer,   -- distinct sellers among the top 100 matching products
  creators             integer,   -- distinct creators among the top 100 matching videos
  shop_revenue_7d      numeric,   -- revenue of the top 100 matching products, last 7 days (USD)
  top3_seller_share    numeric,   -- share of that revenue taken by the top 3 sellers
  hashtag_views        bigint,    -- TikTok hashtag total views

  lead_lag_gap         numeric,   -- outside demand growth minus TikTok Shop supply growth (the key metric)
  paid_share           numeric,   -- share of shoppable-video views that come from ads (0-1)
  spike_risk           boolean,   -- recent rise looks like a one-off spike
  sustained_factor     numeric,   -- 0.5 (brief rise) to 1.0 (steady multi-week climb)
  margin_factor        numeric not null default 1.0,  -- neutral until Phase 2 (1688 costs)

  -- Scoring step
  opportunity_score    numeric,
  rank                 integer,

  details              jsonb not null default '{}'::jsonb,  -- inputs behind each number, for the dashboard
  computed_at          timestamptz not null default now(),
  primary key (concept_id, week_start)
);
create index concept_weekly_week_score_idx on gapfinder.concept_weekly (week_start, opportunity_score desc);

alter table gapfinder.concept_weekly enable row level security;
