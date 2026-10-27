"""PostgreSQL access via psycopg (v3). Uses COPY for fast bulk load."""
import psycopg
from . import retry
from . import types_map


def _retry(label, fn, on_retry=None):
    return retry.call_with_retry(fn, retry.is_pg_transient, f"Postgres: {label}", on_retry=on_retry)


def connect(cfg, on_retry=None):
    def _do():
        return psycopg.connect(
            host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"],
            user=cfg["user"], password=cfg["password"],
        )
    return _retry("connect", _do, on_retry)


def test(cfg, on_retry=None):
    with connect(cfg, on_retry=on_retry) as con:
        def _do():
            with con.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        _retry("test query", _do, on_retry)
    return True


def ensure_schema(con, schema, on_retry=None):
    def _do():
        with con.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        con.commit()
    _retry("ensure schema", _do, on_retry)


# pg_catalog rather than information_schema: the latter hides tables the
# current user holds no privilege on, so they'd look missing.
_TABLES_SQL = (
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r', 'p') AND n.nspname = %s"
)


def table_exists(con, schema, table, on_retry=None):
    def _do():
        with con.cursor() as cur:
            cur.execute(_TABLES_SQL + " AND c.relname = %s", (schema, table))
            return cur.fetchone() is not None
    return _retry("table exists", _do, on_retry)


def list_tables(con, schema, on_retry=None):
    def _do():
        with con.cursor() as cur:
            cur.execute(_TABLES_SQL + " ORDER BY c.relname", (schema,))
            return [r[0] for r in cur.fetchall()]
    return _retry("list tables", _do, on_retry)


def get_columns(con, schema, table, on_retry=None):
    """[{name, type, not_null, has_default}] in column order, for mapping
    onto an existing table."""
    def _do():
        with con.cursor() as cur:
            cur.execute(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                "a.atthasdef OR a.attidentity <> '' OR a.attgenerated <> '' "
                "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped "
                "ORDER BY a.attnum",
                (schema, table),
            )
            return [dict(name=n, type=t, not_null=nn, has_default=d)
                    for n, t, nn, d in cur.fetchall()]
    return _retry("get columns", _do, on_retry)


def create_table_ddl(schema, table, coldefs, pk):
    """The CREATE statement create_table() would run. Shared with the dry-run
    preview, so the DDL shown there is the DDL that executes rather than a
    second rendering of it that can drift."""
    cols_sql = ",\n  ".join(coldefs)
    pk_sql = ""
    if pk:
        pk_cols = ", ".join(f'"{c}"' for c in pk)
        pk_sql = f',\n  PRIMARY KEY ({pk_cols})'
    return f'CREATE TABLE IF NOT EXISTS "{schema}"."{table}" (\n  {cols_sql}{pk_sql}\n)'


def create_table(con, schema, table, coldefs, pk, on_retry=None):
    ddl = create_table_ddl(schema, table, coldefs, pk)

    def _do():
        with con.cursor() as cur:
            cur.execute(ddl)
        con.commit()
    _retry("create table", _do, on_retry)
    return ddl


def row_count(con, schema, table, source=None, on_retry=None):
    """Row count, or just one Oracle table's rows when `source` is given - a
    merged target holds several sources' rows side by side, tagged with
    types_map.SOURCE_COLUMN."""
    where = f' WHERE "{types_map.SOURCE_COLUMN}" = %s' if source else ""
    params = (source,) if source else ()

    def _do():
        with con.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"{where}', params)
            return cur.fetchone()[0]
    return _retry("row count", _do, on_retry)


LOOKUP_CACHE_ROWS = 2_000_000  # bigger lookup tables are queried per chunk instead of held in memory


