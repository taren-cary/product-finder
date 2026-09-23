"""Loads settings from config.yaml and secrets from .env.

Usage:
    from core.config import settings, require_env

    settings["retries"]["attempts"]      # a value from config.yaml
    require_env("APIFY_API_TOKEN")       # a secret from .env (errors clearly if missing)
"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

# The project's root folder (the one containing config.yaml).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env into environment variables. Does nothing if .env is missing.
load_dotenv(PROJECT_ROOT / ".env")

with open(PROJECT_ROOT / "config.yaml", encoding="utf-8") as f:
    settings = yaml.safe_load(f) or {}


def require_env(name: str) -> str:
    """Return a secret from .env, or stop with a clear message if it's missing."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Add it to the .env file in {PROJECT_ROOT} "
            f"(see .env.example for the format)."
        )
    return value
