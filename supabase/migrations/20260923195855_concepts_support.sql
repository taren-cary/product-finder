-- =====================================================================
-- Milestone 2: support for normalizing raw data and grouping items into
-- product concepts.
-- =====================================================================

-- Marks raw responses the normalize step has already turned into items,
-- so each one is processed exactly once.
alter table gapfinder.raw_responses add column processed_at timestamptz;
create index raw_responses_unprocessed_idx
  on gapfinder.raw_responses (source, id) where processed_at is null;

-- Extra text that helps classify an item (e.g. the body of a Reddit post).
alter table gapfinder.items add column body_text text;

-- Classification state, set by the concept step:
--   classified_at null      -> not looked at yet
--   is_product = true       -> a sellable physical product (mapped in item_concept_map)
--   is_product = false      -> not a product (a question, a service, a book...); skipped from now on
alter table gapfinder.items add column classified_at timestamptz;
alter table gapfinder.items add column is_product boolean;
create index items_unclassified_idx on gapfinder.items (last_seen desc) where classified_at is null;

-- Fuzzy text matching, used to shortlist likely concepts for each item once
-- there are too many concepts to send to Claude every time.
create extension if not exists pg_trgm with schema extensions;
create index concepts_name_trgm_idx
  on gapfinder.concepts using gin (name extensions.gin_trgm_ops);
