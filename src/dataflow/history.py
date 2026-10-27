"""SQLite persistence for sync runs: resumable per-table checkpoints,
row-level error logs, and a history of completed/failed runs.

Shares the same SQLite file as store.py (app settings) but keeps its own
tables, since this data accumulates over time - one row per sync run, one
per bad row - rather than being simple key/value app state.
"""
import sqlite3
import time

from . import store


def ensure(c):
    # table_name is "<set id>/<table>"; see store.py for sets
    c.execute("""CREATE TABLE IF NOT EXISTS sync_checkpoints(
        table_name TEXT PRIMARY KEY,
        last_offset INTEGER NOT NULL,
        updated_at REAL NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS sync_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        table_name TEXT NOT NULL,
        pg_table TEXT,
        started_at REAL NOT NULL,
        ended_at REAL,
        rows_loaded INTEGER NOT NULL DEFAULT 0,
        rows_skipped INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'running',
        set_id INTEGER NOT NULL DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS row_errors(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        table_name TEXT NOT NULL,
        row_ref TEXT,
        error TEXT NOT NULL,
        created_at REAL NOT NULL
    )""")
    if "set_id" not in {r[1] for r in c.execute("PRAGMA table_info(sync_runs)")}:
        # from before sets: it all belongs to set 1, like the settings (store.ensure)
        c.execute("ALTER TABLE sync_runs ADD COLUMN set_id INTEGER NOT NULL DEFAULT 1")
        c.execute("UPDATE sync_checkpoints SET table_name = '1/' || table_name")


def _conn():
    c = sqlite3.connect(store.DB_PATH)
    ensure(c)
    return c


def _ck(table):
    return f"{store.active()}/{table}"


# ---------- checkpoints (resumable chunked loads) ----------
def get_checkpoint(table):
    with _conn() as c:
        row = c.execute(
            "SELECT last_offset, updated_at FROM sync_checkpoints WHERE table_name=?", (_ck(table),)
        ).fetchone()
    return dict(last_offset=row[0], updated_at=row[1]) if row else None


def save_checkpoint(table, offset):
    with _conn() as c:
        c.execute(
            "INSERT INTO sync_checkpoints(table_name, last_offset, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(table_name) DO UPDATE SET last_offset=excluded.last_offset, updated_at=excluded.updated_at",
            (_ck(table), offset, time.time()),
        )


def clear_checkpoint(table):
    with _conn() as c:
        c.execute("DELETE FROM sync_checkpoints WHERE table_name=?", (_ck(table),))


# ---------- sync run history ----------
def start_run(table, pg_table):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO sync_runs(table_name, pg_table, started_at, status, set_id) VALUES (?, ?, ?, 'running', ?)",
            (table, pg_table, time.time(), store.active()),
        )
        return cur.lastrowid


def finish_run(run_id, rows_loaded, rows_skipped, status):
    with _conn() as c:
        c.execute(
            "UPDATE sync_runs SET ended_at=?, rows_loaded=?, rows_skipped=?, status=? WHERE id=?",
            (time.time(), rows_loaded, rows_skipped, status, run_id),
        )


def _run_row(r):
    return dict(
        id=r[0], table=r[1], pg_table=r[2], started_at=r[3], ended_at=r[4],
        duration=(r[4] - r[3] if r[4] else None), rows_loaded=r[5], rows_skipped=r[6], status=r[7],
    )


def list_runs(limit=200):
    with _conn() as c:
        rows = c.execute(
            "SELECT id, table_name, pg_table, started_at, ended_at, rows_loaded, rows_skipped, status "
            "FROM sync_runs WHERE set_id=? ORDER BY id DESC LIMIT ?",
            (store.active(), limit),
        ).fetchall()
    return [_run_row(r) for r in rows]


def get_run(run_id):
    with _conn() as c:
        r = c.execute(
            "SELECT id, table_name, pg_table, started_at, ended_at, rows_loaded, rows_skipped, status "
            "FROM sync_runs WHERE id=?",
            (run_id,),
        ).fetchone()
    return _run_row(r) if r else None


# ---------- row-level error log ----------
def log_row_error(run_id, table, row_ref, error):
    with _conn() as c:
        c.execute(
            "INSERT INTO row_errors(run_id, table_name, row_ref, error, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, table, row_ref, error, time.time()),
        )


def list_row_errors(run_id, limit=1000):
    with _conn() as c:
        rows = c.execute(
            "SELECT id, row_ref, error, created_at FROM row_errors WHERE run_id=? ORDER BY id LIMIT ?",
            (run_id, limit),
        ).fetchall()
    return [dict(id=r[0], row_ref=r[1], error=r[2], created_at=r[3]) for r in rows]


def count_row_errors(run_id):
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM row_errors WHERE run_id=?", (run_id,)).fetchone()[0]
