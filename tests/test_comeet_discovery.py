"""Comeet discovery, the one ATS that cannot be guessed.

Every other board in discover_companies.py is found by guessing a token
and asking the API whether it exists. Comeet answers nothing without an
opaque per-company token that appears in no URL, which is why
companies.yml's first entries were extracted by hand and why its header
calls a Common Crawl harvest the alternative nobody had built.

So the shape here is different: the index gives a slug and a uid, the
board page gives the token, and the postings give the employer's own
domain. This checks the parts of that chain that do not need the network.

Run directly, no framework:  python tests/test_comeet_discovery.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

os.environ.setdefault("DATA_BUCKET", "test-bucket")
os.environ.setdefault("AWS_DEFAULT_REGION", "il-central-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import discover_companies as dc  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


class FakeResp:
    def __init__(self, text="", data=None):
        self.text = text
        self._data = data

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class FakeSess:
    """Answers the board page and the positions API, and nothing else."""

    def __init__(self, page_text, positions):
        self.page_text = page_text
        self.positions = positions
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        if "careers-api" in url:
            return FakeResp(data=self.positions)
        return FakeResp(text=self.page_text)


TOKEN = "604241801E141E1460430200604604"
PAGE = '<script>var x = {"company_uid":"06.004","token":"%s"};</script>' % TOKEN


def positions(*locations, **kw):
    out = []
    for i, loc in enumerate(locations):
        p = {"name": "Engineer %d" % i, "location": loc}
        if i == 0:
            p.update(kw)
        out.append(p)
    return out


# The index hit carries both halves, and only both halves together
# address a board.
urls = [
    "https://www.comeet.com/jobs/4Manalytics/B6.00F/some-role/A1.002",
    "https://www.comeet.com/jobs/4Manalytics/B6.00F/other-role/A1.003",
    "https://www.comeet.com/jobs/coralogix/06.004/backend/C2.001",
    "https://www.comeet.com/careers/not-a-board",
]
tokens = dc.extract_tokens("comeet", urls)
check("both halves of a board URL become one candidate",
      tokens == {"B6.00F:4Manalytics", "06.004:coralogix"}, repr(tokens))

# A uid is "B6.00F" and the positions API is not amused by "b6.00f", so
# extraction must not fold case the way the guessable tokens do.
check("uid case survives extraction",
      any(t.startswith("B6.00F:") for t in tokens), repr(tokens))

check("comeet is offered as a source at all", "comeet" in dc.CC_URL_PATTERNS,
      repr(sorted(dc.CC_URL_PATTERNS)))

# Comeet answers with a location object on most postings and a bare
# string on some. Both have to read the same way, or half the Israeli
# roles on a board go uncounted.
check("a location object reads",
      "Tel Aviv" in dc._comeet_where({"location": {"name": "Maskit I", "city": "Tel Aviv"}}))
check("a bare location string reads",
      dc._comeet_where({"location": "Herzliya, Israel"}) == "Herzliya, Israel")
check("a missing location is empty, not a crash", dc._comeet_where({}) == "")

sess = FakeSess(PAGE, positions(
    {"name": "Maskit I", "city": "Herzliya"},
    "Tel Aviv, Israel",
    "New York",
    careers_page_url="https://www.coralogix.com/careers/",
))
got = dc._verify_comeet(sess, "06.004:coralogix")
check("the opaque token is recovered from the board page",
      got and got["token"] == "06.004:" + TOKEN, repr(got and got["token"]))
check("every posting counts, Israeli ones separately",
      got and got["job_count"] == 3 and got["israel_job_count"] == 2,
      repr(got and (got["job_count"], got["israel_job_count"])))
# Unusual for this file: nothing about the domain is inferred from the
# slug, because the postings state it outright.
check("the employer's own domain is read, not guessed",
      got and got["guessed_domain"] == "coralogix.com" and got["domain_verified"] is True,
      repr(got and (got["guessed_domain"], got["domain_verified"])))
check("the token reaches the API, not just the page",
      any("careers-api" in u and TOKEN in u for u in sess.calls), repr(sess.calls))

# A board page that embeds no token is not a board this can use, and
# saying so is better than pinning a company the fast-poll will never
# be able to read.
check("no token in the page means no candidate",
      dc._verify_comeet(FakeSess("<html>nothing here</html>", []), "06.004:coralogix") is None)
check("a board with no open roles is not a find",
      dc._verify_comeet(FakeSess(PAGE, []), "06.004:coralogix") is None)
check("a malformed candidate is refused",
      dc._verify_comeet(FakeSess(PAGE, positions("Tel Aviv")), "nocolon") is None)

# The rare board that names no careers page still falls back to the
# guesser every other ATS here relies on.
dc._guess_domain = lambda token, sess: ("coralogix.io", False)
got = dc._verify_comeet(FakeSess(PAGE, positions("Tel Aviv, Israel")), "06.004:coralogix")
check("a board naming no page falls back to the guesser, and says so",
      got and got["guessed_domain"] == "coralogix.io" and got["domain_verified"] is False,
      repr(got and (got["guessed_domain"], got["domain_verified"])))

print()
if failures:
    print("%d failed: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all good")
