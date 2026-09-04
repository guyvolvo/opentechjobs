"""Auth provisioning: GitHub sign-in, and account creation for the
anonymous email-OTP path. Both need an admin-privileged backend because
aws_cognito_user_pool.main has allow_admin_create_user_only=true (no
public self-registration -- needed to keep GitHub's flow from being a
username-guessing account-creation oracle, see below), which as a side
effect blocks Cognito's own public SignUp API too. So a first-time
email-OTP sign-in needs this Lambda to create the account first, same as
GitHub, before the frontend can hand off to Cognito's native OTP APIs
directly.

Three unrelated jobs, dispatched by how this Lambda is invoked:

1. GET /api/auth/github/callback: exchanges GitHub's OAuth code for the
   user's GitHub identity, then drives Cognito's CUSTOM_AUTH flow via the
   Admin* APIs to mint a real Cognito session for that identity -- no
   GitHub-issued token ever reaches Cognito, because Cognito never sees
   GitHub at all. GitHub has no OIDC discovery document and its OAuth
   token endpoint never returns an id_token -- Cognito's OIDC identity
   provider type needs one, so GitHub can't be wired in as a normal
   federated provider (see infra/cognito.tf's header comment).

2. POST /api/auth/email/start: get-or-create a Cognito user keyed by
   email (username_attributes=["email"] on the pool, so email doubles as
   the Cognito username directly). Cognito's own EMAIL_OTP challenge --
   sending the code, verifying it -- happens entirely client-side after
   this from the browser calling Cognito's public InitiateAuth/
   RespondToAuthChallenge directly; this endpoint only has to make sure
   the account row exists first.

3. Cognito Lambda triggers (DefineAuthChallenge / CreateAuthChallenge /
   VerifyAuthChallengeResponse, wired via aws_cognito_user_pool.main's
   lambda_config): implement one single-round CUSTOM_CHALLENGE whose
   answer is a random token generated and consumed entirely inside job 1's
   own Lambda invocation, never transmitted to or knowable by anyone else
   -- including a legitimate browser calling Cognito's PUBLIC (non-admin)
   InitiateAuth directly with the same USERNAME, which reaches these same
   triggers but can never supply the matching answer. That's what makes
   this secure without a stateful nonce store: the "shared secret" never
   leaves this Lambda's memory. Native EMAIL_OTP (job 2's follow-up) is a
   built-in Cognito factor, not a CUSTOM_CHALLENGE, so it never reaches
   these triggers at all -- no interaction between the two flows.
"""

import json
import os
import secrets
import urllib.parse
import urllib.request

import boto3

GITHUB_CLIENT_ID = os.environ["GITHUB_CLIENT_ID"]
GITHUB_CLIENT_SECRET = os.environ["GITHUB_CLIENT_SECRET"]
POOL_NAME = os.environ["POOL_NAME"]  # e.g. "iljobs-users"
SITE_ORIGIN = os.environ["SITE_ORIGIN"]  # https://openmarket.guyvoloshin.com, no trailing slash

cognito = boto3.client("cognito-idp")

# Not passed in as an env var: infra/cognito.tf's user pool needs this
# Lambda's ARN for its triggers, and this Lambda needing the pool's ID
# back would make that a genuine Terraform dependency cycle (each
# resource referencing the other's computed attribute). Looked up once
# per cold start instead, by the pool's own (unique, stable) name --
# Cognito's own trigger events carry userPoolId directly, so this lookup
# only matters for the HTTP callback path below.
_pool_id = None
_app_client_id = None


def _pool_and_client_ids() -> tuple[str, str]:
    global _pool_id, _app_client_id
    if _pool_id is None:
        pools = cognito.list_user_pools(MaxResults=60)["UserPools"]
        _pool_id = next(p["Id"] for p in pools if p["Name"] == POOL_NAME)
        clients = cognito.list_user_pool_clients(UserPoolId=_pool_id, MaxResults=10)["UserPoolClients"]
        _app_client_id = clients[0]["ClientId"]  # exactly one app client (aws_cognito_user_pool_client.web)
    return _pool_id, _app_client_id


def _post_form(url: str, fields: dict, headers: dict) -> dict:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, headers: dict) -> object:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _redirect(fragment: str) -> dict:
    return {
        "statusCode": 302,
        "headers": {"Location": f"{SITE_ORIGIN}/#{fragment}"},
        "body": "",
    }


def _json_response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _ensure_cognito_user(pool_id: str, username: str, email: str) -> None:
    """Get-or-create, shared by the GitHub and email-start paths. A
    freshly created user's password is random, set once here, and never
    stored or reused -- see the module docstring on why every account in
    this pool has one despite nothing ever authenticating with it.
    """
    try:
        cognito.admin_get_user(UserPoolId=pool_id, Username=username)
    except cognito.exceptions.UserNotFoundException:
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=username,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",
        )
        cognito.admin_set_user_password(
            UserPoolId=pool_id,
            Username=username,
            Password=secrets.token_urlsafe(48) + "aA1!",
            Permanent=True,
        )


def _handle_email_start(event: dict) -> dict:
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return _json_response(400, {"error": "invalid_json"})

    email = (body.get("email") or "").strip().lower()
    # Light sanity check only -- Cognito itself is the real validator
    # (admin_create_user rejects a genuinely malformed address), this
    # just avoids a wasted API round-trip on an obviously-empty value.
    if "@" not in email or len(email) > 254:
        return _json_response(400, {"error": "invalid_email"})

    pool_id, _ = _pool_and_client_ids()
    # allow_admin_create_user_only=true means this account-creation
    # oracle only accepts a caller who already knows one real account's
    # existence is being asked about -- but this call itself doesn't
    # verify the caller OWNS that email; that verification happens next,
    # client-side, when Cognito's own EMAIL_OTP actually emails a code
    # only the real owner can read. Worst case for a wrong email here is
    # an unusable pending account row, not an account takeover.
    _ensure_cognito_user(pool_id, email, email)
    return _json_response(200, {"ok": True})


