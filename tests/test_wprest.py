"""WordPress job post types read over the REST API (probe.f_wprest),
first used for One Technologies.

Shapes are what www.one1.co.il/wp-json answered on 2026-09-18: a `job`
post type, 157 posts over two pages of 100, a `region` taxonomy in
Hebrew, and a `job-category` taxonomy for the field.

Run directly, no framework:  python tests/test_wprest.py
"""

import json
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
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.text = json.dumps(body, ensure_ascii=False) if body is not None else ""
        self.headers = {"Content-Type": "application/json", **(headers or {})}

    def json(self):
        return json.loads(self.text)


class Sess:
    def __init__(self, answers):
        self.answers, self.gets = answers, []
        self.headers = {}

    def get(self, url, **kw):
        self.gets.append(url)
        # Match on the path and query, ignoring parameter order.
        for k, v in self.answers.items():
            if url.split("?")[0] == k.split("?")[0] and set(url.split("?")[1:] and url.split("?")[1].split("&") or []) >= set(k.split("?")[1:] and k.split("?")[1].split("&") or []):
                return v
        return Resp(404)


BASE = "https://www.one1.co.il/wp-json/wp/v2"
TOKEN = "www.one1.co.il:job:Israel"


def post(pid, title, regions, cats, date="2026-09-17T13:33:06"):
    return {"id": pid, "date": date, "modified": date, "link": f"https://www.one1.co.il/job/{pid}/",
            "title": {"rendered": title}, "content": {"rendered": "<p>לארגון <b>מוביל</b> דרוש/ה</p>"},
            "region": regions, "job-category": cats, "jobscope": [289]}


ANSWERS = {
    f"{BASE}/taxonomies?type=job": Resp(200, {
        "region": {"rest_base": "region", "types": ["job"]},
        "job-category": {"rest_base": "job-category", "types": ["job"]},
        "jobscope": {"rest_base": "jobscope", "types": ["job"]},
    }),
    f"{BASE}/region?per_page=100&page=1": Resp(200, [{"id": 295, "name": "גוש דן"}, {"id": 293, "name": "מרכז - שפלה"}],
                                           {"X-WP-TotalPages": "1"}),
    f"{BASE}/job-category?per_page=100&page=1": Resp(200, [{"id": 262, "name": "פיתוח / UI/UX"}, {"id": 270, "name": "DATA &amp; BI"}],
                                                 {"X-WP-TotalPages": "1"}),
    f"{BASE}/job?per_page=100&page=1": Resp(200, [post(3566, "דרוש מוביל/ה טכנולוגי/ת (Tech Lead)", [295, 293], [262]),
                                                  post(3561, "טכנאי/ת מחשבים &#8211; צפון", [], [270])],
                                       {"X-WP-Total": "3", "X-WP-TotalPages": "2"}),
    f"{BASE}/job?per_page=100&page=2": Resp(200, [post(3550, "בודק/ת QA", [293], [])], {"X-WP-Total": "3", "X-WP-TotalPages": "2"}),
}

for bad in ["one1", "one1.co.il", "www.one1.co.il", "www.one1.co.il:job", "", None, ":job:Israel"]:
    sess = Sess(ANSWERS)
    check(f"a guessed token {bad!r} makes no request", probe.f_wprest(sess, bad) is None and not sess.gets)

sess = Sess(ANSWERS)
jobs = probe.f_wprest(sess, TOKEN)
check("walks every page", jobs is not None and [j.external_id for j in jobs] == ["3566", "3561", "3550"],
      repr(jobs and [j.external_id for j in jobs]))
check("stops at the last page rather than asking for one that is not there",
      not any("page=3" in u for u in sess.gets), repr([u for u in sess.gets if "/job?" in u]))
j = jobs[0]
check("title is unescaped text", jobs[1].title == "טכנאי/ת מחשבים – צפון", repr(jobs[1].title))
check("regions become the location, with the country named",
      j.location == "גוש דן; מרכז - שפלה, Israel", repr(j.location))
check("no region means just the country", jobs[1].location == "Israel", repr(jobs[1].location))
check("the category is the department, unescaped", jobs[1].department == "DATA & BI", repr(jobs[1].department))
check("the post's own link is the url", j.url == "https://www.one1.co.il/job/3566/", j.url)
check("the post date is the posting date", (j.posted_at or "").startswith("2026-09-17"), repr(j.posted_at))
check("classified as Israel", all(x.country == "IL" for x in probe._fill_classifications(jobs, "one1.co.il")))
check("content is the cleaned description", "מוביל" in (j.description or "") and "<" not in (j.description or ""),
      repr(j.description)[:60])
check("taxonomies are read once, not per job", sum(1 for u in sess.gets if "/region?" in u) == 1, repr(sess.gets))

# A site whose REST is closed for the type answers 401. That is no
# answer, not an empty board.
closed = dict(ANSWERS)
closed[f"{BASE}/job?per_page=100&page=1"] = Resp(401, {"code": "rest_forbidden"})
check("a refused listing is no answer", probe.f_wprest(Sess(closed), TOKEN) is None)
empty = dict(ANSWERS)
empty[f"{BASE}/job?per_page=100&page=1"] = Resp(200, [], {"X-WP-Total": "0", "X-WP-TotalPages": "0"})
check("an empty listing is an empty board", probe.f_wprest(Sess(empty), TOKEN) == [])

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
