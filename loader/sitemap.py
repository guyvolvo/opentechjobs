"""sitemap.xml, its shards, and feed.xml, generated from the snapshot.

A sitemap that is a hand-edited file goes stale the day the site
changes, and a sitemap of 271,000 listings cannot be hand-edited at all.
So the merge, which is the one process that knows what is open right
now and already publishes bootstrap.json and stats.json beside the
snapshot it just built, writes these too.

What goes in: every listing the board would show, meaning open and
inside the freshness window, at its own page /job/<id>, and every
company with at least one such listing at /company/<domain>, with the
newest open listing's arrival as lastmod (that is when the page last
gained a row). Closed listings
are left out the day they close; api/job_page.py keeps their page up
for thirty days with a "closed" notice and noindex, then answers 410,
so a crawler that already has the URL is told plainly rather than fed
an empty shell.

lastmod is the listing's posting date, or when the board first saw it
for a listing whose source gives no date. It is deliberately not
last_seen, which moves every time a board is polled and would tell a
crawler that 271,000 pages changed this morning.

Shards of 40,000, under the 50,000 limit with room to spare, listed in
a sitemap index. The pages sitemap carries the five hand-built pages;
the board and Explore get the snapshot time as lastmod, since their
content changes with it, and the others carry none rather than a
number nobody can stand behind.

feed.xml is the newest hundred, the same ordering as bootstrap.json,
for readers, aggregators and anyone who wants the new listings without
polling the API.

Publishing is best effort and rate limited to once an hour: the
generation reads every open row, and a five-minute cadence would spend
most of the applier's time rewriting files a crawler reads daily.
"""

import html
import sqlite3
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "api"))

from job_filters import FRESH_CLAUSE  # noqa: E402

SITE = "https://opentechjobs.org"
SHARD = 40_000
FEED_ITEMS = 100
MAX_AGE_S = 3600

# (path, lastmod follows the snapshot?)
PAGES = [("/", False), ("/board", True), ("/map", True), ("/stats", True), ("/api/help", False), ("/contact", False), ("/privacy", False)]

_OPEN = f"closed_at IS NULL AND {FRESH_CLAUSE}"


def _w3c(ts: str | None, fallback: str | None = None) -> str | None:
    """A stored ISO timestamp as sitemaps want it, UTC with a Z."""
    for v in (ts, fallback):
        if not v:
            continue
        try:
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            continue
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _url(loc: str, lastmod: str | None) -> str:
    tail = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
    return f"  <url><loc>{html.escape(loc, quote=True)}</loc>{tail}</url>\n"


def pages_sitemap(now: datetime) -> str:
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = "".join(_url(SITE + path, stamp if follows else None) for path, follows in PAGES)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "</urlset>\n")


def job_shards(conn: sqlite3.Connection) -> list[str]:
    """One XML document per shard, in id order so a listing keeps its
    shard between runs and a crawler's per-file bookkeeping stays useful."""
    rows = conn.execute(f"SELECT id, posted_at, first_seen FROM jobs WHERE {_OPEN} ORDER BY id").fetchall()
    shards = []
    for start in range(0, len(rows), SHARD):
        body = "".join(_url(f"{SITE}/job/{r[0]}", _w3c(r[1], r[2])) for r in rows[start:start + SHARD])
        shards.append('<?xml version="1.0" encoding="UTF-8"?>\n'
                      '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "</urlset>\n")
    return shards


def company_shards(conn: sqlite3.Connection) -> list[str]:
    """Companies with an open listing, at /company/<domain>."""
    rows = conn.execute(
        f"""
        SELECT company_domain, MAX(first_seen) FROM jobs WHERE {_OPEN}
        GROUP BY company_domain ORDER BY company_domain
        """).fetchall()
    shards = []
    for start in range(0, len(rows), SHARD):
        body = "".join(_url(f"{SITE}/company/{r[0]}", _w3c(r[1])) for r in rows[start:start + SHARD] if r[0])
        shards.append('<?xml version="1.0" encoding="UTF-8"?>\n'
                      '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "</urlset>\n")
    return shards


def index(shard_count: int, now: datetime, company_shard_count: int = 0) -> str:
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    names = (["sitemap-pages.xml"] + [f"sitemap-jobs-{i}.xml" for i in range(1, shard_count + 1)]
             + [f"sitemap-companies-{i}.xml" for i in range(1, company_shard_count + 1)])
    body = "".join(f"  <sitemap><loc>{SITE}/{n}</loc><lastmod>{stamp}</lastmod></sitemap>\n" for n in names)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "</sitemapindex>\n")


def feed(conn: sqlite3.Connection, now: datetime) -> str:
    has_name = any(r[1] == "company_name" for r in conn.execute("PRAGMA table_info(companies)"))
    name_sql = ("(SELECT company_name FROM companies WHERE domain = jobs.company_domain)" if has_name else "NULL")
    rows = conn.execute(
        f"""
        SELECT id, title, company_domain, {name_sql} AS company_name, location, department,
               posted_at, first_seen, substr(COALESCE(description, ''), 1, 400) AS blurb
        FROM jobs WHERE {_OPEN}
        ORDER BY posted_at IS NULL, datetime(posted_at) DESC, id DESC
        LIMIT ?
        """, (FEED_ITEMS,)).fetchall()
    items = []
    for r in rows:
        link = f"{SITE}/job/{r['id']}"
        company = r["company_name"] or r["company_domain"]
        when = _w3c(r["posted_at"], r["first_seen"])
        pub = format_datetime(datetime.fromisoformat(when.replace("Z", "+00:00"))) if when else ""
        where = (r["location"] or "").strip()
        summary = " · ".join(x for x in (company, where, r["department"]) if x)
        blurb = (r["blurb"] or "").strip()
        desc = summary + (f" — {blurb}" if blurb else "")
        items.append(
            "    <item>\n"
            f"      <title>{html.escape(r['title'])} at {html.escape(company)}</title>\n"
            f"      <link>{link}</link>\n"
            f'      <guid isPermaLink="true">{link}</guid>\n'
            + (f"      <pubDate>{pub}</pubDate>\n" if pub else "")
            + f"      <description>{html.escape(desc)}</description>\n"
            + (f"      <category>{html.escape(r['department'])}</category>\n" if r["department"] else "")
            + "    </item>\n")
    built = format_datetime(now.astimezone(timezone.utc))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
            "  <channel>\n"
            "    <title>OpenTechJobs newest listings</title>\n"
            f"    <link>{SITE}/board</link>\n"
            f'    <atom:link href="{SITE}/feed.xml" rel="self" type="application/rss+xml" />\n'
            "    <description>The newest listings on OpenTechJobs.org, the open-source job board.</description>\n"
            "    <language>en</language>\n"
            f"    <lastBuildDate>{built}</lastBuildDate>\n"
            + "".join(items)
            + "  </channel>\n</rss>\n")


