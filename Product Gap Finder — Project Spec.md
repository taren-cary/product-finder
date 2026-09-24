# Product Gap Finder — Project Spec

## Goal
Find physical products to sell on TikTok Shop that have **rising demand elsewhere** but **low saturation on TikTok Shop**. The system collects data from multiple sources on a regular schedule (mostly weekly), groups everything into "product concepts," and ranks concepts by opportunity.

The core signal is a **cross-platform gap**: demand is accelerating on Amazon, Reddit, Google, Pinterest, etc., while TikTok Shop seller and creator counts are still low.

Categories are fully open. Nothing is excluded up front except products TikTok Shop prohibits or restricts (the owner will supply that list).

## TikTok-first design (current, 2026-09-24)
TikTok is where the owner sells, so **TikTok data leads and every other source confirms**. This design replaces the earlier "rising elsewhere" weighting wherever the two conflict.
- **Goal:** products rising on TikTok (or rising elsewhere and ready for TikTok) while TikTok Shop is still undersaturated; the ones most people miss because they don't combine all these angles.
- **Discovery is TikTok-first** (Kalodata): small products ($1k–$50k/week) growing fastest, mid-size risers ($50k–$500k/week), new launches, and the most-viewed shoppable videos. Already-dominant top sellers are not a discovery source. Amazon Best Sellers and Reddit still feed items in, as confirmation and as a second route in for products rising elsewhere.
- **TikTok-fit gate:** Claude judges every concept (sellable through short videos, impulse price, small, brand-agnostic, not a replenished staple, not regulated or prohibited). Concepts that aren't a fit are never looked up, scored or ranked. The owner can override the verdict in the dashboard.
- **Lookups are prioritized, not exhaustive** (`collectors/keywords.py`). TikTok checks (Kalodata keyword search, TikTok hashtags) go to the watchlist, shortlisted concepts, this week's TikTok Shop listings, never-checked concepts, concepts rising elsewhere, top scores, then everything else in rotation. Google Trends only confirms the top TikTok candidates, at most every 4 weeks each.
- **Score:** (TikTok momentum + small outside demand) × (1 + 0.15 per confirming source) × TikTok headroom × steadiness × margin. TikTok momentum = TikTok Shop revenue growth (weight 1.0) + shoppable-video views growth (0.5) + hashtag views growth (0.5). Google, Amazon and Reddit each add 0.25 on their own. Reddit's weight is a single setting and can be raised to TikTok's level later.
- **Pipeline:** discovery → normalize → concepts → TikTok fit → TikTok checks → score → Google confirmation → final score.
- **Weekly budget at the defaults:** about 21 Kalodata credits, about $2.50 Apify, and a few dollars of Claude.

## Owner context
- Non-engineer founder. Keep the code simple, readable, and well-commented.
- Prefer boring, reliable tools over clever ones.
- Every collector must fail gracefully: log the error, skip, and continue. One broken source must never stop the pipeline.

## Stack
- Python 3.11+
- Supabase (hosted Postgres) for all storage. Python connects directly to Postgres with `psycopg` using the connection string in `.env`.
  - All tables live in a private `gapfinder` schema that is not exposed to Supabase's public Data API. Row-level security is enabled on every table as a second layer of protection. Only the owner's scripts and dashboard touch the data, so no user auth is needed.
  - Schema changes are SQL migration files in `supabase/migrations/`, tracked in git and applied with the Supabase CLI.
  - Start on the free tier. Watch database size (free tier is roughly 500 MB); move to Pro, or move bulky raw payloads to Supabase Storage, when it gets close.
  - Free projects pause after about a week of inactivity. The pipeline touches the database every day at noon, which keeps it awake; if the scheduled job stops for a week, the project must be resumed from the Supabase dashboard.
