"""Restore the `selection` and `mappings` state that a test of mine overwrote.

Why this exists: tests/test_union_columns.py set DATAFLOW_DB *inside* a test
function, but store.DB_PATH is computed at import time and store was already
imported at module scope - so the test's writes landed in the real
./data-flow.db. It replaced the saved table selection and column mappings.
(That test has since been removed and the remaining test files now assert
they are not pointed at the project database.)

Restored from the snapshot taken at the start of that session. Connections,
sync history and row errors were never touched.

NOT restored - and not recoverable from anything I have:
  * per-column renames / skips / Postgres type overrides for EMPLOYEES (11
    columns), ALL_USER_INFO (13) and ACCESS_ISSUE (11). Only the column
    *count* was ever printed, never the contents. If those were all defaults
    you have lost nothing; if you had renamed or skipped columns, re-enter
    them on the Columns tab.
  * created_fingerprint for each table. Left unset on purpose: fabricating
    one from assumed-default columns would make the app report "no drift"
    about a table it can no longer vouch for. Click "Create selected tables
    in Postgres" once to re-establish drift tracking - every table already
    exists, so it reports "exists" and touches no data.

Run:  .venv/Scripts/python.exe restore_state.py          (shows the diff)
      .venv/Scripts/python.exe restore_state.py --apply  (writes it)
"""
import json
import os
import sqlite3
import sys

DB = os.environ.get("DATAFLOW_DB", "data-flow.db")

SELECTION = [
    "ALL_USER_INFO", "EMPLOYEES", "AP_GENAPPMST", "HR_EMPLOYEE",
    "ACCESS_ISSUE", "ATI_STOCKCHK", "AP_ACCESSONMST",
]

# pg_table and the name each table was actually created as, exactly as read
# before the overwrite. columns={} means "no overrides", which resolve()
# treats identically to a mapping saved with every column left at default.
MAPPINGS = {
    "EMPLOYEES":     {"pg_table": "employees",     "created_pg_table": "employees"},
    "ALL_USER_INFO": {"pg_table": "all_user_info", "created_pg_table": "all_user_info"},
    "AP_GENAPPMST":  {"pg_table": None,            "created_pg_table": "ap_genappmst"},
    "HR_EMPLOYEE":   {"pg_table": None,            "created_pg_table": "hr_employee"},
    "ACCESS_ISSUE":  {"pg_table": "access_issue",  "created_pg_table": "access_issue"},
    "ATI_STOCKCHK":  {"pg_table": None,            "created_pg_table": "ati_stockchk"},
}
for m in MAPPINGS.values():
    m.update(columns={}, fast_load=False, created_fingerprint=None)


def current(con, key):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def main():
    apply = "--apply" in sys.argv
    con = sqlite3.connect(DB)
    print(f"database: {os.path.abspath(DB)}\n")
    for key, wanted in (("selection", SELECTION), ("mappings", MAPPINGS)):
        print(f"--- {key} ---")
        print(f"  now:     {json.dumps(current(con, key))}")
        print(f"  restore: {json.dumps(wanted)}\n")
    if not apply:
        print("Nothing written. Re-run with --apply to restore.")
        return
    for key, wanted in (("selection", SELECTION), ("mappings", MAPPINGS)):
        con.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(wanted)),
        )
    con.commit()
    print("Restored. Reload the browser tab, then click "
          '"Create selected tables in Postgres" once to re-establish drift tracking.')


if __name__ == "__main__":
    main()
