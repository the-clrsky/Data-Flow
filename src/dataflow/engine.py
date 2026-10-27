"""Orchestration: create tables, compare row counts, sync data (streaming)."""
import time

from . import store
from . import oracle_client as ora
from . import pg_client as pg
from . import types_map
from . import mapping
from . import depgraph
from . import retry
from . import history
from . import preflight
from . import groups

CHUNK_SIZE = 10_000  # rows per COPY chunk during sync; adjust here if needed
MISS_CAP = 1_000  # distinct dictionary misses per column reported in the sync log (all are in row errors)


def _retry_hook():
    """A small self-contained event queue so retry attempts happening inside
    a plain (non-generator) DB call can still surface as SSE events at the
    next point the caller yields. Retries themselves happen synchronously
    (including the backoff sleep) inside the call, so events show up as a
    burst right after it resolves rather than truly live during the wait -
    still enough to explain a multi-second pause instead of an unexplained
    stall."""
    queue = []

    def on_retry(label, attempt, max_retries, exc, delay):
        queue.append(dict(type="retry", label=label, attempt=attempt,
                           max_retries=max_retries, delay=delay, error=str(exc)))

    def drain():
        while queue:
            yield queue.pop(0)

    return on_retry, drain


def _cfgs():
    ocfg = store.get("oracle")
    pcfg = store.get("pg")
    if not ocfg or not pcfg:
        raise RuntimeError("Oracle and Postgres connections must be saved first.")
    return ocfg, pcfg


def _ora_cfg():
    ocfg = store.get("oracle")
    if not ocfg:
        raise RuntimeError("Save Oracle connection first.")
    return ocfg


def pg_name(oracle_table):
    """Default Postgres name for an Oracle table (overridden by mapping.py)."""
    return mapping.default_table_name(oracle_table)


def _union_for_target(ocon, owner, sources, on_retry=None):
    """Resolve every Oracle table feeding one Postgres target and union
    their columns. Returns (per_source_resolved, columns, conflicts)."""
    per_source = _group_per_source_named(ocon, owner, sources, on_retry=on_retry)
    columns, conflicts = mapping.union_columns(per_source)
    return per_source, columns, conflicts


def _group_per_source_named(ocon, owner, sources, on_retry=None):
    """{oracle_table: resolved columns} for a list of source tables."""
    per_source = {}
    for s in sources:
        cols = ora.get_columns(ocon, owner, s, on_retry=on_retry)
        if not cols:
            raise RuntimeError(f"{s}: no columns found (check schema/owner)")
        _, resolved = mapping.resolve(s, cols)
        per_source[s] = resolved
    return per_source


def _group_per_source(ocon, owner, group, on_retry=None):
    return _group_per_source_named(ocon, owner, group["sources"], on_retry=on_retry)


def _group_plan(group, per_source):
    """Target columns, DDL and fingerprint for a group, per mode.

    Errors are collected rather than raised so a dry run can show every
    problem with a group at once instead of only the first.
    """
    errors = []
    if group["mode"] == "union":
        columns, conflicts = mapping.union_columns(per_source)
        if conflicts:
            errors.append(_conflict_error(group["target"], conflicts))
        coldefs = _merged_coldefs(columns)
    else:
        columns = groups.join_plan(group, per_source)
        coldefs = [types_map.coldef(c, c["target"], c["pg_type"]) for c in columns]
    if not columns:
        errors.append("every column is skipped - nothing to create")
    return dict(columns=columns, coldefs=coldefs, errors=errors,
                fingerprint=groups.fingerprint(group, per_source))


def _split_plan(ocon, owner, g, on_retry=None):
    """(parts, fingerprint) for a split group, raising on any plan error.
    Each part gets its coldefs and FROM clause ready to use."""
    per_source = _group_per_source(ocon, owner, g, on_retry=on_retry)
    src = g["sources"][0]
    parts, errors = groups.split_plan(g, per_source[src], ora.get_pk(ocon, owner, src, on_retry=on_retry))
    errors += [f'{p["pg_table"]}: every column is skipped' for p in parts if not p["columns"]]
    if errors:
        raise RuntimeError("; ".join(errors))
    for p in parts:
        p["coldefs"] = [types_map.coldef(c, c["target"], c["pg_type"]) for c in p["columns"]]
        p["from_sql"] = groups.split_from(owner, src, p["where"])
    return parts, groups.fingerprint(g, per_source)


def _group_order_keys(ocon, owner, group, per_source, on_retry=None):
    """Qualified key expressions giving a total order over a join's result,
    plus the sources that couldn't supply one.

    Every source contributes its primary key - or a unique constraint whose
    columns are all NOT NULL - not just the driving table. Without a total
    order, two result rows sharing the driving row's key straddle a page
    boundary and the rows after the first are silently skipped. A NULL in a
    key column would do the same, since NULL > x is never true, which is why
    a nullable unique constraint doesn't qualify.
    """
    exprs, missing = [], []
    for i, s in enumerate(group["sources"]):
        nullable = {c["name"]: c["nullable"] for c in per_source[s]}
        key = ora.get_pk(ocon, owner, s, on_retry=on_retry)
        if not key:
            uk = ora.get_unique(ocon, owner, s, on_retry=on_retry)
            if uk and all(not nullable.get(c, True) for c in uk):
                key = uk
        if not key:
            missing.append(s)
            continue
        exprs.extend(f'{groups.alias(i)}."{c}"' for c in key)
    return exprs, missing


def _require_join_keys(ocon, owner, group, per_source, on_retry=None):
    exprs, missing = _group_order_keys(ocon, owner, group, per_source, on_retry=on_retry)
    if missing:
        raise RuntimeError(
            "a join needs a primary key (or a NOT NULL unique constraint) on every "
            "source so its rows can be paged in a stable order; missing on: "
            + ", ".join(missing))
    return exprs
def _conflict_error(pgt, conflicts):
    """Why a merged target can't be built: the same column arriving as two
    different Postgres types. Named per column and per source, since the fix
    is a rename/skip/override on one specific source."""
    parts = []
    for c in conflicts:
        detail = "; ".join(f"{ty} from {', '.join(srcs)}" for ty, srcs in c["types"].items())
        parts.append(f'"{c["column"]}" ({detail})')
    return (f'merged target "{pgt}": sources disagree on column types - '
            + "; ".join(parts)
            + " - rename the column, skip it, or set a matching type override")


def _merged_coldefs(columns):
    return [types_map.coldef(c, c["target"], c["pg_type"]) for c in columns] + [
        types_map.source_coldef()]


