// Signing in, on its own, so every page can offer it. It used to live in
// app.js, which only the board and the account page load, so the landing
// page's "Sign in" could do nothing but send a reader somewhere else.
// Nothing here touches the DOM: the pages own their own dialogs and say
// where a problem is shown (setAuthErrorSink below).
//
// Loaded before app.js, whose own helpers of the same names were moved
// here whole.

// The API's own prefix. Its own name, not app.js's API_BASE: both files
// are plain scripts sharing one global scope, and two `const API_BASE`
// at top level is a redeclaration error that would take the whole page
// down. Reported live: the landing page's sign-in said "API_BASE is not
// defined", because that constant was only ever declared in app.js,
// which that page does not load.
const AUTH_API_BASE = "/api";

// A query string from an object, the same one app.js keeps for the API.
function authQs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === "" || v === null || v === undefined || v === false) continue;
    p.set(k, v);
  }
  return p.toString();
}

// Where a sign-in problem is shown. Each page sets its own; without one
// a failure still reaches the console rather than vanishing.
let authErrorSink = (msg) => console.error("Sign-in:", msg);
function setAuthErrorSink(fn) { authErrorSink = fn; }

// Auth. Three passwordless sign-in paths, no accounts endpoint on
// this API beyond what a Cognito JWT authorizer will eventually protect
// (/me/alerts). See infra/cognito.tf and github_auth_handler.py for the
// backend half of each of these.

// Our own name, not Cognito's free *.auth.<region>.amazoncognito.com
// one: Google's consent screen shows the reader the host of the
// redirect URI. Must match infra/cognito.tf's user pool domain, and
// changing one without the other takes Google and GitHub sign-in down.
const COGNITO_DOMAIN = "auth.opentechjobs.org";
const COGNITO_REGION = "il-central-1";
const COGNITO_CLIENT_ID = "5021pv23cp3udp1uaq34tp38mb";
// OAuth client IDs aren't secret, safe to ship in frontend JS same as
// Google's. The paired client secret is NOT here and never should be:
// it lives only in the github-auth Lambda's environment, set from
// var.github_oauth_client_secret (infra/github_auth_lambda.tf), because
// only the server side of the code exchange is allowed to hold it.
const GITHUB_OAUTH_CLIENT_ID = "Ov23lii8kIqDUL9aLhxh";
// True since 2026-09-21: infra/cognito.tf's aws_cognito_identity_provider
// .google exists on the pool and the web client lists it. It was false for
// as long as it did not, because redirecting to Cognito's
// /oauth2/authorize?identity_provider=Google without a provider behind it
// lands on Cognito's own generic "Login option is not available" hosted-UI
// page: confusing, and a full navigation away from this app's own error
// display. If Google is ever removed from the pool, this goes back to
// false rather than the button being left to fail in the open.
const GOOGLE_CONFIGURED = true;
const AUTH_TOKENS_KEY = "iljobs_auth_tokens";
const PKCE_VERIFIER_KEY = "iljobs_pkce_verifier"; // sessionStorage: only needs to survive the redirect round-trip

function getAuthTokens() {
  try {
    return JSON.parse(localStorage.getItem(AUTH_TOKENS_KEY) || "null");
  } catch {
    return null;
  }
}

function setAuthTokens(tokens) {
  localStorage.setItem(AUTH_TOKENS_KEY, JSON.stringify(tokens));
}

// What a page redraws once the tokens change. app.js sets its topbar's
// own renderer; the landing and contact pages set theirs. Without one
// the tokens still clear, which is the part that must not depend on a
// page having registered anything.
let authRenderSink = () => {};
function setAuthRenderSink(fn) { authRenderSink = fn; }
// Called by signin_dialog.js once a sign-in lands, so whichever
// script owns the bar on this page redraws it. Same sink signOut
// uses, from the other direction.
function notifyAuthRender() { authRenderSink(); }

