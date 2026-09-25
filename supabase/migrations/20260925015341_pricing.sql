-- Price and profit per concept per week, from the TikTok Shop keyword search.
-- The estimates use the cost assumptions in config.yaml ("pricing") until
-- real sourcing costs (1688, Phase 2) are available.
alter table gapfinder.concept_weekly
  add column typical_price           numeric,  -- what buyers actually pay on average (revenue / units, last 7 days)
  add column price_floor             numeric,  -- lowest price among active competitors
  add column units_7d                integer,  -- units sold by the matching products, last 7 days
  add column est_profit_per_unit     numeric,  -- at the typical price, after fees, commission, shipping, product cost
  add column weekly_profit_potential numeric;  -- est. profit if you took an average seller's share of units
