"""Checks for Postgres lookups in value_filler: single and multi-column
match keys, the extra Oracle columns being dropped, misses, and the
mapping/save validation. Pure: Postgres is a fake.

    python tests/test_lookups.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["DATAFLOW_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from decimal import Decimal  # noqa: E402

from dataflow import mapping  # noqa: E402

COLS = [dict(name=n, dtype="VARCHAR2", prec=None, scale=None, clen=10, length=10, nullable=True)
        for n in ("CODE", "BRANCH", "NAME")]

mapping.save_lookup("CUST", "customers", ["code"], "id")
mapping.save_lookup("CUST2", "customers", [" code ", "branch", ""], "id")
assert mapping.lookups()["CUST2"] == dict(table="customers", match=["code", "branch"], value="id")
for args, why in [(("", "t", ["a"], "b"), "Name"), (("X", "t", [], "b"), "needs"),
                  (("X", 't"', ["a"], "b"), "quotes")]:
    try:
        mapping.save_lookup(*args)
        raise AssertionError(args)
    except ValueError as e:
        assert why in str(e), e
mapping.save_dictionary("D", [["oracle", "postgres"], ["1", "2"]])
for clash in (lambda: mapping.save_lookup("D", "t", ["a"], "b"),
              lambda: mapping.save_dictionary("CUST", [["oracle", "postgres"], ["1", "2"]])):
    try:
        clash()
        raise AssertionError("name clash allowed")
    except ValueError as e:
        assert "already" in str(e), e

PARENT = {("A1", "10"): "100", ("A1", "20"): "101", ("7", "10"): "102"}
calls = []


def source(lookup):
    def get(keys):
        calls.append((lookup["table"], set(keys)))
        n = len(lookup["match"])
        return {k: v for k, v in ((k[:n], v) for k, v in PARENT.items()) if k in keys} if n == 2 else \
            {("A1",): "100"}
    return get


# CODE goes to code_id through CUST2, matched with BRANCH, which isn't loaded itself
mapping.save("T", None, [
    dict(name="CODE", target="code_id", dictionary="CUST2", lookup_with=["BRANCH"]),
    dict(name="BRANCH", skip=True),
    dict(name="NAME"),
])
_, resolved = mapping.resolve("T", COLS)
kept = [c for c in resolved if not c["skip"]]
assert mapping.lookup_inputs(kept) == ["BRANCH"]
assert mapping.lookup_tables(kept) == {"customers"}
fill = mapping.value_filler(kept, source)
# rows: kept columns (CODE, NAME) then the lookup inputs (BRANCH)
rows, rejects = fill([("A1", "x", "20"), (Decimal(7), "y", Decimal(10)), (None, "z", "10"),
                      ("A1", "w", "99"), ("A1", "v", None)])
assert rows == [("101", "x"), ("102", "y"), (None, "z")], rows
assert rejects == [(3, [("CODE", "CUST2", "A1 / 99")]), (4, [("CODE", "CUST2", "A1 / NULL")])], rejects
# one call per chunk, with that chunk's distinct keys (a NULL CODE isn't looked up)
assert calls == [("customers", {("A1", "20"), ("7", "10"), ("A1", "99"), ("A1", None)})], calls
assert fill([]) == ([], [])

# single-column lookup: no extra inputs
mapping.save("U", None, [dict(name="CODE", dictionary="CUST"), dict(name="BRANCH", skip=True),
                         dict(name="NAME", skip=True)])
kept = [c for c in mapping.resolve("U", COLS)[1] if not c["skip"]]
assert mapping.lookup_inputs(kept) == []
assert mapping.value_filler(kept, source)([("A1 ",), ("B",)]) == ([("100",)], [(1, [("CODE", "CUST", "B")])])

# a multi-column lookup without its other Oracle columns can't run
mapping.save("V", None, [dict(name="CODE", dictionary="CUST2")])
try:
    mapping.value_filler([c for c in mapping.resolve("V", COLS)[1] if not c["skip"]], source)
    raise AssertionError("missing lookup_with accepted")
except RuntimeError as e:
    assert "1 other Oracle column" in str(e), e

# a lookup isn't DDL: it must not flag drift
fp = mapping.fingerprint("t", mapping.resolve("T", COLS)[1])
mapping.save("T", None, [dict(name="CODE", target="code_id"), dict(name="BRANCH", skip=True), dict(name="NAME")])
assert mapping.fingerprint("t", mapping.resolve("T", COLS)[1]) == fp

print("ok")

# join / split groups: the column settings ride along and the lookup inputs
# are read through the column's own source alias
from dataflow import engine, groups  # noqa: E402

mapping.save("ORD", None, [dict(name="CODE", dictionary="CUST2", lookup_with=["BRANCH"]),
                           dict(name="BRANCH"), dict(name="NAME", if_null="-")])
per_source = {s: mapping.resolve(s, COLS)[1] for s in ("ORD", "T")}
plan = groups.join_plan(dict(sources=["ORD", "T"], joins=[]), per_source)
assert plan[0]["dictionary"] == "CUST2" and plan[0]["lookup_with"] == ["BRANCH"]
exprs, transform, after = engine._aliased_fill(None, "public", plan, None)
assert exprs[-1] == 't0."BRANCH"' and exprs.count('t0."BRANCH"') == 2, exprs
assert after == {"customers"}
assert engine._aliased_fill(None, "public", plan[3:], None)[1] is None  # T: nothing to fill

# where a lookup is used, with the other columns it matches on
assert "ORD.CODE (with BRANCH) → code" in mapping.dictionary_users()["CUST2"], mapping.dictionary_users()

print("ok groups")