// Local cleanup is the part that must never fail, so it happens first
// and the network call follows without being waited on.
//
// Until 2026-09-22 this only deleted our copy of the tokens. The refresh
// token stayed valid at Cognito for its full thirty days, so signing out
// on a shared machine forgot the session rather than ending it: anyone
// holding that token could still mint fresh access tokens from it.
// /oauth2/revoke kills the refresh token and every access token minted
// from it. It needs enable_token_revocation on the app client, which
// infra/cognito.tf now sets.
function signOut() {
  let refresh = null;
  try {
    const t = JSON.parse(localStorage.getItem(AUTH_TOKENS_KEY) || "null");
    refresh = t && t.refresh_token;
  } catch {}
  localStorage.removeItem(AUTH_TOKENS_KEY);
  forgetLocalReaderData();
  authRenderSink();
  revokeRefreshToken(refresh);
}

// Not awaited, and every failure swallowed. The reader is already signed
// out of this browser by the time this runs; a network error must not
// leave them looking at a page that says otherwise. Cognito answers 200
// to a revoke it cannot honour anyway.
function revokeRefreshToken(refresh) {
  if (!refresh) return;
  const body = new URLSearchParams({ token: refresh, client_id: COGNITO_CLIENT_ID });
  const url = `https://${COGNITO_DOMAIN}/oauth2/revoke`;
  // keepalive, because this often runs a moment before a reload or a
  // redirect and would otherwise be cancelled in flight.
  fetch(url, { method: "POST", keepalive: true,
               headers: { "Content-Type": "application/x-www-form-urlencoded" },
               body: body.toString() })
    .catch(() => {});
}

// The CV skills are the signed-out reader's business only in the sense
// that they are not. They come from a profile, but the board keeps them
// in its saved filters so a reload lands on the same view, and those
// filters outlive the token.
//
// Reported live: sign out on Best matches, reload, and the board still
// ranked every listing against a CV it was no longer entitled to read.
// The rows were fetched with skills in the query, so this was not only
// a stale label.
// Everything this browser holds about the person who was signed in.
// Theme, the geo prompt and panel collapse state stay: those are
// preferences of the device, not of the reader.
function forgetLocalReaderData() {
  // Saved jobs and the cached listing pages are the reader's own. On a
  // shared machine the next person saw what the last one had bookmarked.
  //
  // The cache is a key prefix with the query appended, not one key, so
  // this sweeps rather than deletes by name.
  try {
    localStorage.removeItem("iljobs_starred");
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith("iljobs_jobs_cache")) localStorage.removeItem(key);
    }
  } catch {}
  forgetProfileSkills();
}

function forgetProfileSkills() {
  try {
    const raw = localStorage.getItem("iljobs_filters");
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (!saved || typeof saved !== "object") return;
    saved.skills = [];
    // Best matches with nothing to match on is an empty board, so the
    // view falls back to the one a reader arriving fresh would get.
    if (saved.sort === "match") {
      saved.sort = "age";
      saved.dir = "asc";
    }
    localStorage.setItem("iljobs_filters", JSON.stringify(saved));
  } catch {
    // Unreadable or unavailable storage. The token is already gone,
    // which is the part that must not depend on this working.
  }
}

