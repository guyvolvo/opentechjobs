"""EventBridge-triggered Lambda that dispatches a GitHub Actions workflow
via the API, replacing that workflow's own `schedule:` trigger.

GitHub's `schedule:` cron trigger is unreliable on this repo -- already
documented once (scrape_handler.py's own docstring, citing
https://github.com/orgs/community/discussions/147369) for scrape-fast.yml,
and confirmed again 2026-09-08 for merge-discovered-companies.yml: its
cron was tightened to */10 minutes, but its last SCHEDULED run sat
~5 hours stale while workflow_dispatch runs kept working fine on demand.
Same fix as last time: something with a real SLA (EventBridge) triggers
the work instead of trusting GitHub's own scheduler, calling
`workflow_dispatch` (the same entry point a manual `gh workflow run`
uses) rather than reimplementing what the workflow does.

One Lambda, not one per workflow: which workflow to dispatch comes from
the EventBridge rule's own `input` JSON (`{"workflow_file": "..."}`), so
adding a third scheduled workflow later needs only a new EventBridge
rule, not new code here.

The dispatch token (a fine-grained PAT scoped to this one repo,
Actions: Read and write only) lives in SSM Parameter Store, not
anywhere in the repo or this Lambda's own deployment package.
"""

import json
import os
import urllib.request

import boto3

REPO = "guyvolvo/opentechjobs"
TOKEN_PARAM = "/iljobs/github-dispatch-token"


def _get_token() -> str:
    ssm = boto3.client("ssm")
    resp = ssm.get_parameter(Name=TOKEN_PARAM, WithDecryption=True)
    return resp["Parameter"]["Value"]


def lambda_handler(event, context):
    workflow_file = event.get("workflow_file")
    if not workflow_file:
        raise ValueError("event must carry {'workflow_file': '<name>.yml'} -- see the EventBridge rule's own input")

    ref = event.get("ref", "main")
    token = _get_token()

    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{workflow_file}/dispatches"
    body = json.dumps({"ref": ref}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "iljobs-dispatch-lambda",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        # GitHub returns 204 on success with no body; anything else here
        # is a real failure worth surfacing in CloudWatch, not swallowing.
        raise RuntimeError(f"dispatching {workflow_file} failed: {e.code} {e.read().decode(errors='replace')}")

    print(f"dispatched {workflow_file} on {ref}: HTTP {status}")
    return {"workflow_file": workflow_file, "status": status}
