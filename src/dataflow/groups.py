"""Table groups: several Oracle tables feeding one Postgres table, or one
Oracle table split into several.

Three modes, because "many tables into one" means two different things:

- **join** links rows. The sources are joined on PK/FK/UK columns and the
  target holds their combined columns - one wide row per joined result
  row. This is the unified-table case: EMPLOYEES joined to HR_EMPLOYEE
  gives one row per employee carrying columns from both.
- **union** stacks rows. The sources are shaped alike and their rows are
  appended into the shared table, each tagged with types_map.SOURCE_COLUMN
  so one source can be reloaded without disturbing the others.

A group record:

    {"target": "employee_unified",
     "mode": "join",
     "sources": ["EMPLOYEES", "HR_EMPLOYEE"],   # sources[0] drives the join
     "joins": [{"right": "HR_EMPLOYEE", "left": "EMPLOYEES", "type": "inner",
                "on": [{"left_col": "EMP_ID", "right_col": "EMPLOYEE_ID"}]}],
     "created_fingerprint": None, "created_target": None}

- **split** goes the other way: one source, several Postgres tables
  ("parts"). Each part takes a subset of the columns (empty = all) and/or
  the rows matching an Oracle WHERE filter. The source's primary key is
  copied into every part so the pieces can be joined back together. Here
  "target" is only the group's name; the tables are the parts' pg_table.

    {"target": "employee_split", "mode": "split", "sources": ["EMPLOYEES"],
     "parts": [{"pg_table": "emp_core", "columns": ["NAME"], "where": ""},
               {"pg_table": "emp_pay", "columns": ["SALARY"], "where": ""}]}

Per-column renames, skips and type overrides still come from mapping.py: a
group decides which tables combine and how they link, not what the columns
end up called.
"""
import hashlib
import json

from . import store
from . import mapping

_KEY = "groups"
JOIN_TYPES = ("inner", "left")
MODES = ("join", "union", "split")


def alias(i):
    """SQL alias for the i-th source. Shared by the column plan and the
    join SQL so the two always agree."""
    return f"t{i}"


# ---------- storage ----------
def all_groups():
    return store.get(_KEY, [])


def get(target):
    for g in all_groups():
        if g["target"] == target:
            return g
    return None


def save(group):
    """Insert or replace by target name. Returns (group, errors) - nothing is
    stored if errors is non-empty."""
    group = normalize(group)
    others = [g for g in all_groups() if g["target"] != group["target"]]
    errors = validate(group, others)
    if errors:
        return None, errors
    existing = get(group["target"])
    if existing:  # keep the created-state of a group being edited
        group["created_fingerprint"] = existing.get("created_fingerprint")
        group["created_target"] = existing.get("created_target")
    store.put(_KEY, others + [group])
    return group, []


def delete(target):
    store.put(_KEY, [g for g in all_groups() if g["target"] != target])


def for_source(table):
    """The group a given Oracle table belongs to, or None. A table belongs to
    at most one group (validate() enforces it)."""
    for g in all_groups():
        if table in g["sources"]:
            return g
    return None


def by_name(name):
    """A group addressed either by its target name or by one of its sources.

    The drift panel and the recreate button only know the label an event
    carried, which is the group target for a join and a source table for a
    union - both have to find the same group.
    """
    return get(name) or for_source(name) or next(
        (g for g in all_groups() if name in pg_tables(g) and g["mode"] == "split"), None)


def pg_tables(group):
    """Every Postgres table a group writes to."""
    if group["mode"] == "split":
        return [p["pg_table"] for p in group.get("parts", [])]
    return [group["target"]]


def grouped_sources():
    return {s for g in all_groups() for s in g["sources"]}


def active(table_names):
    """Groups with at least one source among `table_names`.

    A group is always handled whole - every source feeds the one target - so
    having one member in the batch pulls in the group, not a slice of it.
    """
    names = set(table_names)
    return [g for g in all_groups() if names.intersection(g["sources"])]


# ---------- validation ----------
def normalize(group):
    out = dict(
        target=(group.get("target") or "").strip().lower(),
        mode=(group.get("mode") or "join").strip().lower(),
        sources=list(dict.fromkeys(group.get("sources") or [])),
        joins=[],
        parts=[],
    )
    if out["mode"] == "split":
        for p in group.get("parts") or []:
            out["parts"].append(dict(
                pg_table=(p.get("pg_table") or "").strip().lower(),
                columns=list(dict.fromkeys(c.strip().upper() for c in (p.get("columns") or []) if c.strip())),
                where=(p.get("where") or "").strip(),
            ))
    if out["mode"] == "join":
        for j in group.get("joins") or []:
            out["joins"].append(dict(
                right=j.get("right"),
                left=j.get("left"),
                type=(j.get("type") or "inner").strip().lower(),
                on=[dict(left_col=p.get("left_col"), right_col=p.get("right_col"))
                    for p in (j.get("on") or []) if p.get("left_col") and p.get("right_col")],
            ))
    return out


