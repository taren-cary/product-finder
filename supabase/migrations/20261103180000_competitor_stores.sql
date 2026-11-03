-- Competitor stores: online stores found running Meta ads (or added by hand),
-- checked weekly for their products and best sellers. Their raw data lives in
-- raw_responses (sources "meta_ads" and "stores"); this table is the list of
-- stores to check and what we know about each.
create table gapfinder.stores (
    domain        text primary key,              -- e.g. "glowtools.com" (no "www.")
    name          text,                          -- Facebook page name of the first ad seen
    source        text not null default 'meta_ads' check (source in ('meta_ads', 'manual')),
    status        text not null default 'new'
                  check (status in ('new', 'tracking', 'not_shopify', 'unreachable', 'ignored')),
    first_seen    date not null default current_date,
    last_checked  date,
    product_count integer,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

alter table gapfinder.stores enable row level security;
