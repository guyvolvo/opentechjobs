"""WSGI entry for the box: `gunicorn serve:app` from this directory.

handler.py speaks API Gateway's HTTP API v2 event shape and nothing
else, on purpose: every route was validated against it, and the same
handler keeps running on Lambda until the box has earned the cutover.
So this turns a WSGI request into that event, calls lambda_handler, and
writes its answer back. No framework.

Two things API Gateway did that a plain WSGI server does not:

- Authorization. The /api/me/* routes read their claims from
  requestContext.authorizer.jwt.claims, which the gateway filled in
  after validating the Cognito ID token. Here the bearer token is
  verified against the pool's JWKS (COGNITO_ISSUER, COGNITO_AUDIENCE)
  and the claims are put in the same place, so the handler is
  unchanged. A missing or bad token on those routes answers 401 before
  the handler runs, as the gateway did. With the two variables unset,
  every /api/me route is 401: safe, not broken.
- The 29 second timeout. Not reproduced. That ceiling is what turned
  a slow download into an outage.
"""

import base64
import os
from http import HTTPStatus

from handler import lambda_handler

COGNITO_ISSUER = os.environ.get("COGNITO_ISSUER", "")
COGNITO_AUDIENCE = os.environ.get("COGNITO_AUDIENCE", "")

_jwks = None


def _claims(environ: dict) -> dict | None:
    """Verified claims from the request's bearer token, or None."""
    global _jwks
    auth = environ.get("HTTP_AUTHORIZATION", "")
    if not auth.startswith("Bearer ") or not (COGNITO_ISSUER and COGNITO_AUDIENCE):
        return None
    try:
        import jwt

        if _jwks is None:
            _jwks = jwt.PyJWKClient(f"{COGNITO_ISSUER}/.well-known/jwks.json", cache_keys=True)
        token = auth[len("Bearer "):].strip()
        key = _jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=COGNITO_AUDIENCE,
                            issuer=COGNITO_ISSUER)
        # The gateway's authorizer accepted ID tokens by audience; an
        # access token carries client_id instead and would have failed
        # there, so it fails here too.
        if claims.get("token_use") != "id":
            return None
        return claims
    except Exception as e:  # noqa: BLE001 -- any failure is "not signed in"
        print(f"jwt rejected: {e.__class__.__name__}: {e}")
        return None


def _event(environ: dict) -> dict:
    headers = {}
    for k, v in environ.items():
        if k.startswith("HTTP_"):
            headers[k[5:].lower().replace("_", "-")] = v
    for k in ("CONTENT_TYPE", "CONTENT_LENGTH"):
        if environ.get(k):
            headers[k.lower().replace("_", "-")] = environ[k]
    body = ""
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length:
        body = environ["wsgi.input"].read(length).decode("utf-8", "replace")
    event = {
        "version": "2.0",
        "rawPath": environ.get("PATH_INFO") or "/",
        "rawQueryString": environ.get("QUERY_STRING") or "",
        "headers": headers,
        "body": body,
        "requestContext": {"http": {"method": environ.get("REQUEST_METHOD", "GET"),
                                    "sourceIp": headers.get("cf-connecting-ip")
                                    or environ.get("REMOTE_ADDR", "")}},
    }
    claims = _claims(environ)
    if claims:
        event["requestContext"]["authorizer"] = {"jwt": {"claims": claims}}
    return event


def app(environ, start_response):
    event = _event(environ)
    path = event["rawPath"]
    if path.startswith("/api/me/") and "authorizer" not in event["requestContext"]:
        start_response("401 Unauthorized", [("Content-Type", "application/json")])
        return [b'{"error": "unauthorized"}']

    result = lambda_handler(event, None) or {}
    status = int(result.get("statusCode", 500))
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = ""
    body = result.get("body") or ""
    if result.get("isBase64Encoded"):
        payload = base64.b64decode(body)
    else:
        payload = body.encode("utf-8") if isinstance(body, str) else body
    headers = [(k, str(v)) for k, v in (result.get("headers") or {}).items()]
    headers.append(("Content-Length", str(len(payload))))
    start_response(f"{status} {reason}", headers)
    return [payload]
