# Cutover runbook

Moving the API and the applier from Lambda to the box. Every command
here has been run against the real box except the ones marked as the
cutover itself, and the unit and path names are copied from the machine
rather than remembered.

The traffic switch is one Cloudflare rule and the rollback is deleting
it. Everything else is preparation and ordering.

## Order matters, and here is why

The dangerous window is the one where both appliers are live. They read
the same delta queue and the same alert table, so if both run they both
delete fragments the other needed and both send every digest. The flag
`OTJ_PRIMARY` is what stops the box doing those things, so the Lambda
has to stop *before* the flag goes on, not after.

Traffic moves last. Until the Cloudflare rule exists, the box can be
fully in charge of the data while the live site still reads the
Lambda's snapshot, which is a safe place to sit and check things.

## Before you start

    # row parity against the live snapshot
    sqlite3 /var/lib/otj/jobs.db "select count(*) from jobs;"
    curl -s https://opentechjobs.org/api/health | python3 -c "import sys,json;print(json.load(sys.stdin)['jobs_total'])"

    # the two rehearsals, both green
    sudo -u ubuntu bash -c 'set -a; . /etc/otj-api.env; set +a; SNAPSHOT_KEY=backups/test-snapshot.db /srv/otj/venv/bin/python /srv/otj/app/box/publish_snapshot.py'
    litestream restore -o /tmp/restore-test.db -config /etc/litestream.yml /var/lib/otj/jobs.db
    sqlite3 /tmp/restore-test.db "pragma quick_check; select count(*) from jobs;"

## 1. Stop the Lambda applier

Disable the schedule, then wait for anything in flight. The function
stays deployed, which is what makes the rollback in the last section
possible.

    aws events disable-rule --name iljobs-scrape-maintenance-schedule
    # wait until no invocation is running
    aws logs tail /aws/lambda/iljobs-scrape-maintenance --since 10m --format short | tail -3

## 2. Let the box catch up

