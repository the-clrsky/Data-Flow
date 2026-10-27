"""Per-table column/table name mapping (rename + skip + Postgres type
override) and drift fingerprinting.

An Oracle table can have extra mappings, each its own Postgres table with
its own columns. They are stored as ordinary mappings under the key
"<ORACLE_TABLE>:<n>" (":" can't appear in an unquoted Oracle name), so
everything keyed by table name - resolve, drift, checkpoints - works on
them unchanged. Use oracle_table(key) wherever Oracle itself is queried.
An extra mapping starts with every column skipped: you pick what it takes.

A type override just changes the declared Postgres column type; Postgres
parses the incoming COPY text into it. The only value transforms are the
per-column "if null" fill and dictionary / Postgres lookup (value_filler).
"""
import csv
import hashlib
import io
import json

import openpyxl

from . import store
from . import types_map

_KEY = "mappings"


def _all():
    return store.get(_KEY, {})


SEP = ":"


def oracle_table(key):
    return key.split(SEP)[0]


def is_extra(key):
    return SEP in key


def default_table_name(key):
    return key.lower().replace(SEP, "_")


def extras(table):
    """Keys of a table's extra mappings, in creation order."""
    keys = [k for k in _all() if is_extra(k) and oracle_table(k) == table]
    return sorted(keys, key=lambda k: int(k.split(SEP)[1]))


def next_extra(table):
    """Key for a new extra mapping. Nothing is stored until it is saved."""
    nums = [int(k.split(SEP)[1]) for k in extras(table)]
    return f"{table}{SEP}{max(nums, default=1) + 1}"


def expand(table_names):
    """Each table followed by its extra mappings, deduplicated."""
    out = []
    for t in table_names:
        for k in [t] + extras(t):
            if k not in out:
                out.append(k)
    return out


def delete(key):
    all_m = _all()
    all_m.pop(key, None)
    store.put(_KEY, all_m)


def pg_table_owner(pg_table, except_key):
    """The mapping key (other than `except_key`) that already writes to
    `pg_table`, or None."""
    for k, m in _all().items():
        if k != except_key and (m.get("pg_table") or default_table_name(k)) == pg_table:
            return k
    return None


def is_saved(key):
    """Whether a mapping has ever been saved with column choices."""
    return bool(_all().get(key, {}).get("columns"))


def get(table):
    """Raw stored mapping for a table, with defaults filled in."""
    m = _all().get(table, {})
    return {
        "pg_table": m.get("pg_table"),
        "columns": m.get("columns", {}),
        "created_fingerprint": m.get("created_fingerprint"),
        "created_pg_table": m.get("created_pg_table"),
        "fast_load": bool(m.get("fast_load", False)),
        "upsert": bool(m.get("upsert", False)),
    }


def save(table, pg_table, columns, fast_load=False, upsert=False):
    """Persist overrides for a table.

    columns: iterable of {name, target, skip, pg_type} for Oracle column
    names. pg_type is a manual Postgres type override, or falsy to use the
    computed default. fast_load toggles drop/recreate of constraints and
    indexes around each sync for this table (see engine.sync_stream) - it
    doesn't affect DDL shape, so it isn't part of the drift fingerprint.
    """
    all_m = _all()
    entry = all_m.get(table, {})
    entry["pg_table"] = (pg_table or "").strip() or None
    col_map = {}
    for c in columns:
        target = (c.get("target") or "").strip() or None
        pg_type_override = (c.get("pg_type") or "").strip() or None
        # if_null: value loaded instead of an Oracle NULL. Postgres parses
        # the text into the column type. Not DDL - no drift.
        if_null = c.get("if_null")
        dest = {
            "target": target, "skip": bool(c.get("skip")), "pg_type": pg_type_override,
            "if_null": if_null if if_null not in ("", None) else None,
            # dictionary: name of an uploaded old -> new value table every
            # non-NULL value goes through, or of a Postgres lookup. Not DDL - no drift.
            "dictionary": c.get("dictionary") or None,
            # lookup_with: Oracle columns supplying a lookup's 2nd, 3rd... match values
            "lookup_with": [w for w in c.get("lookup_with") or [] if w] or None,
        }
        # an Oracle column listed again is one more destination for it
        if c["name"] in col_map:
            col_map[c["name"]].setdefault("copies", []).append(dest)
        else:
            col_map[c["name"]] = dest
    entry["columns"] = col_map
    entry["fast_load"] = bool(fast_load)
    # upsert: load by INSERT ... ON CONFLICT on the target's key instead of
    # emptying it first (see pg_client.upsert_chunk). Not DDL - no drift.
    entry["upsert"] = bool(upsert)
    all_m[table] = entry
    store.put(_KEY, all_m)
    return get(table)


