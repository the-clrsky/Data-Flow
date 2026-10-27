"""Checks for merging several Oracle tables into one Postgres target - the
column union that decides the shared table's shape. Pure, so it runs
without an Oracle or a Postgres.

    python tests/test_union_columns.py
"""
import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["DATAFLOW_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from dataflow.mapping import union_columns, merged_fingerprint  # noqa: E402


def col(target, pg_type="text", nullable=True, skip=False):
    return dict(name=target.upper(), target=target, pg_type=pg_type,
                nullable=nullable, skip=skip)


def test_identical_sources_keep_their_shape():
    cols, conflicts = union_columns({
        "A_2023": [col("id", "bigint", nullable=False), col("amount", "numeric")],
        "A_2024": [col("id", "bigint", nullable=False), col("amount", "numeric")],
    })
    assert conflicts == []
    assert [(c["target"], c["pg_type"], c["nullable"]) for c in cols] == [
        ("id", "bigint", False), ("amount", "numeric", True)]


def test_column_missing_from_one_source_is_forced_nullable():
    # branch_code is NOT NULL in the only source that has it, but the other
    # source has nothing to put there, so the merged column must allow NULL.
    cols, conflicts = union_columns({
        "A": [col("id", "bigint", nullable=False)],
        "B": [col("id", "bigint", nullable=False), col("branch_code", "text", nullable=False)],
    })
    assert conflicts == []
    by_name = {c["target"]: c for c in cols}
    assert by_name["id"]["nullable"] is False
    assert by_name["branch_code"]["nullable"] is True
    assert by_name["branch_code"]["sources"] == ["B"]


def test_nullable_in_any_source_wins():
    cols, _ = union_columns({
        "A": [col("note", "text", nullable=False)],
        "B": [col("note", "text", nullable=True)],
    })
    assert cols[0]["nullable"] is True


def test_type_disagreement_is_reported_not_guessed():
    cols, conflicts = union_columns({
        "A": [col("id", "bigint")],
        "B": [col("id", "text")],
        "C": [col("id", "text")],
    })
    assert len(conflicts) == 1
    assert conflicts[0]["column"] == "id"
    assert conflicts[0]["types"] == {"bigint": ["A"], "text": ["B", "C"]}
    assert len(cols) == 1  # still returned, but the caller must refuse


def test_skipped_columns_are_left_out_entirely():
    cols, conflicts = union_columns({
        "A": [col("id", "bigint"), col("junk", "text", skip=True)],
        "B": [col("id", "bigint"), col("junk", "numeric", skip=True)],
    })
    # junk would have been a type conflict had skip been ignored
    assert conflicts == []
    assert [c["target"] for c in cols] == ["id"]


def test_union_is_in_first_seen_order():
    cols, _ = union_columns({
        "A": [col("b"), col("a")],
        "B": [col("c"), col("a")],
    })
    assert [c["target"] for c in cols] == ["b", "a", "c"]


def test_fingerprint_covers_every_source():
    a = {"A": [col("id")], "B": [col("id")]}
    b = {"A": [col("id")], "B": [col("id", "bigint")]}
    # a change in ANY source must move the group's fingerprint, so drift is
    # flagged on all of them and the shared table gets recreated as a unit
    assert merged_fingerprint("t", a) != merged_fingerprint("t", b)
    assert merged_fingerprint("t", a) == merged_fingerprint("t", dict(reversed(list(a.items()))))
    assert merged_fingerprint("t", a) != merged_fingerprint("other", a)


def test_merged_ddl_shape():
    from dataflow import engine, pg_client, types_map

    cols, conflicts = union_columns({
        "A_2023": [col("id", "bigint", nullable=False), col("amount", "numeric")],
        "A_2024": [col("id", "bigint", nullable=False), col("branch", "text")],
    })
    assert not conflicts
    ddl = pg_client.create_table_ddl("public", "archive", engine._merged_coldefs(cols), [])
    assert '"id" bigint NOT NULL' in ddl          # NOT NULL in every source
    assert '"amount" numeric,' in ddl             # only in A_2023 -> nullable
    assert '"branch" text,' in ddl                # only in A_2024 -> nullable
    assert f'"{types_map.SOURCE_COLUMN}" text NOT NULL' in ddl
    assert "PRIMARY KEY" not in ddl               # sources can share key values


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

# A test must never write to the project's own data-flow.db: store.DB_PATH is
# bound at import time, so DATAFLOW_DB has to be set before the first dataflow
# import. Getting that order wrong once already overwrote real saved state.
from dataflow import store as _store  # noqa: E402
assert "data-flow.db" not in _store.DB_PATH, (
    f"tests are pointed at the real state database ({_store.DB_PATH}); set "
    "DATAFLOW_DB before importing anything from dataflow")