def _handle_callback(event: dict) -> dict:
    params = event.get("queryStringParameters") or {}
    if params.get("error"):
        return _redirect(f"auth_error={urllib.parse.quote(params['error'])}")

    code = params.get("code")
    if not code:
        return _redirect("auth_error=missing_code")

    # Exchange the one-time code for a GitHub access token. Single-use,
    # short-lived, tied to our client_id+client_secret+redirect_uri --
    # this, not a CSRF `state` round-trip, is the real proof a human just
    # completed GitHub's own consent screen. See the module docstring's
    # security note; a full stateful state-nonce store was judged not
    # worth the extra infra for what a login-CSRF could actually expose
    # here (an alert-preferences account tied to nothing sensitive).
    token_resp = _post_form(
        "https://github.com/login/oauth/access_token",
        {
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
        },
        {"Accept": "application/json"},
    )
    access_token = token_resp.get("access_token")
    if not access_token:
        return _redirect("auth_error=github_token_exchange_failed")

    gh_headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "openmarketil-auth/1.0",
    }
    profile = _get_json("https://api.github.com/user", gh_headers)
    gh_id = profile["id"]

    # profile["email"] is often null (a private profile email), so this
    # needs the dedicated emails endpoint -- user:email scope covers it
    # even when nothing is public.
    emails = _get_json("https://api.github.com/user/emails", gh_headers)
    primary = next((e for e in emails if e.get("primary") and e.get("verified")), None)
    email = primary["email"] if primary else None
    if not email:
        return _redirect("auth_error=no_verified_github_email")

    username = f"github_{gh_id}"
    pool_id, app_client_id = _pool_and_client_ids()
    _ensure_cognito_user(pool_id, username, email)

    verified_token = secrets.token_urlsafe(32)
    init = cognito.admin_initiate_auth(
        UserPoolId=pool_id,
        ClientId=app_client_id,
        AuthFlow="CUSTOM_AUTH",
        AuthParameters={"USERNAME": username},
        ClientMetadata={"verified_token": verified_token},
    )
    result = cognito.admin_respond_to_auth_challenge(
        UserPoolId=pool_id,
        ClientId=app_client_id,
        ChallengeName=init["ChallengeName"],
        Session=init["Session"],
        ChallengeResponses={"USERNAME": username, "ANSWER": verified_token},
        ClientMetadata={"verified_token": verified_token},
    )
    tokens = result["AuthenticationResult"]
    return _redirect(
        "id_token={}&access_token={}&refresh_token={}".format(
            tokens["IdToken"], tokens["AccessToken"], tokens["RefreshToken"]
        )
    )


def _handle_define_auth_challenge(event: dict) -> dict:
    session = event["request"]["session"]
    if not session:
        event["response"]["challengeName"] = "CUSTOM_CHALLENGE"
        event["response"]["issueTokens"] = False
        event["response"]["failAuthentication"] = False
    elif session[-1]["challengeName"] == "CUSTOM_CHALLENGE" and session[-1]["challengeResult"] is True:
        event["response"]["issueTokens"] = True
        event["response"]["failAuthentication"] = False
    else:
        # Also the fallback for a bare public InitiateAuth attempt with no
        # legitimate answer available -- fails closed, not open.
        event["response"]["issueTokens"] = False
        event["response"]["failAuthentication"] = True
    return event


def _handle_create_auth_challenge(event: dict) -> dict:
    # Only ever non-empty when this trigger fires from THIS Lambda's own
    # admin_initiate_auth call above, same invocation chain, milliseconds
    # apart -- a separate caller hitting the public InitiateAuth API has
    # no way to populate this, so their challenge's expected answer is
    # always empty, which _handle_verify_auth_challenge_response below
    # never accepts as a match.
    metadata = event["request"].get("clientMetadata") or {}
    token = metadata.get("verified_token", "")
    event["response"]["publicChallengeParameters"] = {}
    event["response"]["privateChallengeParameters"] = {"expectedAnswer": token}
    event["response"]["challengeMetadata"] = "GITHUB_VERIFIED"
    return event


def _handle_verify_auth_challenge_response(event: dict) -> dict:
    expected = event["request"]["privateChallengeParameters"].get("expectedAnswer", "")
    given = event["request"]["challengeAnswer"]
    event["response"]["answerCorrect"] = bool(expected) and secrets.compare_digest(given, expected)
    return event


_TRIGGER_HANDLERS = {
    "DefineAuthChallenge_Authentication": _handle_define_auth_challenge,
    "CreateAuthChallenge_Authentication": _handle_create_auth_challenge,
    "VerifyAuthChallengeResponse_Authentication": _handle_verify_auth_challenge_response,
}


def lambda_handler(event, context):
    trigger_source = event.get("triggerSource")
    if trigger_source in _TRIGGER_HANDLERS:
        return _TRIGGER_HANDLERS[trigger_source](event)
    # Not a Cognito trigger event -- an API Gateway HTTP route instead,
    # two of them sharing this one Lambda.
    path = event.get("rawPath", "")
    if path.endswith("/auth/email/start"):
        return _handle_email_start(event)
    return _handle_callback(event)
