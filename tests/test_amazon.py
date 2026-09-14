"""Amazon and AWS, from amazon.jobs' own search endpoint.

Not an ATS: one company's careers site. That matters more than it
sounds, because every fetcher in FETCHERS is called with every guessed
token, and a guess is a lowercase slug taken off a domain name. Handing
amazon.jobs somebody else's company name as a country filter and
believing whatever comes back is exactly the failure that put a North
Dakota electrician on this board once already. So the token shape is the
guard, and it gets a test rather than a comment.

The rest is the split. business_category is the only thing separating
AWS from the rest of Amazon, and on a job board they are two employers.

Run directly, no framework:  python tests/test_amazon.py
"""

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


ROWS = [
    {"id_icims": "1", "title": "Software Dev Engineer", "business_category": "aws",
     "job_category": "Software Development", "normalized_location": "Tel Aviv-Yafo, Tel Aviv, ISR",
     "job_path": "/en/jobs/1/sde", "posted_date": "September  9, 2026",
     "description": "Build things.", "basic_qualifications": "- Kubernetes",
     "preferred_qualifications": "- Terraform"},
    {"id_icims": "2", "title": "Applied Scientist", "business_category": "amazon",
     "job_category": "Applied Science", "normalized_location": "Haifa, Haifa, ISR",
     "job_path": "/en/jobs/2/as", "posted_date": "June  1, 2026",
     "description": "Research.", "basic_qualifications": "", "preferred_qualifications": ""},
    {"id_icims": "3", "title": "Ops Manager", "business_category": "AWS",
     "job_category": "Ops", "location": "IL, Haifa", "job_path": "/en/jobs/3/om",
     "posted_date": "July 14, 2026", "description": "Run things."},
    # No id at all: skipped rather than written with an empty key, which
    # would collide with the next one like it on every sweep.
    {"title": "Ghost", "business_category": "aws", "job_path": "/en/jobs/x"},
]


class FakePages:
    """Answers with one page, then an empty one, recording every URL."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.urls = []

    def __call__(self, sess, url):
        self.urls.append(url)
        return self.pages.pop(0) if self.pages else {"hits": 0, "jobs": []}


def fetch(token, pages):
    fake = FakePages(pages)
    orig = probe.get_json
    probe.get_json = fake
    try:
        return probe.f_amazon(None, token), fake
    finally:
        probe.get_json = orig


one_page = [{"hits": len(ROWS), "jobs": ROWS}]

# The guard. Nothing here may fire on a token the guess loop invented.
for bad in ["wiz", "amazon", "isr", "ISRC", "ISR|AWS", "", "monday"]:
    jobs, fake = fetch(bad, one_page)
    check(f"a guessed token like {bad!r} makes no request",
          jobs is None and fake.urls == [], repr(fake.urls))

aws, _ = fetch("ISR|aws", [{"hits": len(ROWS), "jobs": ROWS}])
rest, _ = fetch("ISR|-aws", [{"hits": len(ROWS), "jobs": ROWS}])
both, _ = fetch("ISR", [{"hits": len(ROWS), "jobs": ROWS}])

check("aws takes only the aws rows", [j.external_id for j in aws] == ["1", "3"],
      repr([j.external_id for j in aws]))
check("and the case of the category does not decide it",
      "3" in [j.external_id for j in aws])
check("the rest takes everything else", [j.external_id for j in rest] == ["2"],
      repr([j.external_id for j in rest]))
check("no filter takes all of them", [j.external_id for j in both] == ["1", "2", "3"])
check("a row with no id is dropped, not written with an empty one",
      all(j.external_id for j in both))

j = aws[0]
check("the url is absolute", j.url == "https://www.amazon.jobs/en/jobs/1/sde", j.url)
check("the date parses", j.posted_at == "2026-09-09T00:00:00+00:00", repr(j.posted_at))
check("a single-digit day parses despite the double space",
      rest[0].posted_at == "2026-06-01T00:00:00+00:00", repr(rest[0].posted_at))
check("the department is the job category", j.department == "Software Development")
check("the location is the normalized one", j.location == "Tel Aviv-Yafo, Tel Aviv, ISR")
check("and falls back when there is none", aws[1].location == "IL, Haifa")
check("Israel is detectable in that location string",
      any(k in j.location.lower() for k in ("israel", "tel aviv")), j.location)

# The qualifications carry the technology names; the description proper
# is mostly prose about the team.
check("the qualifications are part of the description",
      "Kubernetes" in j.description and "Terraform" in j.description, repr(j.description))
check("and a missing one is not a literal None in the text",
      "None" not in (aws[1].description or ""), repr(aws[1].description))

# Paging follows the raw row count, not the filtered one: hits counts
# the whole country, so an aws-only read would otherwise stop early.
page1 = {"hits": 250, "jobs": ROWS * 25}
page2 = {"hits": 250, "jobs": ROWS * 12}
jobs, fake = fetch("ISR|aws", [page1, page2])
check("it pages until the rows run out", len(fake.urls) == 2, repr(len(fake.urls)))
check("and offsets by the page size", "offset=100" in fake.urls[1], fake.urls[1])
check("filtering does not stop paging early", len(jobs) == (25 + 12) * 2,
      repr(len(jobs)))

# A blip mid-read must not read as "every role closed".
jobs, _ = fetch("ISR|aws", [page1, None])
check("a failure part way through keeps what it already had",
      jobs is not None and len(jobs) == 50, repr(jobs and len(jobs)))
jobs, _ = fetch("ISR|aws", [None])
check("but a failure on the first page is no board at all", jobs is None)

# The pins that make this reachable at all.
pins = probe.load_pins().get("amazon", {})
check("both boards are pinned", set(pins) == {"amazon.com", "aws.amazon.com"}, repr(sorted(pins)))
check("and they ask for different halves of the whole world",
      pins.get("aws.amazon.com", {}).get("token") == "ALL|aws"
      and pins.get("amazon.com", {}).get("token") == "ALL|-aws",
      repr({d: p.get("token") for d, p in pins.items()}))
check("the pipe survived YAML's typing",
      all(isinstance(p["token"], str) and "|" in p["token"] for p in pins.values()))
check("amazon is registered as a fetcher, so the generic pin path finds it",
      probe.FETCHERS.get("amazon") is probe.f_amazon)

# The global read: by country, and a country past the offset ceiling by state.
def global_answer(url):
    if "facets%5B%5D=normalized_country_code" in url:
        return {"facets": {"normalized_country_code_facet": [{"USA": 12000}, {"ISR": 150}]}, "jobs": []}
    if "facets%5B%5D=normalized_state_name" in url:
        return {"facets": {"normalized_state_name_facet": [{"Washington": 6000}, {"New York": 6000}]}, "jobs": []}
    if "offset=0" in url:
        return {"hits": 1, "jobs": [ROWS[0] if "ISR" in url else ROWS[1]]}
    return {"hits": 1, "jobs": []}


urls = []
orig = probe.get_json
probe.get_json = lambda sess, url: (urls.append(url), global_answer(url))[1]
try:
    everywhere = probe.f_amazon(None, "ALL")
    first_read = len(urls)
    rest_half = probe.f_amazon(None, "ALL|-aws")
    aws_start = len(urls)
    aws_half = probe.f_amazon(None, "ALL|aws")
finally:
    probe.get_json = orig
check("the non-AWS half drops AWS after reading", [j.external_id for j in rest_half] == ["2"], repr(rest_half))
check("AWS is asked for on the server, as its own read",
      len(urls) > aws_start and all("business_category%5B%5D=aws" in u for u in urls[aws_start:]),
      repr(urls[aws_start:aws_start + 2]))

probe.get_json = global_answer_fn = (lambda sess, url: global_answer(url))
try:
    capped = probe.f_amazon(None, "ALL", description_budget=1)
finally:
    probe.get_json = orig
check("the description budget caps how many jobs carry text",
      sum(1 for j in capped if j.description) == 1 and len(capped) == 2, repr([(j.external_id, bool(j.description)) for j in capped]))
check("ALL splits the US by state and reads every slice",
      any("normalized_state_name%5B%5D=Washington" in u for u in urls)
      and any("normalized_country_code%5B%5D=ISR" in u and "offset=0" in u for u in urls), repr(urls[:6]))
check("and never pages a slice past the offset ceiling",
      not any("offset=10000" in u for u in urls), repr([u for u in urls if "offset=" in u][-3:]))
check("the same job in two slices is kept once",
      sorted(j.external_id for j in everywhere) == ["1", "2"], repr([j.external_id for j in everywhere]))

probe.get_json = lambda sess, url: None if "New+York" in url or "New%20York" in url else global_answer(url)
probe.time.sleep = lambda s: None
try:
    partial = probe.f_amazon(None, "ALL")
finally:
    probe.get_json = orig
check("a slice that keeps failing makes the whole read fail, so no job is closed by a missing page",
      partial is None, repr(partial and len(partial)))

probe.get_json = lambda sess, url: global_answer(url)
try:
    known = probe.f_amazon(None, "ALL", known_ids={"1"})
finally:
    probe.get_json = orig
check("a job we already have is read without its description",
      {j.external_id: j.description for j in known}.get("1") is None
      and {j.external_id: j.description for j in known}.get("2"), repr({j.external_id: j.description for j in known}))

domains = set((ROOT / "domains.txt").read_text(encoding="utf-8").split())
check("and both domains are in the sweep",
      {"amazon.com", "aws.amazon.com"} <= domains)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
