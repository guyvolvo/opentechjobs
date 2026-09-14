"""The company chart gets each company's resolved logo.

The chart used to guess favicon paths on the domain. Elbit's icon lives
under www.elbitsystems.com/themes/elbit/favicon/, so the biggest employer
on the board showed a monogram in the chart while its own listing rows
showed the real logo. These check that the stats lists carry logo_url, and
that an older database without it degrades to no logo rather than an error.

Run directly, no framework:  python tests/test_bar_list_logos.py
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

import aggregates  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


ELBIT = "https://www.elbitsystems.com/themes/elbit/favicon/apple-touch-icon.png"

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("CREATE TABLE companies (domain TEXT PRIMARY KEY, logo_url TEXT)")
conn.executemany("INSERT INTO companies VALUES (?, ?)", [
    ("elbitsystems.com", ELBIT),
    ("nologo.com", None),
])

rows = aggregates._with_logos(conn, [
    {"domain": "elbitsystems.com", "n": 589},
    {"domain": "nologo.com", "n": 40},
    {"domain": "unknown.com", "n": 12},
    {"domain": None, "n": 3},
])
check("a company with a resolved logo gets it", rows[0]["logo_url"] == ELBIT, repr(rows[0]))
check("a company with none gets null", rows[1]["logo_url"] is None, repr(rows[1]))
check("a domain missing from companies gets null", rows[2]["logo_url"] is None, repr(rows[2]))
check("a null domain is left alone", "logo_url" not in rows[3], repr(rows[3]))
check("the counts are untouched", [r["n"] for r in rows] == [589, 40, 12, 3])

check("an empty list stays empty", aggregates._with_logos(conn, []) == [])

old = sqlite3.connect(":memory:")
old.execute("CREATE TABLE companies (domain TEXT PRIMARY KEY)")
legacy = aggregates._with_logos(old, [{"domain": "elbitsystems.com", "n": 589}])
check("a database without logo_url degrades to no logo, not an error",
      legacy == [{"domain": "elbitsystems.com", "n": 589}], repr(legacy))
bare = sqlite3.connect(":memory:")
check("and one without a companies table at all",
      aggregates._with_logos(bare, [{"domain": "x.com", "n": 1}]) == [{"domain": "x.com", "n": 1}])

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
