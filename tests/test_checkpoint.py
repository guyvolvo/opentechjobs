"""Check Point from its own careers site (probe.f_checkpoint).

The cards below are trimmed from the real search page, keeping the parts
that tripped the parser while it was written: the save button carries
the same job link before the title does, a commented-out seniority label
sits between them, and a card can list "Full-time" where the department
usually goes.

Run directly, no framework:  python tests/test_checkpoint.py
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


class Resp:
    def __init__(self, status=200, text=""):
        self.status_code, self.content = status, text.encode("utf-8")


class Sess:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        for key, resp in self.pages.items():
            if key in url:
                return resp
        return Resp(404)


LINK = "https://careers.checkpoint.com/index.php?m=cpcareers&a=show&joborderid="


def card(jid, title, place, info):
    return f'''<div class="position">
      <button class="iconA save-job-btn" data-id="{jid}" data-title="{title}"
              data-link="{LINK}{jid}">
      </button>
      <!-- <span class="seniority seniorityMi">Mid-Senior Level and Team Leaders</span>
      -->
      </div>
      <a href="{LINK}{jid}">
          {title}            </a>
      <div class="posInfo">
          <p><img src="x.svg" class="place"> {place} </p>
          <p><img src="y.svg" class="briefcase">{info}</p>
      </div>'''


def search_page(cards, total):
    return (f'<html><div id="solrSearch">{"".join(cards)}'
            f'<span class="currentPage">1-10 of {total}</span></div></html>')


CARDS = [
    card("101", "AI Security Engineer, AI Security Unit", "United States: Washington DC",
         ' R&amp;D <span class="pipeline">|</span> Full-time <span class="pipeline">|</span> Job ID: REF112U'),
    card("102", "Account Manager", "Israel: Tel Aviv/ Hybrid (Israel)",
         ' Sales <span class="pipeline">|</span> Full-time <span class="pipeline">|</span> Job ID: 1'),
    card("103", "Office Admin", "Great Britain: London",
         ' Full-time <span class="pipeline">|</span> Job ID: 25995'),
    card("104", "Engineer", "Switzerland: Zürich",
         ' QA <span class="pipeline">|</span> Contract'),
]

DETAIL = '''<div id="pageBody"><div id="jobOrderInfo">
<div class="info"><h3>Why Join Us?</h3><p>At Check Point, what you do matters.</p></div>
<div class="info"><h3>Key Responsibilities</h3><ul><li>Break AI systems</li></ul></div>
<div class="info"><h3>Qualifications</h3><p>Five years of red teaming.</p></div>
</div><div id="shareSection"></div>'''


def read(pages, **kw):
    return probe.f_checkpoint(Sess(pages), "careers.checkpoint.com", **kw)


# The guard. The guess loop hands every fetcher a slug off a domain.
for bad in ["checkpoint", "checkpoint.com", "www.checkpoint.com", "", None]:
    sess = Sess({"a=search": Resp(200, search_page(CARDS, 4))})
    check(f"a guessed token {bad!r} makes no request",
          probe.f_checkpoint(sess, bad) is None and not sess.calls)

# One full read.
sess = Sess({"a=search": Resp(200, search_page(CARDS, 4))})
jobs = probe.f_checkpoint(sess, "careers.checkpoint.com")
check("reads every card", jobs is not None and [j.external_id for j in jobs] == ["101", "102", "103", "104"],
      repr(jobs and [j.external_id for j in jobs]))
check("asks for every row in one page", "rows=" in sess.calls[0][0], sess.calls[0][0])
check("sends a browser user agent, which the site requires",
      "Mozilla" in sess.calls[0][1]["headers"]["User-Agent"])
by_id = {j.external_id: j for j in jobs}
check("the title is the link text, not the save button's",
      by_id["101"].title == "AI Security Engineer, AI Security Unit", repr(by_id["101"].title))
check("location reads city first", by_id["101"].location == "Washington DC, United States",
      by_id["101"].location)
check("the job link is the site's own", by_id["101"].url == LINK + "101", by_id["101"].url)
check("department is unescaped", by_id["101"].department == "R&D", repr(by_id["101"].department))
check("hybrid comes out of the location", (by_id["102"].location, by_id["102"].workplace_type)
      == ("Tel Aviv, Israel", "hybrid"), repr((by_id["102"].location, by_id["102"].workplace_type)))
check("Great Britain reads as the United Kingdom", by_id["103"].location == "London, United Kingdom",
      by_id["103"].location)
check("Full-time in the department slot is not a department", by_id["103"].department is None,
      repr(by_id["103"].department))
check("accents survive", by_id["104"].location == "Zürich, Switzerland", ascii(by_id["104"].location))
check("a first read dates nothing", all(j.posted_at is None for j in jobs))
check("no budget fetches no descriptions", len(sess.calls) == 1 and all(j.description is None for j in jobs))

filled = probe._fill_classifications(jobs, "checkpoint.com")
check("countries resolve", [j.country for j in filled] == ["US", "IL", "GB", "CH"],
      repr([j.country for j in filled]))

# A short read must fail, or the closed-job pass would close the rest.
check("fewer cards than the total is a failed read", read({"a=search": Resp(200, search_page(CARDS, 5))}) is None)
check("no total is a failed read", read({"a=search": Resp(200, "".join(CARDS))}) is None)
check("a 403 is a failed read", read({"a=search": Resp(403, "Request blocked.")}) is None)
check("an empty board is a failed read", read({"a=search": Resp(200, search_page([], 0))}) is None)

# Dates on later reads.
jobs = read({"a=search": Resp(200, search_page(CARDS, 4))}, open_ids={"101", "102", "103"})
dated = {j.external_id: j.posted_at for j in jobs}
check("a role that was not open before is dated now", dated["104"] is not None, repr(dated))
check("roles already open stay undated", [dated[k] for k in ("101", "102", "103")] == [None] * 3,
      repr(dated))

# Descriptions: only for roles not already described, within the budget.
pages = {"a=search": Resp(200, search_page(CARDS, 4)), "a=show": Resp(200, DETAIL)}
sess = Sess(pages)
jobs = probe.f_checkpoint(sess, "careers.checkpoint.com", known_ids={"101"}, description_budget=2)
shows = [u for u, _ in sess.calls if "a=show" in u]
check("fetches only undescribed roles, up to the budget",
      sorted(u.rsplit("=", 1)[1] for u in shows) == ["102", "103"], repr(shows))
body = {j.external_id: j.description for j in jobs}["102"] or ""
check("the description keeps the role's own sections", "Break AI systems" in body and "red teaming" in body,
      repr(body))
check("the company boilerplate is dropped", "what you do matters" not in body, repr(body))
check("description_chars matches", {j.external_id: j.description_chars for j in jobs}["102"] == len(body))

# Wiring.
check("registered as a fetcher", probe.FETCHERS.get("checkpoint") is probe.f_checkpoint)
check("polls hourly, not in the five-minute sweep", "checkpoint" in probe.SLOW_BOARD_ATS)
check("the stray Workable guess is excluded", ("workable", "checkpoint") in probe.KNOWN_FALSE_POSITIVES)
pin = probe.load_pins().get("checkpoint", {}).get("checkpoint.com", {})
check("checkpoint.com is pinned to its careers host", pin.get("token") == "careers.checkpoint.com", repr(pin))
# The handler reads its bucket at import; the value is never used here.
os.environ.setdefault("DATA_BUCKET", "unused-in-this-test")
import scrape_workday_handler  # noqa: E402
check("the hourly Lambda polls it", "checkpoint" in scrape_workday_handler.BIG_TECH_ATS)

# The handler's own call: its budget and every open id, not the generic ones.
seen = {}
real = probe.FETCHERS["checkpoint"]
probe.FETCHERS["checkpoint"] = lambda sess, token, **kw: seen.update(kw) or []
try:
    scrape_workday_handler._poll_big_tech(None, "checkpoint", "checkpoint.com",
                                          {"token": "careers.checkpoint.com"}, {"1"}, {"1", "2"})
finally:
    probe.FETCHERS["checkpoint"] = real
check("the handler passes Check Point's own description budget",
      seen.get("description_budget") == scrape_workday_handler.CHECKPOINT_DESCRIPTIONS_PER_RUN, repr(seen))
check("the handler passes every open id", seen.get("open_ids") == {"1", "2"}, repr(seen))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
