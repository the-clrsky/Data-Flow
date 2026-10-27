"""Sets: named Oracle/Postgres pairs, each with its own selection, mappings,
groups, dictionaries, lookups and sync history (stored as store.py and
history.py describe), and moving them between installations.

An export is a copy of the SQLite store holding only the chosen sets. A file
can be imported two ways: its sets added next to the ones here, or the whole
store replaced by it. A file from before sets reads as one set, "Default".
"""
import json
import os
import re
import sqlite3
from datetime import datetime

from . import store, history

COLORS = ["#4a9eff", "#3fb950", "#d29922", "#f85149", "#a371f7", "#39c5cf"]


def all_sets():
    """[{id, name, color}]"""
    return store.get("sets")


def _find(set_id):
    s = next((s for s in all_sets() if s["id"] == set_id), None)
    if not s:
        raise ValueError(f"No set {set_id}.")
    return s


def _check_name(name, except_id=None):
    name = (name or "").strip()
    if not name:
        raise ValueError("Name the set.")
    if any(s["name"].lower() == name.lower() and s["id"] != except_id for s in all_sets()):
        raise ValueError(f'There is already a set named "{name}".')
    return name


def create(name, color=None, copy_connections=False):
    """A new, empty set; with copy_connections it starts with the active
    set's Oracle and Postgres connections. Returns its id."""
    name = _check_name(name)
    sets = all_sets()
    new_id = max(s["id"] for s in sets) + 1
    sets.append(dict(id=new_id, name=name, color=color or COLORS[(new_id - 1) % len(COLORS)]))
    store.put("sets", sets)
    if copy_connections:
        for key in ("oracle", "pg"):
            cfg = store.get(key)
            if cfg:
                store.put(key, cfg, set_id=new_id)
    return new_id


def update(set_id, name, color=None):
    _find(set_id)
    name = _check_name(name, except_id=set_id)
    store.put("sets", [dict(s, name=name, color=color or s["color"]) if s["id"] == set_id else s
                       for s in all_sets()])


def activate(set_id):
    _find(set_id)
    store.put("active_set", set_id)


def delete(set_id):
    """Forget a set and everything in it. Never touches Oracle or Postgres."""
    _find(set_id)
    rest = [s for s in all_sets() if s["id"] != set_id]
    if not rest:
        raise ValueError("This is the only set - make another one before deleting it.")
    c = _open(store.DB_PATH)
    try:
        with c:
            _wipe(c, set_id)
    finally:
        c.close()
    store.put("sets", rest)
    if store.active() == set_id:
        store.put("active_set", rest[0]["id"])


# ---------- moving sets between stores ----------
def _open(path):
    c = sqlite3.connect(path)
    store.ensure(c)
    history.ensure(c)
    c.commit()
    return c


def _wipe(c, sid):
    c.execute("DELETE FROM settings WHERE key LIKE ?", (f"{sid}/%",))
    c.execute("DELETE FROM sync_checkpoints WHERE table_name LIKE ?", (f"{sid}/%",))
    c.execute("DELETE FROM row_errors WHERE run_id IN (SELECT id FROM sync_runs WHERE set_id=?)", (sid,))
    c.execute("DELETE FROM sync_runs WHERE set_id=?", (sid,))


