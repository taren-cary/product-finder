-- Knowledge base: comprehensive extraction. Each item now says what kind of
-- knowledge it is (tip, hook/script, case study, benchmark, ...) and keeps the
-- speaker's exact words when the wording itself is useful (hooks, scripts, quotes).
-- prompt_version tells which extraction instructions produced the item.
alter table gapfinder.kb_claims add column kind text;
alter table gapfinder.kb_claims add column quote text;
alter table gapfinder.kb_claims add column prompt_version integer not null default 1;
alter table gapfinder.kb_videos add column prompt_version integer;
update gapfinder.kb_videos set prompt_version = 1 where status = 'extracted';
create index kb_claims_kind_idx on gapfinder.kb_claims (kind);
