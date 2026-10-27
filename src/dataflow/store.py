"""Tiny SQLite key/value store for app state (connections, table selection).

Everything belongs to a *set* - one Oracle/Postgres pair with its own
selection, mappings, groups, dictionaries and lookups - and is stored as
"<set id>/<key>". Only the list of sets and which one is active are shared.
"""
import sqlite3
import json
import os

DB_PATH = os.environ.get("DATAFLOW_DB", os.path.join(os.getcwd(), "data-flow.db"))
GLOBAL = ("sets", "active_set")


def ensure(c):
    """Create the settings table. A store from before sets (or a new one)
    becomes set 1, "Default", holding everything it had."""
    c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    if not c.execute("SELECT 1 FROM settings WHERE key='sets'").fetchone():
        c.execute("UPDATE settings SET key = '1/' || key WHERE instr(key, '/') = 0")
        c.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", [
            ("sets", json.dumps([dict(id=1, name="Default", color="#4a9eff")])),
            ("active_set", "1"),
        ])


def _conn():
    c = sqlite3.connect(DB_PATH)
    ensure(c)
    return c


def _key(c, key, set_id):
    if key in GLOBAL:
        return key
    if set_id is None:
        set_id = json.loads(c.execute("SELECT value FROM settings WHERE key='active_set'").fetchone()[0])
    return f"{set_id}/{key}"


def get(key, default=None, set_id=None):
    """`key` of the active set, or of set `set_id`."""
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (_key(c, key, set_id),)).fetchone()
        return json.loads(row[0]) if row else default


def put(key, value, set_id=None):
    with _conn() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_key(c, key, set_id), json.dumps(value)),
        )


def active():
    return get("active_set")
