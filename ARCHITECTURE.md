# Architecture

What runs where, as of 2026-09-23, after the move from Lambda-over-S3 to
a single box. Every number here was measured on the live system on that
date rather than estimated.

`README.md`'s own `## Architecture` section predates the migration and
describes the S3-hosted `jobs-read.db` design. It is stale. This file is
the current truth.

## The shape of it

```
  scrapers                  box (EC2 t4g.small, il-central-1)         readers
  ────────                  ────────────────────────────────         ───────

  GitHub Actions ─┐
  daily, slow     │
  guesses ATS     │         ┌─────────────────────────────┐
  tokens          ├─ S3 ───►│ fetch_fragments.py  (30s)   │
                  │ delta   │   copies to a local spool   │
  EventBridge ────┘ frags   ├─────────────────────────────┤
  Lambda, 5 min             │ shadow_apply.py  (timer)    │
  re-polls known            │   48MB per pass, flock'd    │
  company/ATS pairs         ├─────────────────────────────┤
                            │ jobs.db  3.0GB  WAL         │──► Litestream ──► S3
                            ├─────────────────────────────┤
                            │ publish.py  (timer)         │──► artifacts ──► S3
                            │ publish_snapshot.py (daily) │──► lean copy ──► S3
                            ├─────────────────────────────┤
  browser ──► Cloudflare ──►│ gunicorn -w 2 --threads 4   │
              Worker        │   api/handler.py (WSGI)     │
                            └─────────────────────────────┘
```

## Ingestion

Two scrapers, on different clocks.

A fast one: an EventBridge-scheduled Lambda every five minutes, re-polling
company and ATS pairs already known. Workday tenants are excluded from
this pass; re-polling all of them blew the Lambda's time budget.

A slow one: a GitHub Actions job daily, guessing ATS tokens and scraping
Comeet embeds to find companies nobody has seen yet. Workday's pinned
tenants refresh here instead.

Both write **delta fragments** to S3. A fragment is JSON, about 19MB, and
holds one entry per company polled. Each entry carries that company's
**entire current board**, not a diff, plus an `unchanged` flag that
`load_to_sqlite.py` honours so a company whose scrape returned 304 costs
nothing. A company that did change has all of its jobs re-upserted, so one
new listing on a 2,319-job board rewrites all 2,319 rows.

## Apply

`box/fetch_fragments.py` runs every 30 seconds and copies anything new
into `/var/lib/otj/deltas`. Looking and applying are split on purpose: the
old Lambda still owns deletion and clears a fragment within five minutes,
while a box apply takes minutes, so a fragment could be deleted before the
box ever saw it. Measured the morning after the first night: the box
trailed the live snapshot by 15,500 listings.

`box/shadow_apply.py` then works from the spool, which nothing else can
delete underneath it, at 48MB per pass. That ceiling is not arbitrary: at
96MB the applier reached 1,379MB of anonymous RSS and the kernel killed
it along with sshd and cloudflared.

Everything heavy takes the same `flock` (`box/lock.py`), so an apply, a
publish and a snapshot never walk the database at once.

## What is in the database

3.0GB, and the distribution matters more than the total:

| part | size |
|---|---|
| `jobs_fts_data`, the inverted index | 1,830 MB |
| `jobs` and everything else | ~1,200 MB |
| WAL, at the time of measuring | 430 MB |

Descriptions are **not** in the table. `description` is NULL on every row;
694,397 rows carry a `description_sha` naming an S3 blob, fetched only
when a listing is opened. The full-text index keeps its own copy of that
text, which is the whole of the 1,830MB.

The index covers 1,135,533 rows, including 151,241 closed listings the
board never shows by default. The board's own default view, tech roles, is
227,765. The index is five times larger than the view it serves.

## Serving

```
/api/auth/*                 ──► CloudFront ──► Lambda                     sign-in only
/api/*                      ──► Worker ──► cloudflared ──► box:8000       the API, fast path
/job/*, /company/*          ──► CloudFront ──► cloudflared ──► box:8000   the pages crawlers walk
everything else             ──► CloudFront ──► S3                         static frontend
```

Two routers, on purpose. CloudFront's cache behaviors
(`infra/cloudfront.tf`) can send every one of those paths to the box,
with the tunnel hostname `box.opentechjobs.org` as a second custom
origin, and that is what happens whenever the Worker
(`infra/cloudflare-worker.js`) is over budget or has no route:
Cloudflare fails the route open. The Worker is on `/api/*` alone for
the waiting. Measured 2026-09-25 from Israel, a listing's description
arrived in a steady 204 to 236 ms through the Worker and in a median
331 ms with spikes to 840 ms through CloudFront, which crosses two
edges and often a fresh connection between them. The Worker is not on
`/job/*` and `/company/*` because those are what crawlers walk, and on
2026-09-24 one of them spent the free plan's 100,000 daily Worker
requests by 05:00. CloudFront's free tier is ten million a month.
Cloudflare's own Origin Rules could do none of this: Host Header, SNI
and DNS Record overrides are all Enterprise-only on this plan.

