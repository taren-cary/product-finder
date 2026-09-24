-- =====================================================================
-- TikTok-first redesign.
--
-- 1. Every concept gets a TikTok-fit verdict: would this sell on TikTok
--    Shop? Concepts judged "not for TikTok" are never looked up or scored.
--    Claude sets it; the owner can override it from the dashboard.
-- 2. The weekly metrics gain TikTok-led fields: momentum on TikTok, growth
--    in shoppable-video views, how many outside sources confirm, and whether
--    the concept showed up in this week's TikTok Shop discovery lists.
-- =====================================================================

alter table gapfinder.concepts
  add column tiktok_fit        boolean,   -- null = not judged yet
  add column tiktok_fit_reason text,
  add column tiktok_fit_by     text check (tiktok_fit_by in ('claude', 'manual'));
create index concepts_unjudged_idx on gapfinder.concepts (id)
  where tiktok_fit is null and merged_into_id is null;

alter table gapfinder.concept_weekly
  add column velocity_shopvideos numeric,  -- shoppable-video views for the keyword, weekly growth (Kalodata)
  add column tiktok_momentum     numeric,  -- weighted TikTok demand growth (shop revenue, shop videos, hashtag)
  add column confirmations       integer,  -- how many outside sources (Google, Amazon, Reddit) are also rising
  add column on_tiktok_lists     boolean;  -- appeared in this week's TikTok Shop discovery lists
