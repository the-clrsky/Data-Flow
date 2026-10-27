"""Checks for loading one Oracle column into several Postgres columns.
Pure: no Oracle, no Postgres.

    python tests/test_column_copies.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["DATAFLOW_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from dataflow import mapping, store  # noqa: E402

T = "EMP"
ORA_COLS = [dict(name=n, dtype="VARCHAR2", prec=None, scale=None, length=10, clen=10, nullable=True)
            for n in ("ID", "NAME")]


def test_listing_a_column_twice_adds_a_destination():
    store.put("mappings", {})
    plain_fp = mapping.fingerprint(*mapping.resolve(T, ORA_COLS))
    mapping.save(T, "emp", [
        dict(name="ID", target="id"),
        dict(name="NAME", target="name"),
        dict(name="NAME", target="name_upper", pg_type="text", if_null="?"),
    ])
    _, resolved = mapping.resolve(T, ORA_COLS)
    assert [(c["name"], c["target"], c["copy"]) for c in resolved] == [
        ("ID", "id", False), ("NAME", "name", False), ("NAME", "name_upper", True)]
    assert resolved[2]["pg_type"] == "text" and resolved[2]["if_null"] == "?"
    assert resolved[1]["if_null"] is None  # the copy has its own settings
    # copies shape the table, so adding one is drift; a table without any is not
    assert mapping.fingerprint("emp", resolved) != plain_fp
    mapping.save(T, "emp", [dict(name="ID", target="id"), dict(name="NAME", target="name")])
    assert mapping.fingerprint("emp", mapping.resolve(T, ORA_COLS)[1]) == plain_fp


def test_fan_out_selects_each_oracle_column_once():
    names, widen = mapping.fan_out(["ID", "NAME", "NAME"])
    assert names == ["ID", "NAME"]
    fetch = widen(lambda off, n, key: ([(1, "a"), (2, "b")], "k"))
    assert fetch(0, 2, None) == ([(1, "a", "a"), (2, "b", "b")], "k")
    names, widen = mapping.fan_out(["ID", "NAME"])
    f = lambda: None  # noqa: E731
    assert names == ["ID", "NAME"] and widen(f) is f


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
