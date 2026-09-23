"""Checks that everything is set up correctly. Safe to run any time.

    python check_setup.py

It reports which keys are present in .env (without printing them),
whether the database is reachable, and whether the tables exist.
"""

import os
import sys

from core.config import PROJECT_ROOT

EXPECTED_TABLES = [
    "pipeline_runs", "source_runs", "raw_responses", "items",
    "item_snapshots", "concepts", "item_concept_map", "concept_signals",
]

# Which keys are needed now vs. later, so "missing" isn't alarming too early.
KEYS = {
    "DATABASE_URL": "required now",
    "DATABASE_PASSWORD": "required now",
    "KALODATA_API_KEY": "needed for the Kalodata collector",
    "APIFY_API_TOKEN": "needed for Amazon / TikTok / Creative Center / Trends",
    "REDDIT_CLIENT_ID": "needed for the Reddit collector",
    "REDDIT_CLIENT_SECRET": "needed for the Reddit collector",
    "ANTHROPIC_API_KEY": "needed for concept clustering",
    "SERPAPI_API_KEY": "optional (only if we use SerpApi for Trends)",
}


def main() -> int:
    ok = True

    print("1. Keys in .env")
    if not (PROJECT_ROOT / ".env").exists():
        print("   .env file not found. Copy .env.example to .env and fill it in.")
        return 1
    for key, why in KEYS.items():
        present = bool(os.getenv(key, "").strip())
        print(f"   {'OK     ' if present else 'missing'}  {key}  ({why})")
    if not (os.getenv("DATABASE_URL", "").strip() and os.getenv("DATABASE_PASSWORD", "").strip()):
        return 1

    print("\n2. Database connection")
    from core.db import connect
    try:
        with connect() as conn:
            version = conn.execute("show server_version").fetchone()["server_version"]
            print(f"   OK  connected (Postgres {version})")

            print("\n3. Tables in the gapfinder schema")
            found = {
                r["table_name"]
                for r in conn.execute(
                    "select table_name from information_schema.tables "
                    "where table_schema = 'gapfinder'"
                ).fetchall()
            }
            for t in EXPECTED_TABLES:
                exists = t in found
                ok &= exists
                print(f"   {'OK     ' if exists else 'MISSING'}  {t}")
            if not found:
                print("   No tables yet. Apply the migrations (see README).")
    except Exception as e:
        print(f"   FAILED: {e}")
        return 1

    print("\nAll good." if ok else "\nSome checks failed (see above).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
