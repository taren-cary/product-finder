-- Knowledge base: extraction runs through Anthropic's Batch API (half price).
-- A video sent in a batch remembers the batch id until its results are collected.
alter table gapfinder.kb_videos add column extract_batch_id text;
create index kb_videos_batch_idx on gapfinder.kb_videos (extract_batch_id) where extract_batch_id is not null;
