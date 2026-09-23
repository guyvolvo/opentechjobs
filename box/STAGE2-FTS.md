# Stage 2: move the search index into its own file

Not before cutover. Nothing here fixes a problem the box has today; it
removes the reason the box will need a bigger instance later.

## The problem it solves

The database is 2.66GB and the box has 1.8GB of RAM, so the working set
does not fit. Measured with `dbstat` before the re-seed:

| part | size |
|---|---|
| `jobs_fts_data`, the inverted index | 1,364 MB |
| `jobs` table | 552 MB |
| the three board indexes | ~116 MB |
| everything else | ~75 MB |

Half the file is the full-text index, and the board never reads it. A
listing page filters and sorts on `jobs` and its indexes, about 750MB
all told, which would sit in page cache permanently if the other 1.6GB
were not competing for the same memory. Every search does read it, but
searches are a minority of requests and an index probe is cheap even
cold.

So the two live in one file and evict each other. Splitting them means
the board's working set fits in RAM and stays there, and the search
index is read from disk when a search actually happens.

This is also the answer to the IOPS question. The box's sustained EBS
IOPS are the instance's, not the volume's (the volume already has 3,000
provisioned and 125 MB/s, which is never the limit). Fewer page cache
evictions means fewer reads means less of that budget spent, which is
cheaper than a larger instance and does not need one.

## What changes

`ATTACH DATABASE '/var/lib/otj/jobs-fts.db' AS fts`, and `jobs_fts`
moves there. SQLite can join and subquery across attached databases
freely, so every existing query shape still works:

    jobs.rowid IN (SELECT rowid FROM fts.jobs_fts WHERE fts.jobs_fts MATCH ?)

The rowid contract is the thing to be careful about. FTS5 rowids only
mean anything against the table they were built beside, and `jobs.rowid`
is assigned by insertion order in `jobs.db`. Moving the index to another
file does not change that, but it does mean two files must agree about
it, and nothing enforces it. Today they cannot disagree because a single
`ATTACH`-free transaction writes both.

Concretely:

1. `loader/fts_full.py` builds into `jobs-fts.db` rather than into the
   main file, and stamps `meta.fts_rowid_epoch` in both, a value that
   changes whenever `jobs` rowids are reassigned. Nothing reassigns them
   today, but `VACUUM` on the main file would, and so would any rebuild.
2. `api/job_filters.has_fts_index` gains a fourth check: the two epochs
   match. A mismatch means the index belongs to a different generation
   of the table, and the honest answer is `fts=False`, degrading to
   metadata search exactly as it does now on Lambda. Same rule as
   `fts_complete`: an index that might be wrong is not used.
3. `api/db.py` attaches the file read-only when `DATA_PATH` is set, and
   handles the file being absent by simply not attaching, which
   `has_fts_index` then reports as no index.
4. `load_to_sqlite.index_description` writes to the attached file. It
   already takes the column list; it gains the schema prefix.
5. `box/publish_snapshot.py` gets simpler: the main file no longer
   carries the index, so the drop-and-revacuum step introduced for the
   lean copy goes away. The published snapshot is just a `VACUUM INTO`.
6. Litestream replicates two files instead of one. Both need to be in
   `/etc/litestream.yml`, and a restore needs both. Worth a line in
   `box/CUTOVER.md`'s rollback section, since restoring only the main
   file gives a working board with no description search, which is a
   degraded state rather than an obvious failure.

## What it costs

Total bytes on disk are unchanged. What changes is which bytes compete
for memory.

A transaction can no longer span both files atomically in the way a
single file allows. In practice `index_description` and `upsert_job`
already run in one `with conn:` block, and splitting them means a crash
between the two leaves the index one row stale. That is recoverable and
already handled: a row whose text changed but whose index entry did not
is exactly what `description_sha` detects on the next poll.

## How to verify it worked

The measurement that justifies the work:

- `free -m` buff/cache holding steady with the board's queries served
  from it, rather than the current churn.
- Board endpoints unchanged in latency (they are already 0.07 to 0.4s).
- Search latency: expect it to get slightly *worse* cold, since the
  index is no longer resident, and stay the same warm.
- `EBSIOBalance%` over a normal day: the real target. If it stops
  drifting down under steady load, the instance size question is
  answered without spending anything.

## When to do it

After the cutover has been stable for a week and the Lambda stack is
retired. It touches the query layer, the loader, the API's connection
setup and the backup configuration at once, which is a bad thing to do
while the rollback path still matters.

The trigger to do it sooner: `EBSIOBalance%` trending to zero under
ordinary load, which would mean the applier cannot keep up, which is the
failure that made the Lambda stack unusable in the first place.