def table_columns(table):
    """Live Oracle columns for a table, merged with the stored mapping
    overrides. Used by the column-mapping page."""
    ocfg = _ora_cfg()
    owner = ora.owner(ocfg)
    ocon = ora.connect(ocfg)
    try:
        cols = ora.get_columns(ocon, owner, mapping.oracle_table(table))
        if not cols:
            raise RuntimeError("no columns found (check schema/owner/table name)")
        pgt, resolved = mapping.resolve(table, cols)
        return dict(
            table=table,
            pg_table=pgt,
            pg_type_choices=types_map.PG_TYPE_CHOICES,
            dictionaries=sorted(mapping.dictionaries()) + sorted(mapping.lookups()),
            lookups=mapping.lookups(),
            fast_load=mapping.get(table)["fast_load"],
            upsert=mapping.get(table)["upsert"],
            extra=mapping.is_extra(table),
            saved=mapping.is_saved(table),
            columns=[
                dict(name=c["name"], dtype=c["dtype"], prec=c["prec"], scale=c["scale"],
                     nullable=c["nullable"], target=c["target"], skip=c["skip"],
                     pg_type=c["pg_type"], pg_type_override=c["pg_type_override"],
                     default_pg_type=c["default_pg_type"], if_null=c["if_null"],
                     dictionary=c["dictionary"], lookup_with=c["lookup_with"], copy=c["copy"],
                     pg_only=c["pg_only"])
                for c in resolved
            ],
            constants=mapping.get(table)["constants"],
        )
    finally:
        ocon.close()


def filled_columns(table):
    """Oracle columns of a table that hold any data (see oracle_client)."""
    ocfg = _ora_cfg()
    owner = ora.owner(ocfg)
    ocon = ora.connect(ocfg)
    try:
        t = mapping.oracle_table(table)
        return ora.filled_columns(ocon, owner, t, ora.get_columns(ocon, owner, t))
    finally:
        ocon.close()


def save_mapping(table, pg_table, columns, fast_load=False, upsert=False):
    """Returns an error message, or None once saved. Two mappings writing
    one Postgres table would truncate each other's rows on every sync."""
    pgt = (pg_table or "").strip() or mapping.default_table_name(table)
    other = mapping.pg_table_owner(pgt, table)
    if other:
        return f'"{pgt}" is already the target of the {other} mapping - pick another name.'
    if mapping.is_extra(table) and all(c.get("skip") for c in columns):
        return "Select at least one column."
    targets = [(c.get("target") or c["name"]).strip().lower() for c in columns if not c.get("skip")]
    dups = sorted({t for t in targets if targets.count(t) > 1})
    if dups:
        return "Two columns map to the same target: " + ", ".join(dups)
    if fast_load and upsert:
        return ("Upsert matches rows on the table's primary key, which the "
                "drop/recreate-constraints option removes during the load - pick one.")
    names = {c["name"].upper(): c["name"] for c in columns if c["name"] and not c.get("pg_only")}
    looks = mapping.lookups()
    for c in columns:
        # a Postgres-only row is named by the column it fills
        label = c.get("target") if c.get("pg_only") else c["name"]
        if c.get("pg_only"):
            if not (c.get("target") or "").strip():
                return "A Postgres-only row has no Postgres column."
            if not c["name"] and (c.get("dictionary") or c.get("if_null") in ("", None)):
                return (f"{label}: give it a fixed value, or pick the Oracle column whose value "
                        "the dictionary / lookup looks up.")
            if c["name"] and c["name"].upper() not in names:
                return f"{label}: no Oracle column {c['name']}."
        lk = looks.get(c.get("dictionary"))
        if c.get("skip") or not lk:
            c["lookup_with"] = None
            continue
        with_ = [w.strip() for w in c.get("lookup_with") or [] if w.strip()]
        if len(with_) != len(lk["match"]) - 1:
            return (f'{label}: lookup "{c["dictionary"]}" matches on {", ".join(lk["match"])} - '
                    f'name {len(lk["match"]) - 1} other Oracle column(s) to match with, in that order.')
        unknown = [w for w in with_ if w.upper() not in names]
        if unknown:
            return f'{label}: no Oracle column {", ".join(unknown)} to match with.'
        c["lookup_with"] = [names[w.upper()] for w in with_]
    mapping.save(table, pg_table, columns, fast_load, upsert)
    return None


def mapping_status(table_names):
    """Per-table summary for the "at a glance" UI: rename/skip counts and
    whether the live mapping has drifted from what the table was created with."""
    if not table_names:
        return []
    ocfg = _ora_cfg()
    owner = ora.owner(ocfg)
    ocon = ora.connect(ocfg)
    out = []
    try:
        for t in mapping.expand(table_names):
            m = mapping.get(t)
            g = groups.for_source(mapping.oracle_table(t))
            try:
                cols = ora.get_columns(ocon, owner, mapping.oracle_table(t))
                if not cols:
                    raise RuntimeError("no columns found (check schema/owner)")
                pgt, resolved = mapping.resolve(t, cols)
                renamed = sum(1 for c in resolved if not c["skip"] and c["target"] != c["name"].lower())
                skipped = sum(1 for c in resolved if c["skip"])
                current_fp = mapping.fingerprint(pgt, resolved)
                stored_fp = m["created_fingerprint"]
                out.append(dict(
                    table=t, pg_table=pgt, column_count=len(cols),
                    renamed=renamed, skipped=skipped,
                    created=bool(stored_fp),
                    drift=bool(stored_fp and stored_fp != current_fp),
                    fast_load=m["fast_load"],
                    group=(g["target"] if g else None),
                    group_mode=(g["mode"] if g else None),
                    group_with=([s for s in g["sources"] if s != t] if g else []),
                ))
            except Exception as e:  # noqa: BLE001
                out.append(dict(
                    table=t, pg_table=m["pg_table"] or pg_name(t),
                    error=str(e),
                ))
    finally:
        ocon.close()
    return out


def _create_single(ocon, pcon, schema, owner, t, pgt):
    try:
        cols = ora.get_columns(ocon, owner, mapping.oracle_table(t))
        if not cols:
            raise RuntimeError("no columns found (check schema/owner)")
        pk = ora.get_pk(ocon, owner, mapping.oracle_table(t))
        pgt, resolved = mapping.resolve(t, cols)
        kept = [c for c in resolved if not c["skip"]]
        if not kept:
            raise RuntimeError("all columns skipped - nothing to create")
        target_by_name = {c["name"]: c["target"] for c in reversed(kept)}  # first destination wins
        coldefs = [types_map.coldef(c, c["target"], c["pg_type"]) for c in kept]
        pk_targets = [target_by_name[p] for p in pk if p in target_by_name]
        existed = pg.table_exists(pcon, schema, pgt)
        pg.create_table(pcon, schema, pgt, coldefs, pk_targets)
        mapping.mark_created(t, pgt, mapping.fingerprint(pgt, resolved))
        return dict(table=t, pg_table=pgt, status="exists" if existed else "created")
    except Exception as e:  # noqa: BLE001
        pcon.rollback()
        return dict(table=t, pg_table=pgt, status="error", error=str(e))


