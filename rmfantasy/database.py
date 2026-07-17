"""SQLite database: schema creation and connection management."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    password_enc    BLOB NOT NULL,
    profile_dir     TEXT NOT NULL,
    session_valid   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);

CREATE TABLE IF NOT EXISTS lineups (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE,
    riders  TEXT NOT NULL          -- JSON array of 5 rider names
);

CREATE TABLE IF NOT EXISTS wildcard_pool (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    rider    TEXT NOT NULL UNIQUE,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS assignments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    lineup_id   INTEGER NOT NULL REFERENCES lineups(id) ON DELETE CASCADE,
    enabled     INTEGER NOT NULL DEFAULT 1,
    UNIQUE(account_id, lineup_id)
);

CREATE TABLE IF NOT EXISTS rotation_state (
    account_id             INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    lineup_id              INTEGER NOT NULL REFERENCES lineups(id) ON DELETE CASCADE,
    last_wildcard_position INTEGER NOT NULL DEFAULT -1,
    round_number           INTEGER NOT NULL DEFAULT 0,
    updated_at             TEXT,
    PRIMARY KEY (account_id, lineup_id)
);

CREATE TABLE IF NOT EXISTS submission_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_label  TEXT NOT NULL,
    account_email  TEXT NOT NULL,
    lineup_name    TEXT NOT NULL,
    core_five      TEXT NOT NULL,
    wildcard       TEXT NOT NULL,
    round_number   INTEGER NOT NULL,
    round_label    TEXT NOT NULL,
    success        INTEGER NOT NULL,
    message        TEXT NOT NULL,
    timestamp      TEXT NOT NULL
);

-- Cached rider roster scraped from the site's dropdown (for name resolution).
CREATE TABLE IF NOT EXISTS roster (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    rider    TEXT NOT NULL UNIQUE,
    position INTEGER NOT NULL DEFAULT 0
);

-- Simple key/value store for app state (this-round text, roster timestamp...).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with sane defaults and ensure the schema."""
    if db_path is None:
        config.ensure_dirs()
        db_path = config.DB_PATH
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
