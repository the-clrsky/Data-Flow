"""Checks for table groups - several Oracle tables feeding one Postgres
table, either joined on keys or unioned. Pure: no Oracle, no Postgres.

    python tests/test_groups.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["DATAFLOW_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from dataflow import groups, store  # noqa: E402

# A test must never write to the project's own data-flow.db: store.DB_PATH is
# bound at import time, so DATAFLOW_DB has to be set before the first dataflow
# import. Getting that order wrong once already overwrote real saved state.
from dataflow import store as _store  # noqa: E402
assert "data-flow.db" not in _store.DB_PATH, (
    f"tests are pointed at the real state database ({_store.DB_PATH}); set "
    "DATAFLOW_DB before importing anything from dataflow")


def col(target, pg_type="text", nullable=True, skip=False):
    return dict(name=target.upper(), target=target, pg_type=pg_type,
                nullable=nullable, skip=skip)


def join(right, left, left_col, right_col, type="inner"):
    return dict(right=right, left=left, type=type,
                on=[dict(left_col=left_col, right_col=right_col)])


EMP_HR = dict(
    target="employee_unified", mode="join",
    sources=["EMPLOYEES", "HR_EMPLOYEE"],
    joins=[join("HR_EMPLOYEE", "EMPLOYEES", "EMP_ID", "EMPLOYEE_ID")],
)


def errs(group, others=()):
    return groups.validate(groups.normalize(group), list(others))


def test_a_valid_join_group_passes():
    store.put("mappings", {})
    assert errs(EMP_HR) == []


def test_a_group_needs_two_tables_and_a_target():
    assert any("at least 2" in e for e in errs(dict(EMP_HR, sources=["EMPLOYEES"])))
    assert any("required" in e for e in errs(dict(EMP_HR, target="")))


def test_every_joined_table_needs_a_link():
    bad = dict(EMP_HR, sources=["EMPLOYEES", "HR_EMPLOYEE", "AP_GENAPPMST"])
    assert any("AP_GENAPPMST has no link condition" in e for e in errs(bad))


def test_a_table_cannot_link_to_one_joined_after_it():
    # HR_EMPLOYEE is joined first but links to a table that comes later, which
    # no left-to-right join order can satisfy
    bad = dict(
        target="x", mode="join", sources=["EMPLOYEES", "HR_EMPLOYEE", "AP_GENAPPMST"],
        joins=[join("HR_EMPLOYEE", "AP_GENAPPMST", "A", "B"),
               join("AP_GENAPPMST", "EMPLOYEES", "C", "D")],
    )
    assert any("not joined before it" in e for e in errs(bad))


def test_a_table_belongs_to_only_one_group():
    other = groups.normalize(dict(target="other", mode="union",
                                  sources=["EMPLOYEES", "ACCESS_ISSUE"]))
    assert any("already belongs to group" in e for e in errs(EMP_HR, [other]))


def test_a_plain_table_cannot_own_the_same_target():
    store.put("mappings", {"ACCESS_ISSUE": {"pg_table": "employee_unified"}})
    assert any("already mapped" in e for e in errs(EMP_HR))
    store.put("mappings", {})


def test_join_plan_combines_columns_and_suffixes_collisions():
    per_source = {
        "EMPLOYEES": [col("id", "bigint", nullable=False), col("name")],
        "HR_EMPLOYEE": [col("name"), col("grade")],
    }
    cols = groups.join_plan(groups.normalize(EMP_HR), per_source)
    # both sources have NAME; the target needs one column for each
    assert [c["target"] for c in cols] == ["id", "name", "name_hr_employee", "grade"]
    assert [c["alias"] for c in cols] == ["t0", "t0", "t1", "t1"]
    renamed = [c for c in cols if c["renamed_from"]]
    assert renamed[0]["renamed_from"] == "name" and renamed[0]["source"] == "HR_EMPLOYEE"


def test_left_join_forces_the_right_sides_columns_nullable():
    per_source = {
        "EMPLOYEES": [col("id", "bigint", nullable=False)],
        "HR_EMPLOYEE": [col("grade", "text", nullable=False)],
    }
    g = groups.normalize(dict(
        EMP_HR, joins=[join("HR_EMPLOYEE", "EMPLOYEES", "EMP_ID", "EMPLOYEE_ID", type="left")]))
    cols = {c["target"]: c for c in groups.join_plan(g, per_source)}
    assert cols["id"]["nullable"] is False
    # an unmatched left join supplies NULL for every column of the right side
    assert cols["grade"]["nullable"] is True


def test_nullability_propagates_through_a_left_joined_table():
    g = groups.normalize(dict(
        target="x", mode="join", sources=["A", "B", "C"],
        joins=[join("B", "A", "I", "I", type="left"), join("C", "B", "I", "I")]))
    # C is inner-joined, but only to a table that may itself be all NULLs
    assert groups._nullable_sources(g) == {"B", "C"}


def test_skipped_columns_stay_out_of_the_join():
    per_source = {"EMPLOYEES": [col("id"), col("junk", skip=True)],
                  "HR_EMPLOYEE": [col("grade")]}
    cols = groups.join_plan(groups.normalize(EMP_HR), per_source)
    assert [c["target"] for c in cols] == ["id", "grade"]


def test_join_sql_is_left_to_right_with_matching_aliases():
    g = groups.normalize(dict(
        target="x", mode="join", sources=["EMPLOYEES", "HR_EMPLOYEE", "DEPT"],
        joins=[join("HR_EMPLOYEE", "EMPLOYEES", "EMP_ID", "EMPLOYEE_ID"),
               dict(right="DEPT", left="HR_EMPLOYEE", type="left",
                    on=[dict(left_col="DEPT_ID", right_col="ID"),
                        dict(left_col="CO", right_col="CO")])]))
    sql = groups.join_from("KYAMCH_LIVE", g)
    assert sql == (
        '"KYAMCH_LIVE"."EMPLOYEES" t0 '
        'INNER JOIN "KYAMCH_LIVE"."HR_EMPLOYEE" t1 ON t0."EMP_ID" = t1."EMPLOYEE_ID" '
        'LEFT JOIN "KYAMCH_LIVE"."DEPT" t2 ON t1."DEPT_ID" = t2."ID" AND t1."CO" = t2."CO"')


def test_fingerprint_moves_when_anything_shaping_the_table_moves():
    per_source = {"EMPLOYEES": [col("id")], "HR_EMPLOYEE": [col("grade")]}
    g = groups.normalize(EMP_HR)
    base = groups.fingerprint(g, per_source)
    assert base == groups.fingerprint(groups.normalize(EMP_HR), per_source)
    # a changed join type, a changed mapping, or a changed mode all change
    # the table that would be built, so all must flag drift
    left = groups.normalize(dict(
        EMP_HR, joins=[join("HR_EMPLOYEE", "EMPLOYEES", "EMP_ID", "EMPLOYEE_ID", type="left")]))
    assert groups.fingerprint(left, per_source) != base
    assert groups.fingerprint(g, {"EMPLOYEES": [col("id", "bigint")],
                                  "HR_EMPLOYEE": [col("grade")]}) != base
    assert groups.fingerprint(groups.normalize(dict(EMP_HR, mode="union")), per_source) != base


def test_save_round_trip_and_lookup():
    store.put("groups", [])
    saved, errors = groups.save(EMP_HR)
    assert errors == [] and saved["target"] == "employee_unified"
    assert groups.grouped_sources() == {"EMPLOYEES", "HR_EMPLOYEE"}
    assert groups.for_source("HR_EMPLOYEE")["target"] == "employee_unified"
    # a group is addressable by its target or by any of its sources, because
    # the drift panel only knows the label it was given
    assert groups.by_name("employee_unified")["target"] == "employee_unified"
    assert groups.by_name("EMPLOYEES")["target"] == "employee_unified"
    # one member in the batch pulls in the whole group, never a slice
    assert [g["target"] for g in groups.active(["EMPLOYEES"])] == ["employee_unified"]
    assert groups.active(["SOMETHING_ELSE"]) == []

    groups.mark_created("employee_unified", "fp1")
    assert groups.get("employee_unified")["created_fingerprint"] == "fp1"
    # editing a group keeps its created-state, so drift is detected not reset
    groups.save(dict(EMP_HR, mode="union"))
    assert groups.get("employee_unified")["created_fingerprint"] == "fp1"
    groups.delete("employee_unified")
    assert groups.all_groups() == []


def test_fast_load_drops_a_group_target_once():
    from dataflow import engine
    store.put("groups", [])
    store.put("mappings", {t: {"fast_load": True} for t in ("EMPLOYEES", "HR_EMPLOYEE")})
    groups.save(EMP_HR)
    assert engine._fast_load_targets(["EMPLOYEES", "HR_EMPLOYEE"]) == {
        "employee_unified": "employee_unified"}
    groups.delete("employee_unified")
    store.put("mappings", {})


SPLIT = dict(
    target="emp_split", mode="split", sources=["EMPLOYEES"],
    parts=[dict(pg_table="Emp_Core", columns=["name"]),
           dict(pg_table="emp_pay", columns=["SALARY"], where="SALARY > 0")],
)


def test_split_validate_and_plan():
    store.put("groups", [])
    store.put("mappings", {})
    g, errs = groups.save(SPLIT)
    assert errs == [], errs
    assert groups.pg_tables(g) == ["emp_core", "emp_pay"]  # names lowercased
    assert g["parts"][0]["columns"] == ["NAME"]            # columns uppercased
    assert groups.by_name("emp_pay")["target"] == "emp_split"

    _, errs = groups.save(dict(SPLIT, parts=SPLIT["parts"][:1]))
    assert any("at least 2" in e for e in errs)
    _, errs = groups.save(dict(SPLIT, sources=["A", "B"]))
    assert any("exactly 1" in e for e in errs)
    # another group may not write to a part's table
    _, errs = groups.save(dict(target="emp_pay", mode="union", sources=["X", "Y"]))
    assert any('already writes to "emp_pay"' in e for e in errs)

    resolved = [col("emp_id", nullable=False), col("name"), col("salary"), col("notes", skip=True)]
    parts, errs = groups.split_plan(g, resolved, ["EMP_ID"])
    assert errs == []
    assert [c["target"] for c in parts[0]["columns"]] == ["emp_id", "name"]  # PK always carried
    assert parts[0]["pk"] == ["emp_id"]
    _, errs = groups.split_plan(g, resolved, [])
    assert any("primary key" in e for e in errs)
    _, errs = groups.split_plan(dict(g, parts=[dict(pg_table="x", columns=["NOPE"], where="")]),
                                resolved, ["EMP_ID"])
    assert any("NOPE" in e for e in errs)
    # empty column list = every non-skipped column
    parts, _ = groups.split_plan(dict(g, parts=[dict(pg_table="x", columns=[], where="")]),
                                 resolved, ["EMP_ID"])
    assert [c["target"] for c in parts[0]["columns"]] == ["emp_id", "name", "salary"]

    assert groups.split_from("HR", "EMPLOYEES", "") == '"HR"."EMPLOYEES" t0'
    assert groups.split_from("HR", "EMPLOYEES", "SALARY > 0") == \
        '(SELECT * FROM "HR"."EMPLOYEES" WHERE SALARY > 0) t0'
    groups.delete("emp_split")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
