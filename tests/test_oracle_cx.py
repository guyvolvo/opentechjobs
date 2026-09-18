"""Oracle Recruiting Cloud boards (probe.f_oracle_cx), first used for Akamai.

The payloads below are trimmed from the real pod on 2026-09-18. The
shapes that matter: the rows live under items[0].requisitionList with the
count beside them, locations are plain names, and the full posting only
comes from the per-job call.

Run directly, no framework:  python tests/test_oracle_cx.py
"""

import json
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


TOKEN = "fa-extu-saasfaprod1.fa.ocs.oraclecloud.com:CX_1"


def row(jid, title, place, country, date="2026-09-15", workplace="ORA_HYBRID", secondary=()):
    return {"Id": jid, "Title": title, "PrimaryLocation": place, "PrimaryLocationCountry": country,
            "PostedDate": date, "WorkplaceTypeCode": workplace, "WorkplaceType": "Hybrid",
            "JobFamily": "Engineering", "ShortDescriptionStr": "A short blurb.",
            "secondaryLocations": [{"Name": n} for n in secondary]}


def listing(rows, total):
    return {"items": [{"TotalJobsCount": total, "requisitionList": rows}]}


class Resp:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code, self._payload, self.text = status, payload, text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class Sess:
    """Answers the list endpoint by offset and the detail endpoint by id."""

    def __init__(self, pages, details=None, fail_offsets=()):
        self.pages, self.details, self.fail_offsets = pages, details or {}, set(fail_offsets)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        self.headers = kw.get("headers") or {}
        if "JobRequisitionDetails" in url:
            jid = url.split("Id=%22")[1].split("%22")[0]
            body = self.details.get(jid)
            return Resp(200, {"items": [{"ExternalDescriptionStr": body}]}) if body else Resp(404)
        offset = int(url.split("offset=")[1])
        if offset in self.fail_offsets:
            return Resp(500)
        return Resp(200, self.pages.get(offset, listing([], 0)))


IL = row("1", "Cloud Networking Engineer", "Israel", "IL", workplace="ORA_REMOTE")
US = row("2", "Senior Software Engineer", "United States", "US", secondary=("Canada",))
PAGE = listing([IL, US], 2)

# The guard: the guess loop hands every fetcher a slug off a domain.
for bad in ["akamai", "akamai.com", "", None, "host:", ":CX_1", "nodot:CX_1"]:
    sess = Sess({0: PAGE})
    check(f"a guessed token {bad!r} makes no request",
          probe.f_oracle_cx(sess, bad) is None and not sess.calls)

sess = Sess({0: PAGE})
jobs = probe.f_oracle_cx(sess, TOKEN)
check("reads the board", jobs is not None and [j.external_id for j in jobs] == ["1", "2"],
      repr(jobs and [j.external_id for j in jobs]))
by_id = {j.external_id: j for j in jobs}
check("asks for the secondary locations, or the rows come back empty",
      "expand=requisitionList.secondaryLocations" in sess.calls[0], sess.calls[0])
check("sends a browser user agent and asks for JSON",
      "Mozilla" in sess.headers.get("User-Agent", "") and sess.headers.get("Accept") == "application/json",
      repr(sess.headers))
check("title", by_id["1"].title == "Cloud Networking Engineer")
check("location is the plain name", by_id["1"].location == "Israel", by_id["1"].location)
check("other locations come along", by_id["2"].location == "United States; Canada", by_id["2"].location)
check("the country resolves", probe._fill_classifications(jobs, "akamai.com")[0].country == "IL")
check("remote reads off the workplace code", by_id["1"].workplace_type == "remote", repr(by_id["1"].workplace_type))
check("hybrid too", by_id["2"].workplace_type == "hybrid", repr(by_id["2"].workplace_type))
check("the date is normalised", (by_id["1"].posted_at or "").startswith("2026-09-1"), repr(by_id["1"].posted_at))
check("the link is the site's own job page",
      by_id["1"].url.endswith("/hcmUI/CandidateExperience/en/sites/CX_1/job/1"), by_id["1"].url)
check("department", by_id["1"].department == "Engineering")
check("the blurb stands in for a description with no budget",
      by_id["1"].description == "A short blurb." and by_id["1"].description_chars == len("A short blurb."))
check("no budget means no per-job calls", not any("Details" in c for c in sess.calls))

# Paging: 200 a page, and the count says when to stop.
big = {0: listing([row(str(i), f"Role {i}", "Israel", "IL") for i in range(probe.ORACLE_PAGE)], 250),
       probe.ORACLE_PAGE: listing([row(str(1000 + i), f"Role {i}", "Israel", "IL") for i in range(50)], 250)}
sess = Sess(big)
jobs = probe.f_oracle_cx(sess, TOKEN)
check("pages until it has them all", jobs is not None and len(jobs) == 250, repr(jobs and len(jobs)))
check("and asks for each page once", len([c for c in sess.calls if "offset=" in c]) == 2, repr(sess.calls))

# A page that fails part way is a failed read, not "the rest closed".
check("a failed later page fails the whole read",
      probe.f_oracle_cx(Sess(big, fail_offsets=[probe.ORACLE_PAGE]), TOKEN) is None)
check("a failed first page fails the read", probe.f_oracle_cx(Sess({}, fail_offsets=[0]), TOKEN) is None)
check("an empty board is a failed read", probe.f_oracle_cx(Sess({0: listing([], 0)}), TOKEN) is None)

# Descriptions: only for rows not already described, within the budget.
sess = Sess({0: PAGE}, details={"1": "<p>The whole posting.</p><ul><li>Ship</li></ul>",
                                "2": "<p>Another.</p>"})
jobs = probe.f_oracle_cx(sess, TOKEN, known_ids={"2"}, description_budget=5)
got = {j.external_id: j.description for j in jobs}
check("fetches the full posting for a row it does not have",
      "The whole posting." in (got["1"] or ""), repr(got["1"]))
check("and leaves an already-described row on its blurb", got["2"] == "A short blurb.", repr(got["2"]))
check("one detail call, for the one row", len([c for c in sess.calls if "Details" in c]) == 1, repr(sess.calls))

# Wiring.
check("registered as a fetcher", probe.FETCHERS.get("oracle") is probe.f_oracle_cx)
check("polls hourly, not in the five-minute sweep", "oracle" in probe.SLOW_BOARD_ATS)
pin = probe.load_pins().get("oracle", {}).get("akamai.com", {})
check("akamai.com is pinned to the pod, not its own careers host",
      pin.get("token") == TOKEN, repr(pin))
check("akamai.com is in the sweep", "akamai.com" in (ROOT / "domains.txt").read_text(encoding="utf-8").split())
os.environ.setdefault("DATA_BUCKET", "unused-in-this-test")
import scrape_workday_handler  # noqa: E402
check("the hourly Lambda polls it", "oracle" in scrape_workday_handler.BIG_TECH_ATS)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