def _create_merged(ocon, pcon, schema, owner, pgt, sources):
    """Create one Postgres table from several Oracle tables: the union of
    their mapped columns plus the SOURCE_COLUMN tagging each row's origin.

    No primary key is carried over. Two sources can legitimately hold the
    same key values, so inheriting one source's PK would reject the other's
    rows halfway through a sync. Add one by hand afterwards if the merged
    data really is unique.
    """
    try:
        per_source, columns, conflicts = _union_for_target(ocon, owner, sources)
        if conflicts:
            raise RuntimeError(_conflict_error(pgt, conflicts))
        existed = pg.table_exists(pcon, schema, pgt)
        pg.create_table(pcon, schema, pgt, _merged_coldefs(columns), [])
        fp = mapping.merged_fingerprint(pgt, per_source)
        for s in sources:
            mapping.mark_created(s, pgt, fp)
        status = "exists" if existed else "created"
        return [dict(table=s, pg_table=pgt, status=status, merged_from=list(sources))
                for s in sources]
    except Exception as e:  # noqa: BLE001
        pcon.rollback()
        return [dict(table=s, pg_table=pgt, status="error", error=str(e)) for s in sources]


def create_tables(table_names):
    """Create the Postgres side for everything in this batch: one table per
    ungrouped Oracle table, plus one per group whose sources appear here.

    A grouped source is never created on its own - its rows go into the
    group's target, and creating it separately would make a second table
    nobody loads.
    """
    ocfg, pcfg = _cfgs()
    schema = pcfg["target_schema"]
    owner = ora.owner(ocfg)
    grouped = groups.grouped_sources()
    results = []
    ocon = ora.connect(ocfg)
    pcon = pg.connect(pcfg)
    try:
        pg.ensure_schema(pcon, schema)
        for t in mapping.expand(table_names):
            if mapping.oracle_table(t) in grouped:
                continue
            pgt = mapping.get(t)["pg_table"] or pg_name(t)
            results.append(_create_single(ocon, pcon, schema, owner, t, pgt))
        for g in groups.active(table_names):
            if g["mode"] == "split":
                results.extend(_create_split(ocon, pcon, schema, owner, g))
            else:
                results.append(_create_group(ocon, pcon, schema, owner, g))
    finally:
        ocon.close()
        pcon.close()
    return results


def _create_group(ocon, pcon, schema, owner, g):
    """Create one group's target table. Join mode produces the combined
    columns of every source; union mode produces their union plus the
    source-tag column. Neither carries a primary key over - a join can
    legitimately repeat a source's key, and a union's sources can collide
    with each other."""
    out = dict(kind="group", table=g["target"], pg_table=g["target"],
               mode=g["mode"], sources=list(g["sources"]))
    try:
        per_source = _group_per_source(ocon, owner, g)
        plan = _group_plan(g, per_source)
        if plan["errors"]:
            raise RuntimeError("; ".join(plan["errors"]))
        if g["mode"] == "join":
            _require_join_keys(ocon, owner, g, per_source)
        existed = pg.table_exists(pcon, schema, g["target"])
        pg.create_table(pcon, schema, g["target"], plan["coldefs"], [])
        groups.mark_created(g["target"], plan["fingerprint"])
        return dict(out, status="exists" if existed else "created")
    except Exception as e:  # noqa: BLE001
        pcon.rollback()
        return dict(out, status="error", error=str(e))


def _create_split(ocon, pcon, schema, owner, g):
    """One Postgres table per part, each carrying the source's primary key."""
    out = dict(kind="group", mode="split", sources=list(g["sources"]))
    try:
        parts, fp = _split_plan(ocon, owner, g)
    except Exception as e:  # noqa: BLE001
        return [dict(out, table=pt, pg_table=pt, status="error", error=str(e))
                for pt in groups.pg_tables(g)]
    results = []
    for p in parts:
        row = dict(out, table=p["pg_table"], pg_table=p["pg_table"])
        try:
            existed = pg.table_exists(pcon, schema, p["pg_table"])
            pg.create_table(pcon, schema, p["pg_table"], p["coldefs"], p["pk"])
            results.append(dict(row, status="exists" if existed else "created"))
        except Exception as e:  # noqa: BLE001
            pcon.rollback()
            results.append(dict(row, status="error", error=str(e)))
    if all(r["status"] != "error" for r in results):
        groups.mark_created(g["target"], fp)
    return results


def recreate_tables(table_names):
    """Drop the previously-created Postgres table for each name, clear its
    drift fingerprint, then recreate with the current mapping. Data reload
    is left to a follow-up sync, mirroring the normal create -> sync flow.

    A name that belongs to a group (by target or by source) recreates the
    whole group's table, since every source feeds it.
    """
    ocfg, pcfg = _cfgs()
    schema = pcfg["target_schema"]
    pcon = pg.connect(pcfg)
    plain, group_targets = [], []
    for t in table_names:
        g = groups.by_name(t)
        if g:
            if g["target"] not in group_targets:
                group_targets.append(g["target"])
        else:
            plain.append(t)
    try:
        for t in plain:
            old_pg_table = mapping.get(t)["created_pg_table"]
            if old_pg_table:
                pg.drop_table(pcon, schema, old_pg_table)
            mapping.clear_created(t)
        for target in group_targets:
            for pt in groups.pg_tables(groups.get(target)):
                pg.drop_table(pcon, schema, pt)
                history.clear_checkpoint(f"split:{pt}")
            groups.clear_created(target)
            history.clear_checkpoint(f"group:{target}")
    finally:
        pcon.close()
    reselect = plain + [s for target in group_targets for s in groups.get(target)["sources"]]
    return create_tables(reselect)
def oracle_row_count_estimates():
    """Approximate row counts (from optimizer stats) for every table in the
    Oracle schema, for the Tables tab - before any Postgres connection or
    column mapping exists yet. One dictionary query regardless of how many
    thousands of tables the schema has, unlike a live COUNT(*) per table."""
    ocfg = _ora_cfg()
    return ora.row_count_estimates(ocfg)


def foreign_keys(table_names):
    """Oracle foreign keys among `table_names`, for prefilling a join."""
    ocfg = _ora_cfg()
    owner = ora.owner(ocfg)
    ocon = ora.connect(ocfg)
    try:
        return ora.get_foreign_keys(ocon, owner, table_names)
    finally:
        ocon.close()