def _copy(src, dst, sid, tid):
    """Set `sid` of store `src` becomes set `tid` of store `dst`, replacing
    whatever `tid` held. The caller commits."""
    _wipe(dst, tid)
    rename = lambda k: f"{tid}/{k.split('/', 1)[1]}"  # noqa: E731
    dst.executemany("INSERT INTO settings(key, value) VALUES(?, ?)", [
        (rename(k), v) for k, v in src.execute("SELECT key, value FROM settings WHERE key LIKE ?", (f"{sid}/%",))])
    dst.executemany("INSERT INTO sync_checkpoints(table_name, last_offset, updated_at) VALUES(?, ?, ?)", [
        (rename(t), o, u) for t, o, u in src.execute(
            "SELECT table_name, last_offset, updated_at FROM sync_checkpoints WHERE table_name LIKE ?", (f"{sid}/%",))])
    runs = src.execute("SELECT id, table_name, pg_table, started_at, ended_at, rows_loaded, rows_skipped, status "
                       "FROM sync_runs WHERE set_id=? ORDER BY id", (sid,)).fetchall()
    for run in runs:
        new_id = dst.execute(
            "INSERT INTO sync_runs(table_name, pg_table, started_at, ended_at, rows_loaded, rows_skipped, status, set_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (*run[1:], tid)).lastrowid
        dst.executemany("INSERT INTO row_errors(run_id, table_name, row_ref, error, created_at) VALUES (?, ?, ?, ?, ?)", [
            (new_id, *e) for e in src.execute(
                "SELECT table_name, row_ref, error, created_at FROM row_errors WHERE run_id=? ORDER BY id", (run[0],))])


def export(set_ids, passwords):
    """The chosen sets as the bytes of a .db file. Without passwords, the
    connections keep everything but them."""
    live = _open(store.DB_PATH)
    mem = sqlite3.connect(":memory:")
    try:
        live.backup(mem)
        keep = [s for s in all_sets() if s["id"] in set_ids]
        if not keep:
            raise ValueError("Pick at least one set.")
        for s in all_sets():
            if s["id"] not in set_ids:
                _wipe(mem, s["id"])
        mem.execute("UPDATE settings SET value=? WHERE key='sets'", (json.dumps(keep),))
        if store.active() not in set_ids:
            mem.execute("UPDATE settings SET value=? WHERE key='active_set'", (json.dumps(keep[0]["id"]),))
        if not passwords:
            for k, v in mem.execute("SELECT key, value FROM settings "
                                    "WHERE substr(key, instr(key, '/') + 1) IN ('oracle', 'pg')").fetchall():
                mem.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(json.loads(v) | {"password": ""}), k))
        mem.commit()
        mem.execute("VACUUM")  # the deleted sets' pages, and their passwords, out of the file
        return mem.serialize()
    finally:
        mem.close()
        live.close()


def export_filename(set_ids):
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    if len(set_ids) == 1:
        return f"data-flow-{re.sub(r'[^\w-]+', '-', _find(set_ids[0])['name']).strip('-')}-{stamp}.db"
    return f"data-flow-{stamp}.db"


def check_file(path):
    """Raise sqlite3.DatabaseError unless `path` is a Data Flow store."""
    c = sqlite3.connect(path)
    try:
        c.execute("SELECT key, value FROM settings LIMIT 1")  # every store has this
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("file is damaged")
    finally:
        c.close()


def file_sets(path):
    """The sets in the store at `path` (one already checked), each marked
    `exists` when a set here has its name."""
    c = _open(path)  # a file from before sets gets its "Default" set here
    try:
        theirs = json.loads(c.execute("SELECT value FROM settings WHERE key='sets'").fetchone()[0])
    finally:
        c.close()
    mine = {s["name"].lower() for s in all_sets()}
    return [dict(s, exists=s["name"].lower() in mine) for s in theirs]


def backup():
    """Copy the live store to <db>.bak-<timestamp>; returns that file's name."""
    path = f"{store.DB_PATH}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    replace_store(store.DB_PATH, path)
    return os.path.basename(path)


def replace_store(src_path, dest_path):
    """Overwrite the store at dest_path with the one at src_path (SQLite
    backup API: safe while the app has the destination open)."""
    s, d = _open(src_path), sqlite3.connect(dest_path)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def add_from(path, set_ids, overwrite):
    """Add sets `set_ids` of the store at `path` here. A name already here is
    overwritten, or with overwrite=False the set comes in as "Name (2)".
    Returns the names they got."""
    theirs = {s["id"]: s for s in file_sets(path)}
    sets = all_sets()
    by_name = {s["name"].lower(): s for s in sets}
    next_id = max(s["id"] for s in sets) + 1
    plan = []
    for sid in set_ids:
        s = theirs.get(sid)
        if not s:
            raise ValueError(f"The file has no set {sid}.")
        mine = by_name.get(s["name"].lower())
        if mine and overwrite:
            plan.append((sid, mine["id"], mine["name"]))
            continue
        name, n = s["name"], 2
        while name.lower() in by_name:
            name, n = f'{s["name"]} ({n})', n + 1
        new = dict(id=next_id, name=name, color=s.get("color") or COLORS[(next_id - 1) % len(COLORS)])
        sets.append(new)
        by_name[name.lower()] = new
        plan.append((sid, next_id, name))
        next_id += 1
    src, dst = _open(path), _open(store.DB_PATH)
    try:
        with dst:
            for sid, tid, _ in plan:
                _copy(src, dst, sid, tid)
            dst.execute("UPDATE settings SET value=? WHERE key='sets'", (json.dumps(sets),))
    finally:
        src.close()
        dst.close()
    return [name for _, _, name in plan]
