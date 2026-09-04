# api/

The Lambda behind `/api/*`. Two files, no framework, no dependencies
beyond `boto3` (already present in the Lambda Python runtime, so the
deploy zip doesn't need to vendor it):

- `db.py`: gets jobs.db from S3 onto the execution environment and
  keeps it fresh across warm invocations. See its docstring.
- `handler.py`: routes `/jobs`, `/jobs/{id}`, `/companies`, `/stats`,
  `/health` and runs the actual SQL. See its module docstring for why
  there are no write endpoints. `/jobs/{id}` is a stable permalink for one
  posting, unlike `job.url` (the actual ATS listing, which the ATS
  itself can 404 once a role closes), this always resolves and reports
  `closed_at` instead of disappearing, so a saved/shared link stays
  useful.

## Testing locally without AWS credentials

`db.get_connection()` talks to S3 directly, so testing the routing/query
logic without real AWS access means monkeypatching it to point at a local
SQLite file instead:

```python
import sys, sqlite3
sys.path.insert(0, "api")
import db as db_module

def fake_get_connection():
    if db_module._conn is None:
        conn = sqlite3.connect("path/to/a/jobs.db", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        db_module._conn = conn
    return db_module._conn

db_module.get_connection = fake_get_connection

import handler
event = {"requestContext": {"http": {"method": "GET"}}, "rawPath": "/api/jobs", "rawQueryString": "limit=3"}
print(handler.lambda_handler(event, None))
```

`db.py` still needs `DATA_BUCKET`/`DATA_KEY` env vars set and a boto3
client to construct successfully at import time even though the fake
never calls it. Dummy values are fine:

```
DATA_BUCKET=test DATA_KEY=jobs.db AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x AWS_DEFAULT_REGION=il-central-1
```

Build a local jobs.db to point at with `loader/load_to_sqlite.py --resolved resolved.json --out jobs.db` (no `--bucket` needed for local testing).

This is how the handler was actually validated during development,
including catching a real sort-direction bug (`age desc` was silently
sorting newest-first instead of oldest-first) and two ATS-specific date
format bugs upstream in `probe.py` (Lever epoch-ms, Recruitee's trailing
`" UTC"`) that `julianday()` silently returned `NULL` for instead of
erroring. Don't skip this step when the handler changes.
