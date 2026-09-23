"""Apply pending delta fragments to the box's own jobs.db, in place.

The Lambda applier (scrape_maintenance_handler.py) pulls the 600MB
snapshot from S3, applies a few fragments, pushes it back and deletes
the fragments. This does the middle step against a file that never
moves, and nothing else: it does not delete fragments from S3, upload a
snapshot, or write status files, because while the box runs in the
shadow of the Lambda stack the Lambda still owns all of that.

Fragments come from the local spool that fetch_fragments.py fills, not
from S3 directly. That split is the whole reason the spool exists: the
Lambda deletes a fragment within minutes of it being written, an apply
here can take longer than that, and anything cleared during an apply
used to be lost to this box. See fetch_fragments.py.

What it does share is the loader. load_to_sqlite.py with --resolved and
--out and no --bucket is exactly "apply this to that file," and --box
adds the category column, canonical posted_at and the board's indexes.
Descriptions still go to S3 through the loader's own put_many, keyed by
job id, the same objects the Lambda writes, so the two appliers cannot
disagree about them.

Runs from a systemd timer every minute. A run that finds nothing costs
one directory listing.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))

import build_explore  # noqa: E402
import precompute  # noqa: E402
import sitemap  # noqa: E402

DB = Path(os.environ.get("DATA_PATH", "/var/lib/otj/jobs.db"))
BUCKET = os.environ["DATA_BUCKET"]
SPOOL = DB.parent / "deltas"
WORK = DB.with_name("delta-resolved.json")
# Bounded by bytes, like the Lambda, but without its memory ceiling to
# respect: the box parses into RAM too, and 2GB is the whole machine.
MAX_APPLY_BYTES = 96 * 1024 * 1024
# The frontend bucket, and whether this box may write to it.
#
# Off while the box runs in the shadow of the Lambda stack, because
# these four artifacts are read by the live site: bootstrap.json is its
# first paint, explore.db is the stats page, and the sitemaps are what
# Google reads. Two appliers writing them would mean the live site
# showing whichever one ran last. On at cutover, when the box is the
# only applier left.
FRONTEND_BUCKET = os.environ.get("FRONTEND_BUCKET", "")
PUBLISH_FRONTEND = os.environ.get("OTJ_PUBLISH_FRONTEND") == "1"
# Kept in step with loader/bootstrap.py's VIEWS, same as the Lambda
# handler keeps its own copy, and for the same reason: two lines.
BOOTSTRAP_VIEWS = {"bootstrap.json": {}, "bootstrap-il.json": {"country": "IL"}}


def _read(paths: list[Path]) -> list[dict]:
    """Every result across these fragments, in name order.

    Concatenated rather than merged, for the reason deltas.read_fragments
    gives: load_resolved is an upsert keyed on job id, so replaying an
    older entry before a newer one lands on the newer one.
    """
    out: list[dict] = []
    for p in paths:
        try:
            out.extend(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            # A fragment that cannot be parsed will never parse. Moving
            # it aside keeps the queue draining instead of retrying it
            # every minute forever, and leaves it for a human to look at.
            print(f"unreadable, set aside: {p.name}: {e!r}", file=sys.stderr)
            p.rename(p.with_suffix(".bad"))
    return out


def _publish_frontend() -> str:
    """bootstrap.json, explore.db and the sitemaps, as the Lambda's own
    applier writes them (scrape_maintenance_handler._publish_bootstrap
    and the two publish() calls beside it). Best effort throughout: the
    site falls back to asking the API whenever one of these is missing
    or stale-shaped, so none of it is worth failing an apply over.

    Both the explore build and the sitemap pace themselves internally,
    hourly, so calling them every minute costs a check and nothing
    more."""
    if not (PUBLISH_FRONTEND and FRONTEND_BUCKET):
        return ""
    import boto3

    done = []
    s3 = boto3.client("s3")
    for name, filters in BOOTSTRAP_VIEWS.items():
        out = DB.with_name(name)
        cmd = [sys.executable, str(ROOT / "loader" / "bootstrap.py"),
               "--db", str(DB), "--out", str(out)]
        if filters.get("country"):
            cmd += ["--country", filters["country"]]
        try:
            build = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if build.returncode != 0:
                print(f"{name} build failed (non-fatal): exit {build.returncode}", file=sys.stderr)
                continue
            s3.put_object(
                Bucket=FRONTEND_BUCKET, Key=name, Body=out.read_bytes(),
                ContentType="application/json",
                CacheControl="public, max-age=60, s-maxage=300, stale-while-revalidate=600",
            )
            done.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"{name} publish failed (non-fatal): {e!r}", file=sys.stderr)
    try:
        if build_explore.publish(FRONTEND_BUCKET, DB, DB.parent):
            done.append("explore.db")
    except Exception as e:  # noqa: BLE001
        print(f"explore.db publish failed (non-fatal): {e!r}", file=sys.stderr)
    try:
        done += sitemap.publish(FRONTEND_BUCKET, DB, DB.parent) or []
    except Exception as e:  # noqa: BLE001
        print(f"sitemap publish failed (non-fatal): {e!r}", file=sys.stderr)
    return f", published {len(done)}" if done else ""


def main() -> int:
    started = time.monotonic()
    if not SPOOL.is_dir():
        print("no spool yet, waiting for fetch_fragments")
        return 0
    pending = sorted(SPOOL.glob("*.json"))
    if not pending:
        print("nothing pending")
        return 0

    take, total = [], 0
    for p in pending:
        size = p.stat().st_size
        if take and total + size > MAX_APPLY_BYTES:
            break
        take.append(p)
        total += size

    results = _read(take)
    if not results:
        print(f"{len(take)} fragments held nothing to apply")
        for p in take:
            p.unlink(missing_ok=True)
        return 0
    with WORK.open("w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False)
    companies = len(results)
    results = None
    read_s = time.monotonic() - started

    logos = DB.with_name("company-logos.json")
    cmd = [sys.executable, str(ROOT / "loader" / "load_to_sqlite.py"),
           "--resolved", str(WORK), "--out", str(DB),
           "--drop-description", "--skip-vacuum", "--skip-known", "--box"]
    if logos.exists():
        cmd += ["--logos", str(logos)]
    load = subprocess.run(cmd, capture_output=True, text=True, timeout=1500)
    if load.stderr:
        print(load.stderr.strip())
    if load.returncode != 0:
        # The spool is left alone, so the next run retries exactly this
        # batch. Same posture as the Lambda's own "delete only after a
        # successful push".
        print(f"load_to_sqlite.py exited {load.returncode}", file=sys.stderr)
        return load.returncode

    for p in take:
        p.unlink(missing_ok=True)
    apply_s = time.monotonic() - started
    # The /stats and /facets answers, under this box's own prefix
    # (PRECOMPUTED_PREFIX, see precompute.py) so the Lambda's copies are
    # never touched while the two run side by side. Paced inside
    # publish: it looks at the artifact's age and leaves a fresh one.
    written = precompute.publish(BUCKET, DB, FRONTEND_BUCKET if PUBLISH_FRONTEND else "")
    published = _publish_frontend()
    print(f"applied {companies} companies from {len(take)} of {len(pending)} spooled fragments "
          f"({total / 1048576:.0f}MB): read {read_s:.1f}s, apply {apply_s:.1f}s, "
          f"precomputed {len(written)}{published}, total {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
