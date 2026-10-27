# Data Flow

Selective **Oracle → PostgreSQL** table & data migration with a small web GUI.

Pick a subset of Oracle tables, map/rename columns, optionally combine
several of them into one table (joined on keys, or stacked), create them in
Postgres,
compare row counts, and run a live, resumable, chunked sync with retry/backoff,
row-level error capture, and a full history log — all from the browser. Pure
Python, so it runs identically on Windows and on the Ubuntu server next to
Postgres. **No ora2pg / Perl / Oracle Instant Client required**
(`python-oracledb` runs in thin mode; `psycopg` v3 does the bulk load via
`COPY`).

## Install & run
From the project root:

**Linux / Ubuntu server**
```bash
chmod +x run.sh
./run.sh
```
**Windows**
```bat
run.bat
```
Both create a venv, install the package, and start the app via the `data-flow`
command. Then open `http://localhost:8000` (or `http://<server-ip>:8000` on the
LAN).

To install manually:
```bash
pip install -e .
data-flow            # launches the server
```
Environment overrides: `DATAFLOW_HOST`, `DATAFLOW_PORT`, `DATAFLOW_DB`
(path to the SQLite state file; defaults to `./data-flow.db`).

### Share a portable Windows package
```bat
.venv\Scripts\python.exe build_package.py
```
Builds `dist\data-flow-<version>-windows.zip` (~32 MB): the app plus its own
Python, so the people you send it to need neither Python nor internet. They
unzip it and double-click `Data Flow.bat`; `READ-ME-FIRST.txt` inside explains
the rest. `docs/` is only packed with `--with-docs` (its guides name internal
systems). The zip never contains a `data-flow.db` (it holds passwords). To bring
saved settings along, use History → **Export all data** on the old copy and
**Import saved settings…** on the new one's Connections tab, or copy the old
`data-flow.db` into the new folder before starting it.

## What it does

1. **Connections** — enter + test Oracle and Postgres connections.
2. **Tables** — list Oracle tables, filter, pick which to migrate, save the
   selection. A "Show row counts (approx.)" button pulls a fast, single-query
   row-count estimate per table from Oracle's optimizer statistics — safe to
   use even on schemas with thousands of tables, since it never does a live
   `COUNT(*)` per table.
3. **Groups** — combine **several Oracle tables into one Postgres table**.
   Pick two or more of your selected tables, name the Postgres target, and
   choose how they combine:
   - **Join on keys** — the tables are joined and the target holds their
     combined columns, one wide row per joined result row. The first table
     in the order drives the join; each later one says which earlier table
     it links to, on which column pairs (composite keys supported), as an
     `inner` join (only matched rows) or a `left` join (keep unmatched rows,
     NULL-filled). **Suggest from Oracle FKs** fills the link conditions
     from the schema's foreign keys so you don't type column names.
     Columns from a left-joined table are declared nullable automatically,
     and name collisions between sources (two tables both having `NAME`)
     are suffixed with the source table — a rename on the Columns tab
     overrides that.
   - **Union** — the tables are shaped alike and their rows are stacked.
     The target gets the union of their mapped columns plus a
     `_source_table` column recording each row's origin, which is what lets
     one source be reloaded without disturbing the others. Columns only
     some sources supply become nullable; if two sources send the same
     column as different Postgres types the group is refused, naming the
     conflict rather than silently picking one.

   A group's target gets **no primary key** (a join can repeat a source's
   key, and a union's sources can collide with each other) — add one by hand
   if the combined data really is unique. A table belongs to at most one
   group, and a grouped table is never created or synced on its own. The
   group is created, drift-checked and recreated as a unit: changing any
   member's mapping, the join conditions or the mode flags the whole group.

4. **Columns** — per-table column mapping: rename columns, rename the target
   table, skip columns, override the Postgres type, and flag a table for
   "fast load" (drop constraints/indexes/FKs before sync, recreate after).
   Detects **schema drift** — if the mapping changes after a table was
   created, it's flagged before the next sync so you can recreate deliberately
   rather than get a silent surprise.

5. **Migrate & Sync**
   - Create the selected tables in Postgres (or recreate them to apply a
     changed mapping) and compare row counts side by side.
   - **Pre-flight checks** before every sync: both connections reachable,
     Oracle read access on each source table, Postgres CREATE/INSERT/ownership
     on the target — a failing check blocks the sync before it touches
     anything, with the specific check and reason reported.
   - **Dry run** — preview the DDL a create/recreate would issue, current row
     counts on both sides, and (for fast-load tables) exactly which
     constraints/indexes would be dropped and recreated — without executing
     anything.
   - **Chunked, resumable sync** — data loads in fixed 10,000-row chunks via
     `COPY`, with a checkpoint recorded in SQLite after each chunk commits. An
     interrupted sync resumes from its last checkpoint instead of reloading
     the whole table (with a "start fresh" override if you want a normal
     truncate+reload instead).
   - **Row-level error capture** — if a chunk's bulk `COPY` fails for a
     data-level reason (bad encoding, a constraint violation not caught by
     pre-flight, a type mismatch), it falls back to inserting that chunk
     row-by-row so one bad row doesn't lose the rest of the table. Skipped
     rows are logged with their row reference and error.
   - **Retry with backoff** on transient connection errors (3 retries,
     exponential 1s/2s/4s) for both Oracle and Postgres calls.
   - **Live progress over SSE**: rows loaded, rows/sec throughput, and an ETA,
     in addition to retry/constraint-drop/restore events.
