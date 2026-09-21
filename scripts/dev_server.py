#!/usr/bin/env python3
"""
Local dev server for frontend/. Serves the static files and proxies
/api/*, /job/* and /company/* to the real Lambda handler in-process
(api/handler.py), against a local jobs.db instead of S3.

The quickest way to a database is the live snapshot itself:

    aws s3 cp s3://iljobs-data-876913698688/jobs-read.db loader/jobs.db
    python scripts/dev_server.py

Then open http://localhost:8000/. With AWS credentials in the
environment the handler also reads descriptions and the precomputed
statistics from the real bucket, and /bootstrap.json and /facets.json
come from the frontend bucket; without them those fall back to the API
and the board still works. The handler needs three variables at import;
they are defaulted below to the deployed names, so nothing has to be
set for a read-only session.
"""

import argparse
import json
import os
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"
sys.path.insert(0, str(REPO_ROOT / "api"))

# The handler reads these at import. The deployed values, so a local
# session with credentials sees the real descriptions and statistics.
os.environ.setdefault("DATA_BUCKET", "iljobs-data-876913698688")
os.environ.setdefault("DATA_KEY", "jobs-read.db")
os.environ.setdefault("ALERTS_TABLE", "iljobs-alerts")
os.environ.setdefault("AWS_DEFAULT_REGION", "il-central-1")
FRONTEND_BUCKET = os.environ.get("FRONTEND_BUCKET", "iljobs-frontend-876913698688")
# Published by the maintenance Lambda into the frontend bucket, not in
# the repo. Fetched once per run and held.
PUBLISHED = {"/bootstrap.json": "application/json", "/facets.json": "application/json"}
_published_cache: dict[str, bytes | None] = {}
_s3_client = None
_s3_lock = __import__("threading").Lock()

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".json": "application/json",
    ".txt": "text/plain; charset=utf-8", ".xml": "application/xml",
    ".ttf": "font/ttf", ".woff2": "font/woff2",
    ".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml", ".ico": "image/x-icon",
}


def make_handler(db_path: Path):
    import db as db_module

    def fake_get_connection():
        if db_module._conn is None:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # The same SQL functions the real connection carries
            # (category_of and friends); every listings query calls them.
            from job_filters import register_functions
            register_functions(conn)
            db_module._conn = conn
        return db_module._conn

    db_module.get_connection = fake_get_connection
    import handler as handler_module

    # loader/precomputed/<name>, when present, stands in for the bucket's
    # precomputed/ objects: `python loader/precompute.py --db loader/jobs.db
    # --out loader/precomputed` writes them for the local snapshot, so the
    # facets and stats routes answer from files here too instead of
    # computing over the whole table on every page load.
    local_pre = REPO_ROOT / "loader" / "precomputed"
    if local_pre.is_dir():
        _bucket_precomputed = handler_module._precomputed_json

        def _local_precomputed(name):
            p = local_pre / name
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else _bucket_precomputed(name)

        handler_module._precomputed_json = _local_precomputed

    class DevHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"  {self.address_string()} - {fmt % args}")

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path.startswith(("/api/", "/job/", "/company/")):
                self._serve_api(parsed)
            elif parsed.path in PUBLISHED:
                self._serve_published(parsed.path)
            else:
                self._serve_static(parsed)

        def _serve_published(self, path):
            if path not in _published_cache:
                try:
                    # One client, made under a lock: two page requests
                    # building boto3 clients at once deadlocked the server.
                    with _s3_lock:
                        global _s3_client
                        if _s3_client is None:
                            import boto3
                            _s3_client = boto3.client("s3")
                    obj = _s3_client.get_object(Bucket=FRONTEND_BUCKET, Key=path.lstrip("/"))
                    _published_cache[path] = obj["Body"].read()
                except Exception as exc:  # no credentials or no object: app.js falls back to the API
                    print(f"  {path}: not served ({exc.__class__.__name__}), app.js will use the API")
                    _published_cache[path] = None
            body = _published_cache[path]
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", PUBLISHED[path])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            parsed = urlsplit(self.path)
            if not parsed.path.startswith("/api/"):
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            self._serve_api(parsed, method="POST", body=self.rfile.read(length).decode("utf-8") if length else "")

        def _serve_api(self, parsed, method="GET", body=None):
            event = {
                "requestContext": {"http": {"method": method}},
                "rawPath": parsed.path,
                "rawQueryString": parsed.query,
                "body": body,
            }
            result = handler_module.lambda_handler(event, None)
            self.send_response(result["statusCode"])
            for k, v in result.get("headers", {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(result["body"].encode("utf-8"))

        def _serve_static(self, parsed):
            rel = parsed.path.lstrip("/") or "index.html"
            # Same as the edge function in infra/cloudfront.tf: /board
            # is board.html. The redirects there are not reproduced, so
            # an old /?country=IL link shows the landing page locally.
            if "." not in rel.rsplit("/", 1)[-1]:
                rel += ".html"
            path = (FRONTEND_DIR / rel).resolve()
            if FRONTEND_DIR not in path.parents and path != FRONTEND_DIR:
                self.send_error(403)
                return
            if not path.is_file():
                self.send_error(404, f"no such file: {rel}")
                return
            content_type = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return DevHandler


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=REPO_ROOT / "loader" / "jobs.db")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1",
                     help="bind address. Default is loopback-only; use 0.0.0.0 or a specific "
                          "interface IP to reach this from other devices. No auth on this server, "
                          "so only bind beyond loopback on a network you trust.")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"error: {args.db} does not exist, build one first:", file=sys.stderr)
        print("  aws s3 cp s3://iljobs-data-876913698688/jobs-read.db loader/jobs.db", file=sys.stderr)
        return 1

    handler_cls = make_handler(args.db)
    server = ThreadingHTTPServer((args.host, args.port), handler_cls)
    print(f"OpenTechJobs dev server: http://{args.host}:{args.port}/  (db: {args.db})")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