The fetcher has been spooling all along, so the box already holds every
fragment the Lambda has not deleted. Give it a few minutes to drain
before it becomes the only writer.

    ls /var/lib/otj/deltas/*.json | wc -l      # should fall to 0
    sudo journalctl -u otj-apply -n 5 --no-pager

## 3. Make the box primary

Five env changes, not one. `OTJ_PRIMARY` turns on alerts, fragment
deletion, archiving and the status files; `FRONTEND_BUCKET` is what the
bootstrap, explore and sitemap publishers write to;
`PRECOMPUTED_PREFIX` has to go, because it currently points at
`precomputed-box/` so the two appliers could not overwrite each other,
and from here the box should write the real one.

`COGNITO_ISSUER` and `COGNITO_AUDIENCE` are the two the first cutover
missed, and missing them is silent. The API Gateway used to verify the
bearer token before the Lambda ever saw it; on the box that job belongs
to `_claims()` in `api/serve.py`, which returns None the moment either
is unset. Every `/api/me/*` route then answers 401 with nothing in the
log, so saved jobs, alerts and the CV analyser all come back empty
rather than broken, and the pages say so in their own words. It ran
that way from the cutover until 2026-09-24.

    sudo sed -i '/^PRECOMPUTED_PREFIX=/d' /etc/otj-api.env
    printf 'OTJ_PRIMARY=1\nFRONTEND_BUCKET=iljobs-frontend-876913698688\n' | sudo tee -a /etc/otj-api.env
    printf 'COGNITO_ISSUER=https://cognito-idp.il-central-1.amazonaws.com/il-central-1_0vIcqqXyg\nCOGNITO_AUDIENCE=5021pv23cp3udp1uaq34tp38mb\n' | sudo tee -a /etc/otj-api.env
    sudo systemctl restart otj-api
    sudo systemctl start otj-apply.service     # force one now rather than waiting for the timer
    sudo journalctl -u otj-apply -n 20 --no-pager

Expect the apply line to report `cleared N`, `alerts N checked`, and
`published N`. If it reports `cleared 0` the flag did not take.

The box's own IAM role needs the same treatment, and for the same
reason: it was written for the alert evaluator, which only ever scans
alerts and stamps `last_notified_at`, so it carries Scan, UpdateItem,
GetItem and Query. The API routes the cutover moved onto the box also
write and delete, and the Lambda they came from had both
(`infra/lambda.tf`). Without them, saving a profile, creating an alert
and un-saving a job each return 500 while every read still works, so the
account page looks alive and quietly refuses every change. Fixed
2026-09-24.

    aws iam get-role-policy --role-name otj-box-experiment       --policy-name primary-applier --profile openmarket-tf       --query "PolicyDocument.Statement[?contains(to_string(@),'dynamodb')].Action" --output text
    # expect: DeleteItem GetItem PutItem Query Scan UpdateItem

The role is not in terraform. A rebuilt box gets none of this.

Check the auth separately, because nothing above covers it. Signed in,
`/api/me/saved` answers 200; the log shows `jwt rejected: ...` for a
bad token and nothing at all for a good one. A run of 401s with no
`jwt rejected` line beside them means the two Cognito variables are
missing again.

    journalctl -u otj-api --since '-1h' | grep '/api/me/' | grep -c ' 401 '

## 4. Turn on the snapshot publisher

Hourly during the rollback window, not daily. A rollback restores the
Lambda to whatever is in `jobs-read.db`, so that file's age is the data
you would lose, and the deltas that would have filled the gap have been
deleted by the box. Hourly makes the worst case an hour. Daily is
correct only once the Lambda is retired.

    sudo mkdir -p /etc/systemd/system/otj-snapshot.timer.d
    printf '[Timer]\nOnCalendar=\nOnCalendar=hourly\n' | sudo tee /etc/systemd/system/otj-snapshot.timer.d/rollback-window.conf
    sudo systemctl daemon-reload
    sudo systemctl enable --now otj-snapshot.timer

Company names (added 2026-09-25). Names are resolved into
company-names.json in S3 and were only ever applied by the Lambda merge;
the box needs its own pass or every name resolved after cutover goes
nowhere, which is how 89% of listings came to show a bare domain.

    sudo tee /etc/systemd/system/otj-names.service >/dev/null <<'EOF'
    [Unit]
    Description=OpenTechJobs: resolve company names and stamp them on the box
    [Service]
    Type=oneshot
    User=ubuntu
    WorkingDirectory=/srv/otj/app
    EnvironmentFile=/etc/otj-api.env
    ExecStart=/bin/sh -c '/srv/otj/venv/bin/python resolve_company_names.py --bucket "$DATA_BUCKET" && /srv/otj/venv/bin/python box/apply_company_names.py'
    EOF
    sudo tee /etc/systemd/system/otj-names.timer >/dev/null <<'EOF'
    [Unit]
    Description=Daily company-name resolve and apply
    [Timer]
    OnCalendar=*-*-* 04:10:00 UTC
    Persistent=true
    [Install]
    WantedBy=timers.target
    EOF
    sudo systemctl daemon-reload && sudo systemctl enable --now otj-names.timer
    sudo systemctl start otj-snapshot.service
    sudo journalctl -u otj-snapshot -n 5 --no-pager

## 5. Move the traffic

A Cloudflare origin rule, on the `opentechjobs.org` zone. The paths the
box serves:

    /api/*        except /api/auth/*
    /job/*
    /company/*

There is no `/api/v1`. The API has never carried a version prefix, and
a rule written against one matches nothing, which looks exactly like a
cutover that silently did not happen.

`/api/auth/github/callback` and `/api/auth/email/start` belong to a
different Lambda behind API Gateway and must keep going to CloudFront.
Excluding them is the whole reason this stays simple.

Expression:

    (http.host eq "opentechjobs.org" and
     (starts_with(http.request.uri.path, "/api/") or
      starts_with(http.request.uri.path, "/job/") or
      starts_with(http.request.uri.path, "/company/")) and
     not starts_with(http.request.uri.path, "/api/auth/"))

Origin: `box.opentechjobs.org`.

## 6. Check

    for p in "/api/health" "/api/jobs?limit=5&roles=tech" "/api/jobs?limit=5&search=grpc" "/api/facets?country=IL" "/api/stats"; do
      curl -s -o /dev/null -w "$p %{http_code} %{time_total}s\n" "https://opentechjobs.org$p&_=$RANDOM"
    done

`search=grpc` is the one to watch: it returns 0 on the Lambda stack and
about 1,358 on the box, so it tells you which origin answered without
looking at a header.

Then sign in on the live site, which exercises `/api/auth/*` going to
CloudFront and `/api/me/*` going to the box with a Cognito token the
box verifies itself.

## Rollback

Traffic first, which is instant and fixes the visible thing:

    # delete the Cloudflare origin rule

Then hand the data back:

    sudo sed -i '/^OTJ_PRIMARY=/d' /etc/otj-api.env
    sudo systemctl restart otj-api
    aws events enable-rule --name iljobs-scrape-maintenance-schedule

The Lambda resumes from `jobs-read.db`, which the box has been
publishing hourly, so it starts at most an hour stale and catches up
from the deltas written since. The box keeps running and keeps
spooling, so nothing has to be rebuilt to try again.

## After a week

- Retire `iljobs-api` and `iljobs-scrape-maintenance`, and drop the
  maintenance packaging from `deploy-scrape-lambda.yml`.
- Retire `deploy-api.yml`; `deploy-box.yml` already covers the same
  paths.
- Put the snapshot publisher back on daily by removing the drop-in.
- Import the box into Terraform and replace `var.box_instance_id` with
  `aws_instance.box.id`.
