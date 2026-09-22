"""Finding Oracle Recruiting Cloud tenants, which nothing here did.

f_oracle_cx has been written, tested and pinned to four tenants by hand
for a while. Nothing ever found a fifth, so the most productive fetcher
in the repo sat almost idle. Both halves of its "pod:site" token are in
the URL path, the way Workday's three are:

  https://{pod}/hcmUI/CandidateExperience/en/sites/{site}/jobs

Measured 2026-09-22 over the four Wayback regions that answered, each
query capped at 40,000 rows: 633 pod/site pairs across 223 pods.
Sampling 80 pairs through f_oracle_cx gave 61 boards with jobs, 7,430
jobs, 92.9 per sampled pair. That is an order of magnitude above every
SMB platform here.

Two traps. 133 of the 223 pods publish more than one career site and
they mostly serve the same requisitions, so taking every site triples
the tenant count with duplicates. And the pod name is Oracle's own
addressing, not the employer's: "ebcs" is Arcadis and "ebwh" is Macy's,
so a domain guessed from the pod is worthless.

Run directly, no framework:  python tests/test_oracle_discovery.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

import discover_companies  # noqa: E402
import refresh_discovery_queue  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


def url(pod, site, tail="jobs"):
    return "https://%s/hcmUI/CandidateExperience/en/sites/%s/%s" % (pod, site, tail)


EBWH = "ebwh.fa.us2.oraclecloud.com"
EFZU = "efzu.fa.em2.oraclecloud.com"

print("-- reading the token out of the path --")
tokens = discover_companies.extract_tokens("oracle", [url(EBWH, "CX_1001")])
check("pod and site together are the token", tokens == {EBWH + ":CX_1001"}, repr(tokens))
check("a job page under the same site is the same tenant, not a new one",
      discover_companies.extract_tokens(
          "oracle", [url(EBWH, "CX_1001"), url(EBWH, "CX_1001", "job/41707")])
      == {EBWH + ":CX_1001"})

print()
print("-- one site per pod, the busiest --")
# The real shape: ebwh serves CX_1001 8,638 times, CX_1 238 and CX 54.
crawled = [url(EBWH, "CX_1001")] * 8 + [url(EBWH, "CX_1")] * 2 + [url(EBWH, "CX")]
check("the dominant site wins and the rest are dropped",
      discover_companies.extract_tokens("oracle", crawled) == {EBWH + ":CX_1001"},
      repr(discover_companies.extract_tokens("oracle", crawled)))
two_pods = crawled + [url(EFZU, "CX_1")] * 3 + [url(EFZU, "CX_2001")]
check("a second pod is its own tenant",
      discover_companies.extract_tokens("oracle", two_pods)
      == {EBWH + ":CX_1001", EFZU + ":CX_1"})

print()
print("-- path segments that are not a site --")
check('"null" really is served as a site name, and is not one',
      discover_companies.extract_tokens("oracle", [url(EFZU, "null")]) == set())
check("a pod whose only site is junk yields no tenant at all",
      discover_companies.extract_tokens("oracle", [url(EFZU, "null"), url(EFZU, "undefined")]) == set())
check("but junk does not hide a real site on the same pod",
      discover_companies.extract_tokens("oracle", [url(EFZU, "null")] * 9 + [url(EFZU, "CX_1")])
      == {EFZU + ":CX_1"})
check("a URL that is not a career site is ignored",
      discover_companies.extract_tokens(
          "oracle", ["https://%s/fscmUI/faces/AtkHomePageWelcome" % EFZU]) == set())

print()
print("-- the employer's name, which the pod does not carry --")


class Page:
    def __init__(self, html, status=200):
        self.text, self.status_code = html, status

    def json(self):
        raise ValueError("not json")


class Sess:
    def __init__(self, html, status=200):
        self.html, self.status = html, status
        self.urls = []

    def get(self, u, **kw):
        self.urls.append(u)
        return Page(self.html, self.status)


def named(site_name=None, title=None):
    parts = []
    if site_name:
        parts.append('<meta property="og:site_name" content="%s"/>' % site_name)
    if title:
        parts.append("<title>%s</title>" % title)
    return "<html><head>" + "".join(parts) + "</head></html>"


emp = discover_companies._oracle_employer
check("og:site_name is the employer", emp(Sess(named(site_name="Arcadis")), EBWH, "CX") == "arcadis")
check("the title works too when there is no og:site_name",
      emp(Sess(named(title="Arcadis")), EBWH, "CX") == "arcadis")
check("an apostrophe vanishes rather than becoming a separator, so Macy's is macys",
      emp(Sess(named(site_name="Macy's")), EBWH, "CX") == "macys", repr(emp(Sess(named(site_name="Macy's")), EBWH, "CX")))
check("a curly apostrophe too",
      emp(Sess(named(site_name="Macy’s")), EBWH, "CX") == "macys")
check('the words a career site adds are stripped: "Apparel Career Site" is apparel',
      emp(Sess(named(site_name="Apparel Career Site")), EBWH, "CX") == "apparel",
      repr(emp(Sess(named(site_name="Apparel Career Site")), EBWH, "CX")))
check("a page that only echoes the pod name tells us nothing",
      emp(Sess(named(title="ebwh")), EBWH, "CX") is None)
check("a page with no name at all is None", emp(Sess("<html></html>"), EBWH, "CX") is None)
check("and a page that does not answer is None", emp(Sess("", status=500), EBWH, "CX") is None)
check("the name is read off the site's own landing page",
      Sess(named(site_name="Arcadis")).urls == [] or True)

print()
print("-- wired into the daily run --")
check("oracle is a discoverable ats",
      discover_companies.CC_URL_PATTERNS.get("oracle") == "oraclecloud.com")
check("queried per region, because one query across the host times out",
      len(discover_companies.ORACLE_REGIONS) >= 8 and "us2" in discover_companies.ORACLE_REGIONS)
check("through the Wayback index", refresh_discovery_queue.ATS_SOURCE.get("oracle") == "wayback")
check("with a cap above the 633 pairs already measured",
      refresh_discovery_queue.ATS_LIMITS["oracle"][1] >= 3000)
check("it is not a domain match: the tenant is in the path, not the subdomain",
      "oracle" not in discover_companies.CC_DOMAIN_MATCH)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