- `.env` for all API keys and the database connection string, loaded with python-dotenv. Never hardcode keys and never commit `.env`.
- Streamlit for the dashboard (reads from Supabase the same way the scripts do)
- Claude API (Anthropic SDK) for product-concept clustering. Uses Claude Opus 5 at low effort with structured JSON output (about $0.004 per item); the model is set in `config.yaml` and can be switched to Sonnet 5 or Haiku 4.5 to cut cost.
- Windows Task Scheduler runs `run_daily.py` every day at 12:00 PM (it also runs as soon as the PC is back on if it was off at noon). If the PC being off becomes a problem, move the job to a small cloud VM or GitHub Actions.
- Git for version control.

## Architecture
```
core/            shared helpers: config, database connection, logging
collectors/      one file per source; each returns raw records
  -> raw tables    append-only, every record timestamped (snapshot_date);
                   full API responses stored as jsonb
supabase/        SQL migrations (the database schema)
normalize/       clean titles, prices, and categories into a common schema
concepts/        LLM clustering: map raw items -> canonical "product concept"
features/        compute metrics per concept per week
scoring/         opportunity score + ranking
dashboard/       Streamlit app
run_daily.py     runs everything in order: discovery collectors -> normalize ->
                 concepts -> enrichment collectors -> features/scoring
```

**Time series is the asset.** Velocity metrics need history, so collectors must go live first and run on schedule from day one, even before scoring exists. What matters is how long and how consistent the history is, not how often it is sampled.

**Collection schedule.** The pipeline runs every day, but each collector has its own schedule in `config.yaml` (`weekly` or `daily`):
- **Weekly (Mondays):** paid sources: Kalodata (discovery and keyword search), Amazon, TikTok, Google Trends. Kalodata queries cover the last 7 days, so weekly pulls give complete, non-overlapping coverage. Google Trends returns full history per request and TikTok counts are cumulative, so sampling more often adds little. Weekly cuts paid-API cost by ~7x; the savings buy deeper coverage (more pages / more rows per category).
- **Daily:** free sources (e.g. Reddit).
- A missed weekly run (PC off, run failed) is made up automatically on the next day the pipeline runs.
- Trade-off accepted: a trend may be noticed up to ~6 days later than with daily collection. TikTok Shop saturation typically builds over weeks to months, so this is acceptable. Any source can be switched back to daily in `config.yaml`.

## The hard problem: entity resolution
The same product appears under different names on each platform ("heatless curling rod," "overnight curl ribbon," "satin curler"). Solution:
1. Extract a short normalized product description from each raw item.
2. Send new (unmapped) items to Claude in batches, along with a list of existing concepts (name, short description, a few example titles). Claude assigns each item to an existing concept or proposes a new canonical `product_concept` (for example, "heatless hair curler"). No embeddings.
3. Store the mapping table `item_id -> concept_id` so each item is only classified once. Items that aren't sellable physical products (most Reddit posts, licensed collectibles, vehicles, media, anything in `excluded_product_types`) are marked `is_product = false` and skipped from then on.
   Concepts are generic, brand-free product types (e.g. "Roomba Plus 4020" -> "robot vacuum and mop"), with a category, a one-line description and 1-3 search keywords that the enrichment collectors look up.
4. Allow manual merge and split of concepts from the dashboard.

**Scaling the concept list.** Once there are too many concepts to send with every batch (a few thousand), narrow the candidates before calling Claude, still without embeddings:
- Pre-filter by broad category (for example, only send "Beauty > Hair tools" concepts for a hair-tool item).
- Use Postgres fuzzy text matching (`pg_trgm`) to shortlist the ~50 most similar concepts per batch.

If grouping quality becomes a real problem later, Supabase includes `pgvector`, so embeddings can be added without a new service.

## Data sources

