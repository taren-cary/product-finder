-- Knowledge base: insights carry the kind of knowledge and example quotes.
-- search_all also covers kind and quotes (so exact hook wording is findable);
-- it replaces the older "search" column, which is left unused.
alter table gapfinder.kb_insights add column kind text;
alter table gapfinder.kb_insights add column quote text;
alter table gapfinder.kb_insights add column search_all tsvector generated always as
  (to_tsvector('english', topic || ' ' || coalesce(kind, '') || ' ' || statement || ' ' ||
                          coalesce(details, '') || ' ' || coalesce(quote, ''))) stored;
create index kb_insights_search_all_idx on gapfinder.kb_insights using gin (search_all);
create index kb_insights_kind_idx on gapfinder.kb_insights (kind);
