"""Checks for Postgres-only columns ("Add to Sync"): fixed values and
dictionary / lookup rows keyed on an Oracle column. Pure: no databases.

    python tests/test_pg_only.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["DATAFLOW_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from dataflow import engine, mapping  # noqa: E402

COLS = [dict(name=n, dtype="VARCHAR2", prec=None, scale=None, clen=10, length=10, nullable=True)
        for n in ("ROOM_NO", "FLOOR_NO")]
mapping.save_dictionary("ORG", [["oracle", "postgres"], ["110", "1"], ["2", "3"]])

PLAIN = [dict(name="ROOM_NO"), dict(name="FLOOR_NO", target="floor_id")]
mapping.save("T", None, PLAIN)
fp_plain = mapping.fingerprint("t", mapping.resolve("T", COLS)[1])

# Postgres-only rows may arrive before the Oracle rows they read from
ROWS = [
    dict(name="FLOOR_NO", target="organization_id", dictionary="ORG", pg_only=True),
    dict(name="", target="record_version", if_null="1", pg_only=True),
] + PLAIN
assert engine.save_mapping("T", None, [dict(r) for r in ROWS]) is None
m = mapping.get("T")
assert m["constants"] == [dict(target="record_version", value="1")], m["constants"]
_, resolved = mapping.resolve("T", COLS)
assert [(c["name"], c["target"], c["copy"], c["pg_only"]) for c in resolved] == [
    ("ROOM_NO", "room_no", False, False),
    ("FLOOR_NO", "floor_id", False, False),           # the key's own row stays first
    ("FLOOR_NO", "organization_id", True, True),
], resolved
# they fill columns the table already has: no drift
assert mapping.fingerprint("t", resolved) == fp_plain

# a sync row: ROOM_NO, FLOOR_NO -> floor_id, FLOOR_NO -> organization_id, then the constants
fill = mapping.with_constants(mapping.value_filler(resolved), m["constants"])
assert fill([("A", "110", "110"), ("B", "9", "9")]) == (
    [("A", "110", "1", "1")], [(1, [("FLOOR_NO", "ORG", "9")])])
assert mapping.with_constants(None, m["constants"])([("A",)]) == ([("A", "1")], [])
assert mapping.with_constants(None, []) is None

# validation
for rows, why in [
    ([dict(name="", target="x", pg_only=True)], "fixed value"),
    ([dict(name="", target="x", if_null="1", dictionary="ORG", pg_only=True)], "fixed value"),
    ([dict(name="NOPE", target="x", dictionary="ORG", pg_only=True)], "no Oracle column NOPE"),
    ([dict(name="", target="", if_null="1", pg_only=True)], "no Postgres column"),
    ([dict(name="", target="room_no", if_null="1", pg_only=True)], "same target"),
]:
    err = engine.save_mapping("T", None, PLAIN + rows)
    assert err and why in err, (rows, err)

print("ok")