def build(db_path: Path, now: datetime | None = None) -> dict[str, tuple[bytes, str]]:
    """{key: (body, content type)} for everything to publish."""
    now = now or datetime.now(timezone.utc)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        shards = job_shards(conn)
        companies = company_shards(conn)
        rss = feed(conn, now)
    finally:
        conn.close()
    xml = "application/xml; charset=utf-8"
    out = {
        "sitemap.xml": (index(len(shards), now, len(companies)).encode("utf-8"), xml),
        "sitemap-pages.xml": (pages_sitemap(now).encode("utf-8"), xml),
        "feed.xml": (rss.encode("utf-8"), "application/rss+xml; charset=utf-8"),
    }
    for i, doc in enumerate(shards, 1):
        out[f"sitemap-jobs-{i}.xml"] = (doc.encode("utf-8"), xml)
    for i, doc in enumerate(companies, 1):
        out[f"sitemap-companies-{i}.xml"] = (doc.encode("utf-8"), xml)
    return out


def _fresh_enough(s3, bucket: str) -> bool:
    """Whether the published sitemap is under MAX_AGE_S old.

    Says why when it cannot tell. The first deploy answered False on
    every run because the role could write these keys but not HEAD
    them, and a silent False meant 30MB republished every five minutes
    with nothing in the log to say so. A missing object is the normal
    first-run case; anything else is a problem worth a line.
    """
    try:
        head = s3.head_object(Bucket=bucket, Key="sitemap.xml")
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code not in ("404", "NoSuchKey", "NotFound"):
            print(f"sitemap age check failed ({code or e!r}), publishing anyway", file=sys.stderr)
        return False
    age = (datetime.now(timezone.utc) - head["LastModified"]).total_seconds()
    if age < MAX_AGE_S:
        print(f"sitemap is {age:.0f}s old, leaving it", file=sys.stderr)
        return True
    return False


def publish(frontend_bucket: str, db_path: Path, tmp: Path | None = None) -> list[str]:
    """Write everything to the frontend bucket. Returns the keys written,
    [] on any failure: a merge that has already pushed its snapshot must
    not fail over a sitemap."""
    if not frontend_bucket:
        return []
    try:
        import boto3

        s3 = boto3.client("s3")
        if _fresh_enough(s3, frontend_bucket):
            return []
        docs = build(db_path)
        written = []
        # Shards first, the index last, so a crawler that reads the new
        # index never asks for a shard that is not there yet.
        for key in sorted(docs, key=lambda k: (k == "sitemap.xml", k)):
            body, ctype = docs[key]
            s3.put_object(Bucket=frontend_bucket, Key=key, Body=body, ContentType=ctype,
                          CacheControl="public, max-age=3600")
            written.append(key)
        # A shard count that shrank leaves old files behind; the index no
        # longer names them, but a crawler that remembers them would get
        # a stale list. Deleting a key that is not there is not an error.
        for prefix in ("sitemap-jobs-", "sitemap-companies-"):
            count = sum(1 for k in docs if k.startswith(prefix))
            for i in range(count + 1, count + 4):
                s3.delete_object(Bucket=frontend_bucket, Key=f"{prefix}{i}.xml")
        shard_count = sum(1 for k in docs if k.startswith("sitemap-jobs-"))
        total = sum(len(b) for b, _ in docs.values())
        print(f"published {len(written)} sitemap/feed files ({total} bytes, {shard_count} job shards)", file=sys.stderr)
        return written
    except Exception as e:
        print(f"sitemap publish failed (non-fatal): {e!r}", file=sys.stderr)
        return []
