"""Changed-company results as small S3 fragments, instead of partition rewrites.

The sweep's write cost was the thing blocking single-digit-minute
freshness. Polling every company is cheap now (mostly bodiless 304s), but
persisting the result meant a 48MB pull-modify-push per partition the
sweep touched, at 50-170s each. A run that wanted 18 of them completed 1
and threw the rest away.

Polling and writing scale differently, and partitions only ever made
sense for writing. A fragment holds just the companies that actually
changed on one sweep, so it is kilobytes in steady state and the sweep
never opens a database at all.

Ordering is by key. Fragment names are {iso-timestamp}-{run}.json, which
sorts chronologically as a string, so the applier replays them in the
order they were produced. That matters: two sweeps can both carry the
same company, and the later one has to win.

Fragments are deleted only after the applier has successfully pushed a
snapshot containing them. A crash mid-apply leaves them in place to be
retried, so the failure mode is doing the same work twice rather than
losing a listing. Re-applying is harmless: load_resolved is an upsert.
"""

import json
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3

PREFIX = "deltas/"
WORKERS = 8

# One applier pass will not swallow more than this. A backlog bigger than
# it simply carries over, keeping any single run bounded rather than
# letting a long outage produce one enormous unschedulable catch-up.
MAX_FRAGMENTS_PER_RUN = 400


def put_fragment(bucket: str, results: list[dict]) -> str | None:
    """Write one sweep's changed companies. Returns the key, or None.

    Only companies with real results are written: an unchanged company
    carries no jobs and nothing to apply, so including it would just make
    every fragment the size of the company list.
    """
    if not bucket:
        return None
    payload = [r for r in results if r.get("ats") and not r.get("unchanged")]
    if not payload:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    key = f"{PREFIX}{stamp}-{uuid.uuid4().hex[:8]}.json"
    boto3.client("s3").put_object(
        Bucket=bucket, Key=key,
        Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def list_fragments(bucket: str, limit: int = MAX_FRAGMENTS_PER_RUN) -> list[str]:
    """Pending fragment keys, oldest first."""
    if not bucket:
        return []
    s3 = boto3.client("s3")
    keys: list[str] = []
    token = None
    while len(keys) < limit:
        kw = {"Bucket": bucket, "Prefix": PREFIX, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in resp.get("Contents", []) if o["Key"].endswith(".json")]
        if not resp.get("IsTruncated"):
            break
        token = resp["NextContinuationToken"]
    return sorted(keys)[:limit]


def list_fragments_sized(bucket: str, limit: int = MAX_FRAGMENTS_PER_RUN) -> list[tuple[str, int]]:
    """Pending fragments as (key, bytes), oldest first.

    Same listing as above, keeping the size S3 already returns. The
    applier budgets by bytes rather than by count because a fragment is
    as big as whatever the sweep found: 13MB on a quiet cycle and 100MB
    after an outage, and it is the bytes that decide whether the apply
    fits in memory.
    """
    if not bucket:
        return []
    s3 = boto3.client("s3")
    out: list[tuple[str, int]] = []
    token = None
    while len(out) < limit:
        kw = {"Bucket": bucket, "Prefix": PREFIX, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        out += [(o["Key"], o.get("Size", 0)) for o in resp.get("Contents", [])
                if o["Key"].endswith(".json")]
        if not resp.get("IsTruncated"):
            break
        token = resp["NextContinuationToken"]
    return sorted(out)[:limit]


def read_fragments(bucket: str, keys: list[str]) -> list[dict]:
    """Every result across these fragments, in key order.

    Concatenated rather than merged: load_resolved is an upsert keyed on
    job id, so replaying an older entry before a newer one for the same
    company lands on the newer one, which is what key order guarantees.
    """
    if not keys:
        return []
    s3 = boto3.client("s3")

    def read(key: str) -> tuple[str, list[dict]]:
        try:
            return key, json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        except Exception as e:
            print(f"couldn't read {key} (skipped): {e!r}", file=sys.stderr)
            return key, []

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        by_key = dict(pool.map(read, keys))
    out: list[dict] = []
    for key in keys:            # keys are already sorted; preserve that order
        out.extend(by_key.get(key) or [])
    return out


def delete_fragments(bucket: str, keys: list[str]) -> int:
    """Drop fragments the applier has finished with. Called only after a
    snapshot containing them is safely pushed.
    """
    if not bucket or not keys:
        return 0
    s3 = boto3.client("s3")
    deleted = 0
    for i in range(0, len(keys), 1000):        # DeleteObjects' own hard limit
        chunk = keys[i:i + 1000]
        try:
            resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in chunk]})
            deleted += len(resp.get("Deleted", []))
        except Exception as e:
            print(f"couldn't delete {len(chunk)} fragments (they'll be retried): {e!r}", file=sys.stderr)
    return deleted
