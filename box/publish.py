"""Build the artifacts the site reads, on their own timer.

These used to run at the end of every apply, which was wrong on two
counts and broke the box on the second.

They are not part of applying deltas. An apply's job is to get new
listings into the database and it is finished the moment that commits;
whether a dashboard artifact is fifteen minutes old has nothing to do
with it, and making the apply wait made the apply the slowest of the
two things it was doing.

And their memory adds to the apply's. Measured on the box: the sitemap
build alone is 433MB and 392 seconds, on top of the ~350MB an apply
already holds from the fragments it parsed. Together that reached the
800MB cgroup limit, the apply never finished, and the spool stopped
draining, which is how the site's own freshness badge ended up reading
Offline while the site itself was perfectly healthy.

So they get their own process, their own memory ceiling and their own
cadence, and they take the same lock as the apply and the snapshot
(box/lock.py) so only one heavy job walks the database at a time.

Each piece paces itself internally: precompute against the age of what
it last wrote, the explore build and the sitemap hourly. Running this
every fifteen minutes therefore costs a few cheap checks most of the
time, and the pacing decides what actually gets rebuilt.

    python box/publish.py
"""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "api", ROOT / "loader", Path(__file__).resolve().parent):
    sys.path.insert(0, str(_p))

import build_explore  # noqa: E402
import precompute  # noqa: E402
import sitemap  # noqa: E402
from lock import exclusive  # noqa: E402

DB = Path(os.environ.get("DATA_PATH", "/var/lib/otj/jobs.db"))
BUCKET = os.environ["DATA_BUCKET"]
FRONTEND_BUCKET = os.environ.get("FRONTEND_BUCKET", "")
PRIMARY = os.environ.get("OTJ_PRIMARY") == "1"
# Kept in step with loader/bootstrap.py's VIEWS.
BOOTSTRAP_VIEWS = {"bootstrap.json": {}, "bootstrap-il.json": {"country": "IL"}}


def _bootstrap(s3) -> list[str]:
    done = []
    for name, filters in BOOTSTRAP_VIEWS.items():
        out = DB.with_name(name)
        cmd = [sys.executable, str(ROOT / "loader" / "bootstrap.py"),
               "--db", str(DB), "--out", str(out)]
        if filters.get("country"):
            cmd += ["--country", filters["country"]]
        try:
            build = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if build.returncode != 0:
                # Printed, not swallowed. This failed silently on every
                # apply from cutover until somebody ran it by hand,
                # because job_filters was not on the child's path.
                print(f"{name} build failed: exit {build.returncode}: "
                      f"{build.stderr.strip().splitlines()[-1] if build.stderr else ''}",
                      file=sys.stderr)
                continue
            s3.put_object(
                Bucket=FRONTEND_BUCKET, Key=name, Body=out.read_bytes(),
                ContentType="application/json",
                CacheControl="public, max-age=60, s-maxage=300, stale-while-revalidate=600",
            )
            done.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"{name} publish failed: {e!r}", file=sys.stderr)
    return done


def main() -> int:
    if not PRIMARY:
        print("not primary, nothing to publish")
        return 0
    with exclusive("publish") as got:
        if not got:
            # An apply or a snapshot has the disk. Everything here is
            # paced in minutes and the next tick is in fifteen.
            return 0
        return _publish()


def _publish() -> int:
    started = time.monotonic()
    import boto3

    s3 = boto3.client("s3")
    done: list[str] = []

    t = time.monotonic()
    written = precompute.publish(BUCKET, DB, FRONTEND_BUCKET)
    if written:
        done.append(f"precomputed {len(written)} in {time.monotonic() - t:.0f}s")

    if FRONTEND_BUCKET:
        t = time.monotonic()
        names = _bootstrap(s3)
        if names:
            done.append(f"bootstrap {len(names)} in {time.monotonic() - t:.0f}s")

        t = time.monotonic()
        try:
            explore = build_explore.publish(FRONTEND_BUCKET, DB, DB.parent)
            if explore:
                done.append(f"explore.db {explore['bytes']}B in {time.monotonic() - t:.0f}s")
        except Exception as e:  # noqa: BLE001
            print(f"explore.db failed: {e!r}", file=sys.stderr)

        t = time.monotonic()
        try:
            parts = sitemap.publish(FRONTEND_BUCKET, DB, DB.parent) or []
            if parts:
                done.append(f"sitemap {len(parts)} in {time.monotonic() - t:.0f}s")
        except Exception as e:  # noqa: BLE001
            print(f"sitemap failed: {e!r}", file=sys.stderr)

    print(f"published: {', '.join(done) if done else 'nothing was due'} "
          f"(total {time.monotonic() - started:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
