#!/usr/bin/env bash
# Rebuild the box's database from the live snapshot, in place, without
# losing anything the spool is holding.
#
# Two reasons to run it. The box lost about 15,600 listings to fragments
# the Lambda deleted before fetch_fragments.py existed, and those are
# gone from S3, so the only way back to parity is to take the live
# snapshot and start again. And this is the cutover rehearsal: every
# step the real cutover needs, in order, against a box nothing depends
# on yet.
#
# The fetcher keeps running throughout. That is the point of the spool:
# whatever the sweep produces while this is going lands on disk, and the
# applier picks it up afterwards. Re-applying a fragment the live
# snapshot already contains is harmless, since load_resolved is an
# upsert keyed on job id.
#
#   sudo -u ubuntu bash box/reseed.sh
set -euo pipefail

DB=/var/lib/otj/jobs.db
NEW=/var/lib/otj/jobs.db.new
BUCKET=iljobs-data-876913698688
export AWS_DEFAULT_REGION=il-central-1
cd /srv/otj/app
set -a; . /etc/otj-api.env; set +a

say() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

say "stopping the applier (the fetcher keeps spooling)"
sudo systemctl stop otj-apply.timer
while pgrep -f "loader/load_to_sqlite.py" >/dev/null; do sleep 10; done

say "pulling the live snapshot"
rm -f "$NEW"
time aws s3 cp "s3://$BUCKET/jobs-read.db" "$NEW" --only-show-errors
ls -l "$NEW" | awk '{print "  " $5 " bytes"}'

say "checking what arrived"
sqlite3 "$NEW" "select 'rows ' || count(*) from jobs;"
sqlite3 "$NEW" "pragma quick_check;" | head -1

say "box migration: category column, canonical posted_at, board indexes"
echo "[]" > /tmp/empty-resolved.json
time /srv/otj/venv/bin/python loader/load_to_sqlite.py \
    --resolved /tmp/empty-resolved.json --out "$NEW" \
    --drop-description --skip-vacuum --skip-known --box 2>&1 | grep -viE "share this exact|CLUSTERING" | tail -5

say "building the five-column search index (this is the long part)"
time /srv/otj/venv/bin/python loader/fts_full.py --db "$NEW" --bucket "$BUCKET"

say "what the new file claims"
sqlite3 "$NEW" "select key || '=' || value from meta where key in ('fts_complete','posted_at_utc','board_indexes','fts_retired');"
sqlite3 "$NEW" "select 'fts rows ' || count(*) from jobs_fts_docsize;"

say "swapping it in"
sudo systemctl stop litestream otj-api
mv "$DB" /var/lib/otj/jobs.db.old
rm -f /var/lib/otj/jobs.db.old-wal /var/lib/otj/jobs.db.old-shm
mv "$NEW" "$DB"
rm -f "$DB-wal" "$DB-shm"
sqlite3 "$DB" "pragma journal_mode=wal; pragma journal_size_limit=67108864;"
sudo systemctl start otj-api
sleep 3
curl -s -o /dev/null -w "  api %{http_code} %{time_total}s\n" http://127.0.0.1:8000/api/health

say "litestream starts a fresh generation against the new file"
sudo systemctl start litestream

say "applier back on; it will drain whatever the spool collected"
sudo systemctl start otj-apply.timer
ls /var/lib/otj/deltas/*.json 2>/dev/null | wc -l | awk '{print "  " $1 " fragments spooled while this ran"}'

say "done. old file kept at /var/lib/otj/jobs.db.old until you delete it"