// No verification -- this is display-only (the signed-in email in the
// topbar). The one place a token's signature actually has to hold up is
// server-side, when a JWT authorizer validates it on /me/alerts.
function decodeJwtEmail(idToken) {
  try {
    const payload = JSON.parse(atob(idToken.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return payload.email || null;
  } catch {
    return null;
  }
}

// The reader's own name, when the provider gave one. Google and GitHub
// both send it; an email sign-in has none, and the caller falls back to
// the address rather than inventing something to call them.
function decodeJwtName(idToken) {
  try {
    const payload = JSON.parse(atob(idToken.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return payload.name || payload.given_name || null;
  } catch {
    return null;
  }
}

// The reader's Google photo. Cognito maps Google's picture claim onto
// the user (infra/cognito.tf), so it rides along in the id_token. Email
// and GitHub sign-ins carry no picture claim at all.
function decodeJwtPicture(idToken) {
  try {
    const payload = JSON.parse(atob(idToken.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    const url = String(payload.picture || "");
    // Nothing here verified the signature, so the claim is treated as
    // hostile input: an https URL or nothing, never a javascript: one.
    return url.startsWith("https://") ? url : null;
  } catch {
    return null;
  }
}

// The round mark in the account menu, used by the board, the landing
// page and the contact page alike. The letter is always in the markup
// and the photo sits on top of it, so a photo that 404s later removes
// itself and the letter underneath shows through with no second render.
function avatarHtml(email, idToken) {
  const letter = String((email || "?").charAt(0) || "?").toUpperCase().replace(/[&<>"']/g, "");
  const url = idToken ? decodeJwtPicture(idToken) : null;
  const img = url
    ? `<img class="hero-avatar-img" src="${url.replace(/[&<>"']/g, encodeURIComponent)}" alt="" referrerpolicy="no-referrer" onerror="this.remove()" />`
    : "";
  return `<span class="hero-avatar" aria-hidden="true">${letter}${img}</span>`;
}

async function base64UrlDigest(input) {
  const bytes = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return btoa(String.fromCharCode(...new Uint8Array(digest)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function randomUrlSafe(len) {
  const bytes = crypto.getRandomValues(new Uint8Array(len));
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

async function startGoogleSignIn() {
  if (!GOOGLE_CONFIGURED) {
    authErrorSink("Google sign-in isn't wired up yet.");
    return;
  }
  const verifier = randomUrlSafe(64);
  sessionStorage.setItem(PKCE_VERIFIER_KEY, verifier);
  const challenge = await base64UrlDigest(verifier);
  // Still /, not /board: it has to match the callback Cognito has
  // registered (infra/cognito.tf). The edge forwards /?code= to /board.
  const redirectUri = `${location.origin}/`;
  const url = `https://${COGNITO_DOMAIN}/oauth2/authorize?${authQs({
    client_id: COGNITO_CLIENT_ID,
    response_type: "code",
    scope: "openid email profile",
    redirect_uri: redirectUri,
    identity_provider: "Google",
    code_challenge: challenge,
    code_challenge_method: "S256",
  })}`;
  location.href = url;
}

function startGithubSignIn() {
  if (!GITHUB_OAUTH_CLIENT_ID) {
    authErrorSink("GitHub sign-in isn't wired up yet.");
    return;
  }
  const url = `https://github.com/login/oauth/authorize?${authQs({
    client_id: GITHUB_OAUTH_CLIENT_ID,
    redirect_uri: `${location.origin}/api/auth/github/callback`,
    scope: "read:user user:email",
  })}`;
  location.href = url;
}

// Cognito's InitiateAuth/RespondToAuthChallenge are deliberately public,
// unsigned operations for a user-pool app client -- callable directly
// from the browser, no backend proxy or AWS SDK needed for this part.
// fetch has no timeout of its own, and a request that never comes back
// is the worst shape a failure can take: every caller awaits it forever
// and nothing in the console says so. Reported live 2026-09-24, as the
// account page with Alerts and Saved jobs both stuck on "Loading...".
function abortAfter(ms) {
  if (AbortSignal.timeout) return AbortSignal.timeout(ms);
  const controller = new AbortController();
  setTimeout(() => controller.abort(), ms);
  return controller.signal;
}

async function cognitoRequest(target, body) {
  const res = await fetch(`https://cognito-idp.${COGNITO_REGION}.amazonaws.com/`, {
    method: "POST",
    signal: abortAfter(10000),
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}`,
    },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.message || data.__type || `HTTP ${res.status}`);
  return data;
}

let _pendingOtpEmail = null;
let _pendingOtpSession = null;

async function startEmailSignIn(email) {
  // Ensures the Cognito account row exists first -- required because
  // allow_admin_create_user_only=true also blocks Cognito's own public
  // SignUp API, see github_auth_handler.py's module docstring.
  const res = await fetch(`${AUTH_API_BASE}/auth/email/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  const init = await cognitoRequest("InitiateAuth", {
    ClientId: COGNITO_CLIENT_ID,
    AuthFlow: "USER_AUTH",
    AuthParameters: { USERNAME: email, PREFERRED_CHALLENGE: "EMAIL_OTP" },
  });
  _pendingOtpEmail = email;
  _pendingOtpSession = init.Session;
}

async function verifyEmailOtp(code) {
  const result = await cognitoRequest("RespondToAuthChallenge", {
    ClientId: COGNITO_CLIENT_ID,
    ChallengeName: "EMAIL_OTP",
    Session: _pendingOtpSession,
    ChallengeResponses: { USERNAME: _pendingOtpEmail, EMAIL_OTP_CODE: code },
  });
  const t = result.AuthenticationResult;
  setAuthTokens({ id_token: t.IdToken, access_token: t.AccessToken, refresh_token: t.RefreshToken });
  _pendingOtpEmail = null;
  _pendingOtpSession = null;
}

async function exchangeGoogleCode(code) {
  const verifier = sessionStorage.getItem(PKCE_VERIFIER_KEY);
  sessionStorage.removeItem(PKCE_VERIFIER_KEY);
  const res = await fetch(`https://${COGNITO_DOMAIN}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: authQs({
      grant_type: "authorization_code",
      client_id: COGNITO_CLIENT_ID,
      code,
      redirect_uri: `${location.origin}/`,
      code_verifier: verifier,
    }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error_description || data.error || `HTTP ${res.status}`);
  setAuthTokens({ id_token: data.id_token, access_token: data.access_token, refresh_token: data.refresh_token });
}

// What each auth_error the callback can send back actually means, in
// the reader's terms. Anything not listed falls back to the raw code,
// which is still better than the silence this replaced.
const AUTH_ERRORS = {
  github_not_configured: "GitHub sign-in is not set up on this site yet. Google and email both work.",
  github_token_exchange_failed: "GitHub would not confirm that sign-in. Please try again.",
  no_verified_github_email: "Your GitHub account has no verified primary email, which is what the account here is keyed on.",
  missing_code: "That sign-in link was incomplete. Please try again.",
  access_denied: "Sign-in was cancelled.",
};

// Two unrelated redirect shapes land here, both at /board: Google's
// via Cognito's own authorization-code flow (?code=... query param,
// exchanged client-side above; Cognito sends it to /, and the edge
// forwards any /?code= to /board) and GitHub's via github_auth_handler.py's
// own 302 (#id_token=...&access_token=...&refresh_token=... hash
// fragment -- that Lambda already did the full exchange server-side).
async function handleAuthRedirect() {
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.get("id_token")) {
    setAuthTokens({
      id_token: hash.get("id_token"),
      access_token: hash.get("access_token"),
      refresh_token: hash.get("refresh_token"),
    });
    history.replaceState(null, "", location.pathname + location.search);
    return;
  }
  if (hash.get("auth_error")) {
    const code = hash.get("auth_error");
    console.error("Sign-in failed:", code);
    history.replaceState(null, "", location.pathname + location.search);
    // On screen, not only in the console. A reader who has just been
    // through a provider's consent screen and landed back signed out has
    // earned a sentence about why: without one the board simply looks
    // like it ignored them, which is how GitHub sign-in stayed broken
    // without anyone noticing. Reported live 2026-09-24.
    //
    // The dialog is reopened because the redirect closed it, and it is
    // where the error line and the other two ways in already are.
    setTimeout(() => {
      if (window.openSignIn) window.openSignIn();
      authErrorSink(AUTH_ERRORS[code] || `Sign-in failed (${code}).`);
    }, 0);
    return;
  }

  const params = new URLSearchParams(location.search);
  const code = params.get("code");
  if (code) {
    history.replaceState(null, "", location.pathname); // strip ?code= before the async exchange, not after -- a reload mid-flight must not resubmit a single-use code
    try {
      await exchangeGoogleCode(code);
    } catch (err) {
      console.error("Google sign-in failed:", err);
    }
  }
}
