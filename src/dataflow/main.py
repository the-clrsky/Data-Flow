"""FastAPI backend for the Oracle -> Postgres migration GUI (MVP)."""
import csv
import io
import json
import os
import sqlite3
from datetime import datetime
from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict

from . import store
from . import oracle_client as ora
from . import pg_client as pg
from . import engine
from . import history
from . import groups
from . import mapping
from . import sets as set_store

HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Data Flow")


# ---------- models ----------
class OracleCfg(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    host: str
    port: int = 1521
    service: str
    user: str
    password: str
    schema_: str | None = Field(default=None, alias="schema")  # owner; defaults to user


class PgCfg(BaseModel):
    host: str
    port: int = 5432
    dbname: str
    user: str
    password: str
    target_schema: str = "public"


class Selection(BaseModel):
    tables: list[str]


class ColumnMapping(BaseModel):
    name: str
    target: str | None = None
    skip: bool = False
    pg_type: str | None = None
    if_null: str | None = None
    dictionary: str | None = None
    lookup_with: list[str] | None = None
    # a column of the existing target no Oracle column feeds: `name` is the
    # Oracle column its dictionary/lookup reads, or "" for a fixed value (if_null)
    pg_only: bool = False


class JoinPair(BaseModel):
    left_col: str
    right_col: str


class JoinSpec(BaseModel):
    right: str
    left: str
    type: str = "inner"
    on: list[JoinPair]


class SplitPart(BaseModel):
    pg_table: str
    columns: list[str] = Field(default_factory=list)
    where: str = ""


class GroupSpec(BaseModel):
    """Several Oracle tables feeding one Postgres table (joined on keys, or
    unioned row-wise), or one split into several. See groups.py."""
    target: str
    mode: str = "join"
    sources: list[str]
    joins: list[JoinSpec] = Field(default_factory=list)
    parts: list[SplitPart] = Field(default_factory=list)


class TableMapping(BaseModel):
    pg_table: str | None = None
    columns: list[ColumnMapping]
    fast_load: bool = False
    upsert: bool = False


def _json_default(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _mask(cfg):
    if not cfg:
        return None
    c = dict(cfg)
    if c.get("password"):
        c["password"] = "********"
    return c


# ---------- sets ----------
_syncing = 0  # syncs streaming now; the active set can't change under one


def _busy():
    if _syncing:
        return JSONResponse({"error": "A sync is running - wait for it to finish (or stop it) first."},
                            status_code=409)


class SetSpec(BaseModel):
    name: str
    color: str | None = None
    copy_connections: bool = False


def _set_call(fn, *args):
    try:
        return {"ok": True, "result": fn(*args)}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/sets")
def list_sets():
    return {"sets": set_store.all_sets(), "active": store.active()}


@app.post("/api/sets")
def create_set(spec: SetSpec):
    return _set_call(set_store.create, spec.name, spec.color, spec.copy_connections)


@app.put("/api/sets/{set_id}")
def update_set(set_id: int, spec: SetSpec):
    return _set_call(set_store.update, set_id, spec.name, spec.color)


@app.post("/api/sets/{set_id}/activate")
def activate_set(set_id: int):
    return _busy() or _set_call(set_store.activate, set_id)


@app.delete("/api/sets/{set_id}")
def delete_set(set_id: int):
    return _busy() or _set_call(set_store.delete, set_id)


@app.get("/api/sets/{set_id}/dictionaries")
def set_dictionaries(set_id: int):
    """Another set's dictionaries, to import some into the active one."""
    d = store.get("dictionaries", {}, set_id=set_id)
    return {"dictionaries": [dict(name=n, entries=len(v)) for n, v in sorted(d.items())]}


class DictionaryImport(BaseModel):
    set_id: int
    names: list[str]


@app.post("/api/dictionaries/import")
def import_dictionaries(spec: DictionaryImport):
    return _set_call(mapping.import_dictionaries, spec.set_id, spec.names)


# ---------- config ----------
@app.get("/api/config")
def get_config():
    return {
        "oracle": _mask(store.get("oracle")),
        "pg": _mask(store.get("pg")),
        "selection": store.get("selection", []),
    }


@app.post("/api/config/oracle")
def save_oracle(cfg: OracleCfg):
    store.put("oracle", cfg.model_dump(by_alias=True))
    return {"ok": True}


@app.post("/api/config/pg")
def save_pg(cfg: PgCfg):
    store.put("pg", cfg.model_dump())
    return {"ok": True}


@app.post("/api/test/oracle")
def test_oracle():
    cfg = store.get("oracle")
    if not cfg:
        return {"ok": False, "error": "Save Oracle connection first."}
    try:
        ora.test(cfg)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.post("/api/test/pg")
def test_pg():
    cfg = store.get("pg")
    if not cfg:
        return {"ok": False, "error": "Save Postgres connection first."}
    try:
        pg.test(cfg)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _pg_read(fn):
    """Run fn(con, schema) on a short-lived Postgres connection."""
    cfg = store.get("pg")
    if not cfg:
        return JSONResponse({"error": "Save Postgres connection first."}, status_code=400)
    try:
        con = pg.connect(cfg)
        try:
            return fn(con, cfg["target_schema"])
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/pg/tables")
def pg_tables():
    """Existing tables in the target schema, so a mapping can load into one."""
    return _pg_read(lambda con, schema: {"tables": pg.list_tables(con, schema)})


@app.get("/api/pg/table/{table}/columns")
def pg_table_columns(table: str):
    return _pg_read(lambda con, schema: {"columns": pg.get_columns(con, schema, table)})


# ---------- tables / selection ----------
@app.get("/api/oracle/tables")
def oracle_tables():
    cfg = store.get("oracle")
    if not cfg:
        return JSONResponse({"error": "Save Oracle connection first."}, status_code=400)
    try:
        return {"tables": ora.list_tables(cfg)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/selection")
def save_selection(sel: Selection):
    store.put("selection", sel.tables)
    return {"ok": True, "count": len(sel.tables)}


@app.get("/api/oracle/row-count-estimates")
def oracle_row_count_estimates():
    try:
        return {"counts": engine.oracle_row_count_estimates()}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------- column mapping ----------
@app.get("/api/table/{table}/columns")
def table_columns(table: str):
    try:
        return engine.table_columns(table)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/table/{table}/filled")
def table_filled_columns(table: str):
    """Which Oracle columns have data. Separate from /columns because it
    scans the table and can be slow; the grid highlights them when it lands."""
    try:
        return {"filled": engine.filled_columns(table)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/table/{table}/mapping")
def save_table_mapping(table: str, m: TableMapping):
    error = engine.save_mapping(table, m.pg_table, [c.model_dump() for c in m.columns],
                                m.fast_load, m.upsert)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    return {"ok": True}


@app.post("/api/table/{table}/mapping/new")
def new_table_mapping(table: str):
    """Key for an extra mapping on this Oracle table (stored on first save)."""
    return {"key": mapping.next_extra(mapping.oracle_table(table))}


@app.delete("/api/table/{table}/mapping")
def delete_table_mapping(table: str):
    """Forget a mapping. Never touches Postgres. Deleting a table's main
    mapping also drops its extra mappings and takes the table out of the
    selection, since a selected table always maps somewhere."""
    if not mapping.is_extra(table):
        for k in mapping.extras(table):
            mapping.delete(k)
        store.put("selection", [t for t in store.get("selection", []) if t != table])
    mapping.delete(table)
    return {"ok": True}


@app.get("/api/mappings")
def mappings_status(tables: str = ""):
    names = [t for t in tables.split(",") if t] or store.get("selection", [])
    try:
        return {"rows": engine.mapping_status(names)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------- dictionaries ----------
@app.get("/api/dictionaries")
def list_dictionaries():
    return {"dictionaries": [dict(name=n, entries=len(d)) for n, d in sorted(mapping.dictionaries().items())],
            "lookups": mapping.lookups(),
            "used_by": mapping.dictionary_users()}


@app.get("/api/dictionaries/{name}")
def get_dictionary(name: str):
    """A dictionary's entries as [oracle, postgres] pairs, in upload order."""
    d = mapping.dictionaries().get(name)
    if d is None:
        return JSONResponse({"error": f"No dictionary named {name}."}, status_code=404)
    return {"name": name, "entries": list(d.items())}


@app.post("/api/dictionaries/{name}")
async def save_dictionary(name: str, request: Request, filename: str = "rows.csv", replace: bool = False):
    """The request body is the file itself (.xlsx or delimited text) - no
    multipart, so no extra dependency. Adds/updates entries, or with
    replace swaps the whole dictionary."""
    try:
        rows = mapping.read_table(await request.body(), filename)
        return mapping.save_dictionary(name, rows, replace=replace)
    except Exception as e:  # noqa: BLE001 - bad workbook, bad encoding, bad rows
        return JSONResponse({"error": str(e)}, status_code=400)


class DictionaryKeys(BaseModel):
    oracle: list[str]


@app.post("/api/dictionaries/{name}/delete-entries")
def delete_dictionary_entries(name: str, keys: DictionaryKeys):
    if name not in mapping.dictionaries():
        return JSONResponse({"error": f"No dictionary named {name}."}, status_code=404)
    return mapping.delete_entries(name, keys.oracle)


@app.get("/api/dictionaries/{name}/export")
def export_dictionary(name: str):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["oracle", "postgres"])
    w.writerows(mapping.dictionaries().get(name, {}).items())
    return PlainTextResponse(out.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


@app.delete("/api/dictionaries/{name}")
def delete_dictionary(name: str):
    mapping.delete_dictionary(name)
    return {"ok": True}


class LookupSpec(BaseModel):
    table: str
    match: list[str]
    value: str


@app.post("/api/lookups/{name}")
def save_lookup(name: str, spec: LookupSpec):
    try:
        return mapping.save_lookup(name, spec.table, spec.match, spec.value)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/lookups/{name}")
def delete_lookup(name: str):
    mapping.delete_lookup(name)
    return {"ok": True}


# ---------- table groups ----------
@app.get("/api/groups")
def list_groups():
    return {"groups": groups.all_groups()}


@app.post("/api/groups")
def save_group(spec: GroupSpec):
    group, errors = groups.save(spec.model_dump())
    if errors:
        return JSONResponse({"errors": errors}, status_code=400)
    return {"ok": True, "group": group}


@app.delete("/api/groups/{target}")
def delete_group(target: str):
    groups.delete(target)
    return {"ok": True}


@app.get("/api/oracle/foreign-keys")
def oracle_foreign_keys(tables: str = ""):
    """FKs among the given Oracle tables, so the group editor can offer a
    ready-made link instead of making the user type column names."""
    names = [t for t in tables.split(",") if t]
    try:
        return {"fks": engine.foreign_keys(names)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------- migrate ----------
@app.post("/api/create-tables")
def create_tables(sel: Selection):
    try:
        return {"results": engine.create_tables(sel.tables)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/recreate-tables")
def recreate_tables(sel: Selection):
    try:
        return {"results": engine.recreate_tables(sel.tables)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/compare")
def compare(tables: str = ""):
    names = [t for t in tables.split(",") if t] or store.get("selection", [])
    try:
        return {"rows": engine.compare(names)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/sync")
def sync(tables: str = "", fresh: str = ""):
    names = [t for t in tables.split(",") if t] or store.get("selection", [])
    fresh_tables = {t for t in fresh.split(",") if t}

    def event_stream():
        global _syncing
        _syncing += 1
        try:
            for ev in engine.sync_stream(names, fresh_tables=fresh_tables):
                yield f"data: {json.dumps(ev, default=_json_default)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'fatal', 'error': str(e)})}\n\n"
        finally:
            _syncing -= 1

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------- checkpoints / dry run ----------
@app.get("/api/checkpoints")
def checkpoints(tables: str = ""):
    names = [t for t in tables.split(",") if t] or store.get("selection", [])
    return {"rows": engine.checkpoint_status(names)}


@app.get("/api/dry-run")
def dry_run(tables: str = ""):
    names = [t for t in tables.split(",") if t] or store.get("selection", [])
    try:
        return {"rows": engine.dry_run(names)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------- sync history ----------
@app.get("/api/sync-runs")
def sync_runs(limit: int = 200):
    return {"runs": history.list_runs(limit)}


@app.get("/api/sync-runs/{run_id}/errors")
def sync_run_errors(run_id: int):
    return {"errors": history.list_row_errors(run_id)}


@app.get("/api/sync-runs/{run_id}/export")
def sync_run_export(run_id: int):
    run = history.get_run(run_id)
    if not run:
        return JSONResponse({"error": "run not found"}, status_code=404)
    errors = history.list_row_errors(run_id, limit=100000)

    def ts(v):
        return datetime.fromtimestamp(v).isoformat(sep=" ", timespec="seconds") if v else "—"

    lines = [
        f"Sync run #{run['id']} — {run['table']} -> {run['pg_table']}",
        f"Status: {run['status']}",
        f"Started: {ts(run['started_at'])}",
        f"Ended: {ts(run['ended_at'])}",
        f"Duration: {run['duration']:.1f}s" if run["duration"] is not None else "Duration: —",
        f"Rows loaded: {run['rows_loaded']}",
        f"Rows skipped: {run['rows_skipped']}",
        "",
        f"Row errors ({len(errors)}):",
    ]
    for e in errors:
        lines.append(f"  row {e['row_ref']}: {e['error']}")
    body = "\n".join(lines) + "\n"
    filename = f"sync_run_{run_id}_{run['table']}.txt"
    return PlainTextResponse(body, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------- export / import ----------
@app.get("/api/export")
def export_sets(sets: str = "", passwords: bool = True):
    """Sets (all of them when none are named) with their connections,
    mappings, groups, dictionaries and sync history, as one .db file."""
    ids = [int(i) for i in sets.split(",") if i] or [s["id"] for s in set_store.all_sets()]
    try:
        data = set_store.export(ids, passwords)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return Response(data, media_type="application/vnd.sqlite3",
                    headers={"Content-Disposition": f'attachment; filename="{set_store.export_filename(ids)}"'})


@app.post("/api/import")
async def import_sets(request: Request, mode: str = "inspect", sets: str = "", overwrite: bool = False):
    """The request body is a file from Export. mode=inspect lists its sets;
    mode=add adds sets `sets` of it next to the ones here (a set of the same
    name is overwritten, or with overwrite=false comes in renamed); mode=replace
    swaps everything here for it. Adding or replacing first copies the
    current store to <db>.bak-<timestamp>, so a wrong import can be undone by hand."""
    if mode != "inspect" and (busy := _busy()):
        return busy
    tmp = os.path.join(os.path.dirname(os.path.abspath(store.DB_PATH)),
                       f".import-{datetime.now():%Y%m%d%H%M%S%f}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(await request.body())
        set_store.check_file(tmp)
        if mode == "inspect":
            return {"sets": set_store.file_sets(tmp)}
        backup = set_store.backup()
        if mode == "replace":
            set_store.replace_store(tmp, store.DB_PATH)
            return {"ok": True, "backup": backup}
        added = set_store.add_from(tmp, [int(i) for i in sets.split(",") if i], overwrite)
        return {"ok": True, "added": added, "backup": backup}
    except sqlite3.DatabaseError:
        return JSONResponse({"error": "That is not a Data Flow export (.db file from Export)."},
                            status_code=400)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------- static UI ----------
@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))

@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(HERE, "static", "favicon.ico"))

app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


@app.middleware("http")
async def no_stale_ui(request, call_next):
    """Make the browser revalidate the UI files (cheap 304 via ETag) so an
    updated app.js is never paired with a cached older one."""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp
