#!/usr/bin/env python3
"""
Local dev server for frontend/ -- serves the static files and proxies
/api/* to the real Lambda handler in-process (api/handler.py), against a
local jobs.db instead of S3. No AWS credentials, no deploy, no build step
-- this is how the frontend gets checked against real data before it ever
touches CloudFront.

    python loader/load_to_sqlite.py --resolved resolved.json --out jobs.db
    python scripts/dev_server.py --db jobs.db

Then open http://localhost:8000/. Same technique api/README.md documents
for testing the handler alone; this just adds a static file server in
front of it so the two can be exercised together.
"""

import argparse
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"
sys.path.insert(0, str(REPO_ROOT / "api"))


def make_handler(db_path: Path):
    import db as db_module

    def fake_get_connection():
        if db_module._conn is None:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            db_module._conn = conn
        return db_module._conn

    db_module.get_connection = fake_get_connection
    import handler as handler_module

    class DevHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"  {self.address_string()} - {fmt % args}")

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path.startswith("/api/"):
                self._serve_api(parsed)
            else:
                self._serve_static(parsed)

        def _serve_api(self, parsed):
            event = {
                "requestContext": {"http": {"method": "GET"}},
                "rawPath": parsed.path,
                "rawQueryString": parsed.query,
            }
            result = handler_module.lambda_handler(event, None)
            self.send_response(result["statusCode"])
            for k, v in result.get("headers", {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(result["body"].encode("utf-8"))

        def _serve_static(self, parsed):
            rel = parsed.path.lstrip("/") or "index.html"
            path = (FRONTEND_DIR / rel).resolve()
            if FRONTEND_DIR not in path.parents and path != FRONTEND_DIR:
                self.send_error(403)
                return
            if not path.is_file():
                self.send_error(404, f"no such file: {rel}")
                return
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".ttf": "font/ttf",
                ".txt": "text/plain; charset=utf-8",
            }.get(path.suffix, "application/octet-stream")
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
                     help="bind address -- default is loopback-only; use 0.0.0.0 (or a specific "
                          "interface IP, e.g. a Tailscale 100.x address) to reach this from other "
                          "devices on your network/tailnet. There's no auth on this dev server at "
                          "all, so only bind beyond loopback on a network you trust.")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"error: {args.db} does not exist -- build one first:", file=sys.stderr)
        print("  python loader/load_to_sqlite.py --resolved resolved.json --out jobs.db", file=sys.stderr)
        return 1

    handler_cls = make_handler(args.db)
    server = ThreadingHTTPServer((args.host, args.port), handler_cls)
    print(f"IL/JOBS dev server: http://{args.host}:{args.port}/  (db: {args.db})")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
