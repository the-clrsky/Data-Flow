"""Checks for the per-column "if null, use" fill value and dictionary
lookup. Pure: no Oracle, no Postgres.

    python tests/test_if_null.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["DATAFLOW_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

import io  # noqa: E402
from decimal import Decimal  # noqa: E402

import openpyxl  # noqa: E402

from dataflow import mapping  # noqa: E402

COLS = [dict(name=n, dtype="VARCHAR2", prec=None, scale=None, clen=10, length=10, nullable=True)
        for n in ("A", "B", "C")]

mapping.save("T", None, [
    dict(name="A", if_null="N/A"),
    dict(name="B", if_null=""),       # blank = no fill
    dict(name="C", if_null="0"),      # "0" is a real value, not blank
])
_, resolved = mapping.resolve("T", COLS)
fill = mapping.value_filler(resolved)

assert fill([(None, None, None)]) == ([("N/A", None, "0")], [])
assert fill([("x", "y", "z")]) == ([("x", "y", "z")], [])      # real data wins
assert mapping.value_filler(resolved[1:2]) is None   # nothing to fill
# fill value isn't DDL: it must not flag drift
fp = mapping.fingerprint("t", resolved)
mapping.save("T", None, [dict(name="A", if_null="other")])
assert mapping.fingerprint("t", mapping.resolve("T", COLS)[1]) == fp


# --- dictionaries ---
def table(lines, name="d.csv"):
    return mapping.read_table("\n".join(lines).encode("utf-8"), name)


BOM = chr(0xFEFF)  # Excel's "CSV UTF-8" starts with one
assert mapping.save_dictionary("USER_ID", table([BOM + "Oracle,Postgres", "OLD858,U7895", "858,U1", ""])) == \
    dict(added=2, updated=0, total=2)
assert mapping.dictionaries()["USER_ID"] == {"OLD858": "U7895", "858": "U1"}
for bad, why in [(["a,b", "1,2"], "first row"), (["oracle,postgres", "1,a", "1,b"], "twice"),
                 (["oracle,postgres", "1,"], "both"), (["oracle,postgres"], "No entries")]:
    try:
        mapping.save_dictionary("X", table(bad))
        raise AssertionError(bad)
    except ValueError as e:
        assert why in str(e), e
assert "X" not in mapping.dictionaries()

# add/update keeps what's there; semicolon and tab files (pasted from Excel) both read
assert mapping.save_dictionary("USER_ID", table(["postgres;oracle", "U2;OLD9", "U9999;858"])) == \
    dict(added=1, updated=1, total=3)
TAB = chr(9)
assert mapping.save_dictionary("USER_ID", table([f"oracle{TAB}postgres", f"OLD858{TAB}U7895"])) == \
    dict(added=0, updated=0, total=3)
assert mapping.dictionaries()["USER_ID"] == {"OLD858": "U7895", "858": "U9999", "OLD9": "U2"}
# replace drops entries the new file doesn't have
mapping.save_dictionary("TMP", table(["oracle,postgres", "a,1", "b,2"]))
assert mapping.save_dictionary("TMP", table(["oracle,postgres", "c,3"]), replace=True)["total"] == 1
assert mapping.dictionaries()["TMP"] == {"c": "3"}
mapping.save_dictionary("TMP", table(["oracle,postgres", "d,4", "e,5"]))
assert mapping.delete_entries("TMP", ["c", " d ", "zz", ""]) == dict(removed=2, not_found=["zz"], total=1)
assert mapping.dictionaries()["TMP"] == {"e": "5"}

# .xlsx: first sheet, number cells read without a trailing .0
wb = openpyxl.Workbook()
wb.active.append(["oracle", "postgres"])
wb.active.append([858.0, "U1"])
wb.active.append(["OLD1", 42])
buf = io.BytesIO()
wb.save(buf)
assert mapping.read_table(buf.getvalue(), "Users.XLSX") == [["oracle", "postgres"], ["858", "U1"], ["OLD1", "42"]]

mapping.save("T", None, [dict(name="A", dictionary="USER_ID", if_null="SYSTEM"), dict(name="B")])
fill = mapping.value_filler(mapping.resolve("T", COLS)[1])
assert fill([("OLD858", "x", 1), (None, "y", 2), (Decimal(858), None, 3), ("OLD858 ", "z", 4)]) == ([
    ("U7895", "x", 1), ("SYSTEM", "y", 2), ("U9999", None, 3), ("U7895", "z", 4)], [])
# an unmapped value holds back just its row, reported by its position in the chunk
assert fill([("OLD858", 1, 1), ("NOPE", 1, 1), ("OLD9", 1, 1)]) == (
    [("U7895", 1, 1), ("U2", 1, 1)], [(1, [("A", "USER_ID", "NOPE")])])
# dictionary choice isn't DDL either
assert mapping.fingerprint("t", mapping.resolve("T", COLS)[1]) == fp
mapping.delete_dictionary("USER_ID")
try:
    mapping.value_filler(mapping.resolve("T", COLS)[1])
    raise AssertionError("deleted dictionary must fail")
except RuntimeError as e:
    assert "no longer exists" in str(e)
print("ok")