def validate(group, others):
    """Everything that would otherwise fail halfway through a create or a
    sync, reported as a list of plain messages for the UI."""
    errors = []
    if not group["target"]:
        errors.append("Target table name is required.")
    if group["mode"] not in MODES:
        errors.append(f'Unknown mode "{group["mode"]}".')
    if group["mode"] == "split":
        errors.extend(_validate_split(group))
    elif len(group["sources"]) < 2:
        errors.append("A group needs at least 2 Oracle tables.")

    taken = {s: g["target"] for g in others for s in g["sources"]}
    for s in group["sources"]:
        if s in taken:
            errors.append(f'{s} already belongs to group "{taken[s]}".')
    if any(g["target"] == group["target"] for g in others):
        errors.append(f'Another group is already named "{group["target"]}".')
    for g in others:
        for pt in set(pg_tables(g)).intersection(pg_tables(group)):
            errors.append(f'Group "{g["target"]}" already writes to "{pt}".')

    # A plain (ungrouped) table mapped to the same Postgres name would fight
    # the group for the table on every create and sync.
    for t, m in (store.get("mappings", {}) or {}).items():
        if t in group["sources"]:
            continue
        mapped = m.get("pg_table") or mapping.default_table_name(t)
        if mapped in pg_tables(group):
            errors.append(f'Table {t} is already mapped to "{mapped}" on its own.')

    if group["mode"] == "join":
        errors.extend(_validate_joins(group))
    return errors


def _validate_split(group):
    errors = []
    if len(group["sources"]) != 1:
        errors.append("A split takes exactly 1 Oracle table.")
    if len(group["parts"]) < 2:
        errors.append("A split needs at least 2 Postgres tables.")
    names = [p["pg_table"] for p in group["parts"]]
    if not all(names):
        errors.append("Every part needs a Postgres table name.")
    for n in {n for n in names if n and names.count(n) > 1}:
        errors.append(f'"{n}" is used by more than one part.')
    return errors


def _validate_joins(group):
    errors = []
    by_right = {}
    for j in group["joins"]:
        if j["right"] in by_right:
            errors.append(f'{j["right"]} is linked more than once.')
        by_right[j["right"]] = j

    seen = {group["sources"][0]}
    for s in group["sources"][1:]:
        j = by_right.get(s)
        if not j:
            errors.append(f"{s} has no link condition - say how it joins to the others.")
            continue
        if j["type"] not in JOIN_TYPES:
            errors.append(f'{s}: join type must be one of {", ".join(JOIN_TYPES)}.')
        if not j["on"]:
            errors.append(f"{s}: pick at least one pair of columns to link on.")
        # Ordering matters: the SQL joins sources left to right, so a source
        # can only link to one already joined before it.
        if j["left"] not in seen:
            errors.append(f'{s} links to {j["left"] or "(nothing)"}, '
                          "which is not joined before it - reorder the tables.")
        seen.add(s)

    extra = [r for r in by_right if r not in group["sources"]]
    for r in extra:
        errors.append(f"{r} has a link condition but is not in the group.")
    return errors


# ---------- column plan ----------
def _nullable_sources(group):
    """Sources whose columns must allow NULL because they are reached
    through a LEFT JOIN, directly or via another left-joined table - an
    unmatched row supplies NULL for every one of their columns."""
    nullable = set()
    by_right = {j["right"]: j for j in group["joins"]}
    for s in group["sources"][1:]:
        j = by_right.get(s)
        if j and (j["type"] == "left" or j["left"] in nullable):
            nullable.add(s)
    return nullable


