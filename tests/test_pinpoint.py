"""Pinpoint boards (probe.f_pinpoint) and the domain read that feeds them.

Shapes trimmed from impulsespace.pinpointhq.com and kpmg.pinpointhq.com
on 2026-09-22. Tenants were recovered from the Wayback index, not Common
Crawl, which indexes this host thinly: 1,404 subdomains, 676 of them
answering with open roles and 19,519 postings between them.

Four traps worth a test. Every tenant writes its own robots.txt and a
large minority close the door: 203 of the 676 serve a blanket
"Disallow: /", covering 6,047 postings, and the 473 that do not still
name 1,128 individual postings they want left alone. An unknown tenant
404s with HTML, so an empty `data` list is a real customer between hires
and has to come back as [], not None, or the loader closes their board.
A posting can be marked compensation_visible and still say only
"Competitive", which is an absence of a salary rather than a salary. And
the prose lives in four fields, not one, so a reader that takes only
`description` loses the requirements a skills match runs on.

Run directly, no framework:  python tests/test_pinpoint.py
"""

import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

import discover_companies  # noqa: E402
import probe  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


def posting(pid="290785", title="Senior Development Test Engineer", city="Redondo Beach",
            province="California", name="Redondo Beach ", workplace="onsite",
            department="Assembly, Integration & Test", visible=True, lo=110000.0, hi=180000.0,
            comp="$110,000 - $180,000 / year", **extra):
    row = {
        "id": pid, "title": title,
        "location": {"id": "6679", "city": city, "name": name, "postal_code": "90278",
                     "province": province, "street_address": ""},
        "job": {"id": "306744", "requisition_id": "REQ-0350",
                "department": {"id": "62809", "name": department}, "division": None},
        "url": "https://impulsespace.pinpointhq.com/en/postings/" + pid,
        "path": "/en/postings/" + pid,
        "workplace_type": workplace, "workplace_type_text": workplace.title(),
        "employment_type": "full_time", "employment_type_text": "Full Time",
        "compensation": comp, "compensation_visible": visible,
        "compensation_minimum": lo, "compensation_maximum": hi,
        "compensation_currency": "USD", "compensation_frequency": "year",
        "deadline_at": None,
        "description": "<div><!--block-->As a Senior Development Test Engineer.</div>",
        "key_responsibilities": "<ul><li><!--block-->Design and build fluid systems</li></ul>",
        "key_responsibilities_header": "Responsibilities",
        "skills_knowledge_expertise": "<ul><li><!--block-->5+ years of Python</li></ul>",
        "skills_knowledge_expertise_header": "Minimum Qualifications",
        "benefits": "<div><!--block-->Cryogenic propellants experience</div>",
        "benefits_header": "Preferred Skills and Experience",
    }
    row.update(extra)
    return row


class Resp:
    # Content-Type spelled the way get_json reads it: _request_json does a
    # plain dict lookup, not a requests CaseInsensitiveDict one.
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text
        self.headers = {"Content-Type": "application/json" if payload is not None else "text/html"}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


ROBOTS_OPEN = "User-Agent: *\nDisallow: /mydata\nDisallow: /admin\nDisallow: /companies\n"
ROBOTS_CLOSED = "User-Agent: *\nDisallow: /\n"


class Sess:
    """Answers robots.txt and postings.json separately, because
    f_pinpoint asks for the first before it asks for the second."""

    def __init__(self, payload=None, status=200, text="", robots=ROBOTS_OPEN, robots_status=200):
        self.payload, self.status, self.text = payload, status, text
        self.robots, self.robots_status = robots, robots_status
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        if url.endswith("/robots.txt"):
            if self.robots is None:
                raise requests.RequestException("no answer")
            return Resp(self.robots_status, None, self.robots)
        return Resp(self.status, self.payload, self.text)


print("-- the board --")
sess = Sess({"data": [posting()]})
jobs = probe.f_pinpoint(sess, "impulsespace")
check("asks robots.txt before it asks for the board",
      sess.urls == ["https://impulsespace.pinpointhq.com/robots.txt",
                    "https://impulsespace.pinpointhq.com/postings.json"], repr(sess.urls))
check("reads the board", jobs is not None and len(jobs) == 1, repr(jobs))
j = jobs[0]
check("keyed on the posting id, not the job id", j.external_id == "290785", j.external_id)
check("carries the posting's own url", j.url.endswith("/en/postings/290785"), j.url)
check("department comes out of the nested job object",
      j.department == "Assembly, Integration & Test", repr(j.department))
check("workplace_type maps onto ours", j.workplace_type == "onsite", repr(j.workplace_type))
check("no posted date is claimed, because the payload has none", j.posted_at is None, repr(j.posted_at))

print()
print("-- an empty board is not a missing one --")
empty = probe.f_pinpoint(Sess({"data": []}), "mourant")
check("a real tenant with nothing open answers []", empty == [], repr(empty))
check("and is not None, which would let the loader close the board", empty is not None)
check("an unknown tenant's HTML 404 is None",
      probe.f_pinpoint(Sess(None, status=404, text="<html>"), "nope") is None)
check("so is a 200 that is not the shape we expect",
      probe.f_pinpoint(Sess({"jobs": []}), "nope") is None)

print()
print("-- robots.txt, which every tenant configures for itself --")
# Measured over all 676 live tenants on 2026-09-22: 203 of them serve a
# blanket disallow, covering 6,047 of the 19,514 postings, and the 473
# that do not still name 1,128 individual postings.
closed = Sess({"data": [posting()]}, robots=ROBOTS_CLOSED)
out = probe.f_pinpoint(closed, "priorygroup")
check("a board that says Disallow: / is read as empty, so its listings leave",
      out == [], repr(out))
