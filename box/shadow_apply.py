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

One flag decides which of the two this is. While OTJ_PRIMARY is unset
the Lambda applier is still in charge and this does only what cannot
collide with it. Set it and this takes over the whole job: clearing
fragments from S3, evaluating alerts, writing the status files the site
reads, archiving closed listings and publishing the frontend's
artifacts. Everything behind that flag is something two appliers must
not both do, most obviously the alert digests, which would arrive
twice.

Runs from a systemd timer every minute. A run that finds nothing costs
one directory listing.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "api", ROOT / "loader"):
    sys.path.insert(0, str(_p))

import build_explore  # noqa: E402
import precompute  # noqa: E402
import sitemap  # noqa: E402
from deltas import PREFIX as DELTA_PREFIX, delete_fragments  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lock import exclusive  # noqa: E402

DB = Path(os.environ.get("DATA_PATH", "/var/lib/otj/jobs.db"))
BUCKET = os.environ["DATA_BUCKET"]
SPOOL = DB.parent / "deltas"
WORK = DB.with_name("delta-resolved.json")
# Bounded by bytes, like the Lambda, but without its memory ceiling to
# respect: the box parses into RAM too, and 2GB is the whole machine.
MAX_APPLY_BYTES = 96 * 1024 * 1024
# Whether this box is the only applier. See the module docstring.
PRIMARY = os.environ.get("OTJ_PRIMARY") == "1"
FRONTEND_BUCKET = os.environ.get("FRONTEND_BUCKET", "")
# Listings closed longer ago than this leave the snapshot for S3. Same
# number the Lambda applier uses, and for the same reason: every reader
# of a closed job works inside 14 days. Paced to once a day inside
# archive.py, not once per apply.
ARCHIVE_CLOSED_DAYS = 30
# Kept in step with loader/bootstrap.py's VIEWS, same as the Lambda
# handler keeps its own copy, and for the same reason: two lines.
BOOTSTRAP_VIEWS = {"bootstrap.json": {}, "bootstrap-il.json": {"country": "IL"}}


def _write_status(phase: str, detail: str = "") -> None:
    """merge-status.json, which the board's own status card and the
    countdown on it read through /api/pipeline-status. Same key and same
    shape as the Lambda applier writes, because it is the same file;
    only one of the two may write it, which is what PRIMARY decides.
    Never raises: a status write is not worth failing an apply over."""
    if not PRIMARY:
        return
    import boto3

    try:
        boto3.client("s3").put_object(
            Bucket=BUCKET, Key="merge-status.json", ContentType="application/json",
            Body=json.dumps({
                "phase": phase, "detail": detail,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }).encode("utf-8"),
        )
    except Exception as e:  # noqa: BLE001
        print(f"merge-status.json write failed (non-fatal): {e!r}", file=sys.stderr)


def _run_alerts() -> str:
    """Match new listings against saved filters and send the digests.

    The single most important thing on the other side of PRIMARY. Two
    appliers evaluating the same alerts send every digest twice, and a
    box that never evaluates them sends none at all while looking
    perfectly healthy, which is the failure nobody notices.

    watched-domains.json rides along because this is where the alert
    table is already being read: it tells the sweep which companies
    somebody is following so it can poll them more often (see
    loader/scrape_state.WATCHED_CEILING_S).
    """
    if not PRIMARY:
        return ""
    import boto3

    from alerts import evaluate_alerts

    _write_status("sending alerts", "matching new listings against saved filters")
    try:
        result = evaluate_alerts(DB)
    except Exception as e:  # noqa: BLE001
        print(f"alert evaluation failed (non-fatal): {e!r}", file=sys.stderr)
        return ", alerts failed"
    if result.get("errors"):
        print(f"alert evaluation errors: {result['errors']}", file=sys.stderr)
    domains = result.get("watched_domains")
    if domains is not None:
        # None means evaluate_alerts declined to run at all, and an
        # empty file would send every followed board back to the
        # four-hour polling ceiling.
        try:
            boto3.client("s3").put_object(
                Bucket=BUCKET, Key="watched-domains.json", ContentType="application/json",
                Body=json.dumps({
                    "domains": list(domains),
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }).encode("utf-8"),
            )
        except Exception as e:  # noqa: BLE001
            print(f"watched-domains.json write failed (non-fatal): {e!r}", file=sys.stderr)
    return (f", alerts {result.get('alerts_checked', 0)} checked"
            f"/{result.get('digests_sent', 0)} sent")


