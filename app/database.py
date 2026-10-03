import os
import sqlite3
from .config import DB_PATH


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                root TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                md5 TEXT,
                last_scanned INTEGER NOT NULL,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);
            CREATE INDEX IF NOT EXISTS idx_files_md5_size ON files(md5, size);
            CREATE INDEX IF NOT EXISTS idx_files_root ON files(root);

            CREATE TABLE IF NOT EXISTS scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                files_seen INTEGER NOT NULL DEFAULT 0,
                files_hashed INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                message TEXT
            );
            """
        )
