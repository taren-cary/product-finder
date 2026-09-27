-- Who each concept is for, so the dashboard can show e.g. only women's products.
-- "women" = made for women OR bought mostly by women (satin pillowcases, hair tools).
-- Set by Claude during the TikTok-fit check; the owner can change it by hand.
alter table gapfinder.concepts
  add column audience    text check (audience in ('women', 'men', 'kids', 'pets', 'everyone')),
  add column audience_by text check (audience_by in ('claude', 'manual'));
