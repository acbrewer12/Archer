"""
db.py — SQLite persistence layer, replaces archer_memory.json.

Drop-in replacement: db_save() and db_load() have the same interface as the
JSON file but use WAL-mode SQLite for atomic writes and concurrent reads.
Backward-compatible: if the DB is empty, load_state() falls back to the JSON file.
"""
import os
import json
import sqlite3
import threading

# Default to a path under the app directory; override via ARCHER_DB env var
_DB_PATH = os.environ.get('ARCHER_DB', os.path.join(os.path.dirname(__file__), 'archer_data.db'))
_local = threading.local()

def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, 'conn') or _local.conn is None:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS state (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL,
                updated REAL NOT NULL
            )
        ''')
        conn.commit()
        try:
            os.chmod(_DB_PATH, 0o600)
        except OSError:
            pass
        _local.conn = conn
    return _local.conn

def db_save(data: dict) -> None:
    """Upsert all key-value pairs into the state table atomically."""
    import time
    conn = _get_conn()
    now  = time.time()
    with conn:
        conn.executemany(
            'INSERT OR REPLACE INTO state (key, value, updated) VALUES (?, ?, ?)',
            [(k, json.dumps(v, default=str), now) for k, v in data.items()]
        )

def db_load() -> dict:
    """Return all state as a flat dict of {key: python_value}."""
    conn = _get_conn()
    try:
        rows = conn.execute('SELECT key, value FROM state').fetchall()
        return {k: json.loads(v) for k, v in rows}
    except Exception:
        return {}

def db_has_data() -> bool:
    """True if the DB has any saved state rows."""
    conn = _get_conn()
    row = conn.execute('SELECT COUNT(*) FROM state').fetchone()
    return bool(row and row[0] > 0)