Two consequences of the box sitting behind Cloudflare as well. A request
reaches Cloudflare twice, so the rate limiting rule in the dashboard is
scoped to `http.host eq "oceanofjobs.com"`, or CloudFront's few IPs
would be the busiest client on the zone. And Cloudflare re-stamps
`CF-IPCountry` on the second pass with the CloudFront edge's country, so
the viewer-request function saves the viewer's copy as
`X-Viewer-Country` for `/api/geo`.

Rollback of the Worker is deleting its route; rollback of CloudFront is
pointing the behaviors back at the `api-lambda` origin. The Lambda stack
is still deployed and still receiving code, so what it falls back to is
current.

## Precomputed artifacts

This is why the common paths are fast and everything else is not.

`box/publish.py` runs four builders on a timer, each paced internally:

| builder | output | cost |
|---|---|---|
| `precompute.py` | `facets.json`, `stats.json` | 261MB, 166s |
| `bootstrap.py` | `bootstrap.json`, `bootstrap-il.json` | 17MB, 15s |
| `build_explore.py` | `explore.db` | 188MB, 152s |
| `sitemap.py` | sitemap parts | 123MB, 204s |

End to end the publisher peaks at 308MB rather than the sum, because
CPython reuses arenas between stages.

`/api/facets` and `/api/stats` serve straight from those files for the
unfiltered, `roles=tech` and Israel variants. Those answer in 0.00 to
0.09s. Anything else computes live and costs seconds.

`box/publish_snapshot.py` does a daily `VACUUM INTO` plus `retire_fts`,
publishing a lean 745MB snapshot to S3 for the Lambda to serve. The
lean part is load-bearing: a rolled-back Lambda pulling 2.5GB inside a
29 second gateway timeout is the original outage.

## Durability, and the three different kinds of it

Keeping these apart is the single most useful thing to hold in your head
about this system, because two of them exist and one does not.

```
1. serving state          jobs.db + Litestream            exists
2. recovery artifact      the lean published snapshot     exists
3. replay history         S3 fragments retained           does NOT exist
```

**S3 fragments are a transient queue, not a log.** The Lambda deletes
each one once it has applied it, usually inside five minutes. That
deletion is the whole reason `fetch_fragments.py` spools to local disk
first. Counted on 2026-09-23: 5 objects under `deltas/` in S3 against 53
fragments spooled on the box.

So this is not a recovery path today:

```
S3 ──► rebuild jobs.db      NO. There is nothing there to rebuild from.
```

These are:

```
jobs.db lost
   ├── Litestream restore           69s, exact row parity, markers intact
   └── the lean published snapshot  745MB, what the Lambda serves
```

Turning S3 into a real replay log is cheap and worth considering: stop
deleting fragments after apply, add a lifecycle rule to expire them after
N days. That would give the database a genuine rebuild path and make it
replaceable rather than irreplaceable. It is a proposal, not a
description, and the difference matters: somebody who believes the
rebuild path already exists will find out during an incident.

## How a slow query becomes an outage

Worth stating on its own, because "SQLite is disk-bound" does not explain
the behaviour that actually gets reported.

```
gunicorn -w 2 --threads 4  =  8 execution slots

   slow facet    slow facet    slow facet    slow facet
        │             │             │             │
        └─────────────┴──────┬──────┴─────────────┘
                             ▼
                      all slots occupied
                             ▼
                   every other request queues
                             ▼
                       blank rows
```

A 13 second facets call is not a 13 second problem. Four of them
concurrently, on different filters so nothing shares a cache entry,
consume the entire concurrency budget and the job list queues behind
them. That is how a slow endpoint became three minutes of skeleton rows
on 2026-09-23, and it is why raising the worker count does not help: the
constraint is the disk underneath all of them.

The metric that catches this is not endpoint latency. It is slot
occupancy and queue depth.

## Known pressure points

Measured, in the order they hurt.

**1. The instance runs out of EBS credits.** `t4g.small` has a small
baseline of IOPS plus a burst pool. On 2026-09-23 the pool drained from
21% to 0% over 45 minutes and stayed there. CPU was idle and memory was
free while every query timed out, because nothing was computing and
everything was queued on disk. Applies went from 47s to 1,243s. It
refills at roughly 1% per five minutes with the box completely idle,
which is about eight hours for a full pool.

**2. The working set does not fit in RAM.** 3.0GB of database against
1.8GB of memory, so the page cache cannot hold it and every read reaches
the disk, competing with the applier's writes for the credits in (1).

Splitting the search index into its own file fixes this, and the split
has been built and measured on a copy: `jobs.db` 0.76GB, `jobs-fts.db`
1.84GB, row counts identical, searches correct across the attached file.
See `box/STAGE2-FTS.md`. Two things that experiment found and a live
cutover would have hit: the index is contentless, so its rows cannot be
read back and re-inserted, and SQLite refuses direct writes to FTS5
shadow tables. The split has to be done by copying the whole file twice
and dropping the half each copy does not need. Also, `MATCH` takes the
table name unqualified even when attached: `FROM fts.jobs_fts WHERE
jobs_fts MATCH ?`. Qualifying both sides is a parse error.