def compare(table_names):
    """Row counts side by side: one line per ungrouped table, one per group.

    A group compares as a whole - for a join the Oracle number is the count
    of the joined result, since that is what lands in the target; for a
    union it is the sum of its sources.
    """
    ocfg, pcfg = _cfgs()
    schema = pcfg["target_schema"]
    owner = ora.owner(ocfg)
    ocon = ora.connect(ocfg)
    pcon = pg.connect(pcfg)
    grouped = groups.grouped_sources()
    out = []
    try:
        for t in mapping.expand(table_names):
            if mapping.oracle_table(t) in grouped:
                continue
            pgt = mapping.get(t)["pg_table"] or pg_name(t)
            try:
                oc = ora.row_count(ocon, owner, mapping.oracle_table(t))
            except Exception as e:  # noqa: BLE001
                out.append(dict(table=t, pg_table=pgt, oracle=None, pg=None,
                                diff=None, error=str(e)))
                continue
            out.append(_compare_row(pcon, schema, t, pgt, oc))

        for g in groups.active(table_names):
            if g["mode"] == "split":
                out.extend(_compare_split(ocon, pcon, schema, owner, g))
                continue
            try:
                if g["mode"] == "join":
                    oc = ora.count_rows(ocon, groups.join_from(owner, g))
                else:
                    oc = sum(ora.row_count(ocon, owner, s) for s in g["sources"])
            except Exception as e:  # noqa: BLE001
                out.append(dict(table=g["target"], pg_table=g["target"], oracle=None,
                                pg=None, diff=None, error=str(e), kind="group"))
                continue
            out.append(dict(_compare_row(pcon, schema, g["target"], g["target"], oc),
                            kind="group", mode=g["mode"], sources=list(g["sources"])))
    finally:
        ocon.close()
        pcon.close()
    return out


def _compare_split(ocon, pcon, schema, owner, g):
    extra = dict(kind="group", mode="split", sources=list(g["sources"]))
    out = []
    for p in g["parts"]:
        pt = p["pg_table"]
        try:
            oc = ora.count_rows(ocon, groups.split_from(owner, g["sources"][0], p["where"]))
        except Exception as e:  # noqa: BLE001
            out.append(dict(extra, table=pt, pg_table=pt, oracle=None, pg=None,
                            diff=None, error=str(e)))
            continue
        out.append(dict(_compare_row(pcon, schema, pt, pt, oc), **extra))
    return out


def _compare_row(pcon, schema, label, pgt, oc):
    try:
        pc = pg.row_count(pcon, schema, pgt)
    except Exception:  # table not created yet
        pcon.rollback()
        pc = None
    return dict(table=label, pg_table=pgt, oracle=oc, pg=pc,
                diff=(None if pc is None else oc - pc),
                match=(pc is not None and oc == pc))
def checkpoint_status(table_names):
    """Tables in this batch that have a pending checkpoint from an
    interrupted sync, for the "resume?" panel shown before a sync starts."""
    out = []
    for t in mapping.expand(table_names):
        cp = history.get_checkpoint(t)
        if cp:
            out.append(dict(table=t, last_offset=cp["last_offset"], updated_at=cp["updated_at"]))
    return out


def dry_run(table_names):
    """Preview mode: the DDL a create/recreate would run and row counts on
    both sides, without executing any writes on either database (no
    CREATE/TRUNCATE/COPY/ALTER). Fast-load tables also show the
    constraint/index DDL that would be dropped and recreated.

    Groups preview as one card showing the joined or unioned DDL, the join
    SQL itself, and any columns that had to be renamed to avoid a collision
    - the place to check a merge before it touches anything.
    """
    ocfg, pcfg = _cfgs()
    schema = pcfg["target_schema"]
    owner = ora.owner(ocfg)
    ocon = ora.connect(ocfg)
    pcon = pg.connect(pcfg)
    grouped = groups.grouped_sources()
    out = []
    try:
        for t in mapping.expand(table_names):
            if mapping.oracle_table(t) in grouped:
                continue
            entry = dict(table=t)
            try:
                cols = ora.get_columns(ocon, owner, mapping.oracle_table(t))
                if not cols:
                    raise RuntimeError("no columns found (check schema/owner)")
                pk = ora.get_pk(ocon, owner, mapping.oracle_table(t))
                pgt, resolved = mapping.resolve(t, cols)
                entry["pg_table"] = pgt
                kept = [c for c in resolved if not c["skip"]]
                if not kept:
                    raise RuntimeError("all columns skipped - nothing to create")
                target_by_name = {c["name"]: c["target"] for c in reversed(kept)}  # first destination wins
                coldefs = [types_map.coldef(c, c["target"], c["pg_type"]) for c in kept]
                pk_targets = [target_by_name[p] for p in pk if p in target_by_name]
                entry["create_ddl"] = pg.create_table_ddl(schema, pgt, coldefs, pk_targets)
                stored_fp = mapping.get(t)["created_fingerprint"]
                entry["drift"] = bool(stored_fp and stored_fp != mapping.fingerprint(pgt, resolved))
                entry["oracle_rows"] = ora.row_count(ocon, owner, mapping.oracle_table(t))
                _dry_run_pg_side(pcon, schema, pgt, entry, fast_load=mapping.get(t)["fast_load"])
                ckpt = history.get_checkpoint(t)
                entry["resumable_from"] = ckpt["last_offset"] if ckpt else None
            except Exception as e:  # noqa: BLE001
                entry["error"] = str(e)
            out.append(entry)

        for g in groups.active(table_names):
            if g["mode"] == "split":
                out.extend(_dry_run_split(ocon, pcon, schema, owner, g))
                continue
            entry = dict(table=g["target"], pg_table=g["target"], kind="group",
                         mode=g["mode"], sources=list(g["sources"]))
            try:
                per_source = _group_per_source(ocon, owner, g)
                plan = _group_plan(g, per_source)
                if plan["errors"]:
                    raise RuntimeError("; ".join(plan["errors"]))
                entry["create_ddl"] = pg.create_table_ddl(schema, g["target"], plan["coldefs"], [])
                entry["renamed"] = [
                    dict(source=c["source"], was=c["renamed_from"], now=c["target"])
                    for c in plan["columns"] if c.get("renamed_from")
                ]
                if g["mode"] == "join":
                    from_sql = groups.join_from(owner, g)
                    keys = _require_join_keys(ocon, owner, g, per_source)
                    entry["join_sql"] = "SELECT ... FROM " + from_sql
                    entry["order_by"] = ", ".join(keys)
                    entry["oracle_rows"] = ora.count_rows(ocon, from_sql)
                else:
                    entry["oracle_rows"] = sum(ora.row_count(ocon, owner, s) for s in g["sources"])
                entry["drift"] = bool(g.get("created_fingerprint")
                                      and g["created_fingerprint"] != plan["fingerprint"])
                _dry_run_pg_side(pcon, schema, g["target"], entry)
                ckpt = history.get_checkpoint(f"group:{g['target']}")
                entry["resumable_from"] = ckpt["last_offset"] if ckpt else None
            except Exception as e:  # noqa: BLE001
                entry["error"] = str(e)
            out.append(entry)
    finally:
        ocon.close()
        pcon.close()
    return out


