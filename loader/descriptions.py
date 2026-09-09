"""Job descriptions as individual S3 objects, separate from the index.

Measured: a job row's searchable/displayable metadata is 570 bytes, while
the full row averages 9,521 bytes on disk. Descriptions and their
overhead are ~94% of jobs-read.db, which is what makes that file 1.2GB
today and would make it 8.9GB at a million listings, past the 10GB
Lambda /tmp ceiling that every reader of it has to fit inside. Moving
them out takes the snapshot to ~73MB, which is what lets the merge run
in seconds instead of 70 and lets api/db.py stop pulling a gigabyte
inside a user's request.

Written only when the text actually changes, tracked by a hash on the
row. That matters more than it looks: a naive "write every description
on every load" would be ~1,600 PUTs per shard run and ~460,000 a day,
which is real money for no benefit. In steady state, with conditional
polling, most loads carry no descriptions at all.

Keys are descriptions/{job_id}.json, and job_id is already a stable hash
of {domain}|{ats}|{external_id} (see job_id in load_to_sqlite.py), so a
blob belongs to exactly one listing for that listing's whole life.
"""

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor

import boto3

PREFIX = "descriptions/"
WORKERS = 16


def description_sha(text: str | None) -> str | None:
    """Stable fingerprint of a description, or None when there isn't one.

    Stored on the row so the next load can tell "unchanged" from
    "changed" without reading the blob back out of S3.
    """
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def key_for(job_id: str) -> str:
    return f"{PREFIX}{job_id}.json"


def put_many(bucket: str, items: list[tuple[str, str]]) -> int:
    """Upload [(job_id, description)] concurrently. Returns the count written.

    Best-effort by design: a failed upload leaves the row's description
    in the database column (which this commit still populates) and the
    next load retries, so the worst case is a stale-but-present blob
    rather than a lost description. Never raises into the loader, since
    a description is not worth failing an ingest over.
    """
    if not bucket or not items:
        return 0
    s3 = boto3.client("s3")

    def put(item: tuple[str, str]) -> bool:
        job_id, text = item
        try:
            s3.put_object(
                Bucket=bucket,
                Key=key_for(job_id),
                Body=json.dumps({"id": job_id, "description": text}, ensure_ascii=False).encode("utf-8"),
                ContentType="application/json",
            )
            return True
        except Exception as e:
            print(f"description upload failed for {job_id} (non-fatal): {e!r}", file=sys.stderr)
            return False

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return sum(1 for ok in pool.map(put, items) if ok)


def get_one(bucket: str, job_id: str, s3=None) -> str | None:
    """Read one description back. Returns None for anything unusable, so
    callers can fall back to whatever they already had.
    """
    if not bucket or not job_id:
        return None
    try:
        client = s3 or boto3.client("s3")
        body = client.get_object(Bucket=bucket, Key=key_for(job_id))["Body"].read()
        return json.loads(body).get("description") or None
    except Exception:
        return None