**3. The board cannot use its own indexes.** All three purpose-built
indexes are partial, `WHERE closed_at IS NULL AND confidence =
'verified'`. The board sends `confidence=all` on every request so it can
show both tiers with a badge, so SQLite correctly refuses them:

```
confidence=all       SCAN jobs USING INDEX idx_jobs_posted_at
confidence=verified  SEARCH jobs USING INDEX idx_jobs_open_role_posted
```

This also made `count_index_hint` name an index the query could not
satisfy, which is not ignored but an error: every count on the All roles
view returned 500 in two milliseconds until 2026-09-23.

**4. Live facets are expensive.** Every facet is counted with every other
filter applied but its own dropped, so picking two countries leaves the
locations facet counting the entire open board. `country` and `city` are
comma-joined strings, so that means splitting about a million of them
with a recursive CTE per request: roughly 10s of a 13s call. Storing them
as their own indexed rows is the fix, prototyped at 1,017,409 rows and
84MB.

Points 2, 3 and 4 are all "the box does more disk IO than it needs to",
which is why point 1 keeps happening.

## Operating notes

Do not run a full-database rebuild, split or VACUUM on the production
instance while it is serving. This is not caution, it is measured: the
FTS split experiment on 2026-09-23 copied 2.6GB twice and vacuumed both,
at `nice 19` and `ionice -c3`, and still drained the EBS burst pool from
21% to 0%. Applies went from 47s to 1,243s, the spool grew from 12
fragments to 58, and the API began timing out at 45s. The experiment
itself caused an outage on a box that had no IO headroom to spare.

Heavyweight database work needs either enough IO headroom to absorb it or
an isolated replica to do it on. Litestream can restore a copy elsewhere
for exactly this.

Pausing the pipeline without losing data:

```
sudo systemctl stop  otj-fetch.timer otj-apply.timer otj-publish.timer otj-snapshot.timer
sudo systemctl start otj-fetch.timer otj-apply.timer otj-publish.timer otj-snapshot.timer
```

Stop the timers, never the services: an apply already in flight finishes
its transaction instead of being killed part-way, and fragments keep
accumulating in S3 and the local spool until it resumes.

Litestream replicates `jobs.db` continuously and restores it in 69
seconds with exact row parity. After the FTS split it has to replicate
both files, and a restore needs both: restoring only the main file gives
a working board with no description search, which is a degraded state
rather than an obvious failure.

## What to fix, in order

Hardware first is not a preference. Nothing below can be done safely on an
instance with no IO headroom, which the split experiment proved.

**1. IO headroom.** A larger instance, or storage with a predictable
floor rather than a burst pool. The pool refills at about 1% per five
minutes with the box completely idle, roughly eight hours for a full
one, and the applier drains it far faster than that.

**2. Database layout and recovery correctness, together.** Split the FTS
into its own file, and in the same change make the two files one atomic
recovery generation: a manifest carrying a generation number and a
checksum per file, restored only if both validate together. Two files
that can be restored independently give a board that looks healthy with
search silently degraded, which is worse than an obvious failure. Keep
the resulting published snapshot inside the size the Lambda fallback can
pull within its 29 second timeout.

**3. Facet computation, not just facet storage.** Normalising country and
city into indexed rows is necessary and not sufficient. The cost comes
from the rule that each facet is counted with its own filter removed, so
the locations facet scans the whole open board no matter what is
selected. A perfectly normalised schema still scans a million rows if the
computation model is unchanged. The options worth measuring are
precomputed aggregates, a materialised facet model, cached results per
filter, and narrowing the candidate set first.

**4. Write amplification, one level deeper than it already goes.** The
company-level check already works: an unchanged board costs nothing. What
remains is that a company which did change has every one of its jobs
re-upserted. Compare jobs individually and skip the unchanged ones, and
design the freshness update so it does not re-dirty every row it just
decided not to touch. Writing `last_seen` per job on every scrape
re-dirties the page and the WAL regardless of how little changed, so the
saving only appears if that write is batched or derived.

**5. Concurrency protection and observability.** Slot occupancy, queue
depth, and response sizes, not only latencies. The `/api/health` case is
the argument for the last one: 785KB and 24 seconds, polled every two
minutes by every open tab, because one meta row had grown to 8,372
entries. No infrastructure metric would have found that. An alert on
"this endpoint returned far more bytes than it has any business
returning" would have.

**6. A durable ingestion log, if wanted.** Retain fragments in S3 with a
lifecycle expiry, and the database becomes rebuildable.

A PostgreSQL migration is not justified by any measurement taken here.
SQLite is not the problem; the amount of disk IO asked of one small
machine is. Revisit that only if the product needs concurrent writers,
multiple serving instances or real high availability.
