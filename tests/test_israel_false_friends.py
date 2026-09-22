"""A hospital in Boston is not an office in Israel.

Reported live: 1,075 listings at bilh.myworkdayjobs.com, the Beth Israel
Lahey Health network, counted as Israeli, because the bare keyword
"israel" matched "Beth Israel Deaconess Medical Center" in the location
string. The board called 7,591 jobs Israeli when the real figure was
about 6,496, so the number this project cares most about was overstated
by a sixth.

The trap worth pinning is that there are two answers to one question.
israel_only builds SQL and country=IL reads the resolved column, and
they have diverged before: a live check found the country filter
returning 662 where israel_only returned 2,464. Any fix has to move both
or the two disagree again, so every case here is run through both.

Run directly, no framework:  python tests/test_israel_false_friends.py
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

import countries  # noqa: E402
import job_filters  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


# (location, is it in Israel)
CASES = [
    ("Boston, MA (Beth Israel Deaconess Medical Center)", False),
    ("Beth Israel Lahey Health - Burlington, MA", False),
    ("BETH ISRAEL DEACONESS HOSPITAL - PLYMOUTH", False),
    ("Tel Aviv, Israel", True),
    ("Israel", True),
    ("Herzliya", True),
    ("Haifa, Israel", True),
    ("New York, NY", False),
    ("Boston, MA", False),
    # Both at once. A company with an Israeli office and a Beth Israel
    # site in the same string is still hiring in Israel, so this must not
    # be solved by throwing the whole row away.
    ("Beth Israel Deaconess, Boston; Tel Aviv, Israel", True),
]

conn = sqlite3.connect(":memory:")
conn.execute("CREATE TABLE jobs (id TEXT, location TEXT, country TEXT)")
for i, (loc, _) in enumerate(CASES):
    conn.execute("INSERT INTO jobs VALUES (?,?,'')", (str(i), loc))
sql, args = job_filters.israel_clause(places=False)
matched = {r[0] for r in conn.execute("SELECT id FROM jobs WHERE " + sql, args)}

print("-- the keyword question, asked three ways --")
for i, (loc, want) in enumerate(CASES):
    label = loc[:46]
    check("matches_israel: " + label, countries.matches_israel(loc) is want)
    check("countries_of:   " + label, ("IL" in countries.countries_of(loc)) is want)
    check("israel_clause:  " + label, (str(i) in matched) is want)

print()
print("-- the two answers agree, which is the whole point --")
for i, (loc, _) in enumerate(CASES):
    check("same verdict for " + loc[:44],
          ("IL" in countries.countries_of(loc)) == (str(i) in matched))

print()
print("-- the city is still read out of a location we reject --")
check("Boston is still Boston", countries.cities_of(CASES[0][0]) == ["Boston"],
      repr(countries.cities_of(CASES[0][0])))
check("and Burlington is still Burlington",
      countries.cities_of(CASES[1][0]) == ["Burlington"], repr(countries.cities_of(CASES[1][0])))
check("a mixed string keeps both cities",
      countries.cities_of(CASES[9][0]) == ["Boston", "Tel Aviv"],
      repr(countries.cities_of(CASES[9][0])))

print()
print("-- the exclusion is a list of names, not a rule --")
check("only names actually seen in real locations are listed",
      countries.IL_FALSE_FRIENDS == ("beth israel",), repr(countries.IL_FALSE_FRIENDS))
check("the word Israel on its own is never removed",
      countries.matches_israel("Israel") and countries.matches_israel("israel"))
check("nor is a city that contains no false friend",
      all(countries.matches_israel(c) for c in ("Ramat Gan", "Petah Tikva", "TLV")))

print()
print("-- the places column path is untouched --")
places_sql, places_args = job_filters.israel_clause(places=True)
check("still reads the resolved country column, not the location text",
      "country" in places_sql and places_args == ["%,IL,%"], repr((places_sql, places_args)))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
