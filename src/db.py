"""SQLAlchemy models and lightweight SQLite migrations for Reels-AutoPilot.

Existing models (`Reel`, `Config`) keep their original columns so nothing that
used them before breaks. New columns and tables are added for multi-account
posting, per-source scrape statistics and session rotation.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Boolean,
    Text,
    create_engine,
    text,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker

import config

# Create the database engine
engine = create_engine(
    "sqlite:///" + config.DB_PATH,
    connect_args={"timeout": 30, "check_same_thread": False},
)

# Create a session factory
Session = sessionmaker(bind=engine)

# Create a base class for declarative models
Base = declarative_base()


class Reel(Base):
    """A scraped reel (or YouTube short) and its posting state."""

    __tablename__ = "reels"

    id = Column(Integer, primary_key=True)
    post_id = Column(String)
    code = Column(String, unique=True, index=True)
    account = Column(String)
    file_name = Column(String)
    file_path = Column(String)
    caption = Column(String)
    data = Column(String)
    is_posted = Column(Boolean)
    posted_at = Column(DateTime)

    # --- multi-account posting -------------------------------------------- #
    assigned_to = Column(String)                 # posting account that owns this reel
    posted_by = Column(String)                   # comma separated accounts that posted it
    swap_phase = Column(Integer, default=0)      # 0 = first assignment, 1+ = swapped


class Config(Base):
    """Key/value configuration overrides edited from the dashboard."""

    __tablename__ = "config"

    id = Column(Integer, primary_key=True)
    key = Column(String)
    value = Column(String)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)


class PostingAccount(Base):
    """An Instagram account the bot posts to."""

    __tablename__ = "posting_accounts"

    id = Column(Integer, primary_key=True)
    username = Column(String, nullable=False, unique=True)
    password = Column(String, nullable=False, default="")
    session_id = Column(String)                  # fallback SESSIONID cookie
    session_file = Column(String)                # path to this account's session.json
    is_enabled = Column(Integer, default=1)
    is_2fa = Column(Integer, default=0)          # skip password login when 2FA is on
    login_status = Column(String, default="unknown")   # ok | failed | 2fa | challenged | transient
    last_error = Column(Text)
    challenged_until = Column(DateTime)          # exponential backoff for challenges
    challenge_count = Column(Integer, default=0)
    last_post_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now)


class ScrapeStatus(Base):
    """Per source-account scraping statistics shown on the dashboard."""

    __tablename__ = "scrape_status"

    id = Column(Integer, primary_key=True)
    account = Column(String, nullable=False, unique=True)
    last_scraped_at = Column(DateTime)
    last_success_at = Column(DateTime)
    last_error = Column(Text)
    consecutive_failures = Column(Integer, default=0)
    reels_found = Column(Integer, default=0)
    reels_downloaded = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.now)


# Create the database schema (only creates what does not exist yet)
Base.metadata.create_all(engine, checkfirst=True)


def _existing_columns(connection, table: str) -> List[str]:
    """Return the column names of an existing SQLite table."""
    rows = connection.execute(text(f'PRAGMA table_info("{table}")')).fetchall()
    return [row[1] for row in rows]


def migrate() -> None:
    """Add new columns/indexes to pre-existing databases (idempotent)."""
    additions = {
        "reels": {
            "assigned_to": "TEXT",
            "posted_by": "TEXT",
            "swap_phase": "INTEGER DEFAULT 0",
        },
        "posting_accounts": {
            "login_status": "TEXT DEFAULT 'unknown'",
            "last_error": "TEXT",
            "challenged_until": "DATETIME",
            "challenge_count": "INTEGER DEFAULT 0",
        },
    }
    with engine.begin() as connection:
        for table, columns in additions.items():
            try:
                present = _existing_columns(connection, table)
            except SQLAlchemyError:
                continue
            if not present:
                continue
            for name, ddl in columns.items():
                if name not in present:
                    connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}'))

        # Unique constraint on reels.code (skipped automatically if duplicates exist)
        try:
            duplicates = connection.execute(
                text("SELECT COUNT(*) FROM (SELECT code FROM reels GROUP BY code HAVING COUNT(*) > 1)")
            ).scalar()
            if not duplicates:
                connection.execute(
                    text('CREATE UNIQUE INDEX IF NOT EXISTS "ix_reels_code_unique" ON "reels" ("code")')
                )
        except SQLAlchemyError:
            pass

        # Concurrency friendly journal mode for the Pi's SD card
        try:
            connection.execute(text("PRAGMA journal_mode=WAL"))
        except SQLAlchemyError:
            pass


migrate()


class ReelEncoder(json.JSONEncoder):
    """JSON encoder able to serialise instagrapi media objects."""

    def default(self, obj):
        if hasattr(obj, "dict"):
            return obj.dict()
        elif hasattr(obj, "model_dump"):
            return obj.model_dump()
        elif hasattr(obj, "isoformat"):
            return obj.isoformat()
        try:
            return {
                "id": str(getattr(obj, "id", "")),
                "pk": str(getattr(obj, "pk", "")),
                "code": str(getattr(obj, "code", "")),
                "caption_text": str(getattr(obj, "caption_text", "")),
                "video_url": str(getattr(obj, "video_url", "")),
            }
        except Exception:
            return str(obj)