### Phase 1 (build first)
| Source | Access | What we pull | Role |
|---|---|---|---|
| Kalodata | API key (docs saved in `docs/kalodata/`) | **Discovery** (weekly): fastest-growing products, top sellers, new launches, category rankings. **Keyword search** per watchlist/concept keyword: product and seller counts, revenue, shoppable-video views, and ad share of views/revenue (`ad_view_ratio`, `ad_revenue_ratio`) | TikTok Shop saturation, demand, and paid vs organic |
| TikTok | Apify (`clockworks/tiktok-hashtag-scraper`) | Per keyword's hashtag: total views (running total; growth = trend) and top videos (views, ad / shop-link flags) | Organic TikTok attention |
| Google Trends | Apify (`agenscrape/google-trends-scraper`, run with 1 GB memory) | 12 months of weekly interest per keyword (US) plus top and rising related searches | Broad demand; rising related searches also seed new ideas |
| Amazon Best Sellers | Apify (`amazon-scraper/amazon-bestsellers-scraper`) | Top 50 in 24 physical-product categories, weekly. Movers are computed from our own history (new entries, rank jumps), because Amazon's Movers & Shakers pages return no data to scrapers (tested 2026-09-23). | Breakout demand discovery (the main seed source) |
| Reddit | Official API (PRAW), free, daily | Posts matching buy-intent phrases in ~20 subreddits (lists in `config.yaml`) | Early demand |

**Dropped: TikTok Creative Center** (tested 2026-09-23). The Top Products page is no longer offered by any scraper, and Top Ads scrapers either need the owner's TikTok login cookies or get blocked. Kalodata's keyword search covers the paid-vs-organic signal instead.

**Keyword watchlist.** Google Trends, TikTok and the Kalodata keyword search don't discover products; they look up keywords: first the manual `watchlist` in `config.yaml`, then concept keywords (shortlisted first, then newest). Each has a per-run keyword limit in `config.yaml` to control cost.

### Phase 2
| Source | Access | Role |
|---|---|---|
| Keepa | API (paid, low cost) | Amazon sales-rank history: steady climb vs spike |
| Meta Ad Library | Official API (requires identity verification) | Ad saturation outside TikTok |
| Amazon reviews (1–3 star) | Apify | Pain points and product-improvement angles |
| ImportYeti | Scrape | US import volume per product type (supply signal) |
| 1688 / Alibaba | Apify | Factory-level trends and cost basis (margin estimate) |
| Wikipedia Pageviews API | Free | Curiosity trend for ingredients and materials |
| Pinterest Trends | Scrape | Planning-stage demand |
| Etsy | Official API | Handmade-to-mass-market pipeline |

## Discovery flow (fully open categories)
1. **Seed:** Amazon Best Sellers movers (computed from our history), Kalodata rising products and new launches, Reddit buy-intent mentions, and Google Trends rising related searches form the candidate list.
2. **Resolve:** Map candidates to product concepts.
3. **Enrich:** For each concept, query the other sources by the concept's keywords.
4. **Score and rank.**