def _refresh_logos() -> None:
    """company-logos.json, which resolve_company_logos.py writes to S3 on
    its own schedule and the applier stamps onto the snapshot. Read-only
    and safe for either applier, so it is not behind PRIMARY: without it
    the box's logos are frozen at whenever it was seeded. Conditional on
    ETag, so a run that changes nothing costs one HEAD."""
    import boto3

    dest = DB.with_name("company-logos.json")
    tag = dest.with_suffix(".etag")
    try:
        s3 = boto3.client("s3")
        head = s3.head_object(Bucket=BUCKET, Key="company-logos.json")
        if dest.exists() and tag.exists() and tag.read_text(encoding="utf-8") == head["ETag"]:
            return
        s3.download_file(BUCKET, "company-logos.json", str(dest))
        tag.write_text(head["ETag"], encoding="utf-8")
        print(f"company-logos.json refreshed ({dest.stat().st_size} bytes)")
    except Exception as e:  # noqa: BLE001
        print(f"company-logos.json refresh skipped (non-fatal): {e!r}", file=sys.stderr)


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
    if not (PRIMARY and FRONTEND_BUCKET):
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
    with exclusive("apply") as got:
        if not got:
            # The snapshot job has the disk. The spool keeps filling and
            # the next tick, a minute from now, picks this up.
            return 0
        return _apply()


def _apply() -> int:
    started = time.monotonic()
    if not SPOOL.is_dir():
        print("no spool yet, waiting for fetch_fragments")
        return 0
    pending = sorted(SPOOL.glob("*.json"))
    if not pending:
        _write_status("idle", "no pending deltas")
        print("nothing pending")
        return 0
    _refresh_logos()

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

    _write_status("merging", f"applying {len(take)} of {len(pending)} delta fragments")
    logos = DB.with_name("company-logos.json")
    cmd = [sys.executable, str(ROOT / "loader" / "load_to_sqlite.py"),
           "--resolved", str(WORK), "--out", str(DB),
           "--drop-description", "--skip-vacuum", "--skip-known", "--box"]
    if logos.exists():
        cmd += ["--logos", str(logos)]
    if PRIMARY:
        # Caps the file instead of letting it grow forever. Paced to
        # once a day inside archive.py.
        #
        # --archive-bucket, never --bucket. --bucket drives the loader's
        # whole pull-modify-push cycle, so passing it here downloaded
        # the S3 snapshot over this box's live database on every apply,
        # replaced the 2.66GB file with the 745MB copy that has the
        # search index deliberately stripped, and eventually left the
        # file malformed when the API had it open. load_to_sqlite now
        # refuses --box with --bucket outright.
        cmd += ["--archive-bucket", BUCKET,
                "--archive-closed-days", str(ARCHIVE_CLOSED_DAYS)]
    load = subprocess.run(cmd, capture_output=True, text=True, timeout=1500)
    if load.stderr:
        print(load.stderr.strip())
    if load.returncode != 0:
        # The spool is left alone, so the next run retries exactly this
        # batch. Same posture as the Lambda's own "delete only after a
        # successful push".
        _write_status("error", f"load_to_sqlite.py exited {load.returncode}")
        print(f"load_to_sqlite.py exited {load.returncode}", file=sys.stderr)
        return load.returncode

    for p in take:
        p.unlink(missing_ok=True)
    cleared = 0
    if PRIMARY:
        # Only now, and only here: the rows are committed to a file that
        # is not going anywhere, so the queue can be cleared. While the
        # Lambda is still applying it owns this and the box must not,
        # or the Lambda would lose fragments the way the box used to.
        cleared = delete_fragments(BUCKET, [DELTA_PREFIX + p.name for p in take])
    apply_s = time.monotonic() - started
    # The /stats and /facets answers, under this box's own prefix
    # (PRECOMPUTED_PREFIX, see precompute.py) so the Lambda's copies are
    # never touched while the two run side by side. Paced inside
    # publish: it looks at the artifact's age and leaves a fresh one.
    alerts = _run_alerts()
    _write_status("precomputing", "answering /stats and /facets for the new snapshot")
    written = precompute.publish(BUCKET, DB, FRONTEND_BUCKET if PRIMARY else "")
    published = _publish_frontend()
    print(f"applied {companies} companies from {len(take)} of {len(pending)} spooled fragments "
          f"({total / 1048576:.0f}MB): read {read_s:.1f}s, apply {apply_s:.1f}s, "
          f"cleared {cleared}, precomputed {len(written)}{published}{alerts}, "
          f"total {time.monotonic() - started:.1f}s")
    _write_status("idle", f"last apply: {companies} companies from {len(take)} fragments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
