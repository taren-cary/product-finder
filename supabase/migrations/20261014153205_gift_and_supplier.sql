-- Gift flag: would this make a good gift, and can it be shown on video
-- without a face (hands-only demo)? Set by Claude in the gift step; the owner
-- can change it by hand.
alter table gapfinder.concepts
  add column giftable    boolean,
  add column easy_demo   boolean,
  add column gift_reason text,
  add column gift_by     text check (gift_by in ('claude', 'manual'));

-- Real sourcing from CJ Dropshipping (US warehouses): the cheapest matching
-- product's cost and US-to-US shipping. Null = not checked yet.
alter table gapfinder.concept_weekly
  add column supplier_cost     numeric,  -- product cost per unit, USD
  add column supplier_shipping numeric,  -- shipping to a US customer, USD
  add column ship_days         integer,  -- slowest estimated days from order to delivery
  add column us_stock          boolean;  -- a matching product is stocked in a US warehouse