check("and the board itself is never fetched",
      closed.urls == ["https://priorygroup.pinpointhq.com/robots.txt"], repr(closed.urls))
check("a 404 for robots.txt is permission, not a refusal",
      len(probe.f_pinpoint(Sess({"data": [posting()]}, robots="", robots_status=404), "t") or []) == 1)
unreachable = probe.f_pinpoint(Sess({"data": [posting()]}, robots=None), "t")
check("a host that does not answer at all is no answer, not an empty board",
      unreachable is None, repr(unreachable))
check("and neither is a 500", probe.f_pinpoint(
    Sess({"data": [posting()]}, robots="", robots_status=500), "t") is None)
opted_out = "/en/postings/290785"
one_job_out = Sess({"data": [posting(pid="290785"), posting(pid="290786")]},
                   robots=ROBOTS_OPEN + "Disallow: " + opted_out + "\n")
left = probe.f_pinpoint(one_job_out, "trilongroup")
check("a single posting the employer named is dropped, the rest stay",
      [j.external_id for j in left] == ["290786"], repr([j.external_id for j in left]))

print()
print("-- location, which has no country in it --")
check("city and province, which is what countries.py can read",
      j.location == "Redondo Beach, California", repr(j.location))
only_name = probe.f_pinpoint(Sess({"data": [posting(city="", province="", name="Berks - Slough")]}), "t")[0]
check("the employer's own label is the fallback, not the first choice",
      only_name.location == "Berks - Slough", repr(only_name.location))
no_province = probe.f_pinpoint(Sess({"data": [posting(city="Jersey", province="")]}), "t")[0]
check("a city on its own is still a location", no_province.location == "Jersey", repr(no_province.location))

print()
print("-- the prose, which is four fields --")
body = j.description or ""
check("the description is there", "Senior Development Test Engineer" in body)
check("and so are the responsibilities", "Design and build fluid systems" in body)
check("and the qualifications a skills match needs", "5+ years of Python" in body)
check("under the employer's own headings", "## Minimum Qualifications" in body, body[:200])
check("description_chars counts what is stored", j.description_chars == len(body))
bare = probe.f_pinpoint(Sess({"data": [posting(key_responsibilities="", skills_knowledge_expertise="",
                                               benefits="", description="")]}), "t")[0]
check("a posting with no prose at all stores None, not an empty string",
      bare.description is None, repr(bare.description))

print()
print("-- salary, only when it is one --")
check("a real range is disclosed, not estimated",
      (j.salary_text, j.salary_source, j.salary_is_estimate) == ("$110,000 - $180,000 / year", "disclosed", False),
      repr((j.salary_text, j.salary_source)))
competitive = probe.f_pinpoint(Sess({"data": [posting(lo=None, hi=None, comp="Competitive")]}), "t")[0]
check('"Competitive" is an absence of a salary', competitive.salary_text is None, repr(competitive.salary_text))
check("and claims no source either", competitive.salary_source is None)
hidden = probe.f_pinpoint(Sess({"data": [posting(visible=False)]}), "t")[0]
check("a range the employer chose to hide stays hidden", hidden.salary_text is None)
one_sided = probe.f_pinpoint(Sess({"data": [posting(lo=25250.0, hi=None, comp="£25,250 / year")]}), "t")[0]
check("a single figure is still a salary", one_sided.salary_text == "£25,250 / year", repr(one_sided.salary_text))

print()
print("-- registered where the pipeline looks --")
check("pinpoint is in FETCHERS", probe.FETCHERS.get("pinpoint") is probe.f_pinpoint)
check("and not in SLOW_BOARD_ATS: the whole board is one request",
      "pinpoint" not in probe.SLOW_BOARD_ATS)
check("the crawl harvest knows the host",
      discover_companies.CC_URL_PATTERNS.get("pinpoint") == "pinpointhq.com")
check("and that the tenant is the subdomain", "pinpoint" in discover_companies.CC_DOMAIN_MATCH)
check("Pinpoint's own CDN host is not mistaken for a tenant",
      "marketing-assets" in discover_companies._NON_TENANT_SUBDOMAINS)


class PageSess:
    def __init__(self, html, status=200):
        self.html, self.status = html, status

    def get(self, url, **kw):
        return Resp(self.status, None, self.html)


def links(*hosts):
    return "".join('<a href="https://' + h + '/careers">x</a>' for h in hosts)


print()
print("-- the domain, read off the board page rather than guessed --")
check("the employer's own site wins",
      discover_companies._domain_from_page(
          PageSess(links("d2n5ied94mazop.cloudfront.net", "www.impulsespace.com", "www.x.com")),
          "https://impulsespace.pinpointhq.com/", "impulsespace") == "impulsespace.com")
check("a careers subdomain is the same company one label down",
      discover_companies._domain_from_page(PageSess(links("careers.careys.co")), "u", "careys") == "careys.co")
check("a parent named alongside its own subdomain wins",
      discover_companies._domain_from_page(
          PageSess(links("trustcareers.si.edu", "affiliations.si.edu", "si.edu")), "u", "smithsonian") == "si.edu")
check("a page that links nothing but a CDN yields nothing",
      discover_companies._domain_from_page(
          PageSess(links("res.cloudinary.com", "fonts.adobe.com")), "u", "sandbox") is None)
check("and so does a page that does not answer",
      discover_companies._domain_from_page(PageSess("", status=500), "u", "whoever") is None)
check("a domain that shares no name with the slug is still taken, because the employer linked it",
      discover_companies._domain_from_page(PageSess(links("meliorefoundation.org")), "u", "meliore")
      == "meliorefoundation.org")

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
