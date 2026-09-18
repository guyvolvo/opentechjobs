"""Eightfold careers boards (probe.f_eightfold), first used for Teva.

The payload shape is trimmed from careers.teva on 2026-09-18. The trap
worth a test: the server decides the page size. It answered 10 rows to
num=100, so a reader that advances by the number it asked for skips most
of the board while looking like it read all of it (498 of 558 missed
when this was written).

Run directly, no framework:  python tests/test_eightfold.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

import probe  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


TOKEN = "careers.teva:tevapharm.com"


def pos(jid, name, location, locations=None, department="Quality", created=1787135417,
        workplace="onsite", description=""):
    return {"id": jid, "name": name, "location": location, "locations": locations or [location],
            "department": department, "t_create": created, "t_update": created + 100,
            "work_location_option": workplace, "job_description": description,
            "canonicalPositionUrl": f"https://www.careers.teva/careers/job/{jid}"}


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class Sess:
    """Answers with PAGE rows at a time whatever num asks for, which is
    what the real one does."""

    def __init__(self, rows, page=10, count=None, fail_at=None):
        self.rows, self.page, self.count, self.fail_at = rows, page, count, fail_at
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        self.headers = kw.get("headers") or {}
        start = int(url.split("start=")[1].split("&")[0])
        if self.fail_at is not None and start == self.fail_at:
            return Resp(403, None)
        return Resp(200, {"count": self.count if self.count is not None else len(self.rows),
                          "positions": self.rows[start:start + self.page]})


ROWS = [pos(str(i), f"Role {i}", "Kfar Saba, Israel" if i % 5 == 0 else "Prague, Czechia") for i in range(58)]

for bad in ["teva", "teva.com", "", None, "host:", ":tevapharm.com", "nodot:tevapharm.com"]:
    sess = Sess(ROWS)
    check(f"a guessed token {bad!r} makes no request",
          probe.f_eightfold(sess, bad) is None and not sess.calls)

sess = Sess(ROWS)
jobs = probe.f_eightfold(sess, TOKEN)
check("reads the whole board, not just the first page",
      jobs is not None and len(jobs) == 58, repr(jobs and len(jobs)))
check("advances by what came back, not by what it asked for",
      [int(c.split("start=")[1].split("&")[0]) for c in sess.calls][:3] == [0, 10, 20],
      repr(sess.calls[:3]))
check("sends a browser user agent and asks for JSON",
      "Mozilla" in sess.headers.get("User-Agent", "") and sess.headers.get("Accept") == "application/json",
      repr(sess.headers))
j = jobs[0]
check("title", j.title == "Role 0")
check("location", j.location == "Kfar Saba, Israel", j.location)
check("the country resolves", probe._fill_classifications(jobs, "tevapharm.com")[0].country == "IL")
check("department", j.department == "Quality")
check("workplace", j.workplace_type == "onsite", repr(j.workplace_type))
check("the date comes from the posting", (j.posted_at or "").startswith("2026-"), repr(j.posted_at))
check("the link is the board's own", j.url.endswith("/careers/job/0"), j.url)

# Several locations on one posting.
many = probe.f_eightfold(Sess([pos("9", "Split role", "Tel Aviv, Israel",
                                   ["Tel Aviv, Israel", "Kfar Saba, Israel"])]), TOKEN)
check("every named location comes along", many[0].location == "Tel Aviv, Israel; Kfar Saba, Israel",
      many[0].location)

# Descriptions where the board carries them.
with_desc = probe.f_eightfold(Sess([pos("7", "Described", "Tel Aviv, Israel",
                                        description="<p>Do the work.</p>")]), TOKEN)
check("a description is read when the board has one",
      "Do the work." in (with_desc[0].description or "") and with_desc[0].description_chars > 0,
      repr(with_desc[0].description))
check("and an empty one is not invented",
      probe.f_eightfold(Sess([pos("8", "Bare", "Tel Aviv, Israel")]), TOKEN)[0].description is None)

# Failures never close the rest of the board.
check("a page that fails part way fails the read",
      probe.f_eightfold(Sess(ROWS, fail_at=10), TOKEN) is None)
check("a tenant that refuses (403, as amdocs and qualcomm do) is a failed read",
      probe.f_eightfold(Sess(ROWS, fail_at=0), TOKEN) is None)
check("an empty board is a failed read", probe.f_eightfold(Sess([], count=0), TOKEN) is None)

# Wiring.
check("registered as a fetcher", probe.FETCHERS.get("eightfold") is probe.f_eightfold)
check("polls hourly, not in the five-minute sweep", "eightfold" in probe.SLOW_BOARD_ATS)
pin = probe.load_pins().get("eightfold", {}).get("tevapharm.com", {})
check("tevapharm.com is pinned to its careers host and tenant", pin.get("token") == TOKEN, repr(pin))
check("tevapharm.com is in the sweep",
      "tevapharm.com" in (ROOT / "domains.txt").read_text(encoding="utf-8").split())
os.environ.setdefault("DATA_BUCKET", "unused-in-this-test")
import scrape_workday_handler  # noqa: E402
check("the hourly Lambda polls it", "eightfold" in scrape_workday_handler.BIG_TECH_ATS)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