def lookup_source(con, schema, lookup, on_retry=None):
    """get(keys) -> {key: value} for a mapping.lookups() entry; keys and
    values are text, a key one value per match column. Nothing is read until
    the first call, so a parent table loading earlier in the same sync is in
    place by then. A table of up to LOOKUP_CACHE_ROWS rows is then read once
    and kept; past that each call fetches just its keys.

    Matching is on the text form on both sides, so it doesn't depend on
    which of the two paths a table takes."""
    table, match, value = lookup["table"], lookup["match"], lookup["value"]
    name = f'"{schema}"."{table}"'
    cols = ", ".join(f't."{c}"::text' for c in match)
    select = f'SELECT {cols}, t."{value}"::text FROM {name} t'
    state = {}

    def read(sql, params=()):
        def _do():
            try:
                with con.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.fetchall()
            except Exception:
                con.rollback()
                raise
        out = {}
        for r in _retry(f"lookup {table}", _do, on_retry):
            k, v = tuple(r[:-1]), r[-1]
            if out.get(k, v) != v:
                raise RuntimeError(
                    f'Lookup on {table}: {" / ".join(k)} matches more than one row - '
                    f'{", ".join(match)} must pick out a single row.')
            out[k] = v
        return out

    def batched_sql():
        # each key compared as the column's own type, so an index on it is used
        def _do():
            with con.cursor() as cur:
                cur.execute(
                    "SELECT a.attname, n.nspname, t.typname FROM pg_attribute a "
                    "JOIN pg_type t ON t.oid = a.atttypid JOIN pg_namespace n ON n.oid = t.typnamespace "
                    "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped",
                    (name,))
                return {c: (ns, ty) for c, ns, ty in cur.fetchall()}
        types = _retry(f"lookup {table} types", _do, on_retry)
        missing = [c for c in match if c not in types]
        if missing:
            raise RuntimeError(f'Lookup on {table}: no column {", ".join(missing)}.')
        ks = ", ".join(f"k{i}" for i in range(len(match)))
        arrays = ", ".join(["%s::text[]"] * len(match))
        on = lambda typed: " AND ".join(  # noqa: E731
            f't."{c}" = k{i}::"{types[c][0]}"."{types[c][1]}"' if typed else f't."{c}"::text = k{i}'
            for i, c in enumerate(match))
        join = f" JOIN unnest({arrays}) AS k({ks}) ON "
        return select + join + on(True), select + join + on(False)

    def get(keys):
        if "all" not in state:
            n = row_count(con, schema, table, on_retry=on_retry)
            not_null = " AND ".join(f't."{c}" IS NOT NULL' for c in match)
            state["all"] = read(f"{select} WHERE {not_null}") if n <= LOOKUP_CACHE_ROWS else None
            state["sql"] = None if n <= LOOKUP_CACHE_ROWS else batched_sql()
        if state["all"] is not None:
            return state["all"]
        params = [list(col) for col in zip(*(k for k in keys if None not in k))]
        if not params:
            return {}
        typed, as_text = state["sql"]
        try:
            return read(typed, params)
        except psycopg.errors.DataError:
            # ponytail: a key that isn't valid for the column's type (e.g. "A1"
            # vs an integer column) sends the chunk down the unindexed text compare
            return read(as_text, params)
    return get


def drop_table(con, schema, table, on_retry=None):
    def _do():
        with con.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{schema}"."{table}"')
        con.commit()
    _retry("drop table", _do, on_retry)


def truncate(con, schema, table, on_retry=None):
    """`table` may be a list: one TRUNCATE over several tables is allowed
    even when they reference each other, where one-at-a-time is refused."""
    tables = [table] if isinstance(table, str) else table
    names = ", ".join(f'"{schema}"."{t}"' for t in tables)

    def _do():
        with con.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {names}")
        con.commit()
    _retry("truncate", _do, on_retry)


def fk_edges(con, schema, on_retry=None):
    """(child, parent) table pairs for every FK in the schema."""
    def _do():
        with con.cursor() as cur:
            cur.execute(
                "SELECT c.relname, p.relname FROM pg_constraint k "
                "JOIN pg_class c ON c.oid = k.conrelid JOIN pg_class p ON p.oid = k.confrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE k.contype = 'f' AND n.nspname = %s", (schema,))
            return cur.fetchall()
    return _retry("fk edges", _do, on_retry)


def delete_source_rows(con, schema, table, source, on_retry=None):
    """Clear one source table's rows from a merged target, so re-syncing one
    source leaves the others alone. The single-source path uses TRUNCATE,
    which here would take every other source's rows with it."""
    sql = f'DELETE FROM "{schema}"."{table}" WHERE "{types_map.SOURCE_COLUMN}" = %s'

    def _do():
        with con.cursor() as cur:
            cur.execute(sql, (source,))
        con.commit()
    _retry("delete source rows", _do, on_retry)


def check_privileges(con, schema, table, on_retry=None):
    """Best-effort pre-flight permission check for a sync target.

    CREATE on the schema covers CREATE TABLE / DROP TABLE. If the table
    already exists we also require INSERT (needed for COPY) and ownership
    (needed to ALTER/DROP its constraints for fast-load recreate) - Postgres
    has no has_table_privilege() privilege type for DROP, ownership is the
    real gate for that, so this is the closest direct check available.
    Returns (ok, reason-or-None).
    """
    def _do():
        with con.cursor() as cur:
            cur.execute("SELECT has_schema_privilege(current_user, %s, 'CREATE')", (schema,))
            can_create = bool(cur.fetchone()[0])
            cur.execute(
                "SELECT tableowner FROM pg_tables WHERE schemaname=%s AND tablename=%s",
                (schema, table),
            )
            row = cur.fetchone()
            if row is None:
                return can_create, None, None
            cur.execute(
                "SELECT has_table_privilege(current_user, %s, 'INSERT'), current_user",
                (f'"{schema}"."{table}"',),
            )
            can_insert, cur_user = cur.fetchone()
            return can_create, bool(can_insert), (row[0] == cur_user)
    can_create, can_insert, is_owner = _retry("check privileges", _do, on_retry)
    if not can_create:
        return False, f'current user lacks CREATE privilege on schema "{schema}"'
    if can_insert is False:
        return False, f'current user lacks INSERT privilege on "{schema}"."{table}" (required for COPY)'
    if is_owner is False:
        return False, f'current user does not own "{schema}"."{table}" (required to add/drop constraints)'
    return True, None