## Metrics (per concept, per week)
- `demand_velocity_<source>`: week-over-week and 4-week growth rate per source
- `demand_breadth`: the number of sources showing rising demand (independent confirmation matters more than one big spike)
- `tiktok_saturation`: combines Kalodata seller count, creator count, top-seller GMV share, and TikTok video count
- `lead_lag_gap`: demand rising outside TikTok while TikTok Shop saturation stays flat. **This is the key metric.**
- `paid_vs_organic`: the ad share of attention (from Kalodata's video ad ratios; Phase 1)
- `margin_estimate`: typical sell price minus sourcing cost (Phase 2, from 1688)
- `spike_risk`: flags one-off spikes versus sustained climbs

**Implemented definitions (v1).** Full detail is in `features/run.py`; every threshold and weight is in `config.yaml`.
- `velocity_google`: last 4 weeks vs the 4 before (12-month weekly Trends series; ignored when interest is below 5/100).
- `velocity_amazon`: average Best Sellers rank gain vs last week; a new entry counts as +0.5.
- `velocity_reddit`: buy-intent posts in the last 28 days vs the 28 before (needs at least 2).
- `velocity_tiktok`: hashtag total views, week over week.
- `velocity_tiktokshop`: revenue of the top 100 TikTok Shop products for the keyword, week over week (Kalodata's own 7-day growth until there are two weeks of data).
- `tiktok_saturation` (0 to 1): log-scaled blend of sellers, creators, weekly revenue, top-3 seller share and hashtag views, measured against "fully crowded" reference levels.
- `lead_lag_gap`: weighted outside demand growth (Google, Amazon, Reddit) minus TikTok Shop supply growth (sellers + creators, week over week).
- `spike_risk` / `sustained_factor`: from the Trends series. Sustained = how many of the last 8 weeks sit above the prior baseline, mapped to 0.5 to 1.0.
- Stored per concept per week in `concept_weekly`, with the inputs behind each number in `details`.

## Opportunity score (v1, tune later)
```
opportunity = demand_breadth_weighted_velocity
            * (1 / (1 + tiktok_saturation))
            * sustained_trend_factor
            * margin_factor   # neutral (1.0) until Phase 2
```
Keep all weights in `config.yaml` so the owner can adjust them without touching code.

Implemented as follows:
- demand = the sum of (source weight x rising velocity) x (1 + 0.25 per extra rising source). Velocity is capped at +200%, and falling sources count as 0.
- saturation factor = 1 / (1 + 4 x saturation).
- A spike halves the sustained factor.
- Unknown saturation counts as 0.5; unknown sustainedness counts as 0.75.
- The score is multiplied by 100 and ranked per week.
- `tests/test_metrics.py` checks the math against known answers.

Enrichment keywords are prioritized by score: shortlisted concepts first, then the best-scoring concepts alternating with concepts never looked up yet.

## Dashboard (Streamlit)
- Ranked table of concepts showing score, a breakdown of each component, and sparklines
- A concept detail page with every source's time series side by side and the raw items in the concept
- Filters: minimum demand breadth, maximum saturation, trend age
- Manual controls: merge or split concepts, mark a concept as "reviewed," "shortlisted," or "rejected"

Implemented (Milestone 4) in `dashboard/` with three pages: Opportunities, Concept details, Pipeline health.
- Sparklines: Google interest over the last 6 months and score by week.
- "Trend age" filter = when the concept was first spotted.
- Split = move selected items to another or a new concept. Items can also be marked "not a product".
- Keywords are editable, because they drive the enrichment lookups.
- A Pipeline health page shows failures, spend and credit balances, because the scheduled run has no other visible output.
- Launched with `Open Dashboard.bat` and bound to localhost only (`.streamlit/config.toml`).
- Concepts not looked up yet have no score and no rank (rather than 0).

## Build order
1. Repo skeleton, `.env.example`, Supabase schema (migrations), logging, and the `run_daily.py` shell
2. Kalodata collector. Verify it with real data before moving on.
3. Schedule the pipeline (Windows Task Scheduler, daily at noon; collectors weekly or daily per `config.yaml`) right away so history starts accumulating. Each later collector joins as soon as it is verified.
4. Amazon Best Sellers collector
5. TikTok (Apify) collector
6. Google Trends collector. Trends values are relative (0–100) and shift with the time window, so use a fixed window and store the raw series.
7. Reddit collector (subreddit list and buy-intent phrases live in `config.yaml`)
8. Concept clustering
9. Feature and score computation
10. Dashboard
11. Phase 2 sources, one at a time

After each step, run it for real, show the owner sample output, and confirm before continuing.

## Cost guardrails
- Set per-run spending caps on Apify actors. Log estimated cost per run.
- Cache everything. Never re-fetch data that's already stored for the same day.
- Batch Claude API calls, and only classify items that are new.
- Keep an eye on Supabase database size (see Stack).

## Rules
- Respect rate limits. Add retries with backoff.
- Store raw responses so metrics can be recomputed later without re-fetching.
- Don't invent API endpoints. If documentation is missing, stop and ask the owner for it.