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
# One connection for the process, serialized by _lock. Per-thread connections
# meant every request thread (Werkzeug spawns one per request) reconnected and
# re-ran the setup below on each save.
_conn = None
_lock = threading.Lock()  # guards _conn and _last_written

def _get_conn() -> sqlite3.Connection:
    """Caller must hold _lock."""
    global _conn
    if _conn is None:
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
        _conn = conn
    return _conn

# JSON text this process last wrote per key. save_state() hands over every key
# on every call, but most are unchanged; rewriting them all is wasted I/O on
# the head unit's SD card.
_last_written: dict = {}

def db_save(data: dict) -> None:
    """Upsert the key-value pairs whose value changed since the last save, atomically."""
    import time
    rows = [(k, json.dumps(v, default=str)) for k, v in data.items()]
    with _lock:
        changed = [(k, s) for k, s in rows if _last_written.get(k) != s]
        if not changed:
            return
        conn = _get_conn()
        now  = time.time()
        with conn:
            conn.executemany(
                'INSERT OR REPLACE INTO state (key, value, updated) VALUES (?, ?, ?)',
                [(k, s, now) for k, s in changed]
            )
        _last_written.update(changed)

def db_load() -> dict:
    """Return all state as a flat dict of {key: python_value}."""
    with _lock:
        conn = _get_conn()
        try:
            rows = conn.execute('SELECT key, value FROM state').fetchall()
            return {k: json.loads(v) for k, v in rows}
        except Exception:
            return {}

def db_has_data() -> bool:
    """True if the DB has any saved state rows."""
    with _lock:
        row = _get_conn().execute('SELECT COUNT(*) FROM state').fetchone()
    return bool(row and row[0] > 0)
