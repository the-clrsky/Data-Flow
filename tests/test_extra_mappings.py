"""Checks for extra mappings - a second (third, ...) Postgres table fed from
one Oracle table. Pure: no Oracle, no Postgres.

    python tests/test_extra_mappings.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["DATAFLOW_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from dataflow import mapping, store  # noqa: E402

assert "data-flow.db" not in store.DB_PATH

T = "OP_CONSULTATIONSPECIALIZATION"
ORA_COLS = [dict(name=n, dtype="VARCHAR2", prec=None, scale=None, length=10, clen=10, nullable=True)
            for n in ("SPECIALIZATION_NO", "SPECIALIZATION_NAME", "COMPANY_NO")]


def test_extra_mapping_round_trip():
    store.put("mappings", {})
    mapping.save(T, "set_specilization_category", [], False)
    key = mapping.next_extra(T)
    assert key == f"{T}:2"
    assert mapping.extras(T) == []  # nothing stored until saved
    assert mapping.oracle_table(key) == T and mapping.is_extra(key)

    # an extra mapping starts with every column skipped
    pgt, resolved = mapping.resolve(key, ORA_COLS)
    assert pgt == "op_consultationspecialization_2"
    assert all(c["skip"] for c in resolved)
    # while the main mapping keeps everything by default
    assert not any(c["skip"] for c in mapping.resolve(T, ORA_COLS)[1])

    mapping.save(key, "spec_company", [dict(name="COMPANY_NO", skip=False)], False)
    _, resolved = mapping.resolve(key, ORA_COLS)
    assert [c["name"] for c in resolved if not c["skip"]] == ["COMPANY_NO"]

    assert mapping.next_extra(T) == f"{T}:3"
    assert mapping.expand([T, "OTHER", T]) == [T, key, "OTHER"]
    assert mapping.pg_table_owner("spec_company", T) == key
    assert mapping.pg_table_owner("spec_company", key) is None

    mapping.delete(key)
    assert mapping.extras(T) == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