def copy_chunk(con, schema, table, columns, rows, on_retry=None):
    """COPY exactly one already-fetched chunk of rows, as a single retryable
    unit. Unlike copy_load's batch-iterator form, the caller controls chunk
    boundaries so progress/checkpoints can be recorded between chunks (see
    engine.sync_stream). On any failure the transaction is rolled back
    before propagating, so a retried attempt (or the row-by-row fallback
    the caller uses for non-transient failures) starts from a clean state.
    """
    collist = ", ".join(f'"{c}"' for c in columns)
    sql = f'COPY "{schema}"."{table}" ({collist}) FROM STDIN'

    def _do():
        try:
            with con.cursor() as cur:
                with cur.copy(sql) as cp:
                    for row in rows:
                        cp.write_row(row)
            con.commit()
        except Exception:
            con.rollback()
            raise
    _retry("copy chunk", _do, on_retry)


def conflict_key(con, schema, table, mapped, on_retry=None):
    """Columns an upsert matches rows on: the primary key, else the first
    non-partial unique index whose columns are all in `mapped` (so not an
    identity PK Postgres fills itself). An unmapped column is fine when it is
    always NULL (no default) and the index is NULLS NOT DISTINCT - NULL then
    matches NULL. [] when no key qualifies."""
    def ok(cols, nulls_match, always_null):
        # at least one mapped column, or every row would match the same all-NULL key
        return any(c in mapped for c in cols) and all(
            c in mapped or (nulls_match and n) for c, n in zip(cols, always_null))

    def _do():
        with con.cursor() as cur:
            cur.execute(
                # to_jsonb: indnullsnotdistinct only exists from Postgres 15
                "SELECT array_agg(a.attname ORDER BY k.ord), "
                "COALESCE(bool_or((to_jsonb(i) ->> 'indnullsnotdistinct')::bool), false), "
                "array_agg(NOT a.atthasdef AND a.attidentity = '' AND a.attgenerated = '' ORDER BY k.ord) "
                "FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
                "WHERE n.nspname = %s AND c.relname = %s AND (i.indisprimary OR i.indisunique) "
                "AND i.indpred IS NULL AND i.indexprs IS NULL "
                "GROUP BY i.indexrelid, i.indisprimary "
                "ORDER BY i.indisprimary DESC, i.indexrelid",
                (schema, table),
            )
            return next((list(k) for k, nnd, null in cur.fetchall() if ok(k, nnd, null)), [])
    return _retry("conflict key", _do, on_retry)


def upsert_tail(columns, key):
    """ON CONFLICT clause updating every non-key column from the new row,
    leaving rows whose values are unchanged alone. No key: plain insert,
    skipping rows that clash with any unique key. The INSERT must alias its
    target `_t`."""
    if not key:
        return " ON CONFLICT DO NOTHING"
    keys = ", ".join(f'"{c}"' for c in key)
    rest = [c for c in columns if c not in key]
    if not rest:
        return f" ON CONFLICT ({keys}) DO NOTHING"
    sets = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in rest)
    old = ", ".join(f'_t."{c}"' for c in rest)
    new = ", ".join(f'EXCLUDED."{c}"' for c in rest)
    return f" ON CONFLICT ({keys}) DO UPDATE SET {sets} WHERE ROW({old}) IS DISTINCT FROM ROW({new})"


def upsert_chunk(con, schema, table, columns, rows, key, on_retry=None):
    """Load a chunk without emptying the table: COPY into a temp staging
    table, then INSERT ... ON CONFLICT (key) DO UPDATE into the target. For
    tables other tables reference, where TRUNCATE is refused. Returns
    (inserted, updated); the rest of the chunk was already up to date."""
    collist = ", ".join(f'"{c}"' for c in columns)
    target = f'"{schema}"."{table}"'

    def _do():
        try:
            with con.cursor() as cur:
                cur.execute(f"CREATE TEMP TABLE _df_stage ON COMMIT DROP AS "
                            f"SELECT {collist} FROM {target} WITH NO DATA")
                with cur.copy(f"COPY _df_stage ({collist}) FROM STDIN") as cp:
                    for row in rows:
                        cp.write_row(row)
                # xmax = 0 only on a freshly inserted row
                cur.execute(f"INSERT INTO {target} AS _t ({collist}) SELECT {collist} FROM _df_stage"
                            + upsert_tail(columns, key) + " RETURNING (xmax = 0)")
                fresh = [r[0] for r in cur.fetchall()]
            con.commit()
            return sum(fresh), len(fresh) - sum(fresh)
        except Exception:
            con.rollback()
            raise
    return _retry("upsert chunk", _do, on_retry)


