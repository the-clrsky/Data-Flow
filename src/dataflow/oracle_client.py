"""Oracle access via python-oracledb in THIN mode (no Instant Client needed)."""
import oracledb
from . import retry

# Fetch LOBs as str/bytes directly, and numbers as Decimal for precision.
oracledb.defaults.fetch_lobs = False
oracledb.defaults.fetch_decimals = True


def _retry(label, fn, on_retry=None):
    return retry.call_with_retry(fn, retry.is_oracle_transient, f"Oracle: {label}", on_retry=on_retry)


def connect(cfg, on_retry=None):
    def _do():
        dsn = f"{cfg['host']}:{cfg['port']}/{cfg['service']}"
        return oracledb.connect(user=cfg["user"], password=cfg["password"], dsn=dsn)
    return _retry("connect", _do, on_retry)


def owner(cfg):
    return (cfg.get("schema") or cfg["user"]).upper()


def test(cfg, on_retry=None):
    con = connect(cfg, on_retry=on_retry)
    try:
        def _do():
            cur = con.cursor()
            cur.execute("SELECT 1 FROM dual")
            cur.fetchone()
        _retry("test query", _do, on_retry)
        return True
    finally:
        con.close()


def list_tables(cfg, on_retry=None):
    con = connect(cfg, on_retry=on_retry)
    try:
        def _do():
            cur = con.cursor()
            cur.execute(
                "SELECT table_name FROM all_tables WHERE owner = :o ORDER BY table_name",
                o=owner(cfg),
            )
            return cur.fetchall()
        rows = _retry("list tables", _do, on_retry)
        return [r[0] for r in rows]
    finally:
        con.close()


def row_count_estimates(cfg, on_retry=None):
    """Approximate row count for every table under the owner, from
    optimizer statistics (ALL_TABLES.NUM_ROWS) in one query - not a live
    COUNT(*) per table. On schemas with thousands of tables, running a
    COUNT(*) for each one is both too slow and (via a "one giant table list
    in the URL" request) prone to hitting URL length limits; a single
    dictionary query has neither problem. Counts reflect the last time
    stats were gathered and can be stale; None means stats were never
    gathered for that table.
    """
    con = connect(cfg, on_retry=on_retry)
    try:
        def _do():
            cur = con.cursor()
            cur.execute(
                "SELECT table_name, num_rows FROM all_tables WHERE owner = :o",
                o=owner(cfg),
            )
            return cur.fetchall()
        rows = _retry("row count estimates", _do, on_retry)
        return {name: (int(n) if n is not None else None) for name, n in rows}
    finally:
        con.close()


def get_columns(con, owner_, table, on_retry=None):
    def _do():
        cur = con.cursor()
        cur.execute(
            """
            SELECT column_name, data_type, data_precision, data_scale,
                   data_length, char_length, nullable
            FROM all_tab_columns
            WHERE owner = :o AND table_name = :t
            ORDER BY column_id
            """,
            o=owner_, t=table,
        )
        return cur.fetchall()
    rows = _retry("get columns", _do, on_retry)
    cols = []
    for name, dtype, prec, scale, length, clen, nullable in rows:
        cols.append(dict(
            name=name, dtype=dtype, prec=prec, scale=scale,
            length=length, clen=clen, nullable=(nullable == "Y"),
        ))
    return cols


def filled_columns(con, owner_, table, columns, on_retry=None):
    """Names of the `columns` (get_columns() dicts) holding at least one
    non-NULL value. One EXISTS per column, so a column with data stops at its
    first row; only an empty column costs a full scan. LONG columns can't be
    tested with IS NOT NULL and are left out."""
    names = [c["name"] for c in columns if not c["dtype"].startswith("LONG")]
    if not names:
        return []
    probes = ", ".join(
        f'CASE WHEN EXISTS (SELECT 1 FROM "{owner_}"."{table}" WHERE "{n}" IS NOT NULL) THEN 1 ELSE 0 END'
        for n in names)

    def _do():
        cur = con.cursor()
        cur.execute(f"SELECT {probes} FROM dual")
        return cur.fetchone()
    flags = _retry("columns with data", _do, on_retry)
    return [n for n, f in zip(names, flags) if f]


def get_pk(con, owner_, table, on_retry=None):
    def _do():
        cur = con.cursor()
        cur.execute(
            """
            SELECT cc.column_name
            FROM all_constraints c
            JOIN all_cons_columns cc
              ON c.owner = cc.owner AND c.constraint_name = cc.constraint_name
            WHERE c.owner = :o AND c.table_name = :t AND c.constraint_type = 'P'
            ORDER BY cc.position
            """,
            o=owner_, t=table,
        )
        return cur.fetchall()
    rows = _retry("get primary key", _do, on_retry)
    return [r[0] for r in rows]