def _dry_run_split(ocon, pcon, schema, owner, g):
    base = dict(kind="group", mode="split", sources=list(g["sources"]))
    try:
        parts, fp = _split_plan(ocon, owner, g)
    except Exception as e:  # noqa: BLE001
        return [dict(base, table=pt, pg_table=pt, error=str(e)) for pt in groups.pg_tables(g)]
    out = []
    for p in parts:
        entry = dict(base, table=p["pg_table"], pg_table=p["pg_table"])
        try:
            entry["create_ddl"] = pg.create_table_ddl(schema, p["pg_table"], p["coldefs"], p["pk"])
            if p["where"]:
                entry["join_sql"] = "SELECT ... FROM " + p["from_sql"]
            entry["oracle_rows"] = ora.count_rows(ocon, p["from_sql"])
            entry["drift"] = bool(g.get("created_fingerprint") and g["created_fingerprint"] != fp)
            _dry_run_pg_side(pcon, schema, p["pg_table"], entry,
                             fast_load=mapping.get(g["sources"][0])["fast_load"])
            ckpt = history.get_checkpoint("split:" + p["pg_table"])
            entry["resumable_from"] = ckpt["last_offset"] if ckpt else None
        except Exception as e:  # noqa: BLE001
            entry["error"] = str(e)
        out.append(entry)
    return out


def _dry_run_pg_side(pcon, schema, pgt, entry, fast_load=False):
    entry["exists"] = pg.table_exists(pcon, schema, pgt)
    entry["pg_rows"] = None
    if not entry["exists"]:
        return
    try:
        entry["pg_rows"] = pg.row_count(pcon, schema, pgt)
    except Exception:  # noqa: BLE001
        pcon.rollback()
    if fast_load:
        constraints = pg.introspect_constraints(pcon, schema, pgt)
        entry["fast_load_drop"] = {
            kind: [c["name"] for c in items] for kind, items in constraints.items()
        }
def _fast_load_targets(table_names):
    """Oracle table -> pg table name, for every table in this batch with the
    drop/recreate-for-speed toggle on.

    At most one entry per Postgres table: a group's sources all point at the
    same target, and dropping its constraints once per source would fail on
    the second pass and restore them several times over.
    """
    out = {}
    seen = set()

    def take(label, pgt):
        if pgt not in seen:
            seen.add(pgt)
            out[label] = pgt

    grouped = groups.grouped_sources()
    for t in mapping.expand(table_names):
        if mapping.oracle_table(t) in grouped:
            continue
        m = mapping.get(t)
        if m["fast_load"]:
            take(t, m["pg_table"] or pg_name(t))
    for g in groups.active(table_names):
        if any(mapping.get(s)["fast_load"] for s in g["sources"]):
            for pt in groups.pg_tables(g):
                take(pt, pt)
    return out
def _capture_and_drop_constraints(pcon, schema, fast_pgt, captured):
    """Introspect then drop FK/unique/PK/index for every table in fast_pgt
    (mutated in place - tables whose introspection fails are removed so the
    rest of the sync treats them as if the toggle were off this run).

    Global drop order across the WHOLE batch: FK -> unique -> PK -> plain
    index. This must complete for every toggled table before any of them is
    truncated/loaded, or a still-standing FK on table B referencing table
    A's PK would block dropping A's PK once we get to A (see roadmap notes
    on cross-table ordering). Yields SSE-style event dicts and fills
    `captured` in place - in place rather than returned so that a stream
    abandoned mid-drop still leaves the caller holding whatever was
    captured by then, which is all it has to restore from.
    """
    on_retry, drain = _retry_hook()
    for t, pgt in list(fast_pgt.items()):
        try:
            captured[t] = pg.introspect_constraints(pcon, schema, pgt, on_retry=on_retry)
            yield from drain()
        except Exception as e:  # noqa: BLE001
            yield from drain()
            try:
                pcon.rollback()
            except Exception:
                pass
            yield dict(type="constraint_error", table=t, pg_table=pgt,
                       kind="introspect", error=str(e))
            del fast_pgt[t]

    tally = {t: {"ok": 0, "failed": 0} for t in fast_pgt}

    def _phase(kind, drop_fn):
        # Prune captured[t][kind] down to only what was actually dropped, so
        # the restore phase never retries re-adding something that failed
        # to drop (it's still there - retrying would just fail again with a
        # confusing "already exists" on top of the real error).
        for t, pgt in fast_pgt.items():
            kept = []
            for item in captured[t][kind]:
                try:
                    drop_fn(pgt, item)
                    yield from drain()
                    tally[t]["ok"] += 1
                    kept.append(item)
                except Exception as e:  # noqa: BLE001
                    yield from drain()
                    try:
                        pcon.rollback()
                    except Exception:
                        pass
                    tally[t]["failed"] += 1
                    yield dict(type="constraint_error", table=t, pg_table=pgt,
                               kind=kind, name=item["name"], error=str(e))
            captured[t][kind] = kept

    yield from _phase("fk", lambda pgt, item: pg.drop_constraint(pcon, schema, pgt, item["name"], on_retry=on_retry))
    yield from _phase("unique", lambda pgt, item: pg.drop_constraint(pcon, schema, pgt, item["name"], on_retry=on_retry))
    yield from _phase("pk", lambda pgt, item: pg.drop_constraint(pcon, schema, pgt, item["name"], on_retry=on_retry))
    yield from _phase("index", lambda pgt, item: pg.drop_index(pcon, schema, item["name"], on_retry=on_retry))

    for t, pgt in fast_pgt.items():
        yield dict(type="constraints_dropped", table=t, pg_table=pgt,
                   ok=tally[t]["ok"], failed=tally[t]["failed"])