def resolve(table, oracle_columns):
    """Apply stored overrides + defaults to a live Oracle column list.

    Returns (pg_table_name, [oracle_column_dict with target/skip/pg_type/
    pg_type_override/default_pg_type added]). An Oracle column with several
    destinations yields one entry per destination (same "name"), the extra
    ones flagged copy=True.
    """
    m = get(table)
    overrides = m["columns"]
    resolved = []
    for c in oracle_columns:
        ov = overrides.get(c["name"], {})
        for n, d in enumerate([ov] + ov.get("copies", [])):
            default_pg_type = types_map.pg_type(c)
            pg_type_override = d.get("pg_type")
            resolved.append(dict(
                c, target=d.get("target") or c["name"].lower(),
                skip=bool(d.get("skip", is_extra(table))),
                copy=n > 0,
                default_pg_type=default_pg_type,
                pg_type_override=pg_type_override,
                pg_type=pg_type_override or default_pg_type,
                if_null=d.get("if_null"),
                dictionary=d.get("dictionary"),
                lookup_with=d.get("lookup_with"),
            ))
    pg_table = m["pg_table"] or default_table_name(table)
    return pg_table, resolved


def fan_out(names):
    """For kept columns that may repeat an Oracle column: (the distinct
    names to SELECT, widen). widen(fetch) wraps a fetch so each row comes
    back with one value per entry of `names`. Selecting a column twice is no
    good - Oracle rejects ORDER BY on it (ORA-00960)."""
    uniq = list(dict.fromkeys(names))
    if len(uniq) == len(names):
        return uniq, lambda fetch: fetch
    idx = [uniq.index(n) for n in names]

    def widen(fetch):
        def wide(*a, **kw):
            rows, next_key = fetch(*a, **kw)
            return [tuple(r[i] for i in idx) for r in rows], next_key
        return wide
    return uniq, widen


_DICTS = "dictionaries"


def dictionaries():
    """{name: {oracle value: postgres value}}"""
    return store.get(_DICTS, {})


def _cell(v):
    # an Excel number cell can come back as 858.0
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return "" if v is None else str(v)


def read_table(data, filename):
    """Rows (lists of strings) of an uploaded file: the first sheet of an
    .xlsx, otherwise delimited text split on whichever of comma, semicolon
    or tab its first line uses most."""
    if filename.lower().endswith((".xlsx", ".xlsm")):
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            return [[_cell(v) for v in row] for row in wb.worksheets[0].iter_rows(values_only=True)]
        finally:
            wb.close()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252")  # Excel's "CSV (Comma delimited)" on Windows
    delim = max(",;\t", key=text.split("\n", 1)[0].count)
    return list(csv.reader(io.StringIO(text), delimiter=delim))