def get_unique(con, owner_, table, on_retry=None):
    """Columns of the table's first unique constraint, used to order a join
    when the table has no primary key. Returns [] if there is none."""
    def _do():
        cur = con.cursor()
        cur.execute(
            """
            SELECT c.constraint_name, cc.column_name
            FROM all_constraints c
            JOIN all_cons_columns cc
              ON c.owner = cc.owner AND c.constraint_name = cc.constraint_name
            WHERE c.owner = :o AND c.table_name = :t AND c.constraint_type = 'U'
            ORDER BY c.constraint_name, cc.position
            """,
            o=owner_, t=table,
        )
        return cur.fetchall()
    rows = _retry("get unique constraint", _do, on_retry)
    if not rows:
        return []
    first = rows[0][0]
    return [col for name, col in rows if name == first]


def get_foreign_keys(con, owner_, tables, on_retry=None):
    """Foreign keys pointing from one of `tables` to another of them, so the
    group editor can offer a ready-made link instead of making the user type
    column names. Returns [{name, table, columns, ref_table, ref_columns}].
    """
    if not tables:
        return []
    binds = {f"t{i}": t for i, t in enumerate(tables)}
    placeholders = ", ".join(f":{k}" for k in binds)
    binds["o"] = owner_

    def _do():
        cur = con.cursor()
        cur.execute(
            f"""
            SELECT c.constraint_name, c.table_name, cc.column_name,
                   rc.table_name, rcc.column_name
            FROM all_constraints c
            JOIN all_cons_columns cc
              ON c.owner = cc.owner AND c.constraint_name = cc.constraint_name
            JOIN all_constraints rc
              ON c.r_owner = rc.owner AND c.r_constraint_name = rc.constraint_name
            JOIN all_cons_columns rcc
              ON rc.owner = rcc.owner AND rc.constraint_name = rcc.constraint_name
             AND rcc.position = cc.position
            WHERE c.owner = :o AND c.constraint_type = 'R'
              AND c.table_name IN ({placeholders})
              AND rc.table_name IN ({placeholders})
            ORDER BY c.constraint_name, cc.position
            """,
            **binds,
        )
        return cur.fetchall()
    rows = _retry("get foreign keys", _do, on_retry)
    out = {}
    for name, tab, col, ref_tab, ref_col in rows:
        fk = out.setdefault(name, dict(name=name, table=tab, columns=[],
                                       ref_table=ref_tab, ref_columns=[]))
        fk["columns"].append(col)
        fk["ref_columns"].append(ref_col)
    return list(out.values())


def row_count(con, owner_, table, on_retry=None):
    def _do():
        cur = con.cursor()
        cur.execute(f'SELECT COUNT(*) FROM "{owner_}"."{table}"')
        return int(cur.fetchone()[0])
    return _retry("row count", _do, on_retry)


def check_read_access(con, owner_, table, on_retry=None):
    """Cheap read-only probe used by pre-flight: raises if the current
    Oracle user can't SELECT from the table (missing grant, wrong owner,
    typo'd name, etc.) rather than failing deep inside the sync loop."""
    def _do():
        cur = con.cursor()
        cur.execute(f'SELECT 1 FROM "{owner_}"."{table}" WHERE ROWNUM = 1')
        cur.fetchone()
    _retry("check read access", _do, on_retry)


def get_order_columns(con, owner_, table, on_retry=None):
    """Deterministic ordering for OFFSET-based chunked paging: the table's
    primary key if it has one, else ROWID. Needed so each chunk fetch (and
    a resumed run's fetch from a saved offset) sees rows in a stable order."""
    pk = get_pk(con, owner_, table, on_retry=on_retry)
    return pk if pk else ["ROWID"]


