-- When a concept's search keyword turned out too broad (the TikTok Shop
-- keyword search hit its 100-product limit), Claude narrows it once.
-- This records when, so it isn't narrowed again and again.
alter table gapfinder.concepts
  add column keyword_refined_at timestamptz,
  add column keyword_before_refinement text;
