"""Environment + YAML configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.getenv("FINANCE_CONFIG_DIR", ROOT / "config"))

COUNTRY_NAMES = {"US": "United States", "UK": "United Kingdom", "CA": "Canada", "AU": "Australia"}


def _load(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass(frozen=True)
class Settings:
    database_url: str            # fallback for every component (SQLite in tests)
    component_urls: dict = field(repr=False)
    app_env: str                 # local | production
    llm_provider: str            # gemini | mock
    gemini_api_key: str | None
    desk_model: str
    writer_model: str
    user_agent: str
    http_timeout: float
    feeds: dict = field(repr=False)
    taxonomy: dict = field(repr=False)
    editorial: dict = field(repr=False)
    categories: dict = field(repr=False, default_factory=dict)

    def db_url(self, component: str) -> str:
        """Connection URL for 'pipeline', 'newsroom', 'site' or 'admin'."""
        return self.component_urls.get(component) or self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'finance.db'}"),
        component_urls={
            c: os.getenv(f"{c.upper()}_DATABASE_URL")
            for c in ("pipeline", "newsroom", "site", "admin")
            if os.getenv(f"{c.upper()}_DATABASE_URL")
        },
        app_env=os.getenv("APP_ENV", "local"),
        llm_provider=os.getenv("LLM_PROVIDER", "gemini"),
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        # Model aliases track Google's latest release; pin a dated model in prod
        # if you need reproducible output.
        desk_model=os.getenv("GEMINI_DESK_MODEL", "gemini-flash-latest"),
        writer_model=os.getenv("GEMINI_WRITER_MODEL", "gemini-flash-latest"),
        user_agent=os.getenv(
            "INGEST_USER_AGENT",
            "StoryHubBot/0.1 (+https://yourdomain.com/bot; news aggregation)",
        ),
        http_timeout=float(os.getenv("HTTP_TIMEOUT", "20")),
        feeds=_load("feeds.yaml"),
        taxonomy=_load("taxonomy.yaml"),
        editorial=_load("editorial.yaml"),
        categories=_load("categories.yaml"),
    )