def _restore_constraints(pcon, schema, fast_pgt, captured):
    """Recreate PK -> unique -> FK (parent-before-child) -> index across
    every table in fast_pgt, from the DDL captured before the drop.

    Always attempted for every table regardless of whether that table's own
    load succeeded, so a failed load never leaves a table silently
    unconstrained - failures are surfaced as constraint_error events instead.
    """
    # A stream abandoned mid-drop can leave tables in fast_pgt that were
    # never introspected - there is nothing to restore for those.
    fast_pgt = {t: pgt for t, pgt in fast_pgt.items() if t in captured}
    on_retry, drain = _retry_hook()
    tally = {t: {"ok": 0, "failed": 0} for t in fast_pgt}

    def _phase(kind, get_items, add_fn):
        for t, pgt in fast_pgt.items():
            for item in get_items(captured[t]):
                try:
                    add_fn(pgt, item)
                    yield from drain()
                    tally[t]["ok"] += 1
                except Exception as e:  # noqa: BLE001
                    yield from drain()
                    try:
                        pcon.rollback()
                    except Exception:
                        pass
                    tally[t]["failed"] += 1
                    yield dict(type="constraint_error", table=t, pg_table=pgt,
                               kind=kind, name=item["name"], error=str(e))

    yield from _phase("pk", lambda c: c["pk"],
                       lambda pgt, item: pg.add_constraint(pcon, schema, pgt, item["name"], item["ddl"], on_retry=on_retry))
    yield from _phase("unique", lambda c: c["unique"],
                       lambda pgt, item: pg.add_constraint(pcon, schema, pgt, item["name"], item["ddl"], on_retry=on_retry))

    # Order FK recreation parent-before-child across the toggled tables -
    # belt-and-suspenders on top of the PK/unique phases already having run
    # for every table first, per the roadmap's cross-table ordering ask.
    pgt_to_t = {pgt: t for t, pgt in fast_pgt.items()}
    edges = []
    for t in fast_pgt:
        for item in captured[t]["fk"]:
            parent_t = pgt_to_t.get(item.get("ref_table"))
            if parent_t:
                edges.append((t, parent_t))
    order = depgraph.topo_sort(list(fast_pgt), edges)

    def _fk_phase():
        for t in order:
            pgt = fast_pgt[t]
            for item in captured[t]["fk"]:
                try:
                    pg.add_constraint(pcon, schema, pgt, item["name"], item["ddl"], on_retry=on_retry)
                    yield from drain()
                    tally[t]["ok"] += 1
                except Exception as e:  # noqa: BLE001
                    yield from drain()
                    try:
                        pcon.rollback()
                    except Exception:
                        pass
                    tally[t]["failed"] += 1
                    yield dict(type="constraint_error", table=t, pg_table=pgt,
                               kind="fk", name=item["name"], error=str(e))

    yield from _fk_phase()
    yield from _phase("index", lambda c: c["index"],
                       lambda pgt, item: pg.create_index(pcon, item["ddl"], on_retry=on_retry))

    for t, pgt in fast_pgt.items():
        yield dict(type="constraints_restored", table=t, pg_table=pgt,
                   ok=tally[t]["ok"], failed=tally[t]["failed"])


def _lookup_source(pcon, schema, on_retry):
    return lambda lookup: pg.lookup_source(pcon, schema, lookup, on_retry=on_retry)


def _table_unit(ocon, pcon, schema, owner, t, on_retry=None):
    cols = ora.get_columns(ocon, owner, mapping.oracle_table(t), on_retry=on_retry)
    if not cols:
        raise RuntimeError("no columns found (check schema/owner)")
    pgt, resolved = mapping.resolve(t, cols)
    kept = [c for c in resolved if not c["skip"]]
    if not kept:
        raise RuntimeError("all columns skipped - nothing to load")
    oracle_cols, widen = mapping.fan_out([c["name"] for c in kept] + mapping.lookup_inputs(kept))
    order_cols = ora.get_order_columns(ocon, owner, mapping.oracle_table(t), on_retry=on_retry)
    consts = mapping.get(t)["constants"]  # fixed values, loaded after the kept columns
    pg_cols = [c["target"] for c in kept] + [k["target"] for k in consts]
    def fetch(off, n, key):
        return ora.fetch_chunk(ocon, owner, mapping.oracle_table(t), oracle_cols,
                               order_cols, off, n, last_key=key, on_retry=on_retry)
    fetch = widen(fetch)

    extra = {}
    if mapping.get(t)["upsert"]:
        key = pg.conflict_key(pcon, schema, pgt, pg_cols, on_retry=on_retry)
        extra = dict(
            reset=lambda: None,  # never empty the table: rows are merged on `key`
            truncates=None,
            copy=lambda con, sch, tbl, cols, rows, on_retry=None: pg.upsert_chunk(
                con, sch, tbl, cols, rows, key, on_retry=on_retry),
            counts_changes=True,
            row_tail=pg.upsert_tail(pg_cols, key),
        )
    return dict(
        label=t, pg_table=pgt, pg_cols=pg_cols, checkpoint_key=t,
        fingerprint=mapping.fingerprint(pgt, resolved),
        stored_fp=mapping.get(t)["created_fingerprint"],
        drift_hint=f"Mapping for {t} has changed since it was created.",
        total=lambda: ora.row_count(ocon, owner, mapping.oracle_table(t), on_retry=on_retry),
        fetch=fetch,
        transform=mapping.with_constants(
            mapping.value_filler(kept, _lookup_source(pcon, schema, on_retry)), consts),
        after=mapping.lookup_tables(kept),
        reset=lambda: pg.truncate(pcon, schema, pgt, on_retry=on_retry),
        truncates=pgt,
        pg_count=lambda: pg.row_count(pcon, schema, pgt, on_retry=on_retry),
    ) | extra


def _union_units(ocon, pcon, schema, owner, g, per_source, plan, on_retry=None):
    """One unit per source: a union group's sources each append their own
    rows to the shared table, tagged so they can be reloaded separately."""
    units = []
    for s in g["sources"]:
        kept = [c for c in per_source[s] if not c["skip"]]
        if not kept:
            continue
        oracle_cols, widen = mapping.fan_out([c["name"] for c in kept] + mapping.lookup_inputs(kept))
        order_cols = ora.get_order_columns(ocon, owner, s, on_retry=on_retry)
        target = g["target"]
        fill = (mapping.value_filler(kept, _lookup_source(pcon, schema, on_retry))
                or (lambda rows: (rows, [])))

        @widen
        def fetch(off, n, key, s=s, oracle_cols=oracle_cols, order_cols=order_cols):
            return ora.fetch_chunk(ocon, owner, s, oracle_cols, order_cols,
                                   off, n, last_key=key, on_retry=on_retry)

        def transform(rows, s=s, fill=fill):
            rows, rejects = fill(rows)
            return [tuple(r) + (s,) for r in rows], rejects

        units.append(dict(
            label=s, pg_table=target, checkpoint_key=s,
            pg_cols=[c["target"] for c in kept] + [types_map.SOURCE_COLUMN],
            fingerprint=plan["fingerprint"], stored_fp=g.get("created_fingerprint"),
            drift_hint=f'Group "{target}" has changed since it was created.',
            total=(lambda s=s: ora.row_count(ocon, owner, s, on_retry=on_retry)),
            fetch=fetch,
            transform=transform,
            after=mapping.lookup_tables(kept),
            # only this source's rows - TRUNCATE would take the rest of the union
            reset=(lambda s=s: pg.delete_source_rows(pcon, schema, target, s, on_retry=on_retry)),
            pg_count=(lambda s=s: pg.row_count(pcon, schema, target, source=s, on_retry=on_retry)),
        ))
    return units


def _aliased_fill(pcon, schema, kept, on_retry):
    """(SELECT expressions, transform, lookup tables) for columns read
    through a table alias, as join and split SELECTs do: the lookup inputs
    are qualified with their own source's alias too."""
    kept = [dict(c, lookup_with=[f'{c["alias"]}."{w}"' for w in c.get("lookup_with") or []])
            for c in kept]
    exprs = [f'{c["alias"]}."{c["name"]}"' for c in kept] + mapping.lookup_inputs(kept)
    return (exprs, mapping.value_filler(kept, _lookup_source(pcon, schema, on_retry)),
            mapping.lookup_tables(kept))


