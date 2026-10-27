"""Checks for the chunk-fetch SQL planner - the paging logic that decides
which rows a sync reads next. Pure, so it runs without an Oracle.

    python tests/test_chunk_plan.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dataflow.oracle_client import _chunk_plan  # noqa: E402

plan = lambda cols, order, last=None: _chunk_plan("OWN", "T", cols, order, 50_000, 10, last)


def test_first_chunk_uses_offset():
    sql, binds, key_idx, trim_to = plan(["A", "B"], ["A"])
    assert "WHERE" not in sql
    assert "OFFSET :chunk_offset ROWS FETCH NEXT :chunk_size ROWS ONLY" in sql
    assert binds == {"chunk_offset": 50_000, "chunk_size": 10}
    assert (key_idx, trim_to) == ([0], 2)


def test_later_chunk_pages_by_key():
    sql, binds, _, _ = plan(["A", "B"], ["A"], last=[7])
    assert 'WHERE ("A" > :k0) ORDER BY "A" FETCH NEXT :chunk_size ROWS ONLY' in sql
    assert "OFFSET" not in sql  # the whole point: no re-reading earlier rows
    assert binds == {"k0": 7, "chunk_size": 10}


def test_composite_key_spells_out_the_comparison():
    sql, binds, _, _ = plan(["A", "B"], ["A", "B"], last=[7, "x"])
    assert 'WHERE ("A" > :k0) OR ("A" = :k0 AND "B" > :k1)' in sql
    assert 'ORDER BY "A", "B"' in sql
    assert binds == {"k0": 7, "k1": "x", "chunk_size": 10}


def test_skipped_key_column_is_still_fetched_then_trimmed():
    # B is the PK but the mapping skips it, so it must be appended to the
    # SELECT list and sliced back off before the rows reach COPY.
    sql, _, key_idx, trim_to = plan(["A"], ["B"], last=[1])
    assert 'SELECT "A", "B" FROM' in sql
    assert (key_idx, trim_to) == ([1], 1)


def test_no_primary_key_stays_on_offset():
    for last in (None, [1]):  # no key is ever handed back, but be sure
        sql, binds, key_idx, _ = plan(["A"], ["ROWID"], last=last)
        assert "ORDER BY ROWID" in sql and "OFFSET :chunk_offset" in sql
        assert key_idx == [] and "chunk_offset" in binds


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