def save_dictionary(name, rows, replace=False):
    """Add `rows` - a header row naming an "oracle" and a "postgres" column,
    then entries - to dictionary `name`, overwriting entries whose oracle
    value is already there. With replace, the rows become the whole
    dictionary. Returns {added, updated, total}; raises ValueError for a
    file it can't take, leaving the dictionary unchanged."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Name the dictionary.")
    if name in lookups():
        raise ValueError(f'"{name}" is already a Postgres lookup - pick another name.')
    header = [h.strip().lower() for h in rows[0]] if rows else []
    if "oracle" not in header or "postgres" not in header:
        raise ValueError('The first row must name an "oracle" and a "postgres" column.')
    oi, pi = header.index("oracle"), header.index("postgres")
    entries = {}
    for line, r in enumerate(rows[1:], start=2):
        old = r[oi].strip() if oi < len(r) else ""
        new = r[pi].strip() if pi < len(r) else ""
        if not old and not new:
            continue
        if not old or not new:
            raise ValueError(f"Line {line}: both an oracle and a postgres value are needed.")
        if entries.get(old, new) != new:
            raise ValueError(f'Line {line}: "{old}" is mapped twice, to "{entries[old]}" and "{new}".')
        entries[old] = new
    if not entries:
        raise ValueError("No entries below the header row.")
    all_d = dictionaries()
    base = {} if replace else all_d.get(name, {})
    added = sum(k not in base for k in entries)
    all_d[name] = base | entries
    store.put(_DICTS, all_d)
    return dict(added=added, updated=sum(base.get(k, v) != v for k, v in entries.items()),
                total=len(all_d[name]))


def delete_entries(name, oracle_values):
    """Drop entries by oracle value. Returns {removed, not_found, total}."""
    all_d = dictionaries()
    d = all_d.get(name, {})
    keys = {v.strip() for v in oracle_values if v.strip()}
    removed = [k for k in keys if d.pop(k, None) is not None]
    store.put(_DICTS, all_d)
    return dict(removed=len(removed), not_found=sorted(keys - set(removed)), total=len(d))


def delete_dictionary(name):
    all_d = dictionaries()
    all_d.pop(name, None)
    store.put(_DICTS, all_d)


_LOOKUPS = "lookups"


def lookups():
    """{name: {table, match: [columns], value: column}} - a dictionary read
    from a Postgres table in the target schema at sync time: the Oracle value
    (plus, matching on several columns, those of the column's lookup_with)
    is found in `match` and replaced by that row's `value`, e.g. the id a
    parent table was given when it loaded."""
    return store.get(_LOOKUPS, {})


def save_lookup(name, table, match, value):
    name, table, value = ((s or "").strip() for s in (name, table, value))
    match = [m.strip() for m in match if m.strip()]
    if not name:
        raise ValueError("Name the lookup.")
    if name in dictionaries():
        raise ValueError(f'"{name}" is already a dictionary - pick another name.')
    if not table or not match or not value:
        raise ValueError("A lookup needs a table, at least one match column and a value column.")
    if any('"' in s for s in [table, value, *match]):
        raise ValueError("Table and column names can't contain double quotes.")
    all_l = lookups()
    all_l[name] = dict(table=table, match=match, value=value)
    store.put(_LOOKUPS, all_l)
    return all_l[name]


def delete_lookup(name):
    all_l = lookups()
    all_l.pop(name, None)
    store.put(_LOOKUPS, all_l)


def lookup_inputs(kept):
    """Oracle columns the kept columns' lookups match on besides their own
    value. The caller SELECTs them after the kept columns; value_filler
    drops them again."""
    return list(dict.fromkeys(w for c in kept for w in c.get("lookup_with") or []))


def lookup_tables(kept):
    """Postgres tables the kept columns' lookups read: they load first."""
    looks = lookups()
    return {looks[c["dictionary"]]["table"] for c in kept if c.get("dictionary") in looks}


def _key(v):
    # str() so Oracle NUMBERs (Decimal) match CSV / Postgres text; strip() for CHAR padding
    return None if v is None else str(v).strip()


def value_filler(kept, lookup_source=None):
    """Chunk transform for the kept columns: an Oracle NULL becomes the
    column's if_null value, any other value goes through the column's
    dictionary or Postgres lookup. None when no column needs either.

    Rows carry the kept columns followed by lookup_inputs(kept); fill drops
    the latter. lookup_source(lookup) gives get(keys) -> {key: value} for
    keys as tuples of text, one per match column (pg_client.lookup_source);
    each chunk's keys go to it in one call.

    fill(rows) returns (rows to load, rejects). A row with a value missing
    from its dictionary is held back rather than loaded with the old value,
    which would silently break whatever refers to it; rejects lists each as
    (index in `rows`, [(column, dictionary, value), ...]).
    """
    dicts, looks = dictionaries(), lookups()
    width, extra = len(kept), lookup_inputs(kept)
    sources = {}  # one per lookup, however many columns use it
    cols = []
    for i, c in enumerate(kept):
        d, src = c.get("dictionary"), None
        if d in looks:
            match, with_ = looks[d]["match"], c.get("lookup_with") or []
            if len(with_) != len(match) - 1:
                raise RuntimeError(
                    f'Column {c["name"]} uses lookup "{d}", which matches on {", ".join(match)}: '
                    f'it needs {len(match) - 1} other Oracle column(s) to match with, not {len(with_)}.')
            if lookup_source is None:
                raise RuntimeError(f'Column {c["name"]} uses lookup "{d}", which needs Postgres.')
            if d not in sources:
                sources[d] = lookup_source(looks[d])
            src = (sources[d], [width + extra.index(w) for w in with_])
        elif d and d not in dicts:
            raise RuntimeError(f'Column {c["name"]} uses dictionary "{d}", which no longer exists.')
        if d or c.get("if_null") is not None:
            cols.append((i, c["name"], c.get("if_null"), d, dicts.get(d), src))
    if not cols:
        return None

    def fill(rows):
        keys = {}  # column index -> each row's lookup key
        found = {}
        for i, _, _, _, _, src in cols:
            if src:
                get, with_idx = src
                keys[i] = [(_key(r[i]), *(_key(r[w]) for w in with_idx)) for r in rows]
                wanted = {k for r, k in zip(rows, keys[i]) if r[i] is not None}
                found[i] = get(wanted) if wanted else {}
        out, rejects = [], []
        for n, row in enumerate(rows):
            row, misses = list(row), []
            for i, col, if_null, dname, lookup, src in cols:
                v = row[i]
                if v is None:
                    row[i] = if_null
                    continue
                if src:
                    k = keys[i][n]
                    new, shown = found[i].get(k), " / ".join("NULL" if p is None else p for p in k)
                elif lookup is not None:
                    new, shown = lookup.get(_key(v)), str(v)
                else:
                    continue
                if new is None:
                    misses.append((col, dname, shown))
                row[i] = new
            if misses:
                rejects.append((n, misses))
            else:
                out.append(tuple(row[:width]))
        return out, rejects
    return fill


def fingerprint(pg_table, resolved_columns):
    """Hash of everything that affects DDL shape: target table + per-column
    (oracle name, target name, skip, pg type) in Oracle column order."""
    payload = {
        "pg_table": pg_table,
        "columns": [
            {"name": c["name"], "target": c["target"], "skip": c["skip"], "pg_type": c["pg_type"]}
            for c in resolved_columns
        ],
    }
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def union_columns(per_source):
    """Merge the kept columns of several Oracle tables that share one
    Postgres target.

    `per_source` is an ordered {oracle_table: resolved column list} as
    returned by resolve(); skipped columns are ignored here. Returns
    (columns, conflicts).

    `columns` is the union in first-seen order - each a dict of target /
    pg_type / nullable / sources, shaped for types_map.coldef(). A column
    that not every source supplies is forced nullable: the sources missing
    it have nothing to put there.

    `conflicts` lists {column, types} where sources declare the same target
    column with different Postgres types. Deliberately not resolved here -
    picking one would silently reject or truncate the other source's data,
    so the caller refuses and lets the user settle it with a rename, a skip
    or a matching type override.
    """
    merged = {}
    for src, cols in per_source.items():
        for c in cols:
            if c["skip"]:
                continue
            m = merged.setdefault(c["target"], dict(
                target=c["target"], pg_type=c["pg_type"], nullable=False,
                sources=[], types={}))
            m["nullable"] = m["nullable"] or c["nullable"]
            m["sources"].append(src)
            m["types"].setdefault(c["pg_type"], []).append(src)

    columns, conflicts = [], []
    for m in merged.values():
        if len(m["types"]) > 1:
            conflicts.append(dict(column=m["target"], types=m["types"]))
        columns.append(dict(
            target=m["target"], pg_type=m["pg_type"],
            nullable=m["nullable"] or len(set(m["sources"])) < len(per_source),
            sources=m["sources"],
        ))
    return columns, conflicts


def merged_fingerprint(pg_table, per_source):
    """Fingerprint of a whole merged target: the shared target name plus
    every source's mapping. Changing any one source's mapping flags drift on
    all of them, because the table they share has to be recreated as a unit.
    """
    parts = [f"{s}:{fingerprint(pg_table, cols)}" for s, cols in sorted(per_source.items())]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def mark_created(table, pg_table, fp):
    """Record the mapping fingerprint (and resulting pg table name) that a
    table was actually created with, so drift can be detected before sync."""
    all_m = _all()
    entry = all_m.setdefault(table, {})
    entry["created_fingerprint"] = fp
    entry["created_pg_table"] = pg_table
    all_m[table] = entry
    store.put(_KEY, all_m)


def clear_created(table):
    all_m = _all()
    if table in all_m:
        all_m[table]["created_fingerprint"] = None
        all_m[table]["created_pg_table"] = None
        store.put(_KEY, all_m)
