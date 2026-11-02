"""Checks the sync order follows the selection (a group at its first
source's place) and that deleting history drops runs with their row
errors, one set only. Pure: no databases but a temporary SQLite file.

    python tests/test_sync_order.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["DATAFLOW_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from dataflow import engine, groups, history, mapping, store  # noqa: E402

assert "data-flow.db" not in store.DB_PATH

# ---- an extra follows its table unless it was given a place of its own
mapping.extras = lambda t: [f"{t}:2", f"{t}:3"] if t == "E" else []
assert mapping.expand(["E", "D"]) == ["E", "E:2", "E:3", "D"]
assert mapping.expand(["E", "D", "E:2"]) == ["E", "E:3", "D", "E:2"]
mapping.extras = lambda t: []

# ---- order: C, B, then the split group of A, whatever the creation order
engine._table_unit = lambda *a, **k: dict(label=a[4])
engine._split_units = lambda *a, **k: [dict(label="a_part1"), dict(label="a_part2")]
groups.grouped_sources = lambda: {"A"}
groups.active = lambda names: [dict(target="a", mode="split", sources=["A"])]
units, errors = engine._sync_units(None, None, "s", "o", ["C", "B", "A"])
assert [u["label"] for u in units] == ["C", "B", "a_part1", "a_part2"], units
units, errors = engine._sync_units(None, None, "s", "o", ["A", "C", "B"])
assert [u["label"] for u in units] == ["a_part1", "a_part2", "C", "B"], units

# ---- delete history
r1 = history.start_run("EMP", "emp")
r2 = history.start_run("DEPT", "dept")
history.log_row_error(r1, "EMP", "1", "bad")
store.put("active_set", 2)
other = history.start_run("EMP", "emp")
store.put("active_set", 1)

assert history.delete_runs(r1) == 1
assert [r["id"] for r in history.list_runs()] == [r2]
assert history.count_row_errors(r1) == 0
assert history.delete_runs() == 1 and history.list_runs() == []
assert history.get_run(other), "another set's history is kept"
print("ok")
