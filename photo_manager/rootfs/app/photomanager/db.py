"""The add-on's SQLite database (/data/photos.db): private to the add-on, never on the NAS."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    rel        TEXT PRIMARY KEY,   -- path relative to the photo folder, '/' separated
    size       INTEGER NOT NULL,
    mtime      REAL NOT NULL,
    taken      TEXT,               -- 'YYYY-MM-DD HH:MM:SS' (EXIF capture time, else file time)
    taken_src  TEXT,               -- 'exif' or 'file'
    camera     TEXT,
    has_jpeg   INTEGER NOT NULL DEFAULT 0,
    sha256     TEXT,
    exif_error TEXT
);
CREATE INDEX IF NOT EXISTS photos_size ON photos(size);
CREATE INDEX IF NOT EXISTS photos_sha ON photos(sha256);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- One row per RAW file we have tried to develop
CREATE TABLE IF NOT EXISTS developed (
    rel        TEXT PRIMARY KEY,   -- RAW path relative to the photo folder (as in photos.rel)
    status     TEXT NOT NULL,      -- 'done' or 'failed'
    sha256     TEXT,               -- RAW hash when known, so identical copies aren't developed twice
    raw_size   INTEGER,
    out_rel    TEXT,               -- the JPEG's path relative to the NAS root
    out_size   INTEGER,
    attempts   INTEGER NOT NULL DEFAULT 0,
    at         REAL,
    error      TEXT
);

-- Photos moved to the quarantine folder (never deleted by this add-on)
CREATE TABLE IF NOT EXISTS quarantined (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rel         TEXT NOT NULL,     -- original path relative to the photo folder
    q_rel       TEXT NOT NULL,     -- where it is now, relative to the NAS root
    keep_rel    TEXT,              -- the identical copy that stayed in the library
    size        INTEGER,
    mtime       REAL,
    crc32       TEXT,
    sha256      TEXT,
    taken       TEXT,
    taken_src   TEXT,
    camera      TEXT,
    moved_at    REAL NOT NULL,
    restored_at REAL
);
"""


@contextmanager
def db(cfg):
    """A short-lived connection: committed on success, rolled back on error, always closed."""
    conn = sqlite3.connect(cfg.db_path, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init(cfg):
    with db(cfg) as conn:
        conn.executescript(SCHEMA)
