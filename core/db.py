"""Database connection to Supabase (Postgres).

Usage:
    from core.db import connect

    with connect() as conn:
        rows = conn.execute("select * from gapfinder.concepts limit 5").fetchall()

Leaving the "with" block commits the changes (or rolls them back if an error
happened). Rows come back as dictionaries, e.g. row["name"].
"""

import psycopg
from psycopg.rows import dict_row

from core.config import require_env


def connect() -> psycopg.Connection:
    """Open a new connection to the Supabase database."""
    return psycopg.connect(
        require_env("DATABASE_URL"),
        # Passed separately so special characters in the password just work.
        password=require_env("DATABASE_PASSWORD"),
        row_factory=dict_row,
        # Don't use server-side prepared statements. They break on Supabase's
        # transaction pooler, and we gain nothing from them at our volume.
        prepare_threshold=None,
        connect_timeout=15,
    )
