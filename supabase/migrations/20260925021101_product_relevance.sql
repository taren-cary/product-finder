-- Relevance filter: for each concept, which TikTok Shop products found by its
-- keyword searches are really this product (a crevice brush) and which are
-- something else that matched the words (a window blinds brush). Judged once
-- per concept + product by Claude; seller counts, prices and payouts only use
-- the products marked relevant.
create table gapfinder.concept_product_matches (
  concept_id  bigint not null references gapfinder.concepts (id) on delete cascade,
  product_id  text not null,               -- Kalodata / TikTok Shop product id
  relevant    boolean not null,
  title       text,                        -- kept so the verdict can be reviewed
  judged_by   text not null default 'claude' check (judged_by in ('claude', 'manual')),
  judged_at   timestamptz not null default now(),
  primary key (concept_id, product_id)
);

alter table gapfinder.concept_product_matches enable row level security;