def _join_unit(ocon, pcon, schema, owner, g, per_source, plan, on_retry=None):
    """A join group is a single unit: the joined query is one result set, not
    one per source."""
    from_sql = groups.join_from(owner, g)
    key_exprs = _require_join_keys(ocon, owner, g, per_source, on_retry=on_retry)
    exprs, transform, after = _aliased_fill(pcon, schema, plan["columns"], on_retry)
    select_exprs, widen = mapping.fan_out(exprs)
    target = g["target"]
    return dict(
        label=target, pg_table=target, pg_cols=[c["target"] for c in plan["columns"]],
        checkpoint_key=f"group:{target}",
        fingerprint=plan["fingerprint"], stored_fp=g.get("created_fingerprint"),
        drift_hint=f'Group "{target}" has changed since it was created.',
        total=lambda: ora.count_rows(ocon, from_sql, on_retry=on_retry),
        fetch=widen(lambda off, n, key: ora.fetch_page(
            ocon, select_exprs, from_sql, key_exprs, off, n, last_key=key, on_retry=on_retry)),
        transform=transform,
        after=after,
        reset=lambda: pg.truncate(pcon, schema, target, on_retry=on_retry),
        truncates=target,
        pg_count=lambda: pg.row_count(pcon, schema, target, on_retry=on_retry),
    )


def _split_units(ocon, pcon, schema, owner, g, on_retry=None):
    """One unit per part: each is its own paged SELECT over the source,
    narrowed to the part's columns and rows, ordered by the source PK."""
    parts, fp = _split_plan(ocon, owner, g, on_retry=on_retry)
    key_exprs = [f'{groups.alias(0)}."{k}"'
                 for k in ora.get_pk(ocon, owner, g["sources"][0], on_retry=on_retry)]
    units = []
    for p in parts:
        pt, from_sql = p["pg_table"], p["from_sql"]
        exprs, transform, after = _aliased_fill(
            pcon, schema, [dict(c, alias=groups.alias(0)) for c in p["columns"]], on_retry)
        select_exprs, widen = mapping.fan_out(exprs)
        units.append(dict(
            label=pt, pg_table=pt, pg_cols=[c["target"] for c in p["columns"]],
            checkpoint_key=f"split:{pt}",
            fingerprint=fp, stored_fp=g.get("created_fingerprint"),
            drift_hint=f'Split "{g["target"]}" has changed since it was created.',
            total=(lambda f=from_sql: ora.count_rows(ocon, f, on_retry=on_retry)),
            fetch=widen(lambda off, n, key, f=from_sql, sel=select_exprs: ora.fetch_page(
                ocon, sel, f, key_exprs, off, n, last_key=key, on_retry=on_retry)),
            transform=transform,
            after=after,
            reset=(lambda pt=pt: pg.truncate(pcon, schema, pt, on_retry=on_retry)),
            truncates=pt,
            pg_count=(lambda pt=pt: pg.row_count(pcon, schema, pt, on_retry=on_retry)),
        ))
    return units


def _sync_units(ocon, pcon, schema, owner, table_names, on_retry=None):
    """One work item per thing that loads into a Postgres table.

    A plain table is one unit. A union group is one unit per source. A join
    group is one unit for the whole group. Each unit carries its own fetch /
    reset / count callables, so the sync loop never branches on what kind of
    thing it is loading. Returns (units, errors).
    """
    units, errors = [], []
    grouped = groups.grouped_sources()
    for t in mapping.expand(table_names):
        if mapping.oracle_table(t) in grouped:
            continue
        try:
            units.append(_table_unit(ocon, pcon, schema, owner, t, on_retry=on_retry))
        except Exception as e:  # noqa: BLE001
            errors.append(dict(label=t, error=str(e)))
    for g in groups.active(table_names):
        try:
            if g["mode"] == "split":
                units.extend(_split_units(ocon, pcon, schema, owner, g, on_retry=on_retry))
                continue
            per_source = _group_per_source(ocon, owner, g, on_retry=on_retry)
            plan = _group_plan(g, per_source)
            if plan["errors"]:
                raise RuntimeError("; ".join(plan["errors"]))
            if g["mode"] == "union":
                units.extend(_union_units(ocon, pcon, schema, owner, g, per_source,
                                          plan, on_retry=on_retry))
            else:
                units.append(_join_unit(ocon, pcon, schema, owner, g, per_source,
                                        plan, on_retry=on_retry))
        except Exception as e:  # noqa: BLE001
            errors.append(dict(label=g["target"], error=str(e)))
    return units, errors


