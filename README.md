# Product Gap Finder

Finds products with rising demand elsewhere but low saturation on TikTok Shop.
See `Product Gap Finder — Project Spec.md` for the full design.

## One-time setup

1. **Python packages** (from this folder, in PowerShell):
   ```
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```

2. **Secrets:** copy `.env.example` to `.env` and fill in the values.
   At minimum, set `DATABASE_URL` and `DATABASE_PASSWORD` (instructions are in the file).

3. **Database tables:** the schema in `supabase/migrations/` is already applied
   to the "Product Finder" Supabase project. New migrations are applied the
   same way (by Claude via the Supabase connection, or with the Supabase CLI).

4. **Check everything:**
   ```
   .venv\Scripts\python check_setup.py
   ```

## Daily use

```
.venv\Scripts\python run_daily.py                  # run everything
.venv\Scripts\python run_daily.py --only kalodata  # run one collector
```

**Automatic runs:** Windows Task Scheduler runs the pipeline every day at
12:00 PM (task name "Product Gap Finder - daily run"). If the PC is off or
asleep at noon, it runs as soon as the PC is back on. To change the time,
open Task Scheduler, find the task, and edit its trigger.

Each collector has its own schedule in `config.yaml`: paid sources run
weekly (Mondays), free ones daily. On other days the weekly collectors are
skipped. If a Monday is missed, they catch up on the next run.

Logs go to the screen and to `logs/gapfinder.log`. Every run is also recorded
in the database (`gapfinder.pipeline_runs` and `gapfinder.source_runs`),
including errors and estimated cost.

## Dashboard

Double-click **Open Dashboard.bat** in this folder. It opens in your browser
(http://localhost:8501); keep the black window open while you use it.

- **Opportunities**: concepts ranked by opportunity score, with filters
  (rising sources, TikTok saturation, first spotted, status, search). Select
  rows to shortlist, mark reviewed, reject, or open one.
- **Concept details**: every source's history side by side, why it scored
  what it did, and the items behind it. Fix grouping mistakes here: move
  items to another or a new concept (split), mark items "not a product",
  merge two concepts, or edit the search keywords that get looked up.
- **Pipeline health**: recent runs and failures, spend this month, database
  size, and live Kalodata/Apify balances.

The dashboard only accepts connections from this computer.

## Where things are

| Folder / file | What it does |
|---|---|
| `config.yaml` | All adjustable settings (collectors on/off, retries, spending caps, later the score weights) |
| `core/` | Shared helpers: settings, database connection, logging, retries |
| `collectors/` | One file per data source. `base.py` handles caching, retries, raw storage, cost caps and error handling for all of them |
| `normalize/` | Turns raw responses into items and daily snapshots |
| `concepts/` | Groups items into product concepts with Claude |
| `features/`, `scoring/` | Weekly metrics per concept and the opportunity score |
| `dashboard/` | The Streamlit dashboard |
| `tests/test_metrics.py` | Checks the metric and score math |
| `supabase/migrations/` | The database schema, as SQL files |
| `run_daily.py` | Runs the whole pipeline in order |
| `check_setup.py` | Verifies keys, database connection and tables |

## Database

All tables live in the `gapfinder` schema in Supabase. In the Supabase
dashboard, open the Table Editor and pick `gapfinder` from the schema dropdown
to browse them. This schema isn't exposed through Supabase's public API,
so only these scripts can read or write it.
