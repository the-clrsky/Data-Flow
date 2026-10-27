# Data Flow

[![Latest release](https://img.shields.io/github/v/release/the-clrsky/data-flow)](https://github.com/the-clrsky/data-flow/releases/latest)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

Selective **Oracle → PostgreSQL** table and data migration, driven from a small
web GUI.

Pick the Oracle tables you want, map and rename their columns, translate values
through dictionaries, combine several tables into one (or split one into
several), create the tables in Postgres and run a live, resumable, chunked sync.
It's pure Python, so it runs the same on Windows and Linux. **No ora2pg, Perl
or Oracle Instant Client needed:** `python-oracledb` runs in thin mode and
`psycopg` 3 bulk-loads with `COPY`.

## Features

- **Sets**: save several Oracle/Postgres pairs, each with its own tables,
  mappings, dictionaries and history, and switch between them from the top bar.
- **Column mapping**: rename, skip, retype, fan one column out to several, fill
  nulls, fill Postgres-only columns.
- **Dictionaries and Postgres lookups**: swap old values for new ones from an
  Excel/CSV table, or from a table already in Postgres.
- **Table groups**: join several Oracle tables into one wide table, stack
  look-alike tables into one, or split one table into several.
- **Safe syncs**: pre-flight checks, dry run, schema drift detection, 10,000-row
  chunks with checkpoints, row-level error capture, retry with backoff.
- **Live progress** (rows/sec, ETA) and a full **sync history**.
- **Export / import** of sets as one file, with or without passwords.

## Download (Windows)

Get `data-flow-<version>-windows.zip` from
[Releases](https://github.com/the-clrsky/data-flow/releases/latest). It brings
its own Python, so nothing else needs installing. Unzip it, double-click
`Data Flow.bat`, and open <http://localhost:8000>. `READ-ME-FIRST.txt` inside
explains the rest.

## Run from source

Requires **Python 3.11+** and network access to your Oracle and Postgres
servers.

**Linux / macOS**
```bash
chmod +x run.sh
./run.sh
```

**Windows**
```bat
run.bat
```

Both create a virtual environment, install the package and start the app. Then
open <http://localhost:8000>.

Or install it yourself:
```bash
pip install -e .
data-flow
```

| Environment variable | Default | Meaning |
|---|---|---|
| `DATAFLOW_HOST` | `0.0.0.0` | Address to listen on. Use `127.0.0.1` to keep it to this computer. |
| `DATAFLOW_PORT` | `8000` | Port. |
| `DATAFLOW_DB` | `./data-flow.db` | SQLite file holding your settings and history. |

## How to use it

### Sets

A **set** is one Oracle/Postgres pair with everything built on it: its table
selection, groups, dictionaries, lookups, mappings and sync history. The menu at
the top right shows the active set in its colour. Pick another set there and
every tab switches to it.

- **+ New set…** (in the menu) starts an empty set.
- **Save as new set…** (on Connections) starts one with the current set's
  connections.
- **Export this set / Export sets… / Export all** write a `.db` file. Passwords
  are left out unless you tick **Include passwords**.
- **Import…** reads such a file. Its sets are added next to yours (a name you
  already have is overwritten, or comes in as "Name (2)"), or the file replaces
  everything. Files exported before sets existed come in as one set,
  "Default".

### The tabs

1. **Connections**: name the set, pick its colour, and enter and test its
   Oracle and Postgres connections.
2. **Tables**: list the Oracle tables, filter them, and pick the ones to
   migrate. **Show row counts (approx.)** reads Oracle's optimizer statistics
   in one query instead of running `COUNT(*)` per table, so it stays fast on
   schemas with thousands of tables.
3. **Groups**: several Oracle tables into one Postgres table, or the reverse.
   - **Join on keys**: one wide row per joined result. The first table drives
     the join; each later one links to an earlier table on one or more column
     pairs, as an `inner` or `left` join. **Suggest from Oracle FKs** fills the
     links in from foreign keys. Left-joined columns become nullable, and
     clashing column names get the source table as a suffix (a rename on
     Mappings overrides it).
   - **Union**: look-alike tables stacked into one. The target gets every
     mapped column plus `_source_table`, so one source can be reloaded without
     touching the others. A column only some sources have becomes nullable;
     two sources sending one column as different types is refused.
   - **Split**: one Oracle table into several Postgres tables, each with its
     own columns (none ticked = all) and an optional `WHERE` filter. The
     primary key is copied into each.

   A join or union target gets **no primary key**, since a join can repeat a
   source's key and union sources can collide. Add one yourself if the data
   really is unique. A table belongs to at most one group and is then never
   created or synced on its own. Changing any member's mapping, the links or
   the mode flags the whole group for recreation.

4. **Dictionaries**: value translations for the column mappings.
   **Dictionaries** are old → new value tables, uploaded from Excel or CSV
   (header `oracle,postgres`) or typed in. **Postgres lookups** take the value
   from a table already in Postgres, e.g. the `id` of the row whose `code` is
   the Oracle value, matching on one or more columns. Each card shows which
   mapping columns use it. **View** opens a dictionary's entries, and
   **Import from another set…** copies dictionaries from another set.
5. **Mappings**: per table, rename the target table and its columns, skip
   columns, override Postgres types, send a column to several targets, run it
   through a dictionary or lookup, or give it a value when it's null. A
   Postgres-only column of an existing target can be filled with a fixed value
   or a dictionary/lookup of an Oracle column (**Add to Sync**). **Fast load**
   drops constraints, indexes and FKs before the sync and recreates them after.
   **Schema drift** (a mapping changed after its table was created) is flagged
   before the next sync, so recreating is a deliberate choice.
6. **Migrate & Sync**
   - Create (or recreate) the selected tables in Postgres and compare row
     counts side by side.
   - **Pre-flight checks** before every sync: both connections reachable, read
     access on each Oracle table, CREATE/INSERT/ownership on the target. A
     failing check stops the sync before anything is touched.
   - **Dry run**: the DDL a create/recreate would run, row counts on both
     sides, and the constraints a fast load would drop and recreate. Nothing
     is executed.
   - **Chunked, resumable sync**: 10,000-row chunks via `COPY`, with a
     checkpoint after each. An interrupted sync resumes where it stopped, or
     **start fresh** reloads the table.
   - **Row-level error capture**: if a chunk's `COPY` fails on bad data, that
     chunk is inserted row by row, so one bad row doesn't lose the rest. The
     skipped rows are logged with their error.
   - **Retry with backoff** on transient connection errors (3 retries:
     1 s, 2 s, 4 s).
   - **Live progress** over server-sent events: rows loaded, rows/sec, ETA.
7. **History**: every sync run of the set (table, times, duration, rows
   loaded/skipped, status), with each run's row errors to view or download.

## Security

- **There is no login.** Anyone who can reach the port can use the app and its
  saved connections. Run it on `127.0.0.1` or a trusted network only; never
  expose it to the internet.
- **Passwords are stored in plain text** in `data-flow.db`. Keep that file
  private, and keep it out of version control (`.gitignore` already does).
- Share exports **without passwords** unless the other person should be able to
  connect too.

## Limitations

- Row-count estimates on Tables come from optimizer statistics, so they can be
  stale after heavy recent changes. Compare and sync always use exact counts.
- `types_map.py` covers the common Oracle types; extend it for exotic ones.
- A **join group needs a primary key**, or a unique constraint whose columns
  are all `NOT NULL`, on every source. Without a total order, rows sharing a
  key could straddle a page boundary and be skipped, so such a source is
  refused.
- A join across a one-to-many relationship **multiplies rows**, as SQL joins
  do. The Oracle count on Migrate & Sync is the count of the joined result, so
  you can see what you'll get.
- A **union group** is append-only and doesn't de-duplicate. Keep the
  `_source_table` column: re-syncing one source deletes its rows by that
  column rather than truncating, which is slower on large sources.
- Groups are **load-only**: nothing is written back to Oracle.
- Chunks are read by primary key, so cost stays flat on large tables. Tables
  **without a primary key** fall back to `OFFSET` paging, which gets slow on
  big tables.
- If the browser closes mid-sync, fast-load constraints are still restored. If
  the server process is killed between the drop and the restore, those tables
  lose their keys and indexes; recreate the table or re-run the sync.
- Tables sync one after another, not in parallel. Disk space isn't estimated
  before a sync.

## Development

Tests are plain scripts with no databases needed:
```bash
python tests/test_sets.py      # or any tests/test_*.py
```

Build the portable Windows zip (into `dist\`):
```bat
.venv\Scripts\python.exe build_package.py
```
It refuses to package any `.db` file, so saved settings and passwords never
end up in it.

```
data-flow/
├─ pyproject.toml          # packaging and the `data-flow` command
├─ run.sh / run.bat        # create a venv, install, start
├─ build_package.py        # portable Windows zip with its own Python
├─ tests/                  # plain-script checks
└─ src/dataflow/
   ├─ cli.py               # `data-flow` command: starts uvicorn
   ├─ main.py              # FastAPI routes
   ├─ engine.py            # create/recreate, compare, dry run, chunked sync
   ├─ oracle_client.py     # Oracle metadata and chunked reads (thin mode)
   ├─ pg_client.py         # Postgres DDL, COPY / row-insert load, constraints
   ├─ groups.py            # join / union / split groups
   ├─ mapping.py           # column mappings, dictionaries, lookups, drift fingerprints
   ├─ sets.py              # sets: create/switch/delete, export/import
   ├─ store.py             # SQLite settings store, per set
   ├─ history.py           # SQLite checkpoints, sync runs, row errors
   ├─ types_map.py         # Oracle → Postgres type translation
   ├─ depgraph.py          # FK-safe order for recreating constraints
   ├─ preflight.py         # checks run before a sync
   ├─ retry.py             # retry with backoff for both clients
   └─ static/              # the web UI
```

## Roadmap

- Optional ora2pg-based DDL as an alternative to `types_map.py`.
- Syncing several tables in parallel.

## License

[Apache License 2.0](LICENSE). © 2026 [Sadman Rahamat Bishal](https://sadmanbishal.com)