6. **History** — every sync run (table, start/end time, duration, rows
   loaded/skipped, status) is logged to SQLite and browsable here, so you can
   spot which tables are slow or fail often. Row-level error detail is
   viewable inline and the full run log (summary + row errors) can be
   downloaded as a text file for sharing outside the app.

A minimal footer with attribution appears on every page.

State (connections, column mappings, checkpoints, sync history) lives in
`data-flow.db` (SQLite, gitignored).

## Project layout
```
data-flow/
├─ pyproject.toml          # packaging + the `data-flow` entry point
├─ run.sh / run.bat
├─ LICENSE                 # Apache 2.0
├─ README.md
├─ tests/                   # pure checks: run with `python tests/test_*.py`
└─ src/dataflow/
   ├─ cli.py               # console command -> launches uvicorn
   ├─ main.py              # FastAPI routes (config, test, tables, mapping, migrate, sync/SSE, history)
   ├─ engine.py            # orchestration: create/recreate, compare, dry run, chunked sync stream
   ├─ oracle_client.py     # Oracle connect + metadata + chunked/ordered reads (thin mode)
   ├─ pg_client.py         # Postgres connect + DDL + COPY/row-insert load + constraint introspection (psycopg3)
   ├─ groups.py            # table groups: join/union of several Oracle tables into one target
   ├─ mapping.py           # per-table column rename/skip/type overrides + drift fingerprinting
   ├─ types_map.py         # Oracle -> Postgres type translation
   ├─ depgraph.py          # topological sort for FK-safe constraint recreation order
   ├─ retry.py             # transient-error retry/backoff, shared by both DB clients
   ├─ preflight.py         # connectivity + privilege checks run before a sync starts
   ├─ history.py           # SQLite: sync checkpoints, run history, row-level error log
   ├─ store.py             # SQLite key/value settings store (connections, selection, mappings)
   └─ static/              # UI (index.html, app.js, style.css, favicon.ico)
```

## Notes / limits
- Single-user, no auth — **bind to localhost or a trusted LAN only.** Connection
  passwords are stored in plaintext in the SQLite state file; don't expose publicly.
- `types_map.py` covers common Oracle types; extend it for exotic ones.
- Row-count estimates on the Tables tab come from Oracle's optimizer
  statistics, not a live count — they can be stale on tables with heavy recent
  DML but stay fast even on schemas with thousands of tables. Compare/sync
  elsewhere in the app always use exact counts.
- Disk space estimation before a sync is intentionally out of scope.
- A **join group requires a primary key** — or a unique constraint whose
  columns are all `NOT NULL` — on *every* source. The joined result is paged
  by the combination of all of them; without a total order, rows sharing the
  driving table's key straddle a page boundary and the ones after the first
  are silently skipped. A source with neither is refused rather than loaded
  incorrectly.
- A join to a one-to-many relationship **multiplies rows** (one order with
  three lines gives three result rows, repeating the order's columns). That's
  normal SQL join behaviour, not a bug — use the row counts on Migrate &
  Sync, where a join group's Oracle number is the count of the *joined*
  result, to see what you'll actually get.
- A **union group** is append-only and doesn't de-duplicate: a row present in
  two sources lands twice, tagged with different `_source_table` values. Don't
  drop that column — re-syncing one source issues
  `DELETE ... WHERE _source_table = ?` rather than a `TRUNCATE`, which on a
  large source is slower and leaves dead rows for autovacuum.
- Groups are **load-only**: data flows Oracle → the combined Postgres table.
  Nothing writes back, and the group doesn't track which source a joined
  column came from once it's loaded.
- Sync reads chunks by primary key (`WHERE pk > last`), so chunk cost stays
  flat across a large table. Tables **without a primary key** fall back to
  `OFFSET` paging ordered by ROWID, which re-reads and discards every earlier
  row — fine for small tables, quadratic on big ones.
- If the browser closes mid-sync, fast-load constraints are restored before the
  connections close, but silently (there's no stream left to report on). A hard
  kill of the server process while a fast-load sync is between the drop and the
  restore will leave those tables without their PK/FK/indexes — recreate the
  table, or re-run the sync, to get them back.

## Roadmap
- Optional **ora2pg-backed DDL** path as an alternative to `types_map.py`.
- Parallel per-table sync (currently sequential within one run).

## License
Apache License 2.0 — see [LICENSE](LICENSE).

© 2026 Sadman Rahamat Bishal