def _page_sql(select_exprs, from_sql, key_exprs, offset, chunk_size, last_key):
    """SQL + binds for one ordered page of any SELECT.

    Pages by key (WHERE <key> > <last key>) when `last_key` is supplied,
    else by OFFSET. Oracle has no row-value ">" operator, so a composite
    key comparison is spelled out as
    (a > :k0) OR (a = :k0 AND b > :k1) OR ...

    `key_exprs` must be a *total* order over the result rows. For a join
    that means every source's key, not just the driving table's: two result
    rows sharing the driving row's key would otherwise straddle a page
    boundary and every one after the first would be skipped.
    """
    collist = ", ".join(select_exprs)
    orderby = ", ".join(key_exprs) if key_exprs else "ROWID"
    if last_key and key_exprs:
        terms = []
        for i in range(len(key_exprs)):
            parts = [f"{key_exprs[j]} = :k{j}" for j in range(i)]
            parts.append(f"{key_exprs[i]} > :k{i}")
            terms.append("(" + " AND ".join(parts) + ")")
        where = " WHERE " + " OR ".join(terms)
        tail = " FETCH NEXT :chunk_size ROWS ONLY"
        binds = {f"k{i}": v for i, v in enumerate(last_key)}
    else:
        where = ""
        tail = " OFFSET :chunk_offset ROWS FETCH NEXT :chunk_size ROWS ONLY"
        binds = {"chunk_offset": offset}
    binds["chunk_size"] = chunk_size
    return f"SELECT {collist} FROM {from_sql}{where} ORDER BY {orderby}{tail}", binds


def _chunk_plan(owner_, table, columns, order_cols, offset, chunk_size, last_key):
    """Paging plan for a single table - see fetch_chunk.

    Returns (sql, binds, key_idx, trim_to): `key_idx` are the positions in
    the SELECT list that hold the paging key (empty when the table has no
    usable key), `trim_to` is how many leading values of each returned row
    belong to the caller's `columns`. Key columns the caller skipped are
    appended to the SELECT list so paging still works when the mapping
    drops part of the primary key.
    """
    key_cols = [] if order_cols == ["ROWID"] else list(order_cols)
    extra = [c for c in key_cols if c not in columns]
    sel = list(columns) + extra
    sql, binds = _page_sql([f'"{c}"' for c in sel], f'"{owner_}"."{table}"',
                           [f'"{c}"' for c in key_cols], offset, chunk_size, last_key)
    return sql, binds, [sel.index(c) for c in key_cols], len(columns)


def fetch_page(con, select_exprs, from_sql, key_exprs, offset, chunk_size,
               last_key=None, on_retry=None):
    """Fetch one ordered page of an arbitrary SELECT, as (rows, next_key).

    Used for join groups, where the row source is several tables joined
    together rather than one table. The key expressions are appended to the
    select list so the next page can resume from them, then trimmed off the
    rows handed back.
    """
    sel = list(select_exprs) + list(key_exprs)
    sql, binds = _page_sql(sel, from_sql, key_exprs, offset, chunk_size, last_key)

    def _do():
        cur = con.cursor()
        cur.execute(sql, binds)
        return cur.fetchall()
    rows = _retry("fetch join page", _do, on_retry)
    if not rows:
        return rows, None
    width = len(select_exprs)
    next_key = [rows[-1][width + i] for i in range(len(key_exprs))] if key_exprs else None
    return [r[:width] for r in rows], next_key


def count_rows(con, from_sql, on_retry=None):
    """COUNT(*) over a join's FROM clause - the Oracle-side total for a join
    group's progress, ETA and row-count comparison."""
    def _do():
        cur = con.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {from_sql}")
        return int(cur.fetchone()[0])
    return _retry("join row count", _do, on_retry)


def fetch_chunk(con, owner_, table, columns, order_cols, offset, chunk_size,
                last_key=None, on_retry=None):
    """Fetch one deterministically-ordered page of rows, as (rows, next_key).

    Each chunk is its own retryable unit and its own query (rather than one
    long-lived cursor), so a resumed sync can restart from an arbitrary
    saved offset instead of only from the top of the table.

    Pass the previous chunk's `next_key` back in to page by key
    (WHERE pk > last fetched pk), which is an index range scan on the
    primary key instead of an OFFSET that re-reads and discards every
    earlier row - without it, chunk N costs N*chunk_size rows to fetch.
    The first chunk of a run still uses OFFSET, so a resumed run pays that
    scan once rather than once per chunk; checkpoints stay plain row
    offsets and need no key persistence.

    `next_key` is None for a table with no primary key: ROWID ordering has
    no key we can safely compare against, so those keep paging by OFFSET.
    ponytail: the O(n^2) fetch remains for PK-less tables - revisit with
    ROWID range scans if one turns up big enough to matter.
    """
    sql, binds, key_idx, trim_to = _chunk_plan(
        owner_, table, columns, order_cols, offset, chunk_size, last_key)

    def _do():
        cur = con.cursor()
        cur.execute(sql, binds)
        return cur.fetchall()
    rows = _retry("fetch chunk", _do, on_retry)
    if not rows or not key_idx:
        return rows, None
    next_key = [rows[-1][i] for i in key_idx]
    if trim_to < len(rows[0]):
        rows = [r[:trim_to] for r in rows]
    return rows, next_key