def insert_rows_capture_errors(con, schema, table, columns, rows, on_row_error, on_retry=None,
                               tail=""):
    """Row-by-row fallback for a chunk whose bulk COPY failed for a
    non-transient (data-level) reason: insert each row in its own
    subtransaction so one bad row (encoding, constraint violation, type
    mismatch) doesn't lose the rest of the chunk. `on_row_error(index,
    error)` fires for each row that fails, after its subtransaction is
    rolled back to the savepoint - the rest of the chunk keeps going.
    Returns (ok_count, failed_count). `tail` is appended to each INSERT
    (an upsert's ON CONFLICT clause).
    """
    collist = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f'INSERT INTO "{schema}"."{table}" AS _t ({collist}) VALUES ({placeholders})' + tail
    ok = 0
    failed = 0
    for i, row in enumerate(rows):
        try:
            def _do(row=row):
                with con.transaction():
                    with con.cursor() as cur:
                        cur.execute(sql, row)
            _retry("insert row", _do, on_retry)
            ok += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            on_row_error(i, e)
    con.commit()
    return ok, failed


def introspect_constraints(con, schema, table, on_retry=None):
    """Capture ready-to-run DDL for every PK/unique/FK constraint and every
    standalone index on a table, so a drop-then-recreate can restore them
    exactly - column renames/skips mean this can't just be regenerated from
    the Oracle source schema.

    Returns {"pk": [...], "unique": [...], "fk": [...], "index": [...]},
    each item a dict with "name" and "ddl" (the fragment to use after
    ALTER TABLE ... ADD CONSTRAINT <name>, or a full CREATE INDEX statement
    for indexes). FK items also carry "ref_table" for dependency ordering.
    """
    qualified = f'"{schema}"."{table}"'

    def _do_constraints():
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT c.conname, c.contype, pg_get_constraintdef(c.oid),
                       ci.relname, cf.relname
                FROM pg_constraint c
                LEFT JOIN pg_class ci ON ci.oid = c.conindid
                LEFT JOIN pg_class cf ON cf.oid = c.confrelid
                WHERE c.conrelid = %s::regclass
                """,
                (qualified,),
            )
            return cur.fetchall()
    rows = _retry("introspect constraints", _do_constraints, on_retry)

    pk, unique, fk = [], [], []
    backing_index_names = set()
    for conname, contype, condef, index_name, ref_table in rows:
        item = {"name": conname, "ddl": condef}
        if contype == "p":
            pk.append(item)
        elif contype == "u":
            unique.append(item)
        elif contype == "f":
            item["ref_table"] = ref_table
            fk.append(item)
        if index_name:
            backing_index_names.add(index_name)

    def _do_indexes():
        with con.cursor() as cur:
            cur.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
                (schema, table),
            )
            return cur.fetchall()
    idx_rows = _retry("introspect indexes", _do_indexes, on_retry)
    index = [{"name": n, "ddl": d} for n, d in idx_rows if n not in backing_index_names]

    return {"pk": pk, "unique": unique, "fk": fk, "index": index}


def drop_constraint(con, schema, table, name, on_retry=None):
    def _do():
        with con.cursor() as cur:
            cur.execute(f'ALTER TABLE "{schema}"."{table}" DROP CONSTRAINT "{name}"')
        con.commit()
    _retry("drop constraint", _do, on_retry)


def drop_index(con, schema, name, on_retry=None):
    def _do():
        with con.cursor() as cur:
            cur.execute(f'DROP INDEX IF EXISTS "{schema}"."{name}"')
        con.commit()
    _retry("drop index", _do, on_retry)


def add_constraint(con, schema, table, name, ddl, on_retry=None):
    def _do():
        with con.cursor() as cur:
            cur.execute(f'ALTER TABLE "{schema}"."{table}" ADD CONSTRAINT "{name}" {ddl}')
        con.commit()
    _retry("add constraint", _do, on_retry)


def create_index(con, ddl, on_retry=None):
    """`ddl` is a full statement from pg_indexes.indexdef (already
    schema/table-qualified)."""
    def _do():
        with con.cursor() as cur:
            cur.execute(ddl)
        con.commit()
    _retry("create index", _do, on_retry)