def join_plan(group, per_source):
    """Columns of a joined target, in source order.

    `per_source` is {oracle_table: resolved column list} from
    mapping.resolve(). Returns a list of dicts with source / name / target /
    pg_type / nullable / renamed_from, ready for types_map.coldef().

    Name collisions are suffixed with the source table, because joined
    tables routinely share column names (NAME, CREATED_AT, STATUS) and the
    target needs one column for each. The suffix shows up in the dry-run
    DDL, and a per-column rename on the Columns tab overrides it.
    """
    nullable_sources = _nullable_sources(group)
    taken = set()
    columns = []
    for i, s in enumerate(group["sources"]):
        for c in per_source.get(s, []):
            if c["skip"]:
                continue
            target = c["target"]
            if target in taken:
                target = f'{c["target"]}_{s.lower()}'
                n = 2
                while target in taken:
                    target = f'{c["target"]}_{s.lower()}_{n}'
                    n += 1
            taken.add(target)
            columns.append(dict(
                source=s, alias=alias(i), name=c["name"], target=target,
                pg_type=c["pg_type"],
                nullable=bool(c["nullable"]) or s in nullable_sources,
                renamed_from=(c["target"] if target != c["target"] else None),
                if_null=c.get("if_null"), dictionary=c.get("dictionary"),
                lookup_with=c.get("lookup_with"),
            ))
    return columns


def join_from(owner, group):
    """The FROM clause for a join group: the driving table plus one JOIN per
    linked source, in group order.

    Rendered here rather than in oracle_client so the aliases match
    join_plan()'s exactly - the column list and the FROM clause have to
    agree or the query selects from the wrong table.
    """
    srcs = group["sources"]
    alias_of = {s: alias(i) for i, s in enumerate(srcs)}
    parts = [f'"{owner}"."{srcs[0]}" {alias_of[srcs[0]]}']
    by_right = {j["right"]: j for j in group["joins"]}
    for s in srcs[1:]:
        j = by_right[s]
        on = " AND ".join(
            f'{alias_of[j["left"]]}."{p["left_col"]}" = {alias_of[s]}."{p["right_col"]}"'
            for p in j["on"])
        kind = "LEFT JOIN" if j["type"] == "left" else "INNER JOIN"
        parts.append(f'{kind} "{owner}"."{s}" {alias_of[s]} ON {on}')
    return " ".join(parts)


def split_plan(group, resolved, pk):
    """Per-part columns for a split: [{pg_table, where, columns, pk}].

    `resolved` is the source's mapping.resolve() output, `pk` its Oracle
    primary key. Every part gets the PK columns first, then its chosen
    columns (all non-skipped ones when none are chosen). Returns
    (parts, errors).
    """
    errors = []
    if not pk:
        errors.append("a split needs a primary key on the source - it is copied into "
                      "every part so rows can be paged and joined back together")
    by_name = {c["name"]: c for c in reversed(resolved)}  # first destination wins
    skipped_pk = [k for k in pk if by_name.get(k, {}).get("skip")]
    if skipped_pk:
        errors.append("primary key columns are skipped on the Columns tab: " + ", ".join(skipped_pk))
    parts = []
    for p in group["parts"]:
        unknown = [c for c in p["columns"] if c not in by_name]
        if unknown:
            errors.append(f'{p["pg_table"]}: no such column(s) {", ".join(unknown)}')
        wanted = set(p["columns"]) | set(pk) if p["columns"] else set(by_name)
        cols = [c for c in resolved if c["name"] in wanted and not c["skip"]]
        cols.sort(key=lambda c: c["name"] not in pk)  # PK first, else source order
        parts.append(dict(pg_table=p["pg_table"], where=p["where"], columns=cols,
                          pk=[by_name[k]["target"] for k in pk if k in by_name]))
    return parts, errors


def split_from(owner, table, where):
    """FROM clause for one split part. A filter goes in an inline view so
    the paging WHERE that fetch_page appends stays a single clause."""
    if not where:
        return f'"{owner}"."{table}" {alias(0)}'
    return f'(SELECT * FROM "{owner}"."{table}" WHERE {where}) {alias(0)}'


# ---------- drift ----------
def fingerprint(group, per_source):
    """Hash of everything that shapes the target: the group definition plus
    every source's column mapping. A change to any of them means the shared
    table has to be recreated as a unit."""
    payload = {
        "target": group["target"],
        "mode": group["mode"],
        "sources": group["sources"],
        "joins": group["joins"],
        "mappings": {
            s: [{"name": c["name"], "target": c["target"], "skip": c["skip"],
                 "pg_type": c["pg_type"]} for c in cols]
            for s, cols in sorted(per_source.items())
        },
    }
    if group["mode"] == "split":  # only here, so join/union fingerprints don't change
        payload["parts"] = group["parts"]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def mark_created(target, fp):
    groups = all_groups()
    for g in groups:
        if g["target"] == target:
            g["created_fingerprint"] = fp
            g["created_target"] = target
    store.put(_KEY, groups)


def clear_created(target):
    groups = all_groups()
    for g in groups:
        if g["target"] == target:
            g["created_fingerprint"] = None
            g["created_target"] = None
    store.put(_KEY, groups)