def sync_stream(table_names, fresh_tables=None):
    """Generator of progress dict-events for an SSE stream.

    `fresh_tables`: labels to force a fresh reload for even if a checkpoint
    exists (the "start fresh" override) - everything else resumes
    automatically from its checkpoint if one is present, so an interrupted
    sync doesn't silently reload from scratch.
    """
    fresh_tables = set(fresh_tables or ())
    ocfg, pcfg = _cfgs()
    schema = pcfg["target_schema"]
    owner = ora.owner(ocfg)

    failures = preflight.run(ocfg, pcfg, table_names)
    if failures:
        for f in failures:
            yield dict(type="preflight_error", check=f["check"], table=f["table"], error=f["error"])
        yield dict(type="fatal", error="Pre-flight checks failed; sync aborted before making any changes.")
        return

    on_retry, drain = _retry_hook()
    ocon = ora.connect(ocfg, on_retry=on_retry)
    yield from drain()
    pcon = pg.connect(pcfg, on_retry=on_retry)
    yield from drain()
    fast_pgt = {}
    captured = {}
    restored = False
    try:
        units, unit_errors = _sync_units(ocon, pcon, schema, owner, table_names,
                                         on_retry=on_retry)
        yield from drain()
        for e in unit_errors:
            yield dict(type="table_error", table=e["label"], error=e["error"])

        # Parents load before children, so reloaded child rows find their
        # parent rows already there. Read before fast-load drops the FKs.
        # A table a Postgres lookup reads counts as a parent too.
        by_table = {}
        for i, u in enumerate(units):
            by_table.setdefault(u["pg_table"], []).append(i)
        idx_edges = [(c, p) for ct, pt in pg.fk_edges(pcon, schema, on_retry=on_retry)
                     for c in by_table.get(ct, ()) for p in by_table.get(pt, ())]
        idx_edges += [(c, p) for c, u in enumerate(units) for pt in u.get("after", ())
                      for p in by_table.get(pt, ())]
        units = [units[i] for i in depgraph.topo_sort(range(len(units)), idx_edges)]
        yield from drain()

        fast_pgt = _fast_load_targets(table_names)
        if fast_pgt:
            yield from _capture_and_drop_constraints(pcon, schema, fast_pgt, captured)

        # Empty every table that starts fresh in ONE truncate: Postgres refuses
        # to truncate a parent on its own while a child references it, even if
        # the child is about to be emptied too. If a referencing table is
        # outside this run the combined truncate fails as well, and the
        # per-table resets below report it.
        fresh = [u for u in units if u.get("truncates")
                 and not (u["stored_fp"] and u["stored_fp"] != u["fingerprint"])
                 and (u["label"] in fresh_tables or not history.get_checkpoint(u["checkpoint_key"]))]
        if len(fresh) > 1:
            try:
                pg.truncate(pcon, schema, list(dict.fromkeys(u["truncates"] for u in fresh)),
                            on_retry=on_retry)
                for u in fresh:
                    u["reset"] = lambda: None
            except Exception:  # noqa: BLE001
                pcon.rollback()
            yield from drain()

        for u in units:
            t = u["label"]
            run_id = None
            rows_loaded = 0
            rows_skipped = 0
            inserted = updated = 0
            try:
                if u["stored_fp"] and u["stored_fp"] != u["fingerprint"]:
                    yield dict(
                        type="table_error", table=t, mapping_drift=True,
                        error=(u["drift_hint"] + " Recreate to apply changes? "
                               "This will drop and reload all data."),
                    )
                    continue
                yield dict(type="table_start", table=t, pg_table=u["pg_table"])

                checkpoint = None if t in fresh_tables else history.get_checkpoint(u["checkpoint_key"])
                if checkpoint:
                    offset = checkpoint["last_offset"]
                    yield dict(type="resuming", table=t, offset=offset)
                else:
                    u["reset"]()
                    yield from drain()
                    history.clear_checkpoint(u["checkpoint_key"])
                    offset = 0

                oracle_total = u["total"]()
                yield from drain()
                run_id = history.start_run(t, u["pg_table"])
                start_time = time.monotonic()

                last_key = None
                misses = {}  # (column, dictionary) -> distinct values not in it
                while True:
                    chunk, last_key = u["fetch"](offset, CHUNK_SIZE, last_key)
                    yield from drain()
                    if not chunk:
                        break
                    rows, rejects = u["transform"](chunk) if u.get("transform") else (chunk, [])
                    for i, row_misses in rejects:
                        history.log_row_error(run_id, t, str(offset + i), "; ".join(
                            f'{col}: "{v}" is not in dictionary {d}' for col, d, v in row_misses))
                        for col, d, v in row_misses:
                            seen = misses.setdefault((col, d), set())
                            if len(seen) < MISS_CAP:
                                seen.add(v)
                    # chunk position of each entry of `rows`, for row error refs
                    held = {i for i, _ in rejects}
                    at = [i for i in range(len(chunk)) if i not in held] if held else range(len(chunk))
                    try:
                        changes = u.get("copy", pg.copy_chunk)(pcon, schema, u["pg_table"], u["pg_cols"],
                                                               rows, on_retry=on_retry) if rows else None
                        if changes:
                            inserted += changes[0]
                            updated += changes[1]
                        yield from drain()
                        ok, failed = len(rows), 0
                    except Exception as e:  # noqa: BLE001
                        if retry.is_pg_transient(e):
                            raise  # outage, not a bad row - abort, keep the checkpoint
                        yield from drain()
                        chunk_offset = offset

                        def _on_row_error(i, err, chunk_offset=chunk_offset, t=t, at=at):
                            history.log_row_error(run_id, t, str(chunk_offset + at[i]), str(err))

                        ok, failed = pg.insert_rows_capture_errors(
                            pcon, schema, u["pg_table"], u["pg_cols"], rows,
                            _on_row_error, on_retry=on_retry, tail=u.get("row_tail", ""))
                        yield from drain()

                    rows_loaded += ok
                    rows_skipped += failed + len(rejects)
                    offset += len(chunk)
                    history.save_checkpoint(u["checkpoint_key"], offset)

                    elapsed = time.monotonic() - start_time
                    rate = rows_loaded / elapsed if elapsed > 0 else 0
                    remaining = max(oracle_total - offset, 0)
                    eta_seconds = (remaining / rate) if rate > 0 else None
                    yield dict(type="progress", table=t, rows=offset, rows_loaded=rows_loaded,
                               rows_skipped=rows_skipped, rows_per_sec=rate,
                               eta_seconds=eta_seconds)

                    if len(chunk) < CHUNK_SIZE:
                        break

                history.clear_checkpoint(u["checkpoint_key"])
                oc = u["total"]()
                pc = u["pg_count"]()
                yield from drain()
                status = "success" if rows_skipped == 0 else "partial"
                history.finish_run(run_id, rows_loaded, rows_skipped, status)
                yield dict(type="table_done", table=t, rows=offset, rows_loaded=rows_loaded,
                           rows_skipped=rows_skipped, run_id=run_id,
                           oracle=oc, pg=pc, match=(oc == pc),
                           # ponytail: rows from the row-by-row fallback count as unchanged
                           changes=(dict(inserted=inserted, updated=updated,
                                         unchanged=rows_loaded - inserted - updated)
                                    if u.get("counts_changes") else None),
                           dictionary_misses=[
                               dict(column=col, dictionary=d, values=sorted(v), capped=len(v) >= MISS_CAP)
                               for (col, d), v in misses.items()])
            except Exception as e:  # noqa: BLE001
                yield from drain()
                if run_id is not None:
                    history.finish_run(run_id, rows_loaded, rows_skipped, "failed")
                try:
                    pcon.rollback()
                except Exception:
                    pass
                msg = str(e)
                if "referenced in a foreign key" in msg:
                    msg += ("\nTip: other tables point at this one, so it can't be emptied on its own - "
                            "include the referencing table(s) in this sync so they're emptied together, or "
                            'turn on "Upsert / insert only" for this mapping on the Mappings tab.')
                yield dict(type="table_error", table=t, error=msg)

        if fast_pgt:
            yield from _restore_constraints(pcon, schema, fast_pgt, captured)
            restored = True

        yield dict(type="all_done")
    finally:
        try:
            if fast_pgt and not restored:
                # The browser closed the SSE stream (or the generator was
                # otherwise abandoned) while constraints are still dropped.
                # Restore them with their events discarded: once GeneratorExit
                # has been thrown in here yielding is illegal, and a silent
                # restore beats leaving a table without its PK/FK/indexes and
                # no record that they were ever dropped.
                # ponytail: a hard process kill still strands them - persist
                # `captured` to SQLite if that turns into a real failure mode.
                for _ in _restore_constraints(pcon, schema, fast_pgt, captured):
                    pass
        except Exception:  # noqa: BLE001 - the stream is gone, nothing to report to
            pass
        finally:
            ocon.close()
            pcon.close()
