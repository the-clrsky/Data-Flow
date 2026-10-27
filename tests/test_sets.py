"""Checks for sets: a store from before sets becoming "Default", settings
and history kept apart per set, export (with and without passwords) and
import (add, rename, overwrite, replace), copying dictionaries between sets.
Pure: no databases but temporary SQLite files.

    python tests/test_sets.py
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
TMP = tempfile.mkdtemp()
os.environ["DATAFLOW_DB"] = os.path.join(TMP, "t.db")

# a store as 0.2.1 left it: plain keys, sync_runs without set_id
old = sqlite3.connect(os.environ["DATAFLOW_DB"])
old.executescript("""
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE sync_checkpoints(table_name TEXT PRIMARY KEY, last_offset INTEGER NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE sync_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL, pg_table TEXT,
  started_at REAL NOT NULL, ended_at REAL, rows_loaded INTEGER NOT NULL DEFAULT 0,
  rows_skipped INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'running');
CREATE TABLE row_errors(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, table_name TEXT NOT NULL,
  row_ref TEXT, error TEXT NOT NULL, created_at REAL NOT NULL);
INSERT INTO sync_checkpoints VALUES ('EMP', 500, 1);
INSERT INTO sync_runs(table_name, pg_table, started_at, rows_skipped, status) VALUES ('EMP', 'emp', 1, 1, 'success');
INSERT INTO row_errors(run_id, table_name, row_ref, error, created_at) VALUES (1, 'EMP', '7', 'bad', 1);
""")
old.executemany("INSERT INTO settings VALUES(?, ?)", [
    ("oracle", json.dumps(dict(host="ora", password="secret"))),
    ("pg", json.dumps(dict(host="pg", password="pw"))),
    ("selection", json.dumps(["EMP"])),
    ("dictionaries", json.dumps({"SEX": {"M": "1"}, "DEPT": {"10": "A"}})),
])
old.commit()
old.close()

from dataflow import store, history, mapping, sets  # noqa: E402

assert "data-flow.db" not in store.DB_PATH

# migrated into set 1
assert sets.all_sets() == [dict(id=1, name="Default", color="#4a9eff")] and store.active() == 1
assert store.get("selection") == ["EMP"]
assert history.get_checkpoint("EMP")["last_offset"] == 500
assert [r["table"] for r in history.list_runs()] == ["EMP"]

# a new set starts empty, or with the connections only
b = sets.create("Test")
c = sets.create("Copy", copy_connections=True)
assert store.get("selection", set_id=b) is None and store.get("oracle", set_id=b) is None
assert store.get("oracle", set_id=c)["password"] == "secret" and store.get("selection", set_id=c) is None
for bad in ("", " test "):
    try:
        sets.create(bad)
        raise AssertionError(bad)
    except ValueError:
        pass

sets.activate(b)
assert store.get("selection", []) == [] and history.get_checkpoint("EMP") is None and history.list_runs() == []
store.put("selection", ["X"])
history.save_checkpoint("EMP", 9)
assert store.get("selection", set_id=1) == ["EMP"]
sets.activate(1)
assert history.get_checkpoint("EMP")["last_offset"] == 500

# dictionaries from another set: picked ones, overwriting; a lookup's name blocks
sets.activate(b)
mapping.save_dictionary("SEX", [["oracle", "postgres"], ["F", "2"]])
assert mapping.import_dictionaries(1, ["SEX"]) == ["SEX"]
assert mapping.dictionaries() == {"SEX": {"M": "1"}}
mapping.save_lookup("DEPT", "dept", ["code"], "id")
for names in (["DEPT"], ["NOPE"]):
    try:
        mapping.import_dictionaries(1, names)
        raise AssertionError(names)
    except ValueError:
        pass
sets.activate(1)

# export: only the chosen sets, passwords optional
f1 = os.path.join(TMP, "one.db")
with open(f1, "wb") as f:
    f.write(sets.export([1], passwords=False))
sets.check_file(f1)
x = sqlite3.connect(f1)
assert {k.split("/")[0] for (k,) in x.execute("SELECT key FROM settings") if "/" in k} == {"1"}
assert json.loads(x.execute("SELECT value FROM settings WHERE key='1/oracle'").fetchone()[0]) == dict(host="ora", password="")
assert x.execute("SELECT count(*) FROM row_errors").fetchone()[0] == 1
x.close()
assert store.get("oracle")["password"] == "secret"  # the live store is untouched
assert sets.export_filename([1]).startswith("data-flow-Default-")

# import: add as "Default (2)", or overwrite "Default"
assert [s["exists"] for s in sets.file_sets(f1)] == [True]
assert sets.add_from(f1, [1], overwrite=False) == ["Default (2)"]
new = next(s["id"] for s in sets.all_sets() if s["name"] == "Default (2)")
sets.activate(new)
assert store.get("selection") == ["EMP"] and history.get_checkpoint("EMP")["last_offset"] == 500
run = history.list_runs()[0]
assert run["table"] == "EMP" and [e["error"] for e in history.list_row_errors(run["id"])] == ["bad"]
sets.activate(1)
store.put("selection", ["CHANGED"])
assert sets.add_from(f1, [1], overwrite=True) == ["Default"]
assert store.get("selection") == ["EMP"] and store.get("oracle")["password"] == ""
assert len(history.list_runs()) == 1  # replaced, not doubled

# an export from before sets reads as one "Default" set
legacy = os.path.join(TMP, "legacy.db")
x = sqlite3.connect(legacy)
x.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
x.execute("INSERT INTO settings VALUES('selection', '[\"OLD\"]')")
x.commit()
x.close()
assert [s["name"] for s in sets.file_sets(legacy)] == ["Default"]
assert sets.add_from(legacy, [1], overwrite=False) == ["Default (3)"]

# not a Data Flow file
junk = os.path.join(TMP, "junk.db")
sqlite3.connect(junk).execute("CREATE TABLE t(x)").connection.close()
try:
    sets.check_file(junk)
    raise AssertionError("junk accepted")
except sqlite3.DatabaseError:
    pass

# delete: its data goes, the active set moves; the last one stays
sets.activate(b)
sets.delete(b)
assert store.active() != b and store.get("selection", set_id=b) is None
assert all(s["id"] != b for s in sets.all_sets())
for s in sets.all_sets()[1:]:
    sets.delete(s["id"])
try:
    sets.delete(sets.all_sets()[0]["id"])
    raise AssertionError("deleted the last set")
except ValueError:
    pass

# replace: the file becomes the whole store
sets.replace_store(f1, store.DB_PATH)
assert [s["name"] for s in sets.all_sets()] == ["Default"] and store.get("selection") == ["EMP"]
print("ok")
