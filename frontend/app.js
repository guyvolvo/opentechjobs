// Ocean of Jobs frontend. No framework, no build step, served straight
// from S3/CloudFront, runs as-shipped. Talks to the API at /api/*,
// same-origin (CloudFront routes /api/* to the Lambda).
//
// Starring a listing writes to localStorage first and always, because
// every render path reads that set synchronously, signed in or out.
// Signed in, the same set is mirrored to /me/saved, so a star set on
// this laptop is there on the phone too.

const API_BASE = "/api";
const STAR_KEY = "iljobs_starred";
const PAGE_SIZE = 50;

// Fixed vocabulary. Matches probe.py's _classify_seniority/Job.seniority
// exactly, not derived from the data, since it's a closed enum rather
// than free text like department/company.
const SENIORITY_LABELS = {
  intern: "Intern",
  junior: "Junior",
  mid: "Mid",
  senior: "Senior",
  staff: "Staff",
  principal: "Principal",
  lead: "Lead",
  manager: "Manager",
  director: "Director",
  exec: "Executive",
};

// Matches probe.py's Job.workplace_type exactly; see its docstring.
const WORKPLACE_LABELS = {
  remote: "Remote",
  hybrid: "Hybrid",
  onsite: "Onsite",
};

const state = {
  // One box. Every word must appear somewhere in the listing, quotes
  // keep a phrase whole. It replaced two boxes, q and keywords, that
  // asked visibly different questions with nothing on screen saying so:
  // one read the description and the other did not, one was AND and the
  // other a single substring, and the separator was a semicolon. The API
  // still answers both, because saved alerts carry them.
  search: "",
  // Whether the reader picked the order themselves, from the Sort menu,
  // a column header or a link that named one. Until they do, a search
  // sorts by relevance and an empty box by newest (see followSearchSort).
  // Not persisted and not in the URL: it describes this session's
  // clicking, not the view.
  sortExplicit: false,
  // "any" only when the reader asked for it, from a search that found
  // nothing (see emptySearchState). "" is the default: every word must
  // appear.
  search_mode: "",
  // The CV match, from /account: canonical skill labels, OR-matched
  // and ranked by overlap. Deliberately not folded into `keywords` or
  // `q` -- those ask "which jobs demand all of this" and "which titles
  // contain this exact string", and a CV is neither question.
  skills: [],
  department: [], // labeled "Category" in the UI; backend field stays "department"
  seniority: [],
  company: [], // multi-select; also set via clicking a company in the market panels
  // Both halves of the one Locations dropdown, and both geocoded. They
  // are separate filters that AND together on the server, which is why
  // ticking a country here does not tick its cities: country=IL with
  // city=Berlin is a legitimate way to ask for nothing, not a bug.
  country: [], // ISO 3166-1 alpha-2 codes; a job can carry several
  city: [], // canonical city names, as the locations facet spells them
  workplace: [], // remote|hybrid|onsite
  // Monthly gross shekels, from the rail's two-handle track. Shekels
  // because that is the one currency the snapshot carries numbers for
  // (see loader/salary_range.py): every other figure on the board is in
  // the employer's own currency with no rate anywhere to convert it.
  // "" means that end of the track is at its stop and sends nothing.
  //
  // The track keeps listings that quote nothing, which is most of them,
  // and salary_known is the separate question of whether to. Both are
  // dropped from the rail entirely when the current result set has no
  // shekel figures in it at all, rather than drawing a dead control.
  salary_min: "",
  salary_max: "",
  salary_known: false,
  // The employer's own figure, any currency, as opposed to an estimate.
  salary_disclosed: false,
  confidence: "all", // no confidence filter in the UI; shown inline via badge instead
  max_age_days: "", // "" = any time; else days-since-posting cutoff, straight into the API param of the same name
  starred_only: false,
  // "tech" shows only listings the classifier (api/role_class.py) judged
  // technical work, which is the board's default; "all" is every role.
  roles: "tech",
  sort: "age",
  dir: "asc", // newest first by default
  offset: 0,
};

// Assigned once in wireFilters(); referenced by refreshFacetOptions() and
// the market panels' "click a company" handler.
// Only the alert form on /account builds these now; the board's own
// filters are the rail (see createFilterRail). Left declared because
// populateAlertFilterOptions runs on every page app.js is loaded on and
// checks them before use.
let msDepartment, msSeniority, msCompany, msLocation, msWorkplace;

// The job detail panel's current selection. Not part of `state` above
// since it's ephemeral UI, never an API param. Survives filter changes
// (doesn't get yanked closed), just loses its row highlight if that job
// scrolls off the current page.
let selectedJobId = null;

// The detail panel is a sheet over the board at every width: right-hand
// above 960px, full-screen below (see style.css). This query is only for
// the swipe-to-close gesture, which is written against the bottom
// sheet's translateY and would fight the side sheet's translateX. It
// mirrors the real media query in style.css.
const MOBILE_SHEET_QUERY = window.matchMedia("(max-width: 960px)");

// Guards the close-then-reopen race: closeJobDetail's hidden=true is
// delayed to let the slide-out transition finish, so picking a
// different job mid-close would otherwise let that stale timeout yank
// the just-opened panel back to hidden a moment later.
let jobDetailCloseTimer = null;

// storage

function getStarred() {
  try {
    return new Set(JSON.parse(localStorage.getItem(STAR_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function toggleStar(id) {
  const s = getStarred();
  s.has(id) ? s.delete(id) : s.add(id);
  localStorage.setItem(STAR_KEY, JSON.stringify([...s]));
  return s;
}

function setStarred(ids) {
  localStorage.setItem(STAR_KEY, JSON.stringify([...ids]));
}

// Repaints the star controls for one job wherever they are on screen,
// which is the table row's button and, when that job is the one the
// detail panel has open, its Save button too. Used by the paths that
// change the set without going through a click, so the glyph still
// follows the data.
function paintStar(id, starredSet) {
  const on = starredSet.has(id);
  const rowBtn = document.querySelector(`[data-star="${id}"].star-btn`);
  if (rowBtn) {
    rowBtn.classList.toggle("on", on);
    rowBtn.textContent = on ? "★" : "☆";
  }
  syncDetailStarButton(id, starredSet);
}

// The server mirror. The click handlers never await this. A star is a
// localStorage toggle that has always felt instant, and making it wait
// on a Lambda round-trip would be a visible downgrade for the one thing
// on this board that never had to wait.
function pushStar(id, saved) {
  if (!getAuthTokens()) return; // signed out, and then localStorage is the whole story, exactly as before
  authedFetch(`/me/saved/${encodeURIComponent(id)}`, { method: saved ? "PUT" : "DELETE" })
    .catch(() => revertStar(id, saved));
}

// A write that did not land must not leave this browser quietly
// disagreeing with the account. Put the local set back and repaint that
// one star, then say so in the board's own error line, which is the
// same place a failed /jobs fetch reports itself.
function revertStar(id, attempted) {
  // A token that expired mid-request signs the tab out; the local star
  // is then no less valid than any other signed-out star, so leave it.
  if (!getAuthTokens()) return;
  const s = getStarred();
  if (s.has(id) !== attempted) return; // clicked again since, and that newer intent owns the row now
  toggleStar(id);
  const now = getStarred();
  paintStar(id, now);
  if (state.starred_only) loadJobs(); // the saved view is a row off until it re-reads the set
  const errEl = document.getElementById("jobs-error");
  if (!errEl) return;
  errEl.textContent = attempted
    ? "Could not save that listing to your account. The star has been put back."
    : "Could not remove that listing from your account. The star has been put back.";
  errEl.style.display = "block";
}

// Boot merge, one direction each way. Someone who starred jobs before
// ever signing in keeps them, and someone signing in on a new phone
// gets what they starred on the laptop, which is why this is a union
// and not a download that overwrites.
async function syncSavedFromServer() {
  if (!getAuthTokens()) return;
  let data;
  try {
    data = await authedFetch("/me/saved");
  } catch {
    // Signed out, token dead, endpoint down. Local stays authoritative
    // and nothing on screen changes.
    return;
  }
  const local = getStarred();
  // /me/saved comes back newest first; reversed it appends oldest
  // first, which keeps the array's tail the recent end. Nothing reads
  // this order except the 200-id cap in renderStarredOnly.
  const remote = (data?.saved || []).map((r) => r.job_id).reverse();
  const union = new Set([...local, ...remote]);
  if (union.size !== local.size) setStarred(union);

  // Local-only ids go up so both sides end up holding the same set.
  // Not pushStar, because a failure here must not roll back a star the
  // reader set before they ever signed in. The next boot tries again.
  const remoteSet = new Set(remote);
  for (const id of local) {
    if (remoteSet.has(id)) continue;
    authedFetch(`/me/saved/${encodeURIComponent(id)}`, { method: "PUT" }).catch(() => {});
  }

  if (union.size === local.size) return;
  if (state.starred_only) {
    loadJobs(); // the saved view is now short a few rows
    return;
  }
  // Stars that arrived from another device land on rows already
  // painted, so flip those in place rather than refetching the page.
  document.querySelectorAll(".star-btn").forEach((btn) => paintStar(btn.dataset.star, union));
  if (selectedJobId) paintStar(selectedJobId, union); // open detail panel may be a job the current page does not list
}

// fetch helpers

async function getJSON(path, { signal } = {}) {
  const res = await fetch(`${API_BASE}${path}`, signal ? { signal } : undefined);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${res.status}`);
  }
  return res.json();
}

function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === "" || v === null || v === undefined || v === false) continue;
    p.set(k, v);
  }
  return p.toString();
}

// formatting

// Google's favicon endpoint, keyed off the domain we already track for
// every job/company. Clearbit's free logo API (the previous go-to for
// this) shut down in December 2025 -- this is the zero-setup
// fallback: no signup, no key, no per-request cost. It's genuinely low
// quality though -- most sites' actual favicon.ico is a crude 16x16,
// and Google just upscales whatever's there (reported live: Overwolf's
// detailed claw-mark logo turned to mush at display size). See
// companyLogoImg below for the higher-res attempt this backs up.
function companyLogoUrl(domain, size = 32) {
  return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=${size}`;
}

// Generated, not fetched -- a flat ink-on-paper initial in the same
// square/no-radius language as everything else in DESIGN.md, for when
// no real icon worth showing exists anywhere. A data: URI, so it's
// always available with no network round-trip once picked.
function monogramLogoSvg(domain) {
  const letter = (domain || "").trim().charAt(0).toUpperCase() || "?";
  // Ink on paper, no fill. The tile behind it already draws the white
  // square and the hairline, so a coloured block here put a second,
  // louder shape inside the first and made a company with no logo the
  // loudest thing in the row.
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><text x="32" y="33" font-family="Helvetica Neue, Helvetica, Arial, sans-serif" font-size="30" font-weight="700" fill="#40513b" text-anchor="middle" dominant-baseline="central">${letter}</text></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

// Small, hand-verified exceptions to the guess-based cascade below, same
// shape as probe.py's own KNOWN_FALSE_POSITIVES: the general rule (try
// apple-touch-icon, then /favicon.ico, then Google) is sound, but a
// specific domain's own guessable asset can be a real, valid image file
// that just isn't a logo -- nothing in the HTTP response says so, only
// looking at the actual pixels does. duda.co/favicon.ico: confirmed
// live, a genuine 32x32 24-bit ICO, no alpha, every pixel solid white --
// stage 1/2 "succeed" (200, decodes fine) so onerror never fires and the
// cascade below never continues past it on its own. Values here are
// which stage to jump to instead of guessing that domain's own asset at
// all. Add to this as more turn up; there's no way to detect "loads
// fine but is blank" automatically without pixel access, which loading
// these cross-origin (no CORS headers on a random site's favicon.ico)
// doesn't allow.
const LOGO_STAGE_OVERRIDES = {
  "duda.co": 3, // jump straight to Google's favicon service
  // unframe.com: apple-touch-icon.png and favicon.ico both confirmed
  // dead (soft-404s), and Google's own favicon service returns a raw
  // "301 Moved" HTML error page for this domain, not an image or even
  // its usual 16x16 placeholder -- unreliable enough to skip rather
  // than keep retrying an inconsistent signal. Straight to the monogram.
  "unframe.com": 4,
  // Three where the monogram is the right answer, not a gap to fix.
  // Checked 2026-09-11: no apple-touch-icon, no favicon.ico, and
  // Google's service 404s for each. Listed so the cascade stops
  // guessing rather than issuing three doomed requests per row.
  "elbitsystems.com": 4,  // elbitsystems.com serves no icon at any of the three
  "iscar.co.il": 4,       // the site does not answer us at all
  "referralsuseonly.com": 4,  // a board placeholder, not a company with a logo
};

// Companies whose stored domain is a wrong guess from discover_companies.py's
// {token}.com heuristic (confirmed live 2026-09-08: 39 of the 160 companies
// merged that night had a company_domain that doesn't resolve at all --
// mostly enterprise ATS tenant slugs like "deloitte6" or "aecom2" that were
// never meant to be read as a domain). Fixing the icon here rather than
// renaming company_domain itself: several of these real domains (e.g.
// deloitte.com) are plausibly ALREADY a separately-tracked company under
// their own real token, so rewriting jobs.db's own domain key risks
// conflating two genuinely different job sets under one identity. This map
// only changes which domain the icon cascade below fetches from -- the
// underlying company identity, apply links, and everything else stay on the
// original (wrong) domain, same as before. See discover_companies.py's own
// verify_candidate() for the matching pipeline-side fix that stops new
// wrong guesses like these from being accepted in the first place.
const LOGO_DOMAIN_OVERRIDES = {
  // Audited 2026-09-11 across the 124 companies with open Israeli
  // listings: 14 were falling through to the monogram. Most were this
  // same shape, a tenant slug that discovery's domain guesser turned
  // into a plausible-looking domain nobody owns. Google has a good
  // 64x64 for each of the real ones.
  "sentinellabs.io": "sentinelone.com",  // SentinelOne's research blog, not the company site
  "doitintl.com": "doit.com",            // was pulling an unrelated 98x53 logo from Google
  "gongio.com": "gong.io",
  "pagayais.com": "pagaya.com",
  "eleoshealth.com": "eleos.health",
  "chainalysis-careers.com": "chainalysis.com",
  "zafran-security.com": "zafran.io",
  "chamelio.com": "chamelio.io",
  "pointfive.com": "pointfive.ai",
  "atbayjobs.com": "at-bay.com",
  "couchbaseinc.com": "couchbase.com",
  "accenturefederalservices.com": "accenturefederal.com",
  "alten-mexico-1.com": "alten.com",
  "avamere-skilled-advisors-llc.com": "avamere.com",
  "abm-careers.com": "abm.com",
  "archer56.com": "archer.com",
  "activate-interactive-pte-ltd.com": "activateinteractive.com",
  "addepar1.com": "addepar.com",
  "betatechnologiesinc.com": "beta.team",
  "asco-equipment.com": "ascoequipment.com",
  "aecom2.com": "aecom.com",
  "aboutyougmbh.com": "aboutyou.de",
  "americanironandmetal.com": "aimetals.com",
  "apf-entreprises.com": "apf-entreprises.fr",
  "applusidiada1.com": "applusidiada.com",
  "artemedse.com": "artemed.de",
  "asburycommunities.com": "asbury.org",
  "avaloq1.com": "avaloq.com",
  "baywaag.com": "baywa.com",
  "bertelsmann-jobs.com": "bertelsmann.com",
  "beumergroup1.com": "beumergroup.com",
  "cityandcountyofsanfrancisco1.com": "sf.gov",
  "collabera2.com": "collabera.com",
  "colliers1.com": "colliers.com",
  "colliersinternationalemea.com": "colliers.com",
  "contilia1.com": "contilia.com",
  "culinagroup1.com": "culinagroup.com",
  "deloitteat.com": "deloitte.com",
  "deloittenordic.com": "deloitte.com",
  "deloitte6.com": "deloitte.com",
  "deutschetelekomitsolutionsslovakia.com": "t-systems.com",
  "deutschetelekomitsolutions.com": "t-systems.com",
  // tenableinc.com's own apple-touch-icon.png is a soft-404 (HTTP 200,
  // Content-Type: text/html, not an image) -- reported live, and it's
  // the wrong domain anyway. discover_companies.py's own _guess_domain
  // now strips this exact "inc" shape going forward (see its docstring).
  "tenableinc.com": "tenable.com",
  // wix2.com / redwoodmaterials.co: both resolve to 200 OK, but both
  // are domain-parking pages (confirmed live: identical IP, identical
  // 114-byte "window.location.href" redirect stub) -- a HEAD-only check
  // can't tell that from a real site. discover_companies.py's own
  // _guess_domain now inspects response bodies for exactly this going
  // forward (see _looks_parked).
  "wix2.com": "wix.com",
  "redwoodmaterials.co": "redwoodmaterials.com",
  // reindeer-ai.com doesn't exist at all (confirmed: DNS NXDOMAIN) --
  // the real domain is reindeer.ai, the token's own trailing "-ai"
  // standing in for the TLD dot, not part of the name. Same pattern
  // now tried automatically going forward (see _HYPHEN_TLD_RE).
  "reindeer-ai.com": "reindeer.ai",
  // cermaticom.com: no real domain found -- keeps the monogram fallback.
};

// Reported live: Overwolf's real favicon.ico is a genuine 16x16 (verified
// via a direct curl, not assumed) with no apple-touch-icon anywhere on
// the site either -- Google's service was never the bug there, it was
// already showing the best real source, just upscaled. Four tiers, each
// one a same-origin/no-key image request or a local fallback, cheapest
// and most honest first:
//   1. apple-touch-icon.png -- the high-res convention, when it exists.
//   2. the domain's own favicon.ico, fetched directly (not through a
//      resizing proxy).
//   3. Google's favicon service -- catches the case a site's icon isn't
//      at a guessable path at all (declared via a <link> tag instead);
//      that's the one failure mode a direct path guess can't solve.
//   4. the generated monogram, unconditionally available.
// Stage 2 used to reject anything <=32px naturalWidth and drop straight
// to the monogram rather than show it upscaled -- reported live, that
// meant real, recognizable marks (Cisco, Mastercard: both genuinely just
// a small classic .ico, confirmed live) were losing to a flat letter
// square. A soft, real mark beats a generic initial for a company this
// recognizable, so stage 2 now renders whatever it gets, same as stage 3
// always has.
// Once ONE <img> for a domain settles on a final stage (a real icon
// found, or every stage exhausted down to the monogram), remember it --
// so the next occurrence of the same domain skips straight to the
// known-good stage instead of re-running the whole apple-touch-icon ->
// favicon.ico -> Google fallback from scratch. Reported live: a
// company-filtered search with 50 rows of the same domain made up to
// 150 redundant network requests (a failed stage's own 404 isn't
// cached by the browser by default), serialized by the browser's
// per-host connection limit -- visibly slow for exactly that shape of
// page, and for the Fastest Growing Companies panel + that same
// company's own rows both resolving the same domain independently.
// Stores only the STAGE, not a fully-built src -- callers ask for
// different sizes (the Top Hiring panel uses 16px, job rows something
// larger), and a cached src would freeze whichever size resolved it
// first. Persisted to localStorage too, not just this page's in-memory
// session, so a repeat VISIT benefits, not just repeated occurrences on
// one page -- wrapped in try/catch since localStorage can throw
// (private browsing, blocked site data), same as every other
// try/catch around it in this file.
// Bumped to v2 on 2026-09-11 with the override additions above. The
// cached value short-circuits the cascade, so a visitor who already
// resolved sentinellabs.io to the monogram would keep seeing it forever
// no matter what the override table says. Any future change to
// LOGO_DOMAIN_OVERRIDES or LOGO_STAGE_OVERRIDES has to bump this too.
const LOGO_RESOLVED_KEY = "iljobs-logo-resolved-v2";
let logoResolvedCache = new Map();
try {
  logoResolvedCache = new Map(Object.entries(JSON.parse(localStorage.getItem(LOGO_RESOLVED_KEY) || "{}")));
} catch { /* private browsing or blocked storage -- fall back to in-memory only */ }

function rememberLogoStage(domain, stage) {
  logoResolvedCache.set(domain, stage);
  try {
    localStorage.setItem(LOGO_RESOLVED_KEY, JSON.stringify(Object.fromEntries(logoResolvedCache)));
  } catch { /* same as above */ }
}
window.rememberLogoStage = rememberLogoStage;

// A logo the server already resolved and verified (company_logo.py: the
// company's own upload to its ATS, else whatever its site declares, else
// Google's favicon service). Passed straight through, because it was
// fetched and checked once rather than guessed here on every page view.
// The cascade below remains for rows loaded before the column existed,
// and for the panels that have a domain but no job record to read from.
function companyLogoImg(domain, size, extraClass = "", resolved = null) {
  if (resolved) {
    const cls = extraClass ? `company-logo ${extraClass}` : "company-logo";
    // Two fallbacks, not one. A resolved URL can fail for reasons the
    // server cannot see: a CDN 404s a logo that verified weeks ago, and
    // an ad blocker refuses anything served from an ad network's own
    // domain. Reported live: Taboola's logo is a valid 300x300 PNG on
    // taboola.com, which Brave blocks outright, so a company that used
    // to show an icon started showing a letter. Google's favicon service
    // is a neutral host and survives both, so it goes between the
    // resolved URL and giving up.
    const logoDomain = LOGO_DOMAIN_OVERRIDES[domain] || domain;
    return `<img class="${cls}" src="${escapeHtml(resolved)}" alt="" loading="lazy"
      data-google="${escapeHtml(companyLogoUrl(logoDomain, size))}"
      data-monogram="${escapeHtml(monogramLogoSvg(domain))}"
      onerror="if(this.dataset.google&&this.src!==this.dataset.google){this.src=this.dataset.google;}else{this.onerror=null;this.src=this.dataset.monogram;}" />`;
  }
  return companyLogoGuess(domain, size, extraClass);
}

function companyLogoGuess(domain, size, extraClass = "") {
  // LOGO_DOMAIN_OVERRIDES only affects where the icon itself is fetched
  // from -- domain (used below for the monogram initial, and by every
  // caller for the actual company identity/apply link) stays as-is.
  const logoDomain = LOGO_DOMAIN_OVERRIDES[domain] || domain;
  const touchIcon = escapeHtml(`https://${logoDomain}/apple-touch-icon.png`);
  const directFavicon = escapeHtml(`https://${logoDomain}/favicon.ico`);
  const googleFavicon = escapeHtml(companyLogoUrl(logoDomain, size));
  const monogram = escapeHtml(monogramLogoSvg(domain));
  const cls = extraClass ? `company-logo ${extraClass}` : "company-logo";
  const startStage = logoResolvedCache.get(domain) || LOGO_STAGE_OVERRIDES[logoDomain] || 1;
  const startSrc = { 1: touchIcon, 2: directFavicon, 3: googleFavicon, 4: monogram }[startStage];
  // Reported live (tomorrow.io): neither its favicon.ico nor its
  // apple-touch-icon exist, and Google's own favicon service can't find
  // one for it either -- but Google still answers 200 with an image
  // (its own generic default-globe placeholder), not a load failure, so
  // onerror alone never advances past it to the monogram. Confirmed
  // live: that generic placeholder always comes back a fixed 16x16, at
  // ANY requested sz -- but so can a real, found icon (reported live:
  // salesforce.com's own real favicon is genuinely only cached at 32x32
  // on Google's end even at sz=64, and a first version of this check
  // treated "smaller than requested" as "Google has nothing," wrongly
  // sending Salesforce's real icon to the monogram too). 16x16 exactly
  // is Google's own specific "nothing found" size -- but ONLY a
  // meaningful signal when we actually asked for more than that: the
  // Companies with most open roles panel calls this with size=16 itself (a real,
  // correctly-found 16x16 icon there is indistinguishable from the
  // placeholder by dimensions alone), and a second version of this
  // check missed that, wrongly monogram-ing real icons for every
  // company on that panel. Gated on size > 16 now, not just the pixel
  // dimensions matching.
  // rememberLogoStage calls below are what make logoResolvedCache above
  // actually useful: the FIRST <img> for a domain to settle (success or
  // exhausted to the monogram) is what every later occurrence on this
  // page, or a future visit, gets to skip straight to.
  return `<img class="${cls}" src="${startSrc}" alt="" loading="lazy"
    data-domain="${escapeHtml(domain)}" data-stage="${startStage}" data-size="${size}"
    data-direct-favicon="${directFavicon}" data-google-favicon="${googleFavicon}" data-monogram="${monogram}"
    onerror="if(this.dataset.stage==='1'){this.dataset.stage='2';this.src=this.dataset.directFavicon;}else if(this.dataset.stage==='2'){this.dataset.stage='3';this.src=this.dataset.googleFavicon;}else{this.onerror=null;this.onload=null;this.src=this.dataset.monogram;rememberLogoStage(this.dataset.domain,4);}"
    onload="if(this.dataset.stage==='3'&&Number(this.dataset.size)>16&&this.naturalWidth===16&&this.naturalHeight===16){this.onerror=null;this.onload=null;this.src=this.dataset.monogram;rememberLogoStage(this.dataset.domain,4);}else{rememberLogoStage(this.dataset.domain,Number(this.dataset.stage));}" />`;
}

function fmtInt(n) {
  return (n ?? 0).toLocaleString("en-US");
}

function fmtPct(n) {
  return `${Math.round((n ?? 0) * 1000) / 10}%`;
}

function fmtAge(days) {
  if (days === null || days === undefined) return "-";
  const totalMinutes = Math.floor(days * 24 * 60);
  const totalHours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  const hours = totalHours % 24;

  if (totalMinutes < 1) return "<1M";
  if (totalHours < 1) return `${minutes}M`;
  if (totalHours < 12) return `${totalHours}H ${minutes}M`;
  if (totalHours < 24) return `${totalHours}H`;
  if (days < 3) return `${Math.floor(days)}D ${hours}H`;
  return `${Math.floor(days)}D`;
}

// The same age as fmtAge, said as a time rather than a length of one. A
// listing row is a list of things that happened, so "2h ago" is the
// answer to what a reader is actually asking. fmtAge stays for the
// places that do mean a duration, such as the median open age on /stats,
// where "12d ago" would be an outright lie.
//
// Lowercase, and the minutes dropped when there are none: "3h 0m ago"
// was what the old format said at the top of every hour.
function fmtAgeAgo(days) {
  if (days === null || days === undefined) return "-";
  const totalMinutes = Math.floor(days * 24 * 60);
  const totalHours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  const hours = totalHours % 24;

  if (totalMinutes < 1) return "just now";
  if (totalHours < 1) return `${minutes}m ago`;
  if (totalHours < 12) return minutes ? `${totalHours}h ${minutes}m ago` : `${totalHours}h ago`;
  if (totalHours < 24) return `${totalHours}h ago`;
  if (days < 3) return hours ? `${Math.floor(days)}d ${hours}h ago` : `${Math.floor(days)}d ago`;
  return `${Math.floor(days)}d ago`;
}

function fmtMinutesAgo(mins) {
  if (mins === null || mins === undefined) return "UNKNOWN";
  if (mins < 60) return `${Math.round(mins)}M AGO`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)}H AGO`;
  return `${Math.round(mins / (60 * 24))}D AGO`;
}

// Deliberately no "AGO". This is the age of the data being shown, not
// the time since some event, and "29M AGO" under a heading about the
// API invited exactly the wrong reading.
// Spelled out rather than 18M/3H/2D. Next to a heading about how old the
// data is, "18M" reads as eighteen months at least as easily as eighteen
// minutes, and the two answers are a year apart.
function fmtDataAge(mins) {
  if (mins === null || mins === undefined) return null;
  if (mins < 60) return `${Math.round(mins)} min`;
  if (mins < 60 * 24) {
    const h = Math.round(mins / 60);
    return `${h} hour${h === 1 ? "" : "s"}`;
  }
  const d = Math.round(mins / (60 * 24));
  return `${d} day${d === 1 ? "" : "s"}`;
}

function escapeHtml(s) {
  return (s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function debounce(fn, ms) {
  let t;
  const run = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  // For the callers that have a "do it now" path beside the timer, so
  // pressing Enter doesn't run the same search again 500ms later.
  run.cancel = () => clearTimeout(t);
  return run;
}

// metrics dashboard

// Set from both renderMetrics() (every 2-min /stats poll) and
// refreshFreshness() (every 30s /health poll, see below), read by
// tickApiStatus() so the "X AGO" text and the sync countdown can keep
// moving client-side between polls instead of sitting frozen at
// whatever the last poll said.
let lastCheckedAt = null;

let lastReadingAt = null;

// last_checked only ever moves forward (MAX(companies.last_checked) in
// the DB), so two independently-cached samples of it (/stats and
// /health are separate CloudFront cache entries, each up to 120s stale
// on its own clock -- see infra/cloudfront.tf's api cache policy) should
// never be combined by just overwriting: whichever poll happens to land
// on a staler cached copy would yank the countdown backwards. Take the
// newer of the two instead.
function setLastCheckedAt(iso) {
  if (!iso) return;
  // When we last actually heard back, as opposed to how old the data was
  // when we heard. Set before the monotonic guard below, because a poll
  // that returns the same timestamp still proves the API answered.
  lastReadingAt = Date.now();
  const t = new Date(iso).getTime();
  if (lastCheckedAt === null || t > lastCheckedAt) lastCheckedAt = t;
}



// Shared by the topbar status dot/text and the API Status card: past
// this, both flip from LIVE (green) to OFFLINE (red) together.
//
// This tracked the write cycle up to 75 while the merge was hourly. It
// has to come down with it, or the site claims LIVE through fifteen
// consecutive missed cycles, which is most of a morning of stale data
// wearing a green light.
//
// 20 is four cycles rather than the 1.25x the hourly threshold used,
// because at five minutes a single slow cycle is a much larger share of
// the budget and the countdown poll itself is 2 minutes wide. Anything
// tighter flaps on ordinary jitter.
const FRESH_THRESHOLD_MINUTES = 20;

// The number here is MAX(companies.last_checked) from the current
// snapshot, so it answers "how old is the freshest listing data I'm
// holding", not "when did I last reach the API" and not "when did the
// sync run". Those are three different clocks and the card used to
// present the first as if it were the second: a heading of "API Status"
// over "29M AGO" reads as the API having been unreachable for half an
// hour, which is alarming and wrong, since the API answers in
// milliseconds and the figure is really bounded by the write cycle.
// Reported live, asking whether the sync cadence was what the card
// implied. It was not, and it has been wrong in this spot twice since.
// Three states on AWS's service-health vocabulary, not two.
//
// A binary green/red had nothing to say about the middle, which is where
// the interesting failures live: the snapshot is written every 5
// minutes, so one 12 minutes old has quietly missed two cycles while
// still sitting inside the 20-minute threshold that decides whether the
// board claims to be current at all. That is worth showing as degraded
// rather than as either fine or broken.
//
// DEGRADED_AFTER_MINUTES is deliberately two missed cycles rather than
// one. A single late merge is ordinary.
const DEGRADED_AFTER_MINUTES = 10;

// Old data with a live pipeline behind it is not an outage. Reported
// live: the tile said OFFLINE at 21 minutes while the merge had reported
// in two minutes earlier and was batching the next update. That scares a
// visitor over nothing.
//
// The merge writes its status on every 5-minute run, idle or not, and the
// API already marks a merge that died mid-run as "unknown" after 15
// minutes. So a merge status younger than PIPELINE_HEARTBEAT_MINUTES that
// is not an error means the pipeline is alive, and the status shows
// UPDATING with a spinner instead of DEGRADED or OFFLINE.
//
// UPDATING_CEILING_MINUTES keeps that honest. A merge that keeps running
// without ever applying anything is still a broken pipeline, and past 45
// minutes of old data the status says OFFLINE whatever the heartbeat says.
const PIPELINE_HEARTBEAT_MINUTES = 10;
const UPDATING_CEILING_MINUTES = 45;

function pipelineAlive() {
  const merge = pipelinePhase && pipelinePhase.merge;
  if (!merge || !merge.at || ["error", "unknown"].includes(merge.phase)) return false;
  return (Date.now() - new Date(merge.at).getTime()) / 60000 <= PIPELINE_HEARTBEAT_MINUTES;
}

// DEGRADED is eight characters and the tile is narrow, so the type is
// sized to the longest word rather than the words cut to the type (see
// #metric-api-status .value). It used to render as DEGRADE, clipped
// mid-word by the panel's overflow, and OFFLINE was quietly overflowing
// the same tile without being obvious enough for anyone to catch.
const STATUS_LEVELS = {
  operational: { symbol: "status-positive", label: "Operational", text: "Live" },
  degraded: { symbol: "status-warning", label: "Degraded", text: "Degraded" },
  outage: { symbol: "status-negative", label: "No recent updates", text: "Offline" },
  updating: { symbol: "status-updating", label: "Updating", text: "Updating" },
};

function apiStatusFields() {
  // The age is only meaningful if our own reading is current. Both polls
  // are gated on the tab being visible, and a sleeping machine misses
  // them with the tab still in front, while the one-second tick counts up
  // regardless. So an idle tab walks itself to OFFLINE while every cycle
  // behind it ran on time: CloudWatch shows both halves of the pipeline
  // firing every 5 minutes, 48 for 48, through the window a tile claimed
  // 20 minutes of silence.
  //
  // Freeze the clock at the last reading we actually took rather than
  // counting past it. Staleness we have not observed is not evidence.
  // The next successful poll unfreezes it.
  const stale = lastReadingAt === null || Date.now() - lastReadingAt > HEALTH_POLL_MS * 2;
  const readAt = stale ? lastReadingAt : Date.now();
  const minutesSince = lastCheckedAt === null || readAt === null ? null : (readAt - lastCheckedAt) / 60000;
  const age = minutesSince ?? 9999;
  const fresh = age <= FRESH_THRESHOLD_MINUTES;
  let level = !fresh ? "outage" : age > DEGRADED_AFTER_MINUTES ? "degraded" : "operational";
  if (level !== "operational" && age <= UPDATING_CEILING_MINUTES && pipelineAlive()) level = "updating";
  const spelled = fmtDataAge(minutesSince);
  return {
    fresh,
    level,
    // The word follows the level, not `fresh`. It used to follow `fresh`,
    // so the whole degraded band rendered a green LIVE beside the orange
    // warning glyph: at 18 minutes the tile said everything was fine and
    // flagged a problem in the same line. Reported live, from a
    // screenshot of exactly that.
    value: STATUS_LEVELS[level].text,
    sub: spelled === null ? "age unknown" : `${spelled} old`,
  };
}

// One writer for both places the state appears, so the topbar and the
// Data Health tile cannot drift apart. Swapping the <use> target rather
// than the markup keeps the sprite as the single definition of each
// glyph.
function paintStatusIcon(el, level) {
  if (!el) return;
  const spec = STATUS_LEVELS[level] || STATUS_LEVELS.operational;
  const use = el.querySelector("use");
  if (use) use.setAttribute("href", `#${spec.symbol}`);
  el.classList.remove("degraded", "outage", "updating");
  if (level !== "operational") el.classList.add(level);
  el.setAttribute("aria-label", spec.label);
}

function statusIconHtml(level) {
  const spec = STATUS_LEVELS[level] || STATUS_LEVELS.operational;
  const cls = level === "operational" ? "status-icon" : `status-icon ${level}`;
  return `<svg class="${cls}" role="img" aria-label="${spec.label}">`
    + `<use href="#${spec.symbol}"></use></svg>`;
}

// What the pipeline is doing, in its own words.
//
// Replaced a countdown to the next sync. That was always an estimate of
// a routine cycle rather than an observation, and an estimate of
// something that already happened five minutes ago is not information.
// Every stage of the pipeline already writes what it is doing to S3
// (see _write_status in the three handlers), so this shows that instead.
//
// Both sides, not just the merge. The old version deliberately ignored
// the scrape side on the grounds that this card is about data freshness
// and only the merge moves it. That was the right call for a countdown
// and the wrong one here: "sweeping 1,100 of 3,434 companies due now" is
// exactly the kind of thing worth seeing, and it is true far more of the
// time than the merge is busy.
//
// The merge wins a tie because it is the stage that changes what a
// visitor is looking at.
function pipelineActivityText() {
  if (!pipelinePhase) return null;
  const quiet = (p) => !p || !p.phase || ["idle", "unknown"].includes(p.phase);
  const describe = (p) => (p.detail ? `${p.phase}: ${p.detail}` : p.phase);

  for (const side of [pipelinePhase.merge, pipelinePhase.scrape]) {
    if (!quiet(side)) return describe(side);
  }
  // Nothing running. The idle detail says what the last run actually
  // did, which beats inventing a prediction about the next one.
  const last = pipelinePhase.merge || pipelinePhase.scrape;
  return last && last.detail ? last.detail : null;
}

function tickApiStatus() {
  const card = document.getElementById("metric-api-status");
  if (!card) return;
  const { level, value, sub } = apiStatusFields();
  // Green is for operational only. Degraded and outage colour the word
  // to match their own glyph.
  card.classList.toggle("highlight", level === "operational");
  card.classList.toggle("degraded", level === "degraded");
  card.classList.toggle("outage", level === "outage");
  card.classList.toggle("updating", level === "updating");
  // Only when it changes. This runs every second, and rewriting the markup
  // each time recreates the glyph, which restarted the Updating spinner's
  // rotation every second and made it stutter.
  const valueEl = card.querySelector(".value");
  const valueHtml = `${statusIconHtml(level)}${escapeHtml(value)}`;
  if (valueEl.dataset.painted !== valueHtml) {
    valueEl.innerHTML = valueHtml;
    valueEl.dataset.painted = valueHtml;
  }
  card.querySelector(".sub").textContent = sub;
  const syncEl = card.querySelector(".sync-countdown");
  if (syncEl) syncEl.textContent = pipelineActivityText() ?? "";
  // The topbar's own copy of this mark is gone: the overview panel's
  // "Last updated" line says the same thing beside the number it
  // qualifies. The ids stay guarded rather than removed, because /stats
  // still carries them.
  const dot = document.getElementById("status-dot");
  if (dot) paintStatusIcon(dot, level);
  const text = document.getElementById("status-text");
  if (text) text.textContent = STATUS_LEVELS[level].text;
}

// The scoped answer the API gave for one exact set of board filters,
// plus the filter string it was asked for. The key is what lets the
// 2-minute global /stats tick re-render these tiles without quietly
// repainting a filtered board with whole-board numbers: if the key does
// not match what the filter bar says right now, the numbers in here
// belong to a search nobody is looking at.
// `data: null` with `degraded: true` is the answer that came back
// without a `scoped` object at all.
let latestScoped = null;

// One shape for the scopeable numbers, whichever half of the payload
// they came out of. /api/stats' `scoped` object mirrors totals.* /
// throughput.* / age.* / top_companies field for field on purpose, so
// the tiles never have to branch on where a number came from.
function scopeShape(src) {
  return {
    open_jobs: src.open_jobs,
    companies_hiring: src.companies_hiring,
    new_jobs_24h: src.new_jobs_24h,
    new_jobs_7d: src.new_jobs_7d,
    closed_jobs_24h: src.closed_jobs_24h,
    closed_jobs_7d: src.closed_jobs_7d,
    median_open_days: src.median_open_days,
    oldest_open_days: src.oldest_open_days,
    top_companies: Array.isArray(src.top_companies) ? src.top_companies : [],
  };
}

function globalScope(stats) {
  return scopeShape({
    ...stats.totals,
    ...stats.throughput,
    ...stats.age,
    top_companies: stats.top_companies || [],
  });
}

// Four states, because three of them look identical if you only track
// "filtered or not" and each one has to be labelled differently:
//
//   global       nothing real is filtered, the tiles are whole-board
//   pending      filtered, the scoped answer has not landed yet
//   scoped       filtered, the numbers on screen are the filtered ones
//   unavailable  filtered, but the API answered without a `scoped` key
//                (a stale artifact, or the route before it shipped), so
//                the numbers fall back to whole-board and say so
//
// The "is anything really filtered" test is refreshFacetOptions', not a
// second definition of its own: the board sends a confidence on every
// request, so it never counts as a filter here.
function currentScopeMode() {
  const params = qs(countingParams());
  if (!params) return "global";
  if (!latestScoped || latestScoped.params !== params) return "pending";
  return latestScoped.degraded ? "unavailable" : "scoped";
}

// What the board is filtered by, in the words the filter bar itself
// uses. Read from state rather than from the response, because it has
// to be right the moment a filter changes, which is up to 2.9s before
// the numbers it describes arrive.
// starred_only and sort/offset are deliberately absent: the first is a
// client-local view the API never sees, the other two do not change
// which listings are counted.
function activeFilterSummary() {
  const parts = [];
  if (state.search) parts.push(`"${state.search}"`);
  if (state.department.length) parts.push(state.department.join(", "));
  if (state.seniority.length) parts.push(state.seniority.map((s) => SENIORITY_LABELS[s] || s).join(", "));
  if (state.company.length) parts.push(state.company.join(", "));
  if (state.country.length) parts.push(state.country.map(countryLabel).join(", "));
  if (state.city.length) parts.push(state.city.join(", "));
  if (state.workplace.length) parts.push(state.workplace.map((w) => WORKPLACE_LABELS[w] || w).join(", "));
  if (state.skills.length) parts.push(`${state.skills.length} CV skill${state.skills.length === 1 ? "" : "s"}`);
  if (state.max_age_days) parts.push(`posted in the last ${state.max_age_days} days`);
  return parts;
}

// Says so plainly when nothing is applied rather than disappearing. An
// indicator that is only there sometimes teaches the reader nothing
// about the times it is missing, and "these are global right now" is
// exactly what they need to know to trust the tile above.
function renderScopeLine() {
  const el = document.getElementById("stats-scope");
  if (!el) return;
  const mode = currentScopeMode();
  // The view is part of the scope: on the default view the numbers are
  // tech roles only, and the line says so before the filters.
  const view = state.roles === "tech" && !state.starred_only ? ["Tech roles"] : [];
  const applied = [...view, ...activeFilterSummary()].join(" · ");
  if (mode === "global") {
    el.textContent = "No filters applied";
    return;
  }
  if (mode === "pending") {
    el.textContent = `${applied}. Counting…`;
    return;
  }
  if (mode === "unavailable") {
    el.textContent = `${applied}. No scoped totals came back, so these are whole-board numbers.`;
    return;
  }
  el.textContent = applied;
}

function renderMetrics(stats) {
  setLastCheckedAt(stats.freshness.last_checked);
  renderScopedMetrics(stats);
  renderPipelineTile();
}

// The five tiles that can follow the board's filters. Data Health is not
// among them any more; it answers a question about the pipeline, not
// about the selected listings, and lives in its own block below.
function renderScopedMetrics(stats) {
  const el = document.getElementById("metrics-grid");
  // /board no longer draws this: the Statistics column became the
  // detail pane's empty state and /stats. The same function still
  // runs on any page that does have the grid, and refreshStats
  // still fetches what feeds it, so nothing here is dead.
  if (!el) return;
  const mode = currentScopeMode();
  const pending = mode === "pending";
  // Only "scoped" puts filtered numbers on screen. "pending" is about to,
  // so it labels itself the same way; "unavailable" falls back to
  // whole-board numbers and has to keep the whole-board labels with them.
  const narrowed = mode === "scoped" || pending;
  const d = mode === "scoped" ? latestScoped.data : globalScope(stats);
  renderScopeLine();

  const cards = [
    {
      // The word "global" survives on this tile only while the number
      // under it really is global. A filtered count beneath a label
      // reading GLOBAL OPEN JOBS is the failure this whole section
      // exists to rule out.
      label: narrowed ? "Open roles" : "Global open jobs",
      value: fmtInt(d.open_jobs),
      // open_jobs_best_effort is a whole-board figure with no scoped
      // twin in the contract, so it cannot ride along under a filtered
      // count pretending to describe it.
      sub: narrowed ? "matching these filters" : `${fmtInt(stats.meta.open_jobs_best_effort)} more unverified`,
      hl: true,
    },
    {
      label: "Companies hiring",
      value: fmtInt(d.companies_hiring),
      sub: narrowed ? "with a matching open role" : "with a fresh open role",
    },
    {
      // The label said 7d while the number under it was the 24h figure,
      // with the real 7d total demoted to the caption. Both tiles read
      // the same way, so both said the wrong period. Caught while
      // relabelling the panel for scoping, which is the whole point of
      // that exercise: a number nobody can name is worse than no number.
      label: "New listings in 24h",
      value: `+${fmtInt(d.new_jobs_24h)}`,
      sub: `${fmtInt(d.new_jobs_7d)} in 7d`,
      hl: d.new_jobs_24h > 0,
    },
    {
      label: "Closed in 24h",
      value: `-${fmtInt(d.closed_jobs_24h)}`,
      sub: `${fmtInt(d.closed_jobs_7d)} in 7d`,
    },
    {
      label: "Median open age",
      value: fmtAge(d.median_open_days),
      sub: `oldest ${fmtAge(d.oldest_open_days)}`,
    },
  ];

  el.innerHTML = cards
    .map(
      (c) => `
      <div class="metric-card ${pending ? "pending" : c.hl ? "highlight" : ""}"${pending ? ' aria-busy="true"' : ""}>
        <div class="label">${c.label}</div>
        <div>
          <div class="value">${pending ? '<span class="skeleton" aria-hidden="true"></span>' : c.value}</div>
          <div class="sub">${pending ? '<span class="skeleton sk-sub" aria-hidden="true"></span>' : c.sub}</div>
        </div>
      </div>`
    )
    .join("");
}

// Never scoped, and its own block says why. It describes the freshness
// of the whole scrape, so narrowing it to a filter would be meaningless
// even if the API offered a way to.
function renderPipelineTile() {
  const el = document.getElementById("pipeline-grid");
  // /board no longer draws this: the Statistics column became the
  // detail pane's empty state and /stats. The same function still
  // runs on any page that does have the grid, and refreshStats
  // still fetches what feeds it, so nothing here is dead.
  if (!el) return;
  const status = apiStatusFields();
  const sub2 = pipelineActivityText();
  // The first paint has to land on the same class the one-second tick
  // would set right after, or the tile flashes green before correcting.
  el.innerHTML = `
      <div class="metric-card ${status.level === "operational" ? "highlight" : status.level}" id="metric-api-status"
           title="Freshness of the whole pipeline across every company we poll. Never narrowed by the board's filters.">
        <div class="label">Data health</div>
        <div>
          <div class="value">${statusIconHtml(status.level)}${escapeHtml(status.value)}</div>
          <div class="sub">${status.sub}</div>
          ${sub2 ? `<div class="sync-countdown">${sub2}</div>` : ""}
        </div>
      </div>`;

  const dot = document.getElementById("status-dot");
  if (dot) paintStatusIcon(dot, status.level);
  const text = document.getElementById("status-text");
  if (text) text.textContent = STATUS_LEVELS[status.level].text;
}

// Recomputes from lastCheckedAt every 1s -- the sync countdown needs a
// real per-second tick to read as "live"; the AGO text along for the
// ride is a no-op most seconds, negligible cost either way.
const API_STATUS_TICK_MS = 1_000;

// Market-insight panels: who's hiring, what for, where. No ATS-vendor
// breakdown here; that's plumbing, not a market signal (still available
// as open_jobs_by_ats for anyone polling the raw API).
function renderBarList(rows, nameKey, { clickable = false, limit = 0 } = {}) {
  const max = Math.max(1, ...rows.map((r) => r.n));
  return rows
    .map((r, i) => {
      const name = escapeHtml(r[nameKey]);
      // Rows past `limit` are rendered but hidden until the panel is
      // expanded (see .bar-row-extra). title= shows a truncated name in full.
      const extra = limit && i >= limit ? " bar-row-extra" : "";
      return `
      <div class="bar-row${clickable ? " clickable" : ""}${extra}" ${clickable ? `data-company="${name}"` : ""}>
        <div class="name" title="${name}">${clickable ? companyLogoImg(r[nameKey], 16, "", r.logo_url || null) : ""}${name}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${(r.n / max) * 100}%"></div></div>
        <div class="n">${fmtInt(r.n)}</div>
      </div>`;
    })
    .join("");
}

// SVG bar chart, 14 days of new-listing counts, with a closed-jobs line
// on its own scale. Closed counts run much smaller, so sharing one
// scale would flatline the line near zero. Not pixel-comparable to each
// other; the legend says so. viewBox-scaled for responsiveness; native
// <title> gives a free per-point tooltip.
// Smooth SVG path through a list of [x,y] points via Catmull-Rom-to-Bezier
// conversion -- no charting library, just the standard spline formula.
// Centripetal parameterization (alpha=0.5): the variant that stays
// well-behaved (no loops/cusps) even when points aren't evenly spaced,
// unlike the uniform (alpha=0) version. Barry & Goldman's formula, via
// https://qroph.github.io/2018/07/30/smooth-paths-using-catmull-rom-splines.html
// Clamps the neighbor lookup at both ends so the curve doesn't overshoot
// past the first/last point.
function smoothPath(points, alpha = 0.5) {
  if (points.length < 2) return "";
  const p = points;
  const n = p.length;
  const dist = (a, b) => Math.hypot(b[0] - a[0], b[1] - a[1]);
  let d = `M${p[0][0].toFixed(1)},${p[0][1].toFixed(1)}`;

  for (let i = 0; i < n - 1; i++) {
    const p0 = p[Math.max(i - 1, 0)];
    const p1 = p[i];
    const p2 = p[i + 1];
    const p3 = p[Math.min(i + 2, n - 1)];

    const t01 = Math.pow(dist(p0, p1), alpha) || 1e-6;
    const t12 = Math.pow(dist(p1, p2), alpha) || 1e-6;
    const t23 = Math.pow(dist(p2, p3), alpha) || 1e-6;

    const m1x = p2[0] - p1[0] + t12 * ((p1[0] - p0[0]) / t01 - (p2[0] - p0[0]) / (t01 + t12));
    const m1y = p2[1] - p1[1] + t12 * ((p1[1] - p0[1]) / t01 - (p2[1] - p0[1]) / (t01 + t12));
    const m2x = p2[0] - p1[0] + t12 * ((p3[0] - p2[0]) / t23 - (p3[0] - p1[0]) / (t12 + t23));
    const m2y = p2[1] - p1[1] + t12 * ((p3[1] - p2[1]) / t23 - (p3[1] - p1[1]) / (t12 + t23));

    // Hermite tangents -> cubic Bezier control points.
    const c1x = p1[0] + m1x / 3;
    const c1y = p1[1] + m1y / 3;
    const c2x = p2[0] - m2x / 3;
    const c2y = p2[1] - m2y / 3;

    d += ` C${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
  }
  return d;
}

function renderTrendChart(daily) {
  // Each line is normalized to its own max, not a shared one: closed
  // counts run much smaller than new counts, so sharing a scale would
  // flatline it near zero.
  const maxNew = Math.max(1, ...daily.map((d) => d.n));
  const maxClosed = Math.max(1, ...daily.map((d) => d.closed || 0));
  const w = 320;
  const h = 64;
  const stepX = daily.length > 1 ? w / (daily.length - 1) : 0;

  const newXY = daily.map((d, i) => [i * stepX, h - (d.n / maxNew) * h]);
  const closedXY = daily.map((d, i) => [i * stepX, h - ((d.closed || 0) / maxClosed) * h]);
  const newLine = smoothPath(newXY);
  const closedLine = smoothPath(closedXY);

  return `
    <svg class="trend-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <path class="trend-line" d="${newLine}"><title>New listings</title></path>
      <path class="trend-line closed" d="${closedLine}"><title>Closed listings</title></path>
    </svg>
    <div class="trend-legend">
      <span><span class="dot new"></span>New</span>
      <span><span class="dot closed"></span>Closed</span>
    </div>
    <div class="trend-axis"><span>${daily[0].date.slice(5)}</span><span>${daily[daily.length - 1].date.slice(5)}</span></div>`;
}

// Standalone line chart: open_jobs_history, reconstructed from
// first_seen/closed_at rather than a real snapshot.
function renderOpenJobsChart(history) {
  const max = Math.max(1, ...history.map((d) => d.n));
  const w = 320;
  const h = 64;
  const stepX = history.length > 1 ? w / (history.length - 1) : 0;
  const xy = history.map((d, i) => [i * stepX, h - (d.n / max) * h]);
  const line = smoothPath(xy);
  return `
    <svg class="trend-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <path class="trend-line open" d="${line}"><title>Open jobs over time</title></path>
    </svg>
    <div class="trend-axis"><span>${history[0].date.slice(5)}</span><span>${history[history.length - 1].date.slice(5)}</span></div>`;
}

// One headline percentage: how much of the open board looks like it's
// stopped moving. threshold_days comes from the API, not hardcoded here,
// so a backend change to the cutoff doesn't need a matching frontend edit.
function renderGhostStat(ghost, openJobs) {
  const pct = Math.round(ghost.dormant_pct * 1000) / 10;
  return `
    <div class="ghost-pct">${pct}%</div>
    <div class="ghost-sub">${fmtInt(ghost.dormant_count)} of ${fmtInt(ghost.sample_size)} open listings haven't been filled in over ${ghost.threshold_days} days (${fmtInt(openJobs)} open in total).</div>`;
}

// Every panel in the Market overview block is whole-board.
function renderPanels(stats) {
  const el = document.getElementById("panel-grid");
  // /board no longer draws this: the Statistics column became the
  // detail pane's empty state and /stats. The same function still
  // runs on any page that does have the grid, and refreshStats
  // still fetches what feeds it, so nothing here is dead.
  if (!el) return;

  el.innerHTML = `
    <div class="panel">
      <div class="panel-title">New listings, last 14 days</div>
      ${renderTrendChart(stats.daily_new_jobs)}
    </div>
    <div class="panel">
      <div class="panel-title">Open jobs over time</div>
      ${renderOpenJobsChart(stats.open_jobs_history)}
    </div>
    <div class="panel">
      <div class="panel-title">Fastest growing (new reqs, 7d)</div>
      ${
        stats.top_movers_7d.length
          ? renderBarList(stats.top_movers_7d, "domain", { clickable: true })
          : '<div class="sub" style="color:var(--grey)">Nothing new in the last 7 days.</div>'
      }
    </div>
    <div class="panel">
      <div class="panel-title">Top categories</div>
      ${renderBarList(stats.top_departments, "department")}
    </div>
    <div class="panel">
      <div class="panel-title">Seniority spread</div>
      ${renderBarList(
        stats.seniority_breakdown.map((r) => ({ seniority: SENIORITY_LABELS[r.seniority] || r.seniority, n: r.n })),
        "seniority"
      )}
    </div>
    <div class="panel">
      <div class="panel-title">Dormant listings</div>
      ${renderGhostStat(stats.ghost, stats.totals.open_jobs)}
    </div>`;

  wireCompanyBarClicks(el);
}

// How many companies show before View all. The API returns 10; at the
// sidebar's width ten long domains read as a wall. Module-level so the
// 2-minute stats refresh doesn't collapse a list the reader opened.
const COMPANY_ROWS_SHOWN = 7;
let companiesExpanded = false;

// Companies with most open roles, the one bar list the contract can
// narrow, so it sits in the Current search block with the tiles rather
// than beside the charts.
function renderScopedPanels(stats) {
  const el = document.getElementById("scoped-panel-grid");
  // /board no longer draws this: the Statistics column became the
  // detail pane's empty state and /stats. The same function still
  // runs on any page that does have the grid, and refreshStats
  // still fetches what feeds it, so nothing here is dead.
  if (!el) return;
  const mode = currentScopeMode();

  if (mode === "pending") {
    // Bones rather than the previous filter's leaderboard. A list of
    // companies is read as an answer, and holding the old one there for
    // up to 2.9s answers a question the reader has stopped asking. The
    // bones are the ones the page stamps in before this file loads (see
    // board.html's #panel-skeleton): the real panel's shape, so the
    // column keeps its height while the scoped answer is out.
    el.innerHTML = document.getElementById("panel-skeleton").innerHTML.replace('aria-hidden="true"', 'aria-busy="true"');
    return;
  }

  const scoped = mode === "scoped";
  const rows = scoped ? latestScoped.data.top_companies : stats.top_companies || [];
  const moreLabel = () => (companiesExpanded ? "Show fewer" : `View all ${rows.length}`);
  el.innerHTML = `
    <div class="panel${companiesExpanded ? " expanded" : ""}">
      <div class="panel-title">Companies with most open roles</div>
      ${
        rows.length
          ? renderBarList(rows, "domain", { clickable: true, limit: COMPANY_ROWS_SHOWN })
          : '<div class="sub" style="color:var(--grey)">No company has a matching open role.</div>'
      }
      ${
        rows.length > COMPANY_ROWS_SHOWN
          ? `<button type="button" class="bar-more" aria-expanded="${companiesExpanded}">${moreLabel()}</button>`
          : ""
      }
    </div>`;

  wireCompanyBarClicks(el);
  el.querySelector(".bar-more")?.addEventListener("click", (e) => {
    companiesExpanded = !companiesExpanded;
    const btn = e.currentTarget;
    btn.closest(".panel").classList.toggle("expanded", companiesExpanded);
    btn.setAttribute("aria-expanded", String(companiesExpanded));
    btn.textContent = moreLabel();
  });
}

// Clicking a company in any bar list filters the board to it. Shared by
// both grids, since the same rows now render in two places.
function wireCompanyBarClicks(el) {
  el.querySelectorAll("[data-company]").forEach((row) => {
    row.addEventListener("click", () => {
      state.company = [row.dataset.company];
      state.starred_only = false;
      state.offset = 0;
      if (msCompany) msCompany.setSelected(state.company);
      renderFilterRail();
      renderActiveChips();
      loadJobs();
      window.scrollTo({ top: document.getElementById("board").offsetTop - 60, behavior: "smooth" });
    });
  });
}

// job board

let lastJobsResponse = null;

// Show all, Best matches, Saved. Derived from state rather than
// stored, so a link carrying ?skills= or ?starred=1 lands on the right
// view with nothing else to keep in step.
function currentView() {
  if (state.starred_only) return "saved";
  if (state.skills.length) return "matches";
  return state.roles === "tech" ? "tech" : "all";
}

function paintViewSwitch() {
  const view = currentView();
  // "Best match" in the Sort dropdown only means something with skills to
  // rank by, and it is the one way back after picking Newest in that view.
  const matchOption = document.querySelector('#f-sort option[value="match:asc"]');
  if (matchOption) matchOption.hidden = !state.skills.length;
  document.querySelectorAll("#view-switch [data-view]").forEach((b) => {
    const on = b.dataset.view === view;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", String(on));
  });
}

// The CV match, stated. A ranked board looks like an unranked one, so in
// Best matches this names what the order is built from and lets the
// reader drop a skill without leaving the board. Adding skills is the CV
// analyser's job, on /account, so this links there instead of growing a
// second editor.
//
// The skills fold away under "My skills". Forty of them wrapped to three
// rows above the results, so they start folded and the choice is kept.
const MATCH_SKILLS_OPEN_KEY = "iljobs_match_skills_open";

function matchSkillsOpen() {
  try { return localStorage.getItem(MATCH_SKILLS_OPEN_KEY) === "1"; } catch { return false; }
}

function renderMatchPanel() {
  const panel = document.getElementById("match-panel");
  if (!panel) return;
  if (!state.skills.length || state.starred_only) {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }
  panel.hidden = false;
  const open = matchSkillsOpen();
  panel.innerHTML = `<div class="match-panel-head">`
    + `<button type="button" class="match-panel-toggle" aria-expanded="${open}" aria-controls="match-skills">`
    + `My skills <span class="match-panel-count">${state.skills.length}</span>${MS_CHEVRON_SVG}</button>`
    + `<span class="match-panel-label"`
    + ` title="Listings that share at least one of your CV skills, ordered by how many, with newer roles counted higher: every two weeks since posting counts as one skill fewer.">`
    + `Ranked by relevance</span>`
    + `<a class="link match-panel-edit" href="/account#cv">Edit skills</a>`
    + `</div>`
    + `<div class="match-skills-wrap${open ? " open" : ""}" id="match-skills"><div class="match-skills">`
    // data-match-skill, not data-skill: renderJobRows wires every
    // [data-skill] on the page as "search for this skill", and that
    // handler stops propagation, so a shared attribute turned removing a
    // skill here into a text search for it.
    + state.skills.map((s) => `<button type="button" class="match-skill" data-match-skill="${escapeHtml(s)}"`
      + `${open ? "" : " tabindex=\"-1\""} title="Stop matching on ${escapeHtml(s)}">${escapeHtml(s)} <span aria-hidden="true">✕</span></button>`).join("")
    + `</div></div>`;
}

// Best matches with nothing to rank by. Says what the view needs rather
// than showing a board that looks exactly like Show all.
function showMatchesPrompt() {
  const panel = document.getElementById("match-panel");
  panel.hidden = false;
  panel.innerHTML = getAuthTokens()
    ? `<span class="match-panel-label">Best matches orders listings by the skills on your CV.</span>`
      + ` <a class="link match-panel-edit" href="/account#cv">Analyze your CV</a>`
    : `<span class="match-panel-label">Best matches orders listings by the skills on your CV.`
      + ` Sign in, then analyze your CV on your account page.</span>`;
}

// The skills the CV analyser saved to the reader's profile. Null when
// signed out or when nothing has been saved yet.
async function profileSkills() {
  if (!getAuthTokens()) return null;
  try {
    const data = await authedFetch("/me/profile");
    const skills = (data && data.profile && data.profile.skills) || (data && data.skills) || [];
    return skills.length ? skills : null;
  } catch {
    return null;
  }
}

async function setView(view) {
  state.offset = 0;
  if (view === "saved") {
    state.starred_only = true;
    loadJobs();
    return;
  }
  state.starred_only = false;
  if (view === "all" || view === "tech") {
    state.roles = view;
    state.skills = [];
    if (state.sort === "match") setActiveSortHeader("age", "asc");
    loadJobs();
    return;
  }
  // Best matches keeps every filter already set: it orders what they
  // leave. Skills already on the board, from a link out of the CV
  // analyser or an earlier visit, are used as they are.
  if (!state.skills.length) {
    const skills = await profileSkills();
    if (!skills) {
      paintViewSwitch();
      showMatchesPrompt();
      return;
    }
    state.skills = skills;
  }
  setActiveSortHeader("match", "asc");
  loadJobs();
}

// The filter (not sort/pagination) portion of state. Shared by loadJobs,
// the facet counts and the scoped statistics, so they all describe the
// same result set.
function currentFilterParams() {
  return {
    search: state.search,
    search_mode: state.search_mode,
    department: state.department.join(","),
    seniority: state.seniority.join(","),
    company: state.company.join(","),
    country: state.country.join(","),
    city: state.city.join(","),
    workplace: state.workplace.join(","),
    skills: state.skills.join(","),
    salary_min: state.salary_min,
    salary_max: state.salary_max,
    salary_known: state.salary_known ? "1" : "",
    salary_disclosed: state.salary_disclosed ? "1" : "",
    confidence: state.confidence,
    max_age_days: state.max_age_days,
    // Not in the Saved view: a listing someone saved stays on their
    // list whatever the classifier makes of it.
    roles: !state.starred_only && state.roles === "tech" ? "tech" : "",
  };
}

// URL query-param syncing: filters/sort/page state and the open job
// drawer both round-trip through the URL, so a bookmarked or shared link
// reproduces the exact view. buildShareParams/shareUrl/syncUrl (write)
// pair with applyStateFromUrl/applyStateToFilterUI (read) below.
// replaceState throughout here, not pushState -- a filter change or page
// turn isn't a distinct "page" a user expects Back to undo one keystroke
// at a time. Opening a job IS treated as a real navigation (see
// openJobDetailAndPush), so that one pushes instead.

// Only ever set for a non-default value, so a plain visit to /board
// stays a plain /board instead of growing every field's default into the URL.
function buildShareParams() {
  const p = new URLSearchParams();
  if (state.search) p.set("search", state.search);
  if (state.search_mode) p.set("search_mode", state.search_mode);
  if (state.department.length) p.set("department", state.department.join(","));
  if (state.seniority.length) p.set("seniority", state.seniority.join(","));
  if (state.company.length) p.set("company", state.company.join(","));
  if (state.country.length) p.set("country", state.country.join(","));
  if (state.city.length) p.set("city", state.city.join(","));
  if (state.workplace.length) p.set("workplace", state.workplace.join(","));
  if (state.skills.length) p.set("skills", state.skills.join(","));
  if (state.salary_min) p.set("salary_min", state.salary_min);
  if (state.salary_max) p.set("salary_max", state.salary_max);
  if (state.salary_known) p.set("salary_known", "1");
  if (state.salary_disclosed) p.set("salary_disclosed", "1");
  if (state.confidence !== "all") p.set("confidence", state.confidence);
  if (state.max_age_days) p.set("max_age_days", state.max_age_days);
  if (state.starred_only) p.set("starred", "1");
  if (state.roles !== "tech") p.set("roles", state.roles);
  // Against the order this view would take on its own (see
  // followSearchSort), not against "age" flat: with a search in the box
  // that IS relevance, so a reader who chose Newest has to have it
  // written down or the link would come back ranked.
  if (state.sort !== (state.search ? "relevance" : "age")) p.set("sort", state.sort);
  if (state.dir !== "asc") p.set("dir", state.dir);
  if (state.offset) p.set("offset", String(state.offset));
  if (selectedJobId) p.set("job", selectedJobId);
  return p;
}

function shareUrl() {
  const qsStr = buildShareParams().toString();
  return location.pathname + (qsStr ? `?${qsStr}` : "");
}

// Called from loadJobs() so every state-mutating handler (they all call
// loadJobs() right after) keeps the URL in step with zero extra wiring
// at each individual call site.
function syncUrl() {
  history.replaceState(null, "", shareUrl());
}

// Populates `state` from a query string -- location.search on boot, or
// whatever new URL a Back/Forward navigation hands back via popstate.
// Anything absent keeps state's existing default; an absent param means
// One gate for every filter value that reaches `state`, whatever it came
// from. Both sources are user-editable and neither was checked: a URL is
// hand-typed, shared and truncated, and localStorage is whatever a
// previous version of this file happened to leave there.
//
// Found live: ?max_age_days=abc sent "abc" straight to the API, which
// answers 400, so the board showed "Could not load jobs". That alone
// would be a bad link. But max_age_days is also persisted, so the junk
// was saved, and every later visit to the plain site reloaded it and
// broke again. A single malformed link locked a browser out of the board
// until its site data was cleared, with the only escape a Reset button
// inside a panel the reader has no reason to open.
//
// An invalid value is dropped, never clamped to something plausible:
// silently turning ?max_age_days=-5 into 30 would show results the URL
// did not ask for. The one exception is offset, which was already
// floored at 0 before this existed.
// "match" has no column header to click. It is not a column: it is what
// the board orders by while a CV match is on, set when the match arrives
// and dropped the moment someone clicks a real header.
// "relevance" is in the same position as "match": no column header,
// chosen from the Sort dropdown, and meaningless without a search.
// "title" is deliberately absent: the Listing header no longer offers
// it (see board.html) because there is no index on title and the sort
// is a full pass over every open listing. A link still carrying
// sort=title falls back to the default rather than hanging the board.
const SORTABLE_KEYS = new Set(["age", "match", "relevance"]);

function cleanFilterValue(key, value) {
  switch (key) {
    case "salary_min":
    case "salary_max": {
      // Whole shekels, positive. Dropped rather than clamped, the same
      // rule max_age_days follows below: quietly turning a bad bound
      // into a plausible one would show results the URL did not ask
      // for, and this pair is persisted, so the lie would outlive the
      // link.
      if (value === "" || value == null) return "";
      const s = String(value);
      return /^\d+$/.test(s) && Number(s) > 0 ? s : undefined;
    }
    case "salary_known":
    case "salary_disclosed": {
      if (value === true || value === "1") return true;
      if (value === false || value === "" || value === "0" || value == null) return false;
      return undefined;
    }
    case "max_age_days": {
      if (value === "" || value == null) return "";
      const s = String(value);
      return /^\d+$/.test(s) && Number(s) > 0 ? s : undefined;
    }
    case "sort":
      return SORTABLE_KEYS.has(value) ? value : undefined;
    case "dir":
      return value === "asc" || value === "desc" ? value : undefined;
    default:
      return value;
  }
}

// "default," not "clear," so a partial URL (just ?q=... say) doesn't
// stomp the rest back to defaults.
function applyStateFromUrl(search) {
  const p = new URLSearchParams(search);
  // A CV match is the exception to "default, not clear" above. It says
  // show me my matches, not show me my matches inside whatever I last
  // typed, and the filters it would otherwise inherit are saved ones the
  // reader cannot see from the link they just clicked.
  //
  // It also digs out a specific hole. The first version of this feature
  // sent the skills as q=, a single substring match, so everyone who
  // clicked See my matches before today has the phrase "Python Azure
  // Linux CI/CD Git Terraform..." saved as their text search. Reported
  // live: the match arrived, the chip appeared, and the board said NO
  // RESULTS, because the saved q was still intersecting it.
  //
  // Reset first, then merge, so the link's own params still land. A
  // shared ?skills=...&department=... keeps its department.
  if (p.has("skills")) {
    state.search = "";
    state.search_mode = "";
    state.sortExplicit = false;
    state.department = [];
    state.seniority = [];
    state.company = [];
    state.country = [];
    state.city = [];
    state.workplace = [];
    state.confidence = "all";
    state.max_age_days = "";
    state.salary_min = "";
    state.salary_max = "";
    state.salary_known = false;
    state.salary_disclosed = false;
    state.starred_only = false;
  }
  if (p.has("search")) state.search = p.get("search");
  state.search_mode = p.get("search_mode") === "any" ? "any" : "";
  // Links older than the single box. Semicolons were the separator
  // there; here a space is, and quoting keeps a multi-word term whole.
  if (p.has("q") || p.has("keywords")) {
    state.search = [p.get("q") || "", ...(p.get("keywords") || "").split(";")]
      .map((t) => t.trim())
      .filter(Boolean)
      .map((t) => (t.includes(" ") ? `"${t}"` : t))
      .join(" ");
  }
  for (const key of ["department", "seniority", "company", "country", "city", "workplace", "skills"]) {
    if (p.has(key)) state[key] = p.get(key).split(",").filter(Boolean);
  }
  // A link from the CV analyser carries skills and no sort, and its
  // whole purpose is the ranking. Without this the board asks for
  // sort=age, the server honours it, and a ranked list comes back in
  // date order looking exactly like no match at all.
  if (p.has("skills") && !p.has("sort") && state.skills.length) state.sort = "match";
  if (p.has("confidence")) state.confidence = p.get("confidence");
  // Links older than the country filter. israel_only was replaced by
  // country=IL, and the two now return the same listings, so this is an
  // exact translation rather than the guess the old location param would
  // have needed. The API still answers israel_only for saved alerts.
  if (p.get("israel_only") === "1" && !state.country.includes("IL")) {
    state.country = [...state.country, "IL"];
  }
  if (p.has("max_age_days")) {
    const v = cleanFilterValue("max_age_days", p.get("max_age_days"));
    if (v !== undefined) state.max_age_days = v;
  }
  for (const key of ["salary_min", "salary_max", "salary_known", "salary_disclosed"]) {
    if (!p.has(key)) continue;
    const v = cleanFilterValue(key, p.get(key));
    if (v !== undefined) state[key] = v;
  }
  if (p.has("starred")) state.starred_only = p.get("starred") === "1";
  if (p.has("roles")) state.roles = p.get("roles") === "all" ? "all" : "tech";
  if (p.has("sort")) {
    const v = cleanFilterValue("sort", p.get("sort"));
    if (v !== undefined) {
      state.sort = v;
      state.sortExplicit = true;
    }
  } else {
    followSearchSort();
  }
  if (p.has("dir")) {
    const v = cleanFilterValue("dir", p.get("dir"));
    if (v !== undefined) state.dir = v;
  }
  if (p.has("offset")) {
    const n = parseInt(p.get("offset"), 10);
    state.offset = Number.isFinite(n) && n > 0 ? n : 0;
  }
}

// Filters persisting across browser sessions -- requested directly: the
// URL round-trip above only reproduces a filter set that's actually IN
// the address bar (a shared/bookmarked link), so a plain revisit to /board
// after closing the tab landed back on hardcoded defaults regardless of
// what was last picked. Same offset/job exclusions as buildShareParams,
// for the same reason (a fresh visit shouldn't resume on page 3, or with
// a job drawer open) -- everything else that's a real filter choice
// round-trips, sort/dir included.
const FILTERS_KEY = "iljobs_filters";
const PERSISTED_FILTER_KEYS = [
  "search", "department", "seniority", "company", "country", "city",
  "workplace", "skills", "confidence", "max_age_days", "starred_only",
  "sort", "dir", "roles", "salary_min", "salary_max", "salary_known", "salary_disclosed",
];

function saveFiltersToStorage() {
  try {
    const saved = {};
    for (const key of PERSISTED_FILTER_KEYS) saved[key] = state[key];
    localStorage.setItem(FILTERS_KEY, JSON.stringify(saved));
  } catch {
    // Full quota or unavailable (private browsing) -- same as
    // setCachedJobs below, a pure UX nicety, not worth failing over.
  }
}

// Loaded as the new BASELINE before applyStateFromUrl runs, not after --
// that function only ever overrides keys actually present in the query
// string (see its own comment), so calling it second means an explicit
// URL param always wins over whatever's saved locally, never the other
// way around. Unknown/malformed storage (an older shape, a hand-edited
// value) is ignored key-by-key rather than rejecting the whole thing.
function applyStoredFilters() {
  let saved;
  try {
    saved = JSON.parse(localStorage.getItem(FILTERS_KEY) || "null");
  } catch {
    return;
  }
  if (!saved || typeof saved !== "object") return;
  // Saved by a version that had two boxes. Fold them into the one box
  // rather than dropping them: somebody had a search running, and this
  // is the visit where they find out it changed shape.
  // The same translation applyStateFromUrl does for an old link. Someone
  // browsing with Israel-only ticked should still be looking at Israeli
  // listings after the checkbox it lived in stops existing.
  if (saved.israel_only && !Array.isArray(saved.country)) {
    saved.country = ["IL"];
  } else if (saved.israel_only && !saved.country.includes("IL")) {
    saved.country = [...saved.country, "IL"];
  }
  if (!("search" in saved) && (saved.q || saved.keywords)) {
    saved.search = [saved.q || "", ...String(saved.keywords || "").split(";")]
      .map((t) => t.trim())
      .filter(Boolean)
      .map((t) => (t.includes(" ") ? `"${t}"` : t))
      .join(" ");
  }
  for (const key of PERSISTED_FILTER_KEYS) {
    if (!(key in saved)) continue;
    // Validated on the way out as well as in, so a browser already
    // holding a poisoned value from before this existed heals itself on
    // the next visit instead of needing its site data cleared.
    const value = cleanFilterValue(key, saved[key]);
    if (value !== undefined) state[key] = value;
  }
}

// Reflects `state` (just populated by applyStateFromUrl) into the actual
// filter controls. setSelected()/a direct .value assignment don't fire
// onChange, so this never double-triggers loadJobs() on its own -- the
// caller (boot, or the popstate handler) does that once, itself, after.
// Requires wireFilters() to have already run (msDepartment etc. assigned).
function applyStateToFilterUI() {
  setSearchBox(state.search);
  document.getElementById("f-date-posted").value = state.max_age_days || "";
  paintViewSwitch();
  renderFilterRail();
  renderActiveChips();
  setActiveSortHeader(state.sort, state.dir);
}

// Stale-while-revalidate for the job list: keyed by the exact
// filter/sort/page combo, so a revisit with the same view renders
// instantly from whatever was cached last time instead of sitting on a
// loading screen, while the real fetch still always runs in the
// background and silently replaces it the moment fresh data lands.
// Reported live: every visit meant a few seconds of "Loading
// listings..." even though the underlying data rarely changes that
// fast (the EventBridge-scheduled scrape-fast Lambda re-polls every 5
// min, most visits land well inside that window).
const JOBS_CACHE_PREFIX = "iljobs_jobs_cache:";

function getCachedJobs(params) {
  try {
    const raw = localStorage.getItem(JOBS_CACHE_PREFIX + params);
    const parsed = raw ? JSON.parse(raw) : null;
    // An empty cached result is treated as a miss, never rendered.
    // Reported live: the board showed "No listings match these filters"
    // with no filters set and 109,878 jobs available, because a single
    // empty response had been cached at some point and the cache-first
    // path renders whatever it finds. A stale result with jobs in it is
    // useful while the real one loads; a stale EMPTY one tells the user
    // something false and looks identical to a broken board. It also
    // outlives the transient that produced it forever, since nothing
    // ever overwrites it until a fetch succeeds. Skeletons instead.
    return parsed && Array.isArray(parsed.jobs) && parsed.jobs.length ? parsed : null;
  } catch {
    return null;
  }
}

function setCachedJobs(params, data) {
  try {
    // Same reasoning as getCachedJobs: never persist an empty result.
    // A genuine no-match is cheap to re-ask for and must not be able to
    // survive as a false "the board is empty" on the next visit.
    if (!data || !Array.isArray(data.jobs) || !data.jobs.length) return;
    localStorage.setItem(JOBS_CACHE_PREFIX + params, JSON.stringify(data));
  } catch {
    // Full quota or unavailable (private browsing) -- this is a pure UX
    // nicety, silently skip rather than break the real fetch over it.
  }
}

// Loading affordances. Three of them, each scoped to a different kind of
// wait, because one spinner for all of them is what makes an app feel
// slow: a full-screen blocker for a 200ms background refetch reads as
// "everything stopped" when nothing did.
//
//   skeleton rows  -> first load, nothing on screen yet
//   the load bar   -> a refetch while real results are still readable
//   .btn-busy      -> one control the user just clicked
const SKELETON_ROWS = 16;

// The empty and error boxes stand where the rows were, not above a
// header row with nothing under it. Hiding the table when one shows
// also keeps its top where it was: a box appearing above sixteen
// skeleton rows pushed the whole table down, measured live as a 0.05
// layout shift, and a box appearing in place of it moves nothing.
function showJobsTable(on) {
  document.querySelector("table.jobs").style.display = on ? "" : "none";
}

function jobsSkeletonHtml(n = SKELETON_ROWS) {
  // The row shape lives in board.html's #skeleton-row template, which
  // the page stamps in before this file has loaded; this reads the same
  // template so there is one skeleton row, not two that drift apart.
  // aria-hidden throughout: a screen reader gets the status line
  // instead, not a page of nothing.
  const tpl = document.getElementById("skeleton-row");
  return tpl ? tpl.innerHTML.repeat(n) : "";
}

// How many bones to show while a fetch is out: as many rows as are on
// screen now, so the page keeps its height and nothing under the table
// (pagination, and on a phone the whole statistics column) jumps up and
// back down. Never fewer than the first-paint sixteen, never more than
// a page.
function skeletonRowCount() {
  const shown = lastJobsResponse?.jobs?.length || 0;
  return Math.min(PAGE_SIZE, Math.max(SKELETON_ROWS, shown));
}

// The precomputed first pages (loader/bootstrap.py), published by the
// merge Lambda and served straight from CloudFront's edge. Used only for
// the views listed below, and only when a file's recorded params match
// exactly what this build was about to request, so the two can never
// drift into rendering something subtly wrong: any mismatch just falls
// through to a normal fetch.
//
// Worth it because api/db.py re-downloads the whole ~1.26GB jobs-read.db
// inside a user's request whenever its ETag moves. Measured across 809
// real requests: median 1.49s, p90 9.08s, max 25.00s, which is the API
// Lambda's own timeout. This file is 4.4KB gzipped and involves no
// Lambda at all.
//
// Two of them, keyed by the exact query string they answer. The country
// page exists because the geo prompt's accept button asks for precisely
// this view and nothing was precomputed behind it: measured over 50 cold
// visits, five of them waited 14.3 seconds while the other 45 waited
// under 17ms, the difference being whether the API instance already had
// the snapshot. The keys must match loader/bootstrap.py's VIEWS, and a
// request for anything else gets no file and a normal fetch.
const BOOTSTRAP_FILES = {
  "confidence=all&roles=tech&sort=age&dir=asc&limit=50&offset=0": "/bootstrap.json",
  "country=IL&confidence=all&roles=tech&sort=age&dir=asc&limit=50&offset=0": "/bootstrap-il.json",
};
const bootstrapFetches = new Map();
function getBootstrap(params) {
  const url = BOOTSTRAP_FILES[params];
  // Any other view: nothing is precomputed for it, so don't spend a
  // request finding that out.
  if (!url) return Promise.resolve(null);
  // Once per file per page. board.html preloads the default one, so that
  // one usually resolves from the browser's own preload cache.
  if (!bootstrapFetches.has(url)) {
    bootstrapFetches.set(url, fetch(url)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null)); // missing or unreachable is a normal, expected state
  }
  return bootstrapFetches.get(url);
}

// Which request the page is currently waiting for.
//
// Reported live: type quickly and the board sticks on the previous
// filter. Nothing ordered the responses, so two requests in flight
// finished in whatever order the network gave them, and a slow one for
// the old filter painted over a fast one for the new. Reproduced with a
// stub: search box reading "bbb", rows reading "RESULT FOR aaa". A cold
// Lambda answering after a warm one does this for real.
//
// A counter rather than a timestamp: two calls in the same millisecond
// are exactly the case this has to separate. Every await in loadJobs is
// followed by a check, because any of them can be the point a newer
// call overtakes this one.
let jobsRequestSeq = 0;
let jobsInFlight = null;
// The count is its own request now (see loadJobCount), so it needs its
// own controller: a count for a search nobody is waiting on must not land.
let jobCountInFlight = null;

// background: a timer or tab-return refresh, not something the reader
// did. Only those may hold new rows back (see holdForReader).
async function loadJobs({ background = false, append: wantAppend = false } = {}) {
  let append = wantAppend;
  // An infinite list has one starting point. state.offset is where the
  // NEXT page begins, which loadMoreJobs walks forward, so any load that
  // replaces the list has to put it back or it fetches from wherever the
  // reader happened to have scrolled to. Reported live: sign out after
  // scrolling and the board redrew page four as though it were page one,
  // leaving the reader parked below the end of a list a fifth the size.
  if (!append && !background) {
    state.offset = 0;
    listReloading = true;
    // Out of view at once, so the observer has nothing to fire on while
    // the old rows are still standing.
    const more = document.getElementById("jobs-more");
    if (more) more.hidden = true;
  }
  // A background refresh of a grown list is not a refresh, it is a
  // truncation: it asks for one page and the no-flicker comparison then
  // replaces two hundred rows with fifty. New listings still arrive
  // through the same banner they always did, on the next real load.
  if (background && ((lastJobsResponse && lastJobsResponse.jobs) || []).length > PAGE_SIZE) return;
  const seq = ++jobsRequestSeq;
  if (!background) clearHeldJobs();
  // Cancel rather than ignore. Ignoring would still cost the reader's
  // bandwidth and our Lambda invocation for an answer nobody will see,
  // and someone typing a ten-character search fires several of these.
  if (jobsInFlight) jobsInFlight.abort();
  const inFlight = new AbortController();
  jobsInFlight = inFlight;

  // Every state-mutating handler in this file calls loadJobs() right
  // after, so state is already final for this transition -- one call
  // here covers all of them instead of one at each call site.
  syncUrl();
  saveFiltersToStorage();

  const tbody = document.getElementById("jobs-body");
  const starred = getStarred();
  renderActiveChips();
  renderMatchPanel();
  paintViewSwitch();

  if (state.starred_only) {
    // Hands over this call's seq and controller rather than starting
    // its own, so a slow saved fetch loses to a newer view the same way
    // every other request here does.
    renderStarredOnly(starred, seq, inFlight);
    // Saved is a client-local view, but the rail still describes
    // whatever else is selected, so its counts are still due.
    scheduleFacets();
    return;
  }

  document.getElementById("jobs-error").style.display = "none";
  document.getElementById("jobs-empty").style.display = "none";

  const params = qs({
    ...currentFilterParams(),
    sort: state.sort,
    dir: state.dir,
    limit: PAGE_SIZE,
    offset: state.offset,
  });

  let cached = getCachedJobs(params);

  // Nothing cached locally, but this might be the default view, which is
  // already sitting on the edge. Only await it when there's nothing
  // better to show, so a returning visitor never waits on it.
  if (!cached) {
    const boot = await getBootstrap(params);
    if (seq !== jobsRequestSeq) return; // a newer filter won while that resolved
    if (boot && boot.params === params && boot.jobs && boot.jobs.jobs.length) {
      cached = boot.jobs;
    }
  }

  showJobsTable(true);
  if (append) {
    // Nothing here: the rows on screen stay exactly as they are and the
    // new ones land after them. Skeletons or a cached render would both
    // wipe the list this call is supposed to be extending.
  } else if (background) {
    // The screen already shows this view. Drawing the cached copy first
    // could put up rows a previous refresh held back (holdForReader
    // caches what it holds), which is the jump holding exists to avoid.
    // What changed is decided against the rows on screen instead.
    cached = lastJobsResponse;
  } else if (cached) {
    document.getElementById("jobs-loading").textContent = "";
    lastJobsResponse = cached;
    renderJobs(cached, starred);
    paintMoreButton(cached);
  } else {
    // Bones, not "Loading listings…". Same height as the rows about to
    // replace them, so the page doesn't reflow when data lands.
    showJobsTable(true);
    tbody.innerHTML = jobsSkeletonHtml(skeletonRowCount());
    document.getElementById("jobs-loading").textContent = "Loading listings";
  }

  // The total runs the whole WHERE a second time, and for a typed search
  // that WHERE is four substring scans over every row. Paying it twice
  // put a search at three seconds. A foreground load asks for the rows
  // alone and picks the number up afterwards, while the reader is
  // already reading. A background refresh still asks for both: nobody is
  // waiting on it, and a response shaped like the cached one is what
  // lets the no-flicker comparison below do its job.
  const deferCount = !background;
  try {
    let data = await getJSON(`/jobs?${params}${deferCount ? "&count=skip" : ""}`, { signal: inFlight.signal });
    if (seq !== jobsRequestSeq) return;
    // Revisiting a view we already have a number for: keep showing it
    // rather than blanking the total and putting it back a moment later.
    // Same params means the same WHERE, so the cached count is exactly
    // as fresh as the cached rows already on screen, and loadJobCount
    // replaces it with the truth either way.
    if (data.total === null && cached && typeof cached.total === "number") data.total = cached.total;
    document.getElementById("jobs-loading").textContent = "";
    // Skip the re-render when the background refresh just confirms
    // nothing changed -- avoids a jarring flicker/scroll-reset for what
    // will be the common case (revisiting within the same 5-min window).
    const changed = append || !cached || JSON.stringify(data) !== JSON.stringify(cached);
    setCachedJobs(params, data);
    if (changed && background && holdForReader(data, starred)) return;
    if (append && params.replace(/&?offset=\d+/, "") !== listParams) {
      // The filters moved while this page was in the air. It belongs to
      // a list that is no longer on screen, so it is dropped rather than
      // appended to a different one.
      append = false;
    }
    if (append) {
      // Deduped against what is already on screen as well as within the
      // page: the same listing can arrive twice across two pages when a
      // company is reachable through two ATS tokens.
      const seen = new Set(((lastJobsResponse && lastJobsResponse.jobs) || [])
        .map((j) => (j.url || "").trim().toLowerCase() || `${j.company_domain} ${j.external_id || j.id}`));
      data = { ...data, jobs: dedupeJobs(data.jobs, seen) };
      // One list, grown. lastJobsResponse is what findKnownJob and the
      // pane's own prev/next read, so the accumulated rows have to live
      // there rather than only in the DOM.
      const all = [...((lastJobsResponse && lastJobsResponse.jobs) || []), ...data.jobs];
      lastJobsResponse = { ...data, jobs: all, total: data.total ?? lastJobsResponse?.total ?? null };
      appendJobRows(data.jobs, starred);
      renderResultCount(lastJobsResponse);
    } else {
      data = { ...data, jobs: dedupeJobs(data.jobs) };
      lastJobsResponse = data;
      listParams = params.replace(/&?offset=\d+/, "");
      if (changed) {
        clearHeldJobs();
        renderJobs(data, starred);
      }
    }
    paintMoreButton(data);
    if (deferCount) loadJobCount(params, seq);
    // Last, and debounced. The rail's counts are the most expensive
    // thing the API answers and the least urgent thing on screen.
    scheduleFacets();
  } catch (err) {
    // An abort is this function cancelling itself, not a failure, and a
    // stale rejection belongs to a filter nobody is looking at.
    if (seq !== jobsRequestSeq || err.name === "AbortError") return;
    document.getElementById("jobs-loading").textContent = "";
    if (!cached) { tbody.innerHTML = ""; showJobsTable(false); } // bones would otherwise sit there forever behind the error
    // A cached render is still on screen and still useful -- don't bury
    // it under an error banner over a transient fetch failure.
    if (!cached) {
      const errEl = document.getElementById("jobs-error");
      errEl.textContent = `Could not load jobs: ${err.message}`;
      errEl.style.display = "block";
    }
  } finally {
    // Always, never gated on the sequence: a stale request that declined
    // to clear this would leave the list believing it is still being
    // replaced, and the infinite scroll sentinel would never fire again.
    if (!append && !background) listReloading = false;
  }
}

// New rows a background refresh found while the reader was further down
// the list. Rendering them would push everything down by their height and
// move whatever the reader was looking at, so they wait behind a button,
// or until the reader is back at the top of the list. A refresh that only
// changed rows already on screen renders in place.
let heldJobs = null;

function jobsListTop() {
  const table = document.getElementById("jobs-body").closest("table");
  return table ? table.getBoundingClientRect().top : 0;
}

// The topbar is sticky, and taller on a phone (66px) than on a desktop
// (58px), so it is measured. The list's top edge below it is on screen.
function topbarBottom() {
  const bar = document.querySelector(".topbar");
  return bar ? bar.getBoundingClientRect().bottom : 0;
}

function holdForReader(data, starred) {
  const shown = new Set([...document.querySelectorAll("#jobs-body tr[data-id]")].map((tr) => tr.dataset.id));
  const fresh = data.jobs.filter((j) => !shown.has(j.id)).length;
  if (!fresh || jobsListTop() >= topbarBottom()) return false;
  heldJobs = { data, starred };
  const btn = document.getElementById("new-listings");
  btn.parentElement.style.top = `${Math.round(topbarBottom()) + 12}px`;
  btn.textContent = `${fmtInt(fresh)} new listing${fresh === 1 ? "" : "s"} · Show`;
  btn.hidden = false;
  return true;
}

function showHeldJobs({ scroll = true } = {}) {
  if (!heldJobs) return;
  const { data, starred } = heldJobs;
  clearHeldJobs();
  lastJobsResponse = data;
  renderJobs(data, starred);
  paintMoreButton(data);
  if (scroll) {
    window.scrollTo({ top: window.scrollY + jobsListTop() - topbarBottom() - 12, behavior: "smooth" });
  }
}

function clearHeldJobs() {
  heldJobs = null;
  const btn = document.getElementById("new-listings");
  if (btn) btn.hidden = true;
}

// Every empty state says the same two things: that there is nothing to
// show, then why.
// The most recently applied filter, paired with the way to undo it.
// Order matches the chips, so "the last one" means the one furthest
// right, which is the one the reader most likely just added.
function lastAppliedFilter() {
  const undo = [
    ["search", () => state.search, () => { state.search = ""; setSearchBox(""); }, () => `"${state.search}"`],
    ["max_age_days", () => state.max_age_days, () => { state.max_age_days = 0; const el = document.getElementById("f-date-posted"); if (el) el.value = ""; }, () => `posted in the last ${state.max_age_days} days`],
    ["workplace", () => state.workplace.length, () => { state.workplace = []; }, () => state.workplace.map((w) => WORKPLACE_LABELS[w] || w).join(", ")],
    ["city", () => state.city.length, () => { state.city = []; }, () => state.city.join(", ")],
    ["country", () => state.country.length, () => { state.country = []; }, () => state.country.map(countryLabel).join(", ")],
    ["company", () => state.company.length, () => { state.company = []; }, () => state.company.join(", ")],
    ["seniority", () => state.seniority.length, () => { state.seniority = []; }, () => state.seniority.map((x) => SENIORITY_LABELS[x] || x).join(", ")],
    ["department", () => state.department.length, () => { state.department = []; }, () => state.department.join(", ")],
  ];
  for (const [, has, clear, label] of undo) if (has()) return { label: label(), clear };
  return null;
}

function emptyState(line) {
  const last = lastAppliedFilter();
  return `<strong>No roles match</strong><span>${line}</span>`
    + (last
      ? `<button type="button" class="btn ghost empty-undo" id="empty-undo">Remove ${escapeHtml(last.label)}</button>`
      : "");
}

// Wired after the state is written, because emptyState is a string.
function wireEmptyUndo() {
  const btn = document.getElementById("empty-undo");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const last = lastAppliedFilter();
    if (!last) return;
    last.clear();
    state.offset = 0;
    railApply();
  });
}

// The words the current search is matching on, lowercased. Read from
// state rather than the response so a row highlights the moment it
// renders, including from cache.
function searchTermsInPlay() {
  const out = [];
  for (const m of (state.search || "").matchAll(/"([^"]*)"|(\S+)/g)) {
    const term = (m[1] || m[2] || "").trim().toLowerCase();
    if (term) out.push(term);
  }
  return out.slice(0, 10);
}

// With a search in the box, relevance is the order that answers it; with
// an empty box there is nothing to rank, so newest is. The board follows
// the box until the reader picks an order themselves, and then leaves it
// alone. sort=relevance with no search reads as newest on the server
// too, so the two never disagree.
function followSearchSort() {
  if (state.sortExplicit) return;
  const wanted = state.search ? "relevance" : "age";
  if (state.sort !== wanted) setActiveSortHeader(wanted, "asc");
}

// Marks the search words inside text a row shows, so a reader can see
// why it came back. Escapes first, then wraps: the mark tags are the
// only markup this ever adds.
function highlight(text) {
  const safe = escapeHtml(text ?? "");
  // A one-letter word marks half the alphabet on every row (2,094 marks
  // across 50 rows, measured), which tells the reader nothing. It still
  // filters; it just is not worth pointing at.
  const terms = searchTermsInPlay().filter((t) => t.length > 1);
  if (!terms.length) return safe;
  const pattern = terms
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .sort((a, b) => b.length - a.length)
    .join("|");
  try {
    return safe.replace(new RegExp(`(${pattern})`, "gi"), "<mark>$1</mark>");
  } catch {
    return safe; // a term that will not compile is not worth failing a row over
  }
}

// A search that found nothing says what it asked, rather than leaving the
// reader to guess that every word had to appear. The ways out are offered
// as buttons, never taken automatically: results must always answer the
// question that was actually put.
function emptySearchState(data) {
  const terms = (data.search && data.search.terms) || searchTermsInPlay();
  const mode = (data.search && data.search.mode) || "all";
  const shown = terms.map((t) => `<code>${escapeHtml(t)}</code>`).join(" · ");
  const lines = [
    `<strong>No roles match</strong>`,
    `<span>${mode === "any" ? "No listing mentions any of these words" : `No listing has all ${terms.length} of these words`}: ${shown}</span>`,
  ];
  const actions = [];
  if (mode === "all" && terms.length > 1) {
    actions.push(`<button type="button" class="btn ghost btn-small" data-search-any>Search any of these words</button>`);
    for (const term of terms.slice(-2).reverse()) {
      actions.push(`<button type="button" class="btn ghost btn-small" data-drop-term="${escapeHtml(term)}">Without ${escapeHtml(term)}</button>`);
    }
  }
  actions.push(`<button type="button" class="btn ghost btn-small" data-clear-search>Clear the search</button>`);
  // The words may not be what emptied it. When something else is on
  // too, offer that back as well rather than making the reader find it.
  const other = lastAppliedFilter();
  if (other && !String(other.label).startsWith('"')) {
    actions.push(`<button type="button" class="btn ghost btn-small" id="empty-undo">Remove ${escapeHtml(other.label)}</button>`);
  }
  lines.push(`<span class="empty-actions">${actions.join("")}</span>`);
  return lines.join("");
}

// The terms past the cap were dropped in silence, so a reader could be
// looking at results for four of their six words and have no way to know.
function renderSearchNotice(data) {
  const el = document.getElementById("search-notice");
  if (!el) return;
  const info = data && data.search;
  const ignored = (info && info.ignored) || [];
  const bits = [];
  if (ignored.length) {
    bits.push(`Searching the first ${MAX_SEARCH_TERMS} words. Not used: ` +
              ignored.map((t) => `<code>${escapeHtml(t)}</code>`).join(" · "));
  }
  if (info && info.mode === "any" && (info.terms || []).length > 1) {
    bits.push(`Any of these words: ${info.terms.map((t) => `<code>${escapeHtml(t)}</code>`).join(" · ")}` +
              ` <button type="button" class="link-inline" data-search-all>Require all</button>`);
  }
  el.innerHTML = bits.join(" ");
  el.hidden = !bits.length;
}

// Mirrors job_filters.MAX_SEARCH_TERMS.
const MAX_SEARCH_TERMS = 10;

// The saved view used to filter lastJobsResponse, which is whichever 50
// rows the board happens to be holding. Save a job, change one filter,
// open Saved, and it showed nothing. So it asks for the ids instead.
// include_closed/include_outdated go with them on purpose. A listing
// you saved three weeks ago and are still chasing is exactly the one
// the default filters would drop, and having it vanish from your own
// saved list is worse than showing it closed.
async function renderStarredOnly(starred, seq, inFlight) {
  document.getElementById("jobs-loading").textContent = "";
  document.getElementById("jobs-error").style.display = "none";
  const more = document.getElementById("jobs-more");
  if (more) more.hidden = true;
  const tbody = document.getElementById("jobs-body");
  const empty = document.getElementById("jobs-empty");

  // Stays synchronous for the nothing-saved case. No request, no bones,
  // no flicker on the way to an empty list.
  if (!starred.size) {
    empty.innerHTML = emptyState("You have not saved any listings yet.");
    empty.style.display = "block";
    tbody.innerHTML = "";
    showJobsTable(false);
    document.getElementById("result-count").innerHTML = "";
    return;
  }
  empty.style.display = "none";

  // 200 is the endpoint's cap on ids. The tail of the set is the recent
  // end (toggleStar appends), so an outlier with 300 saved jobs sees the
  // 200 they starred most recently rather than the 200 they forgot.
  const ids = [...starred].slice(-200);
  const params = qs({
    ids: ids.join(","),
    include_closed: 1,
    include_outdated: 1,
    limit: 200,
  });

  showJobsTable(true);
  tbody.innerHTML = jobsSkeletonHtml(skeletonRowCount());
  document.getElementById("jobs-loading").textContent = "Loading listings";
  document.getElementById("result-count").innerHTML = "";
  try {
    const data = await getJSON(`/jobs?${params}`, { signal: inFlight.signal });
    if (seq !== jobsRequestSeq) return;
    document.getElementById("jobs-loading").textContent = "";
    // Same bookkeeping renderJobs does, so clicking a saved row opens
    // the detail panel from data already in hand instead of refetching.
    lastJobsResponse = data;
    const rows = data.jobs || [];
    if (!rows.length) {
      // Saved ids that the API no longer knows at all. Rare, and it
      // reads as a bug if the view just goes blank.
      empty.innerHTML = emptyState("None of your saved listings are available any more.");
      empty.style.display = "block";
      tbody.innerHTML = "";
      return;
    }
    renderJobRows(rows, starred);
    document.getElementById("result-count").innerHTML = `<b>${rows.length}</b> saved`;
  } catch (err) {
    if (seq !== jobsRequestSeq || err.name === "AbortError") return;
    document.getElementById("jobs-loading").textContent = "";
    tbody.innerHTML = ""; // bones would otherwise sit there forever behind the error
    showJobsTable(false);
    const errEl = document.getElementById("jobs-error");
    errEl.textContent = `Could not load your saved listings: ${err.message}`;
    errEl.style.display = "block";
  }
}

// Which skills the server matched on, straight from the response rather
// than from state: the two can differ for a moment during a refetch, and
// marking a chip that did not actually put the row here is a small lie
// in the one place the reader is looking for an explanation.
let matchedSkills = new Set();

// What the count is counting, in the words of the view the reader is in:
// every role on the board, the ones their filters leave, or the ones their
// CV matches. activeFilterSummary is the list the sidebar's scope line
// names, so the two never disagree about whether a filter is on.
function resultNoun() {
  if (currentView() === "matches") return "roles matching your CV";
  const noun = state.roles === "tech" && !state.starred_only ? "tech roles" : "roles";
  return activeFilterSummary().length ? `matching ${noun}` : noun;
}

// The second half of a foreground load: the number, once the rows are
// already on screen. Same sequence guard as the rows, so a count for a
// search the reader has moved on from is dropped rather than drawn.
//
// A count that never arrives is not an error worth a banner. The rows
// are up and useful, and the line simply keeps reading "Showing 1-50"
// without a total.
async function loadJobCount(params, seq) {
  if (jobCountInFlight) jobCountInFlight.abort();
  const inFlight = new AbortController();
  jobCountInFlight = inFlight;
  try {
    const data = await getJSON(`/jobs?${params}&count=only`, { signal: inFlight.signal });
    if (seq !== jobsRequestSeq || !lastJobsResponse) return;
    lastJobsResponse.total = data.total;
    // Back into the cache with the number in it. The rows were stored a
    // moment ago with total null, and without this a reader returning to
    // a view they have already seen watches the total blink out and come
    // back. getCachedJobs reads localStorage, so mutating what it
    // returned would change nothing.
    setCachedJobs(params, lastJobsResponse);
    renderResultCount(lastJobsResponse);
    paintMoreButton(lastJobsResponse);
  } catch (err) {
    if (err.name !== "AbortError" && seq === jobsRequestSeq) console.debug("count:", err.message);
  } finally {
    if (jobCountInFlight === inFlight) jobCountInFlight = null;
  }
}

// The one line above the table. Its own function because the number now
// arrives after the rows: this redraws a line instead of the table.
// How long ago the loader last wrote to the database, from /stats. It is
// the one number on the board that says whether what you are reading is
// current. Absent until stats land, and the line simply omits it then.
function updatedAgo() {
  // lastCheckedAt, not latestStats. /stats.json is a precomputed
  // artifact: its freshness block is frozen at the moment the file was
  // built, so reading it here dated the board by however long ago that
  // was. Measured 2026-09-24: the artifact said 2.8 minutes, the file
  // was 32 minutes old, and the loader had actually written 1 minute
  // earlier, so the board claimed half an hour of staleness that did
  // not exist. lastCheckedAt is the /api/health poll's own answer,
  // refreshed every 120s and monotonic (see setLastCheckedAt).
  if (lastCheckedAt === null) return "";
  const mins = Math.max(0, Math.round((Date.now() - lastCheckedAt) / 60000));
  if (!Number.isFinite(mins)) return "";
  if (mins < 1) return "Updated just now";
  if (mins < 60) return `Updated ${mins} min ago`;
  const hrs = Math.round(mins / 60);
  return hrs < 48 ? `Updated ${hrs}h ago` : `Updated ${Math.round(hrs / 24)}d ago`;
}

// The list column's head: the total on the title line, the range and the
// freshness under it. The old "Showing 1–50 of N" in the filter bar is
// gone, so the count is said once.
function renderResultCount(data) {
  const el = document.getElementById("result-count");
  const sub = document.getElementById("result-sub");
  // The pane's empty state says the same total. Drawn from here so the
  // two cannot disagree, and before the early return, so an empty
  // result set updates it rather than leaving the last one up.
  renderDetailEmpty();
  if (!el || !sub) return;
  if (!data || !data.jobs || !data.jobs.length) {
    el.textContent = `No ${resultNoun()}`;
    sub.textContent = updatedAgo();
    return;
  }
  // The list accumulates rather than paging, so the range always starts
  // at the first row and ends at however many are on screen.
  const from = 1;
  const total = data.total;
  // The range is true without the total, and it does not move when the
  // total lands, so nothing on the line jumps.
  const shown = data.jobs.length;
  const to = total == null ? shown : Math.min(shown, total);
  el.innerHTML = total == null
    ? `<b>${fmtInt(data.jobs.length)}+</b> ${escapeHtml(resultNoun())}`
    : `<b>${fmtInt(total)}</b> ${escapeHtml(resultNoun())}`;
  sub.textContent = [`Showing ${from}–${to}`, updatedAgo()].filter(Boolean).join(" · ");
}

// The rail's counts, asked for once the listings are on screen. See the
// comment on scheduleFacets for why the order matters.
let facetsTimer = 0;
function scheduleFacets() {
  clearTimeout(facetsTimer);
  facetsTimer = setTimeout(() => {
    refreshFacetOptions();
    // Last of all, and the heaviest. "Saved" is a client-local view
    // rather than an API filter, so its own scoped block still answers
    // for whatever else is selected and is still worth asking for.
    refreshScopedStats();
  }, 400);
}

function renderJobs(data, starred) {
  matchedSkills = new Set(data.matched_skills || []);
  renderSearchNotice(data);

  if (!data.jobs.length) {
    document.getElementById("jobs-empty").innerHTML = state.search
      ? emptySearchState(data)
      : emptyState("No listings match these filters.");
    document.getElementById("jobs-empty").style.display = "block";
    wireEmptyUndo();
    document.getElementById("jobs-body").innerHTML = "";
    showJobsTable(false);
    document.getElementById("result-count").innerHTML = "";
    return;
  }
  document.getElementById("jobs-empty").style.display = "none";
  renderJobRows(data.jobs, starred);
  renderResultCount(data);
  // A filter change redraws every row, which drops the .selected class
  // with them. The pane is still open on that listing, so the row it
  // came from is marked again when it survived the change, and the pane
  // is closed when it did not: a pane describing a listing the filters
  // have just excluded is the board contradicting itself.
  if (selectedJobId !== null) {
    const still = data.jobs.some((j) => j.id === selectedJobId);
    if (still) {
      document.querySelector(`tr[data-id="${selectedJobId}"]`)?.classList.add("selected");
      paneHead(findKnownJob(selectedJobId));
      revealSelectedRow();
    } else {
      closeJobDetailAndSync();
    }
  }
}

// "Company · Department · Location (Workplace)" -- one scannable line
// under the title, LinkedIn-card style, standing in for what used to be
// three separate table columns (Company was already folded in as the
// row's ".company" div; Location and Category had their own <td>s).
// What to actually call a company on screen. company_domain is an
// internal key, not an identity: discovery guesses {ats-token}.com and
// keeps the guess even when it resolves to nothing, so a fifth of them
// are hosts that never existed (headoutcareers.com for Headout,
// informagroupplc.com for Informa Group Plc.). company_name is the name
// the company's own ATS reports, resolved once by
// resolve_company_names.py. Falls back to the domain for Lever and
// Workday, which expose no name anywhere, about 1% of companies.
//
// A company with no website of its own is keyed under the reserved
// .invalid name (paragon-solutions.invalid). Until its name is resolved,
// that key reads as "Paragon Solutions", never as the key itself.
function companyLabel(j) {
  return j.company_name || domainLabel(j.company_domain);
}

function domainLabel(domain) {
  const m = /^(.+)\.invalid$/.exec(domain || "");
  if (!m) return domain;
  return m[1].split(/[-.]/).filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(" ");
}

function jobMetaLine(j) {
  // The company gets its own span so it can carry more contrast than the
  // rest of the line. Everything here used to be one flat grey, so the
  // employer read with exactly the same weight as the department it
  // happens to be hiring into, and a reader scanning the column had
  // nothing to land on between the title and the location.
  //
  // It is a link to /company/<domain> when the row knows the domain. The
  // row's own click handler ignores anything inside an <a>, so this
  // navigates instead of opening the listing. A company with no domain,
  // or one whose domain is a .invalid placeholder we invented, has no
  // page to point at, so it stays plain text.
  const label = highlight(companyLabel(j));
  const domain = j.company_domain && !/\.invalid$/.test(j.company_domain) ? j.company_domain : "";
  const parts = [domain
    ? `<a class="job-company" href="/company/${encodeURIComponent(domain)}"
          title="Every open role at ${escapeHtml(companyLabel(j))}">${label}</a>`
    : `<span class="job-company">${label}</span>`];
  if (j.department) parts.push(highlight(j.department));
  if (j.location) parts.push(highlight(j.location));
  let line = parts.join(" · ");
  if (j.workplace_type) line += ` (${escapeHtml(WORKPLACE_LABELS[j.workplace_type] || j.workplace_type)})`;
  return line;
}

// The row's two middle lines. Who is hiring reads at full ink and where
// the job is reads muted under it, so a column of fifty rows sorts
// itself by employer first. One line each, both ellipsised.
//
// The design asks for the office or team on the where line, to tell two
// near-identical listings from one company apart. The jobs table has no
// such column (title, location, department, country, city and workplace
// are all of it), so the line is what we have and nothing is invented.
function jobWhoLine(j) {
  const label = highlight(companyLabel(j));
  const domain = j.company_domain && !/\.invalid$/.test(j.company_domain) ? j.company_domain : "";
  const who = domain
    ? `<a class="job-company" href="/company/${encodeURIComponent(domain)}"
          title="Every open role at ${escapeHtml(companyLabel(j))}">${label}</a>`
    : `<span class="job-company">${label}</span>`;
  return j.department ? `${who} · ${highlight(j.department)}` : who;
}

function jobWhereLine(j) {
  const parts = [];
  if (j.location) parts.push(highlight(j.location));
  if (j.workplace_type) parts.push(escapeHtml(WORKPLACE_LABELS[j.workplace_type] || j.workplace_type));
  return parts.join(" · ");
}

// What actually stands behind the number, in the reader's own terms.
// Three sources with genuinely different evidence, and the difference
// that matters most to someone reading it is whether the employer said
// this or we did. Both estimates say so outright rather than leaving it
// to the "Est." prefix.
// Both estimates are monthly gross, so both say so. A disclosed figure
// is quoted exactly as the employer wrote it, which may be annual or
// hourly, so its note claims no period at all.
const SALARY_SOURCE_NOTE = {
  disclosed: "Published by the employer on this listing, in their own terms.",
  table: "Our estimate of monthly gross pay, from Israeli market rates for this role and seniority. Not the employer's own figure.",
  estimated: "Our estimate of monthly gross pay, from what comparable roles actually pay at this company and location. Not the employer's own figure.",
};

// Real disclosed comp shown plainly; an estimate prefixed "Est." and
// never given the weight of a real number. Neither present just reads
// "Undisclosed," matching this board's principle of showing an absence
// as an absence rather than hiding it.
//
// Falls back to the older salary_is_estimate boolean, because a cached
// page or a bootstrap.json written before salary_source existed will
// arrive without it, and "table" is what every estimate was then.
// The arrow on Apply, drawn rather than typed. It was the character ↗
// (U+2197), which iOS renders as a colour emoji inside a button unless the
// font is told otherwise, so every Apply on an iPhone carried a blue emoji
// tile. Reported live. An inline SVG in currentColor looks the same on
// every platform and follows the button's own colour on hover.
const EXTERNAL_ARROW_SVG =
  '<svg class="external-arrow" viewBox="0 0 10 10" width="10" height="10" aria-hidden="true">'
  + '<path d="M2.5 7.5 7.5 2.5M3.5 2.5h4v4" fill="none" stroke="currentColor" stroke-width="1.6"'
  + ' stroke-linecap="round" stroke-linejoin="round"/></svg>';

// Drawn rather than typed, for the reason EXTERNAL_ARROW_SVG spells
// out: ☆ and ★ render as colour emoji inside a button on iOS unless the
// font is told otherwise, and they are two different glyph widths, so a
// row twitched sideways as it was saved.
const STAR_SVG =
  '<svg class="star-mark" viewBox="0 0 16 16" aria-hidden="true">'
  + '<path d="M8 1.6l1.95 3.95 4.35.63-3.15 3.07.74 4.33L8 11.53l-3.89 2.05.74-4.33L1.7 6.18l4.35-.63z"'
  + ' fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';

const MATCH_TICK_SVG =
  '<svg class="match-tick" viewBox="0 0 10 10" width="10" height="10" aria-hidden="true">'
  + '<path d="M2 5.3 4.1 7.4 8 3.2" fill="none" stroke="currentColor" stroke-width="1.7"'
  + ' stroke-linecap="round" stroke-linejoin="round"/></svg>';

const CLOSE_SVG =
  '<svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">'
  + '<path d="M3 3l8 8M11 3l-8 8" fill="none" stroke="currentColor" stroke-width="1.8"'
  + ' stroke-linecap="round"/></svg>';

// The board shows an estimate as an outlined chip: "Est." in the row's
// own ink, the range in green, nothing behind it. Disclosed pay is the
// employer's own figure and gets no "Est.".
//
// A listing with no salary shows nothing here at all. It used to say
// "Undisclosed" on every row, and since nine listings in ten have no
// figure that put the same grey word down the whole column, where it
// read as a property of the board rather than of the job. The word is
// still in the detail pane, where it answers a question somebody has
// actually asked by opening the listing.
function jobSalaryChip(j) {
  if (!j.salary_text) return "";
  const source = j.salary_source || (j.salary_is_estimate ? "table" : "disclosed");
  const isEstimate = source !== "disclosed";
  const note = SALARY_SOURCE_NOTE[source] || SALARY_SOURCE_NOTE.estimated;
  return `<span class="job-chip salary" data-salary-source="${escapeHtml(source)}" title="${escapeHtml(note)}">`
    + `${isEstimate ? '<span class="salary-est-label">Est.</span> ' : ""}`
    + `<span class="salary-range">${escapeHtml(j.salary_text)}</span></span>`;
}

// Three, and only three. A row listing eleven skills read as a wall
// before the title did, and the chip line has to share one row with the
// salary. Clicking one searches for it, which is what the old skill
// chips did and the only thing on a row that narrows the board.
const ROW_SKILLS_SHOWN = 3;

function jobSalaryLine(j) {
  if (!j.salary_text) return '<span class="job-salary undisclosed">Undisclosed</span>';
  const source = j.salary_source || (j.salary_is_estimate ? "table" : "disclosed");
  const isEstimate = source !== "disclosed";
  const note = SALARY_SOURCE_NOTE[source] || SALARY_SOURCE_NOTE.estimated;
  return `<span class="job-salary${isEstimate ? " estimate" : ""}" title="${escapeHtml(note)}">`
    + `${isEstimate ? '<span class="salary-est-label">Est.</span> ' : ""}`
    + `${escapeHtml(j.salary_text)}</span>`;
}

// Best matches puts one line where every other view puts the chip
// line: the salary first as everywhere else, then the skills this
// listing shares with the CV, then how many of them and what it asks
// for that the CV does not have. One row, fixed height, and the summary
// is what truncates, because the chips carry the specifics.
//
// It replaced two lines, a matched one and a "Missing skills" one, each
// measuring itself and hiding chips from the end until it fitted. That
// machinery existed because the row could grow. It cannot any more, and
// the full breakdown is in the detail pane, which is where somebody who
// wants it has already gone.
const MATCH_CHIPS_SHOWN = 3;

function jobMatchLine(j) {
  const listed = (j.skills || "").split(",").filter(Boolean);
  const hits = listed.filter((sk) => matchedSkills.has(sk));
  const asks = listed.filter((sk) => !matchedSkills.has(sk));
  const summary = `${hits.length} of ${matchedSkills.size}`
    + (asks.length ? ` · missing ${asks.join(", ")}` : "");
  // Three chips at most. The count is the part that has to survive:
  // eight of them filled the line and pushed "6 of 8" off the end, so
  // the row showed which skills matched and never how many. The chips
  // are the examples, the summary is the answer, and its own title
  // attribute carries every name either way.
  return jobSalaryChip(j)
    + hits.slice(0, MATCH_CHIPS_SHOWN)
        .map((sk) => `<span class="job-chip skill matched">${MATCH_TICK_SVG}${escapeHtml(sk)}</span>`).join("")
    + `<span class="job-match-summary" title="${escapeHtml(summary)}">${escapeHtml(summary)}</span>`;
}

function jobSkillChips(j) {
  return (j.skills || "")
    .split(",")
    .filter(Boolean)
    .slice(0, ROW_SKILLS_SHOWN)
    .map((s) => `<button type="button" class="job-chip skill" data-skill="${escapeHtml(s)}"
                   title="Search for ${escapeHtml(s)}">${escapeHtml(s)}</button>`)
    .join("");
}

// Two rows for one job, which happens when a company is reachable
// through two ATS tokens and both were crawled. Same URL, same job, and
// nothing downstream of here can tell them apart, so they are collapsed
// before anything is drawn rather than deduplicated in the eye.
function dedupeJobs(jobs, seen = new Set()) {
  return jobs.filter((j) => {
    const key = (j.url || "").trim().toLowerCase() || `${j.company_domain}\u0000${j.external_id || j.id}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

// Bring the selected row into view inside the list's own scroller.
// scrollTop rather than scrollIntoView: the latter scrolls every
// ancestor that can scroll, which on this board means the window and the
// pane move too. Only nudges when the row is actually out of view, so
// clicking a row you can already see does not shift the list under you.
function revealSelectedRow() {
  const list = document.querySelector(".board-list");
  const row = selectedJobId === null ? null : document.querySelector(`tr[data-id="${selectedJobId}"]`);
  if (!list || !row) return;
  const head = list.querySelector(".list-head");
  const top = head ? head.getBoundingClientRect().height : 0;
  const lb = list.getBoundingClientRect();
  const rb = row.getBoundingClientRect();
  if (rb.top < lb.top + top) list.scrollTop -= lb.top + top - rb.top + 8;
  else if (rb.bottom > lb.bottom) list.scrollTop += rb.bottom - lb.bottom + 8;
}

// Split from the wiring below, because the infinite list appends rows to
// a body whose existing rows are already wired and must not be rebuilt.
function jobRowsHtml(jobs, starred) {
  // The tooltip stopped being true once /me/saved existed. Signed in,
  // the star does follow you, and saying otherwise talks people out of
  // using it.
  const starTitle = getAuthTokens() ? "Save to your account" : "Save (this browser only)";
  return jobs
    .map((j) => {
      const age = j.posted_at
        ? (Date.now() - new Date(j.posted_at).getTime()) / 86400000
        : null;
      const fresh = age !== null && age <= 3;
      const isStarred = starred.has(j.id);
      return `
      <tr data-id="${j.id}" class="${j.id === selectedJobId ? "selected" : ""}" tabindex="0"
          role="button" aria-label="${escapeHtml(j.title)}, ${escapeHtml(companyLabel(j))}">
        <td class="star-cell">
          <button class="star-btn ${isStarred ? "on" : ""}" data-star="${j.id}"
                  title="${starTitle}" aria-pressed="${isStarred}"
                  aria-label="${isStarred ? "Saved" : "Save"} ${escapeHtml(j.title)}">${STAR_SVG}</button>
        </td>
        <td class="logo-cell">${companyLogoImg(j.company_domain, 48, "listing", j.logo_url)}</td>
        <td class="main-cell">
          <div class="job-card-title">
            <!-- title=, because the row is a fixed height and this is
                 ellipsised. escapeHtml, not highlight(): the visible
                 span keeps the search highlighting, an attribute cannot
                 hold markup. -->
            <span class="job-title-text" title="${escapeHtml(j.title)}">${highlight(j.title)}</span>
            ${j.seniority ? `<span class="badge seniority">${escapeHtml(SENIORITY_LABELS[j.seniority] || j.seniority)}</span>` : ""}
            ${j.confidence === "best_effort" ? '<span class="badge best-effort" title="Scraped from the company\'s own page, not a live ATS API">best_effort</span>' : ""}
            ${j.closed_at ? '<span class="badge closed" title="This listing is no longer open">Closed</span>' : ""}
          </div>
          <!-- The age rides at the end of the details line on a phone,
               where its own column would steal the width the title
               needs, and hides on desktop where the column exists.
               Same trick the board used before this layout. -->
          <div class="job-meta"><span class="job-who">${jobWhoLine(j)}</span><span class="meta-age"> · <span class="meta-age-value ${fresh ? "fresh" : ""}">${fmtAgeAgo(age)}</span></span></div>
          <div class="job-where">${jobWhereLine(j)}</div>
          <div class="job-chips">${matchedSkills.size ? jobMatchLine(j) : jobSalaryChip(j) + jobSkillChips(j)}</div>
          <div class="job-links">
            <a class="apply-link" href="${escapeHtml(j.url || "#")}" target="_blank" rel="noopener" title="Open the original listing to apply">Apply ${EXTERNAL_ARROW_SVG}</a>
            <button class="copy-link-btn" data-copy-url="${escapeHtml(j.url || "")}" title="Copy the application link">Save link</button>
          </div>
          <div class="job-salary-line">${jobSalaryLine(j)}</div>
        </td>
        <td data-label="Age" class="age-cell ${fresh ? "fresh" : ""}">${fmtAgeAgo(age)}</td>
      </tr>`;
    })
    .join("");
}

function renderJobRows(jobs, starred) {
  showJobsTable(true);
  document.getElementById("jobs-body").innerHTML = jobRowsHtml(jobs, starred);
  wireJobRowControls();
  // A replaced list starts at its own top. Left where it was, a reader
  // who had scrolled to the end and then changed a filter landed at the
  // bottom of fifty new rows, and the load-more sentinel was already in
  // view, so the next page appended itself before they saw the first.
  const list = document.querySelector(".board-list");
  if (list) list.scrollTop = 0;
}

// Rebinding every handler after an append is cheap and has no state to
// lose: these are all stateless clicks reading a data attribute.
function wireJobRowControls() {
  // Scoped to the list. [data-star] also matches the detail pane's own
  // button and the sticky bar's, and an append that rebound those would
  // leave two listeners on each, so one click would toggle twice.
  const body = document.getElementById("jobs-body");
  if (!body) return;
  body.querySelectorAll("[data-star]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation(); // inside a clickable row; starring shouldn't also open it
      const s = toggleStar(btn.dataset.star);
      const on = s.has(btn.dataset.star);
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", String(on));
      syncDetailStarButton(btn.dataset.star, s);
      pushStar(btn.dataset.star, on);
      if (state.starred_only) loadJobs();
    });
  });

  body.querySelectorAll("[data-copy-url]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation(); // inside a clickable row; copying should not also open it
      copyToClipboard(btn, btn.dataset.copyUrl);
    });
  });

  body.querySelectorAll("[data-skill]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation(); // same reasoning as the star button above
      // Quoted, so "REST API" stays one term rather than two words the
      // listing must both mention separately.
      state.search = btn.dataset.skill.includes(" ") ? `"${btn.dataset.skill}"` : btn.dataset.skill;
      state.offset = 0;
      setSearchBox(state.search);
      loadJobs();
    });
  });
}

// The detail pane
//
// Beside the list rather than over it, and never empty: with nothing
// selected it answers what the current filters add up to (see
// renderDetailEmpty). Apply and Save live here now rather than on every
// row, because fifty rows carrying two buttons each is ninety-eight
// buttons for the one listing anybody is reading.

function jobFact(label, value, cls = "") {
  return `<div class="fact ${cls}"><span class="fact-value">${value}</span>`
    + `<span class="fact-label">${escapeHtml(label)}</span></div>`;
}

function detailSalaryHtml(job) {
  if (!job.salary_text) return '<span class="fact-absent">Undisclosed</span>';
  const source = job.salary_source || (job.salary_is_estimate ? "table" : "disclosed");
  const isEstimate = source !== "disclosed";
  const note = SALARY_SOURCE_NOTE[source] || SALARY_SOURCE_NOTE.estimated;
  return `<span title="${escapeHtml(note)}">`
    + `${isEstimate ? '<span class="salary-est-label">Est.</span> ' : ""}`
    + `<span class="salary-range">${escapeHtml(job.salary_text)}</span></span>`;
}

function renderJobDetailBody(job, { descriptionLoading = false, descriptionError = null } = {}) {
  const age = job.posted_at ? (Date.now() - new Date(job.posted_at).getTime()) / 86400000 : null;
  const starred = getStarred().has(job.id);

  let descriptionHtml;
  if (descriptionError) {
    descriptionHtml = `<div class="error-state">Could not load the full description: ${escapeHtml(descriptionError)}</div>`;
  } else if (descriptionLoading) {
    // Lines where the text will be, rather than a spinner. The pane is
    // already on screen holding the row's own title and company, so a
    // spinner in the middle of it reads as the whole pane loading when
    // only the description is.
    descriptionHtml = '<div class="job-detail-description skeleton-desc" role="status" aria-label="Loading description">'
      + '<span class="skeleton sk-line"></span><span class="skeleton sk-line"></span>'
      + '<span class="skeleton sk-line"></span><span class="skeleton sk-line short"></span></div>';
  } else if (job.description) {
    descriptionHtml = `<div class="job-detail-description">${renderDescriptionLines(job.description)}</div>`;
  } else {
    descriptionHtml = '<div class="job-detail-description empty">No description provided by this listing.</div>';
  }

  // No close button here any more: the pane has a head of its own and
  // the X lives in it, level with the other two columns' heads.
  return `
    <div class="job-detail-company">
      ${companyLogoImg(job.company_domain, 52, "detail", job.logo_url)}
      <div class="job-detail-company-text">
        <span class="job-detail-company-name">${escapeHtml(companyLabel(job))}</span>
        <span class="job-detail-company-place">${escapeHtml(job.location || "-")}</span>
      </div>
    </div>

    <!-- No level badge here. The title already says it most of the
         time ("Security Research Team Lead" beside a "Lead" chip), and
         the row you clicked to get here was showing it a moment ago.
         The badges that stay are the two that warn rather than repeat:
         a listing scraped from a career page instead of an ATS, and one
         that has closed. -->
    <h2 class="job-detail-title">${escapeHtml(job.title)}${
      job.confidence === "best_effort" ? ' <span class="badge best-effort" title="Scraped from the company\'s own page, not a live ATS API">best_effort</span>' : ""}${
      job.closed_at ? ' <span class="badge closed">Closed</span>' : ""}</h2>

    <div class="job-detail-actions">
      <a class="job-detail-apply" href="${escapeHtml(job.url || "#")}" target="_blank" rel="noopener" title="Open the original listing to apply">Apply ${EXTERNAL_ARROW_SVG}</a>
      <button type="button" class="job-detail-star ${starred ? "on" : ""}" data-star="${job.id}" aria-pressed="${starred}">${STAR_SVG}<span>${starred ? "Saved" : "Save"}</span></button>
      <button type="button" class="link job-detail-permalink" data-copy-permalink="${escapeHtml(jobPermalink(job.id))}" title="Copy a link to this listing">Save link</button>
    </div>

    <div class="job-detail-facts">
      ${jobFact("Monthly salary", detailSalaryHtml(job))}
      ${jobFact("Posted", age !== null ? escapeHtml(fmtAgeAgo(age)) : '<span class="fact-absent">Unreported</span>')}
      ${jobFact("Department", job.department ? escapeHtml(job.department) : '<span class="fact-absent">-</span>')}
      ${jobFact("Workplace", job.workplace_type ? escapeHtml(WORKPLACE_LABELS[job.workplace_type] || job.workplace_type) : '<span class="fact-absent">-</span>')}
    </div>

    ${job.skills ? `<div class="job-detail-skills">${jobSkillChips(job)}</div>` : ""}

    <div class="job-detail-section-title">About this role</div>
    ${descriptionHtml}

    <!-- The description here is a copy, and an old one by the time
         anybody reads it. This is the version that is actually true. -->
    <a class="job-detail-source" href="${escapeHtml(job.url || "#")}" target="_blank" rel="noopener">View original posting ${EXTERNAL_ARROW_SVG}</a>`;
}

// The empty state
//
// The pane is never blank. With nothing selected it says what the
// filters currently add up to, which is the one moment a reader is
// looking at the board rather than at a job.
//
// Built as a string here rather than as markup in board.html, unlike
// the Statistics block it replaced: nothing else paints into it, so
// there is no half-drawn state for a re-render to wipe.
// The pane is a head and a body, like the other two columns. The head
// stays while the body scrolls, and it is the only thing that changes
// shape between the two states the pane has.
const paneBody = () => document.getElementById("pane-body");

function paneHead(job) {
  const head = document.getElementById("pane-head");
  if (!head) return;
  if (job) {
    // Where this listing sits in the list behind it, and the way through
    // them without going back to the column. The position counts from
    // the page offset, so it is the reader's place in the whole result
    // set rather than in the fifty rows currently loaded.
    const rows = (lastJobsResponse && lastJobsResponse.jobs) || [];
    const i = rows.findIndex((r) => r.id === job.id);
    const total = lastJobsResponse && lastJobsResponse.total;
    // The list is cumulative, so a row's index in it is its position.
    // state.offset is the next page's starting point, not this list's.
    const at = i < 0 ? null : i + 1;
    const label = at === null ? "Listing"
      : `${fmtInt(at)} of ${total == null ? `${fmtInt(rows.length)}+` : fmtInt(total)}`;
    head.innerHTML = `
      <div class="pane-nav">
        <button type="button" class="pane-step" data-step="-1" aria-label="Previous listing"
                ${i <= 0 ? "disabled" : ""}>&#8249;</button>
        <span class="col-head-title pane-pos">${escapeHtml(label)}</span>
        <button type="button" class="pane-step" data-step="1" aria-label="Next listing"
                ${i < 0 || i >= rows.length - 1 ? "disabled" : ""}>&#8250;</button>
      </div>
      <button type="button" class="pane-close job-detail-close" aria-label="Close listing">&#10005;</button>`;
    head.querySelector(".pane-close").addEventListener("click", closeJobDetailAndSync);
    head.querySelectorAll("[data-step]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const next = rows[i + Number(btn.dataset.step)];
        if (next) openJobDetailAndPush(next.id);
      });
    });
    return;
  }
  const view = state.roles === "tech" && !state.starred_only ? "Tech roles" : "All roles";
  head.innerHTML = `<div class="list-head-text">
      <span class="col-head-title">Results overview</span>
      <span class="col-head-sub">${escapeHtml([...activeFilterSummary(), view].join(" · "))}</span>
    </div>`;
}

// The facets count companies by domain, because that is the key the
// board filters on, but a domain is not what a company is called. The
// rows on screen already carry both, so the name is looked up there and
// the domain is only shown when nothing on this page knows better.
function companyNameFor(domain) {
  // The facets carry the name beside the count (compute_facets asks the
  // companies table for it), and they cover every company on the list,
  // where the fifty loaded rows cover only whichever happen to be on
  // screen. Reported live: Hiring most read "nvidia.com" and "iai.co.il"
  // on a board whose rows said NVIDIA and IAI, because neither company
  // had a listing in the first page.
  const facet = (railFacets.companies || []).find((c) => c.value === domain && c.name);
  if (facet) return facet.name;
  const rows = (lastJobsResponse && lastJobsResponse.jobs) || [];
  const hit = rows.find((j) => j.company_domain === domain && j.company_name);
  return hit ? hit.company_name : domainLabel(domain);
}

// The same lookup for the logo. A resolved URL when something already
// on this page carries one (a loaded row, or the stats leaderboards,
// which come with logos attached); otherwise null, and companyLogoImg
// falls back to its guess cascade, the way it does for any row that
// arrived without one.
function companyLogoFor(domain) {
  const rows = (lastJobsResponse && lastJobsResponse.jobs) || [];
  const hit = rows.find((j) => j.company_domain === domain && j.logo_url);
  if (hit) return hit.logo_url;
  for (const list of [latestScoped?.data?.top_companies, latestStats?.top_companies]) {
    const c = (list || []).find((r) => r.domain === domain && r.logo_url);
    if (c) return c.logo_url;
  }
  return null;
}

function renderDetailEmpty() {
  const panel = document.getElementById("job-detail");
  if (!panel || selectedJobId !== null) return;
  // Every route into the empty state goes through here, and the guard
  // above means this cannot fire while a listing is open, so this is the
  // one place that has to get the bar right.
  clearStickyActions();
  paneHead(null);

  const mode = currentScopeMode();
  const scoped = mode === "scoped" ? latestScoped.data : latestStats ? globalScope(latestStats) : null;
  const total = lastJobsResponse?.total;
  const hiring = (railFacets.companies || []).slice(0, 4);
  const remote = (railFacets.workplace || []).find((r) => r.value === "remote");

  // Value first, label under it, and a tile is dropped rather than
  // shown holding a dash: a panel of em dashes says nothing four times.
  // A number still being counted shows bones, because a scoped /stats
  // was measured at up to 2.9s and a stale figure read as this one's.
  const pending = mode === "pending";
  const bone = '<span class="skeleton sk-line"></span>';
  const tile = (value, label, cls = "") =>
    `<div class="ov-tile"><span class="ov-value ${cls}">${value}</span>`
    + `<span class="ov-label">${escapeHtml(label)}</span></div>`;
  const tiles = [];
  const add = (value, label, cls) => { if (value !== null) tiles.push(tile(value, label, cls)); };
  add(total == null ? (pending || !lastJobsResponse ? bone : null) : fmtInt(total), "Matching roles");
  add(pending ? bone : scoped?.new_jobs_24h == null ? null : `+${fmtInt(scoped.new_jobs_24h)}`,
      "New today", "ov-new");
  add(pending ? bone : scoped?.companies_hiring == null ? null : fmtInt(scoped.companies_hiring),
      "Companies");
  // Fourth, so it lands under New today in the two-column grid. The same
  // seven-day figure the scoped stats already carry beside the daily
  // one; it was in the response and nowhere on the page.
  add(pending ? bone : scoped?.new_jobs_7d == null ? null : `+${fmtInt(scoped.new_jobs_7d)}`,
      "New this week", "ov-new");

  // Search health. The loader's own last write, which is the one number
  // that says whether this is current. "Sources responding" is in the
  // design and not in the data: /stats carries pipeline.error_count and
  // a company total, which is a different question (how many companies
  // errored, ever) and would be a made-up percentage dressed as a
  // measurement. The row stays out until something measures it.
  // Same source as the text beside it, for the same reason.
  const freshMins = lastCheckedAt === null ? null : (Date.now() - lastCheckedAt) / 60000;
  const health = updatedAgo();

  paneBody().innerHTML = `
    <div class="detail-empty">
      ${tiles.length ? `<div class="ov-tiles">${tiles.join("")}</div>` : ""}

      ${hiring.length ? `
        <div class="ov-block">
          <span class="ov-block-title">Companies with most open roles</span>
          ${hiring.map((c) => `
            <button type="button" class="ov-row ov-row-co" data-company="${escapeHtml(c.value)}"
                    title="Show only ${escapeHtml(companyNameFor(c.value))}">
              <span class="ov-co">${companyLogoImg(c.value, 32, "ov-logo", companyLogoFor(c.value))}<span class="ov-co-name">${escapeHtml(companyNameFor(c.value))}</span></span>
              <span class="ov-row-n">${fmtInt(c.n)}</span>
            </button>`).join("")}
        </div>` : ""}

      ${health ? `
        <div class="ov-block">
          <span class="ov-block-title">Search health</span>
          <div class="ov-row static">
            <span>Last updated</span>
            <span class="ov-dot-row"><span class="ov-dot ${freshMins != null && freshMins > 120 ? "stale" : ""}"></span>${escapeHtml(health.replace("Updated ", ""))}</span>
          </div>
        </div>` : ""}

      <div class="ov-hint">Select a listing to see its details here.</div>
    </div>`;

  paneBody().querySelectorAll("[data-company]").forEach((row) => {
    row.addEventListener("click", () => {
      state.company = [row.dataset.company];
      railApply();
    });
  });
}

async function copyToClipboard(btn, url) {
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
  } catch {
    // Clipboard API needs a secure context (https, or localhost) and
    // isn't guaranteed everywhere. Fall back to a hidden textarea copy
    // rather than silently failing on older/locked-down browsers.
    const ta = document.createElement("textarea");
    ta.value = url;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
  }
  // innerHTML, not textContent -- a plain-text button (Save link) round-trips
  // through either the same way, but an icon-only button (an inline <svg>,
  // no text content at all) needs innerHTML or the restore below would wipe
  // the icon out instead of bringing it back.
  const original = btn.innerHTML;
  btn.textContent = "Copied";
  btn.classList.add("copied");
  setTimeout(() => {
    btn.innerHTML = original;
    btn.classList.remove("copied");
  }, 1200);
}

// The list grows as you reach the end of it, rather than being cut into
// pages. With a million rows a page number is not something anyone
// navigates by, and "next" on a board is always just "more".
//
// The button is not a fallback nobody sees: it is what the observer
// clicks, so there is one path into a load and one thing to disable
// while it runs. An observer that never fires (a short list, a browser
// that blocks it) leaves a button that still works.
let moreObserver = null;
let loadingMore = false;
// Which query the rows on screen belong to. An append that comes back
// for a different one is a page of the list the reader has already left,
// and accumulating it produced a count of 300 over a list of 50.
let listParams = null;
// True while a load that replaces the list is in flight. The old rows
// are still on screen for that whole time, so the sentinel at the bottom
// of them is still in view and the observer will happily ask for "more"
// of a list that is about to be thrown away. That is how signing out
// after scrolling ended up appending page four onto a fresh page one.
let listReloading = false;

function appendJobRows(jobs, starred) {
  const body = document.getElementById("jobs-body");
  if (!body) return;
  const tmp = document.createElement("tbody");
  tmp.innerHTML = jobRowsHtml(jobs, starred);
  // The rows already on screen are not re-rendered: their star handlers
  // and their selected state stay exactly as they are.
  while (tmp.firstChild) body.appendChild(tmp.firstChild);
  wireJobRowControls();
}

function paintMoreButton(data) {
  const wrap = document.getElementById("jobs-more");
  if (!wrap) return;
  const shown = ((lastJobsResponse && lastJobsResponse.jobs) || []).length;
  const total = (lastJobsResponse && lastJobsResponse.total) ?? data?.total;
  // Before the count lands, a full page is reason enough to believe
  // there is another one.
  const more = total == null ? (data?.jobs?.length || 0) >= PAGE_SIZE : shown < total;
  wrap.hidden = !more;
  const btn = document.getElementById("jobs-more-btn");
  if (btn) {
    btn.disabled = loadingMore;
    btn.textContent = loadingMore ? "Loading…" : "Load more";
  }
}

async function loadMoreJobs() {
  if (loadingMore || listReloading) return;
  const shown = ((lastJobsResponse && lastJobsResponse.jobs) || []).length;
  const total = lastJobsResponse && lastJobsResponse.total;
  if (total != null && shown >= total) return;
  loadingMore = true;
  paintMoreButton(lastJobsResponse);
  const previous = state.offset;
  state.offset = shown;
  try {
    await loadJobs({ append: true });
  } catch {
    state.offset = previous;
  } finally {
    loadingMore = false;
    paintMoreButton(lastJobsResponse);
  }
}

function wireInfiniteList() {
  const wrap = document.getElementById("jobs-more");
  const btn = document.getElementById("jobs-more-btn");
  const list = document.querySelector(".board-list");
  if (!wrap || !btn || !list) return;
  btn.addEventListener("click", loadMoreJobs);
  if (moreObserver) moreObserver.disconnect();
  // rootMargin so the next page is already arriving as the last rows
  // come into view, rather than after the reader has hit the bottom.
  moreObserver = new IntersectionObserver(
    ([e]) => { if (e.isIntersecting && !wrap.hidden) loadMoreJobs(); },
    { root: list, rootMargin: "600px 0px" },
  );
  moreObserver.observe(wrap);
}

// Replaced by the infinite list below. Kept out of the board entirely
// rather than left wired to a hidden element.
function renderPagination(data) {
  const el = document.getElementById("pagination-pages");
  const current = Math.floor(state.offset / PAGE_SIZE) + 1;
  // Before the count lands, a full page means there is almost certainly
  // another one. Deriving the last page from the total alone would show
  // a dead next button that quietly comes alive a moment later.
  const known = data.total !== null && data.total !== undefined;
  const totalPages = known
    ? Math.max(1, Math.ceil(data.total / PAGE_SIZE))
    : current + (data.jobs.length === PAGE_SIZE ? 1 : 0);
  const start = Math.max(1, current - 3);
  const end = Math.min(totalPages, start + 6);

  let html = `<button ${current === 1 ? "disabled" : ""} data-page="${current - 1}">‹</button>`;
  for (let p = start; p <= end; p++) {
    html += `<button class="${p === current ? "active" : ""}" data-page="${p}">${p}</button>`;
  }
  html += `<button ${current === totalPages ? "disabled" : ""} data-page="${current + 1}">›</button>`;
  el.innerHTML = html;

  el.querySelectorAll("[data-page]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.offset = (parseInt(btn.dataset.page, 10) - 1) * PAGE_SIZE;
      loadJobs();
      window.scrollTo({ top: document.getElementById("board").offsetTop - 60, behavior: "smooth" });
    });
  });
}

// job detail panel: opens beside the list on selecting a row (never a
// modal). Renders instantly from the row's already-known fields, then
// fills in `description` once GET /api/jobs/{id} resolves, since the list
// endpoint doesn't carry full descriptions.

function findKnownJob(id) {
  return lastJobsResponse?.jobs?.find((j) => j.id === id) || null;
}

// Keeps the panel's own star button in step with whichever table row was
// clicked (either direction: starring from the row, or from the panel).
function syncDetailStarButton(id, starredSet) {
  if (id !== selectedJobId) return;
  const btn = document.querySelector(".job-detail-star");
  if (!btn) return;
  paintDetailStar(btn, starredSet.has(id));
}

// One place that knows what a saved star looks like, because the row
// and the pane both have to agree and they are drawn by different
// functions. The mark itself never changes; the fill does, via the
// class, so the button cannot change width as it is pressed.
function paintDetailStar(btn, on) {
  btn.classList.toggle("on", on);
  btn.setAttribute("aria-pressed", String(on));
  const label = btn.querySelector("span");
  if (label) label.textContent = on ? "Saved" : "Save";
}

// probe.py's _clean_text marks section headings with a leading "## ".
// Render those bold rather than showing the marker literally. Every line
// is still escaped individually, so nothing in the source text is ever
// treated as markup.
function renderDescriptionLines(description) {
  return description
    .split("\n")
    .map((line) =>
      line.startsWith("## ")
        ? `<strong class="job-detail-desc-heading">${escapeHtml(line.slice(3))}</strong>`
        : escapeHtml(line)
    )
    .join("\n");
}

// Hand-written, not an icon font/library -- this project has zero
// external dependencies anywhere in the frontend, and one glyph doesn't
// change that. currentColor so it inherits the button's own ink/hover
// color for free, same as every other flat, monochrome control here.
const LINK_ICON_SVG = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.07 0l2.83-2.83a5 5 0 0 0-7.07-7.07L11.5 4.5"/><path d="M14 11a5 5 0 0 0-7.07 0L4.1 13.83a5 5 0 0 0 7.07 7.07L13.5 19.5"/></svg>`;

// Deep link to one job, independent of whatever filters are currently
// active -- see openJobDetail's "not in the current list" fallback,
// which is what makes opening this link from cold actually work.
function jobPermalink(id) {
  return `${location.origin}${location.pathname}?job=${encodeURIComponent(id)}`;
}

// The duplicate action bar, shown only while the real one is off screen.
// An observer rather than a scroll handler: the question is literally
// "is that element visible inside this scroller", which is what an
// IntersectionObserver answers without running code on every frame.
let paneStickyObserver = null;

// Everything the sticky bar is, undone in one place, so "no listing is
// open" and "the bar is on screen" cannot disagree.
//
// The observer has to go with it. It watches .job-detail-actions inside
// the pane body, and the empty state replaces that body, so the observed
// node ends up detached: the observer then either reports it as not
// intersecting, which unhides the bar, or stops reporting at all and
// leaves it unhidden from before. Either way the bar kept the listing
// that had just been closed. Reported live 2026-09-24 as "Apply on
// nvidia.com" sitting under "Select a listing to see its details here",
// after a filter change dropped the open listing out of the results.
function clearStickyActions() {
  if (paneStickyObserver) {
    paneStickyObserver.disconnect();
    paneStickyObserver = null;
  }
  const bar = document.getElementById("pane-sticky");
  if (!bar) return;
  // Emptied as well as hidden: the Apply link inside it is focusable, and
  // a hidden bar that still holds one is a tab stop leading to the wrong
  // company.
  bar.innerHTML = "";
  bar.hidden = true;
}

function wireStickyActions(job) {
  const bar = document.getElementById("pane-sticky");
  const body = paneBody();
  const actions = body && body.querySelector(".job-detail-actions");
  if (paneStickyObserver) paneStickyObserver.disconnect();
  if (!bar || !actions) return;
  const starred = getStarred().has(job.id);
  // Star and copy first as square targets, then Apply taking whatever is
  // left, named after the company so the button says where it is sending
  // you rather than just that it sends you somewhere.
  bar.innerHTML = `
    <button type="button" class="pane-act job-detail-star ${starred ? "on" : ""}" data-star="${job.id}"
            aria-pressed="${starred}" aria-label="${starred ? "Saved" : "Save"}">${STAR_SVG}</button>
    <button type="button" class="pane-act" data-copy-permalink="${escapeHtml(jobPermalink(job.id))}"
            aria-label="Copy a link to this listing"><svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M10.5 5.5v-1a1.5 1.5 0 0 0-1.5-1.5H4a1.5 1.5 0 0 0-1.5 1.5v5A1.5 1.5 0 0 0 4 11h1" fill="none" stroke="currentColor" stroke-width="1.5"/></svg></button>
    <a class="job-detail-apply pane-apply" href="${escapeHtml(job.url || "#")}" target="_blank" rel="noopener">Apply on ${escapeHtml(companyLabel(job))} ${EXTERNAL_ARROW_SVG}</a>`;
  const copy = bar.querySelector("[data-copy-permalink]");
  if (copy) copy.addEventListener("click", () => copyToClipboard(copy, copy.dataset.copyPermalink));
  bar.querySelector("[data-star]").addEventListener("click", (e) => {
    const on = toggleStar(job.id).has(job.id);
    paintDetailStar(e.currentTarget, on);
    const other = body.querySelector(".job-detail-star");
    if (other) paintDetailStar(other, on);
    e.currentTarget.setAttribute("aria-label", on ? "Saved" : "Save");
    const rowBtn = document.querySelector(`[data-star="${job.id}"].star-btn`);
    if (rowBtn) {
      rowBtn.classList.toggle("on", on);
      rowBtn.setAttribute("aria-pressed", String(on));
    }
    pushStar(job.id, on);
    if (state.starred_only) loadJobs();
  });
  bar.hidden = true;
  paneStickyObserver = new IntersectionObserver(
    ([entry]) => { bar.hidden = entry.isIntersecting; },
    { root: body, threshold: 0 },
  );
  paneStickyObserver.observe(actions);
}

function wireJobDetailPanel(job) {
  const panel = document.getElementById("job-detail");
  wireStickyActions(job);
  panel.querySelector(".job-detail-close")?.addEventListener("click", closeJobDetailAndSync);
  panel.querySelector(".job-detail-star")?.addEventListener("click", (e) => {
    const s = toggleStar(job.id);
    const on = s.has(job.id);
    paintDetailStar(e.currentTarget, on);
    const rowBtn = document.querySelector(`[data-star="${job.id}"].star-btn`);
    if (rowBtn) {
      rowBtn.classList.toggle("on", on);
      rowBtn.setAttribute("aria-pressed", String(on));
    }
    pushStar(job.id, on);
    if (state.starred_only) loadJobs();
  });
  const permalinkBtn = panel.querySelector("[data-copy-permalink]");
  if (permalinkBtn) permalinkBtn.addEventListener("click", () => copyToClipboard(permalinkBtn, permalinkBtn.dataset.copyPermalink));
}

async function openJobDetail(id) {
  const panel = document.getElementById("job-detail");
  // Not always in the currently loaded/filtered page -- a deep link (see
  // jobPermalink/applyStateFromUrl) can point at a job that isn't on
  // this view at all. When it's on-screen, its row data renders
  // immediately as a placeholder while the full fetch is in flight, same
  // as before; when it isn't, there's nothing to render early, just a
  // loading state, and no row to highlight.
  const known = findKnownJob(id);

  const previousId = selectedJobId;
  selectedJobId = id;
  setCanonical(`/job/${encodeURIComponent(id)}`);
  document.querySelector(`tr[data-id="${previousId}"]`)?.classList.remove("selected");
  if (known) document.querySelector(`tr[data-id="${id}"]`)?.classList.add("selected");
  // Opened from the keyboard or from the pane's own prev/next, the row
  // may be off screen; a selection you cannot see is not one.
  revealSelectedRow();

  clearTimeout(jobDetailCloseTimer);
  panel.hidden = false;
  panel.scrollTop = 0; // a new listing starts at its own top, not the last one's
  paneHead(known);
  paneBody().innerHTML = known
    ? renderJobDetailBody(known, { descriptionLoading: true })
    : `<div class="loading-state">Loading job…</div>`;
  if (known) wireJobDetailPanel(known);

  // Slide it in on the next frame. The class goes on after hidden=false
  // has painted, or there's no off-screen starting position for the
  // transition to animate from. The page behind is deliberately not
  // scroll-locked, so the board keeps scrolling under the pointer (see
  // .job-detail in style.css).
  requestAnimationFrame(() => {
    panel.classList.add("open");
    document.getElementById("job-scrim")?.classList.add("open");
  });

  try {
    const full = await getJSON(`/jobs/${encodeURIComponent(id)}`);
    if (selectedJobId !== id) return; // a different row was picked while this was in flight
    paneHead(full);
    paneBody().innerHTML = renderJobDetailBody(full);
    wireJobDetailPanel(full);
  } catch (err) {
    if (selectedJobId !== id) return;
    if (known) {
      paneBody().innerHTML = renderJobDetailBody(known, { descriptionError: err.message });
      wireJobDetailPanel(known);
    } else if (/no job with that id/i.test(err.message)) {
      // A shared or bookmarked link to a listing that has left the board.
      // It used to read "Could not load this job: no job with that id",
      // which sounds like the site broke rather than the role closing.
      panel.innerHTML = `<div class="job-gone">
        <p class="job-gone-label">Listing not found</p>
        <p>This listing is no longer on the board. Roles get filled and taken down, so links to them go stale.</p>
        <button type="button" class="btn ghost btn-small" data-gone-close>Back to listings</button>
      </div>`;
      panel.querySelector("[data-gone-close]").addEventListener("click", closeJobDetailAndSync);
    } else {
      panel.innerHTML = `<div class="error-state">Could not load this job: ${escapeHtml(err.message)}</div>`;
    }
  }
}

// Wraps openJobDetail for a real, user-initiated navigation (a row
// click, or restoring a deep link on boot) -- pushes a new history
// entry so Back closes the drawer, same expectation as any other
// permalink-backed detail view. The popstate handler below calls
// openJobDetail directly instead, since the URL there already changed
// out from under it; pushing again would double up the history stack.
async function openJobDetailAndPush(id) {
  await openJobDetail(id);
  if (selectedJobId === id) history.pushState(null, "", shareUrl());
}

function closeJobDetail() {
  const panel = document.getElementById("job-detail");
  panel.classList.remove("open");
  document.getElementById("job-scrim")?.classList.remove("open");
  document.querySelector(`tr[data-id="${selectedJobId}"]`)?.classList.remove("selected");
  selectedJobId = null;
  // Now, not in settle() below: on a sheet that is 250ms away, and the
  // bar must not outlive the listing it belongs to for a quarter of a
  // second while the pane slides out.
  clearStickyActions();
  setCanonical("/board");
  // The pane has a column to itself, so closing a listing does not
  // leave a hole: it goes back to saying what the filters add up to.
  // The swap is delayed by the width of the slide-out below 1100px,
  // where the pane really is a sheet, so the empty state is not drawn
  // mid-flight across the screen. Cleared by the next openJobDetail so
  // picking a different listing mid-close cannot get yanked shut.
  clearTimeout(jobDetailCloseTimer);
  const settle = () => {
    panel.scrollTop = 0;
    renderDetailEmpty();
  };
  if (paneIsSheet()) jobDetailCloseTimer = setTimeout(settle, 250);
  else settle();
}

// Below this the pane has no column of its own and comes over the list
// as a sheet, dimming it. Mirrors the real media query in style.css.
const PANE_SHEET_QUERY = window.matchMedia("(max-width: 1100px)");

function paneIsSheet() {
  return PANE_SHEET_QUERY.matches;
}

// The board's canonical follows whichever listing is open. A listing has
// its own page at /job/<id> (api/job_page.py), and that page is what
// the sitemap lists; ?job= on the board is the same content behind a
// parameter, so it points at the page rather than competing with it.
function setCanonical(path) {
  const link = document.getElementById("canonical");
  if (link) link.href = `${location.origin}${path}`;
}

// Same reasoning as openJobDetailAndPush -- closing via the header
// button is a real, user-initiated step back, so the URL drops ?job=
// right away rather than waiting for the next filter change to notice.
function closeJobDetailAndSync() {
  closeJobDetail();
  syncUrl();
}

function wireRailSheet() {
  const open = () => {
    document.body.classList.add("rail-open");
    document.getElementById("rail-toggle")?.setAttribute("aria-expanded", "true");
    // Focus goes into the sheet so a reader who opened it with the
    // keyboard is inside it rather than still on the button behind it.
    // Not into the search box, though: on a phone that summons the
    // on-screen keyboard the moment the sheet appears, which takes half
    // the sheet and makes it jump as it opens. The close button is in
    // the sheet, is the first thing in it, and types nothing.
    const first = matchMedia("(max-width: 800px)").matches
      ? document.getElementById("rail-close")
      : document.querySelector("#filter-rail .rail-search");
    first?.focus({ preventScroll: true });
  };
  document.getElementById("rail-toggle")?.addEventListener("click", () => {
    document.body.classList.contains("rail-open") ? closeRailSheet() : open();
  });
  document.getElementById("rail-close")?.addEventListener("click", closeRailSheet);
  // The sheet covers the list, so a tap on what is left of the list is
  // the nearest way out, same as the job sheet's scrim.
  document.getElementById("job-scrim")?.addEventListener("click", () => {
    if (document.body.classList.contains("rail-open")) closeRailSheet();
  });
}

function closeRailSheet() {
  document.body.classList.remove("rail-open");
  const btn = document.getElementById("rail-toggle");
  if (btn) {
    btn.setAttribute("aria-expanded", "false");
    btn.focus();
  }
}

function wireJobDetail() {
  document.getElementById("jobs-body").addEventListener("click", (e) => {
    if (e.target.closest("a, button")) return; // Apply/Save link/star handle their own click
    const row = e.target.closest("tr[data-id]");
    if (row) openJobDetailAndPush(row.dataset.id);
  });
  // The rows are focusable now, so Enter has to do what a click does.
  // Space is left alone: on a focused row it should still scroll.
  document.getElementById("jobs-body").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    if (e.target.closest("a, button")) return;
    const row = e.target.closest("tr[data-id]");
    if (!row) return;
    e.preventDefault();
    openJobDetailAndPush(row.dataset.id);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (document.body.classList.contains("rail-open")) return closeRailSheet();
      if (selectedJobId !== null) closeJobDetailAndSync();
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    // Not while somebody is typing into the search box or dragging a
    // salary handle: there the arrows belong to the control.
    const el = document.activeElement;
    if (el && el.closest("input, textarea, select, [contenteditable]")) return;
    const rows = [...document.querySelectorAll("#jobs-body tr[data-id]")];
    if (!rows.length) return;
    e.preventDefault();
    const at = rows.findIndex((r) => r.dataset.id === selectedJobId);
    // Nothing open yet: down opens the first listing, up opens the
    // last, so the keyboard can reach the list without a click first.
    const next = at === -1
      ? (e.key === "ArrowDown" ? 0 : rows.length - 1)
      : Math.min(rows.length - 1, Math.max(0, at + (e.key === "ArrowDown" ? 1 : -1)));
    if (next === at) return;
    rows[next].scrollIntoView({ block: "nearest" });
    openJobDetailAndPush(rows[next].dataset.id);
  });

  // Clicking the dimmed board closes the sheet. Standard for anything
  // covering the page, and it's the nearest target: the close button is over on the far side of
  // the sheet, but the thing the reader is looking at is the list.
  document.getElementById("job-scrim")?.addEventListener("click", () => {
    if (selectedJobId !== null) closeJobDetailAndSync();
  });

  wireJobDetailSwipe();
}

// Requested live: the sheet takes the whole screen on mobile, so
// closing it should also work as a swipe, not just tapping the small X
// in the corner. Gated to that full-screen variant, since it drags on
// translateY and the side sheet above 960px slides on translateX. Only cares about a drag starting on the panel's own
// header (job-detail-actions and above -- the description/skills area
// below has its own vertical scroll to preserve, so a swipe starting
// there would fight it), and only a downward drag by more than a
// quarter of the panel's own height counts as "close" -- anything
// short of that snaps back, same as any native bottom-sheet gesture.
function wireJobDetailSwipe() {
  const panel = document.getElementById("job-detail");
  let startY = null;
  let dragging = false;

  panel.addEventListener("touchstart", (e) => {
    if (!MOBILE_SHEET_QUERY.matches || !panel.classList.contains("open")) return;
    if (!e.target.closest(".job-detail-actions, .job-detail-meta")) return;
    startY = e.touches[0].clientY;
    dragging = true;
    panel.style.transition = "none";
  }, { passive: true });

  panel.addEventListener("touchmove", (e) => {
    if (!dragging) return;
    const delta = e.touches[0].clientY - startY;
    if (delta <= 0) return; // upward: not a close gesture, let it sit at rest
    panel.style.transform = `translateY(${delta}px)`;
  }, { passive: true });

  panel.addEventListener("touchend", (e) => {
    if (!dragging) return;
    dragging = false;
    panel.style.transition = "";
    const delta = e.changedTouches[0].clientY - startY;
    panel.style.transform = "";
    if (delta > panel.getBoundingClientRect().height * 0.25) {
      closeJobDetailAndSync();
    }
  });
}

// multi-select filter dropdown (Category / Level / Company)

// One open dropdown at a time. Opening a second one closes whichever
// was already open, same as a native <select> would behave.
const OPEN_MULTISELECTS = new Set();

// remoteSearch, when given, is asked for options the list does not hold
// yet: the Companies list is the 500 biggest employers, and typing a name
// past that line used to say No matches. keepSelected keeps a picked value
// selected when a refresh of the list no longer carries it, which is what
// a company found by search, or named in a shared link, needs.
function createMultiSelect(containerId, { placeholder, options = [], searchable = false, onChange, pinnedOption = null,
                                          remoteSearch = null, keepSelected = false }) {
  const container = document.getElementById(containerId);
  const selected = new Set();
  let currentOptions = options;
  const foundOptions = new Map();
  // Which values were already ticked when the menu was last opened. See
  // renderOptions for why this is a snapshot and not `selected` itself.
  let pinOrder = [];
  let searchSeq = 0;
  let searchTimer = 0;

  container.innerHTML = `
    <button type="button" class="ms-toggle" aria-haspopup="listbox" aria-expanded="false">${escapeHtml(placeholder)}</button>
    <div class="ms-menu" hidden>
      ${
        pinnedOption
          ? `<label class="ms-option ms-pinned">
               <input type="checkbox" id="${pinnedOption.id}" ${pinnedOption.checked ? "checked" : ""} />
               ${escapeHtml(pinnedOption.label)}
             </label>
             <div class="ms-pinned-divider"></div>`
          : ""
      }
      ${searchable ? '<input type="text" class="ms-search" placeholder="Filter…" />' : ""}
      <div class="ms-options" role="listbox"></div>
      <button type="button" class="ms-clear">Clear</button>
    </div>
  `;
  const toggle = container.querySelector(".ms-toggle");
  const menu = container.querySelector(".ms-menu");
  const optionsEl = container.querySelector(".ms-options");
  const searchEl = container.querySelector(".ms-search");

  if (pinnedOption) {
    container.querySelector(`#${pinnedOption.id}`).addEventListener("change", (e) => {
      pinnedOption.onChange(e.target.checked);
    });
  }

  function allOptions() {
    const known = new Set(currentOptions.map((o) => o.value));
    const extra = [...foundOptions.values()].filter((o) => !known.has(o.value));
    // A selected value neither list carries still gets a row, so it can
    // be seen and unticked.
    const missing = keepSelected
      ? [...selected].filter((v) => !known.has(v) && !foundOptions.has(v)).map((v) => ({ value: v, label: v }))
      : [];
    return [...currentOptions, ...extra, ...missing];
  }

  function renderOptions(filterText = "") {
    const q = filterText.trim().toLowerCase();
    const pool = allOptions();
    const visible = q ? pool.filter((o) => o.label.toLowerCase().includes(q)) : pool;
    // Ticked rows first, in the order they were ticked. Reported live:
    // the Companies list is alphabetical over ten thousand names, so a
    // reader who had chosen five opened it and saw 2k.com, 3m.ai,
    // 42dot.com and none of their own. The toggle said "5 selected" and
    // the list showed none of them.
    //
    // Only while there is no filter text. Typing is a search, and a
    // search that answers with something else on top is not a search.
    //
    // pinOrder is taken once, on the way open, rather than read live.
    // Ticking a box asks the board to reload, which refreshes these
    // counts and redraws this list; reading `selected` here meant the
    // row a reader had just clicked jumped to the top from under the
    // cursor still resting on it. Anything ticked while the menu is
    // open keeps its place and rises the next time it is opened.
    if (!q && pinOrder.length) {
      const order = pinOrder;
      visible.sort((x, y) => {
        const a2 = order.indexOf(x.value);
        const b2 = order.indexOf(y.value);
        if (a2 === -1 && b2 === -1) return 0;   // both unticked, leave the list alone
        if (a2 === -1) return 1;
        if (b2 === -1) return -1;
        return a2 - b2;
      });
    }
    optionsEl.innerHTML =
      visible
        .map(
          (o) => `
        <label class="ms-option">
          <input type="checkbox" value="${escapeHtml(o.value)}" ${selected.has(o.value) ? "checked" : ""} />
          ${escapeHtml(o.label)}
        </label>`
        )
        .join("") || '<div class="ms-empty">No matches.</div>';
  }

  function updateLabel() {
    if (selected.size === 0) {
      toggle.textContent = placeholder;
    } else if (selected.size === 1) {
      const opt = allOptions().find((o) => o.value === [...selected][0]);
      toggle.textContent = opt ? opt.label : [...selected][0];
    } else {
      toggle.textContent = `${selected.size} selected`;
    }
    toggle.classList.toggle("active", selected.size > 0);
  }

  function close() {
    menu.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    OPEN_MULTISELECTS.delete(close);
  }

  function open() {
    OPEN_MULTISELECTS.forEach((closeOther) => closeOther());
    pinOrder = [...selected];
    renderOptions(searchEl ? searchEl.value : "");
    menu.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    OPEN_MULTISELECTS.add(close);
    if (searchEl) searchEl.focus();
  }

  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    menu.hidden ? open() : close();
  });
  menu.addEventListener("click", (e) => e.stopPropagation()); // clicks inside the menu shouldn't bubble to document and self-close it

  optionsEl.addEventListener("change", (e) => {
    if (!e.target.matches('input[type="checkbox"]')) return;
    e.target.checked ? selected.add(e.target.value) : selected.delete(e.target.value);
    updateLabel();
    onChange([...selected]);
  });

  if (searchEl) searchEl.addEventListener("input", () => {
    renderOptions(searchEl.value);
    if (!remoteSearch) return;
    clearTimeout(searchTimer);
    const q = searchEl.value.trim();
    if (q.length < 2) return;
    searchTimer = setTimeout(async () => {
      const seq = ++searchSeq;
      try {
        const found = await remoteSearch(q);
        if (seq !== searchSeq) return; // a later keystroke has its own answer coming
        found.forEach((o) => foundOptions.set(o.value, o));
        renderOptions(searchEl.value);
      } catch {
        // Non-fatal: the list keeps what it already had.
      }
    }, 250);
  });

  container.querySelector(".ms-clear").addEventListener("click", () => {
    selected.clear();
    renderOptions(searchEl ? searchEl.value : "");
    updateLabel();
    onChange([]);
  });

  renderOptions();
  updateLabel();

  return {
    setOptions(opts) {
      const current = [...selected];
      currentOptions = opts;
      // Drop any selected value no longer in the new option set (e.g.
      // Location narrowing to IL-only, dropping a non-IL pick). Not with
      // keepSelected, where the list is a top-N and a pick outside it is
      // still a real filter.
      selected.clear();
      current.filter((v) => keepSelected || opts.some((o) => o.value === v)).forEach((v) => selected.add(v));
      renderOptions(searchEl ? searchEl.value : "");
      updateLabel();
      // Tell the caller if something was silently dropped, so its state
      // doesn't keep sending a value this widget no longer shows selected.
      if (selected.size !== current.length) onChange([...selected]);
    },
    reset() {
      selected.clear();
      renderOptions("");
      updateLabel();
      if (searchEl) searchEl.value = "";
    },
    setSelected(values) {
      selected.clear();
      values.forEach((v) => selected.add(v));
      renderOptions(searchEl ? searchEl.value : "");
      updateLabel();
    },
  };
}

// The Locations dropdown: countries at the top level, each country's own
// cities folded underneath it.
//
// A sibling of createMultiSelect above rather than a mode inside it.
// Almost nothing survives the jump: two independent selection sets
// instead of one, expansion state no flat caller has, a two-argument
// setSelected, an onChange that hands back a pair. Every internal of the
// flat widget would have grown a grouped/flat branch for the benefit of
// one caller while its ten flat callers paid for branches they never
// take. The one thing the two genuinely share is the one-open-at-a-time
// contract, and that already lives outside both, in OPEN_MULTISELECTS.
//
// Country and city are independent filters that AND together on the
// server, so ticking a country here deliberately does not tick its
// cities and ticking a city does not tick its country. country=IL with
// city=Berlin is a legitimate way to ask for nothing.
//
// Same drawing as the chevron on .filters select (see style.css), so the
// two arrows sitting a few pixels apart in the filter row match. Inline
// here rather than a background-image data URI, because currentColor
// only follows the element's color when the SVG is really in the DOM.
const MS_CHEVRON_SVG =
  '<svg viewBox="0 0 10 6" width="10" height="6" aria-hidden="true">'
  + '<path d="M1 1l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.5"'
  + ' stroke-linecap="round" stroke-linejoin="round"/></svg>';

function createLocationSelect(containerId, { placeholder, onChange }) {
  const container = document.getElementById(containerId);
  const selectedCountries = new Set();
  const selectedCities = new Set();
  // Which countries are open. View state, not filter state: it never
  // reaches the URL or localStorage, and a Back/Forward navigation has no
  // business re-collapsing a country somebody just opened.
  const expanded = new Set();
  let countries = [];

  container.innerHTML = `
    <button type="button" class="ms-toggle" aria-haspopup="listbox" aria-expanded="false">${escapeHtml(placeholder)}</button>
    <div class="ms-menu" hidden>
      <input type="text" class="ms-search" placeholder="Filter…" />
      <div class="ms-options" role="listbox"></div>
      <button type="button" class="ms-clear">Clear</button>
    </div>
  `;
  const toggle = container.querySelector(".ms-toggle");
  const menu = container.querySelector(".ms-menu");
  const optionsEl = container.querySelector(".ms-options");
  const searchEl = container.querySelector(".ms-search");

  function optionHtml(kind, row, checked) {
    // Its own span, so a long name is what truncates, never the number.
    // Reported live: "United States (71,7…" with the count cut off.
    const count = row.n == null ? "" : `<span class="ms-option-count">(${fmtInt(row.n)})</span>`;
    return `
      <label class="ms-option ms-${kind}">
        <input type="checkbox" data-kind="${kind}" value="${escapeHtml(row.value)}" ${checked ? "checked" : ""} />
        <span class="ms-option-text">${escapeHtml(row.label)}</span>${count}
      </label>`;
  }

  // Only the search box's own value, so a country the search opened can
  // still be collapsed by hand while that same search is still typed.
  // Auto-expanding on every render instead would make the chevron a
  // no-op for exactly the rows the reader is looking at.
  let lastQuery = null;

  function renderOptions(filterText = "") {
    const q = filterText.trim().toLowerCase();
    const freshQuery = q !== lastQuery;
    lastQuery = q;
    const blocks = [];
    for (const c of countries) {
      const cities = c.cities || [];
      const countryHit = !q || c.label.toLowerCase().includes(q);
      const cityHits = q ? cities.filter((t) => t.label.toLowerCase().includes(q)) : cities;
      if (q && !countryHit && !cityHits.length) continue;
      // A city that matched what somebody typed but sits inside a
      // collapsed country reads as no match at all, so open it.
      if (freshQuery && q && cityHits.length) expanded.add(c.value);
      const isOpen = expanded.has(c.value);
      // When the query itself picked cities out, show those and not the
      // country's other forty.
      const shownCities = q && cityHits.length ? cityHits : cities;
      const buriedPicks = !isOpen && cities.some((t) => selectedCities.has(t.value));
      blocks.push(`
        <div class="ms-group">
          <div class="ms-group-row">
            ${optionHtml("country", c, selectedCountries.has(c.value))}
            ${
              cities.length
                ? `<button type="button" class="ms-chevron${buriedPicks ? " has-picks" : ""}"
                     data-country="${escapeHtml(c.value)}" aria-expanded="${isOpen}"
                     aria-label="${isOpen ? "Hide" : "Show"} cities in ${escapeHtml(c.label)}">${MS_CHEVRON_SVG}</button>`
                : ""
            }
          </div>
          ${
            isOpen && shownCities.length
              ? `<div class="ms-cities">${shownCities
                  .map((t) => optionHtml("city", t, selectedCities.has(t.value)))
                  .join("")}</div>`
              : ""
          }
        </div>`);
    }
    optionsEl.innerHTML = blocks.join("") || '<div class="ms-empty">No matches.</div>';
  }

  function emit() {
    onChange({ countries: [...selectedCountries], cities: [...selectedCities] });
  }

  function labelFor(value, kind) {
    if (kind === "country") {
      const hit = countries.find((c) => c.value === value);
      return hit ? hit.label : countryLabel(value);
    }
    for (const c of countries) {
      const hit = (c.cities || []).find((t) => t.value === value);
      if (hit) return hit.label;
    }
    return value;
  }

  function updateLabel() {
    const total = selectedCountries.size + selectedCities.size;
    if (total === 0) {
      toggle.textContent = placeholder;
    } else if (total === 1) {
      toggle.textContent = selectedCountries.size
        ? labelFor([...selectedCountries][0], "country")
        : labelFor([...selectedCities][0], "city");
    } else {
      toggle.textContent = `${total} selected`;
    }
    toggle.classList.toggle("active", total > 0);
  }

  function close() {
    menu.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    OPEN_MULTISELECTS.delete(close);
  }

  function open() {
    OPEN_MULTISELECTS.forEach((closeOther) => closeOther());
    menu.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    OPEN_MULTISELECTS.add(close);
    searchEl.focus();
  }

  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    menu.hidden ? open() : close();
  });
  menu.addEventListener("click", (e) => e.stopPropagation()); // clicks inside the menu shouldn't bubble to document and self-close it

  optionsEl.addEventListener("change", (e) => {
    if (!e.target.matches('input[type="checkbox"]')) return;
    const set = e.target.dataset.kind === "city" ? selectedCities : selectedCountries;
    e.target.checked ? set.add(e.target.value) : set.delete(e.target.value);
    updateLabel();
    emit();
  });

  // The chevron sits outside the <label> on purpose: a click anywhere
  // inside a label toggles its checkbox, and expanding a country is not
  // the same act as filtering by it.
  optionsEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".ms-chevron");
    if (!btn) return;
    const value = btn.dataset.country;
    expanded.has(value) ? expanded.delete(value) : expanded.add(value);
    renderOptions(searchEl.value);
  });

  searchEl.addEventListener("input", () => renderOptions(searchEl.value));

  container.querySelector(".ms-clear").addEventListener("click", () => {
    selectedCountries.clear();
    selectedCities.clear();
    renderOptions(searchEl.value);
    updateLabel();
    emit();
  });

  renderOptions();
  updateLabel();

  return {
    setOptions(next) {
      // An empty list is what a facets payload still in the old shape
      // degrades to (see normalizeLocationFacets). Reconciling against
      // it would drop every pick the reader can still read in the URL,
      // so treat it as no news and keep what is already on screen.
      if (!next.length) return;
      // Picks are kept, never pruned. The list is the top 40 countries
      // under the other filters, so a pick can fall off it without being
      // wrong: Israel with Workplace set to Remote has 64 roles, not
      // enough for the top 40. Pruning it here re-ran the search without
      // it, and the reader landed on the global remote board. Reported
      // live. A kept pick is shown at the top of the list, without a
      // count, carrying whatever cities it had last time, so it is still
      // visible and can be unticked.
      const merged = next.map((c) => ({ ...c, cities: [...(c.cities || [])] }));
      const byValue = new Map(merged.map((c) => [c.value, c]));
      const shownCities = () => new Set(merged.flatMap((c) => c.cities.map((t) => t.value)));
      const kept = [];
      for (const v of selectedCountries) {
        if (byValue.has(v)) continue;
        const old = countries.find((c) => c.value === v);
        const row = { value: v, label: old ? old.label : countryLabel(v), n: null,
                      cities: old ? [...(old.cities || [])] : [] };
        kept.push(row);
        byValue.set(v, row);
      }
      const onScreen = shownCities();
      for (const v of selectedCities) {
        if (onScreen.has(v) || kept.some((c) => c.cities.some((t) => t.value === v))) continue;
        const home = countries.find((c) => (c.cities || []).some((t) => t.value === v));
        const city = home && home.cities.find((t) => t.value === v);
        const row = byValue.get(home ? home.value : "");
        const entry = { value: v, label: city ? city.label : v, n: null };
        if (row) {
          row.cities.unshift(entry);
        } else if (home) {
          const group = { value: home.value, label: home.label, n: null, cities: [entry] };
          kept.push(group);
          byValue.set(group.value, group);
        }
        // A city whose country was never listed (a shared link, say) has
        // no row to hang from. It stays selected and in the label anyway.
      }
      countries = [...kept, ...merged];
      renderOptions(searchEl.value);
      updateLabel();
    },
    reset() {
      selectedCountries.clear();
      selectedCities.clear();
      expanded.clear();
      searchEl.value = "";
      lastQuery = null;
      renderOptions("");
      updateLabel();
    },
    setSelected(countryValues, cityValues) {
      selectedCountries.clear();
      selectedCities.clear();
      countryValues.forEach((v) => selectedCountries.add(v));
      cityValues.forEach((v) => selectedCities.add(v));
      renderOptions(searchEl.value);
      updateLabel();
    },
  };
}

document.addEventListener("click", () => OPEN_MULTISELECTS.forEach((closeOther) => closeOther()));

// The filter rail
//
// Every filter that narrows by a value, as groups of checkboxes with
// their counts beside them, instead of the row of dropdowns this used
// to be. The dropdowns each held their counts one click away, so
// choosing between Security (516) and Data & Analytics (842) meant
// opening a menu, reading it, closing it, opening another. The counts
// are the whole reason to pick one filter over another, so they are on
// the page.
//
// The rail owns no filter state of its own. It reads `state` and writes
// `state`, then asks the board to reload, which is what lets a shared
// link, the Reset button and a click in here all arrive at the same
// place without three copies of the truth. The only things it does own
// are what is expanded and what has been typed into a group's search
// box, neither of which belongs in a URL.

// Group order is how often a group gets used, not the alphabet: where,
// then what kind of work, then how senior, then how it is worked, then
// who, then what it pays.
const RAIL_GROUPS = [
  { key: "location", title: "Country and city", kind: "tree", search: "Add a country or city" },
  { key: "department", title: "Category", kind: "list", facet: "categories" },
  { key: "seniority", title: "Level", kind: "list", facet: "seniority", labels: SENIORITY_LABELS },
  { key: "workplace", title: "Workplace", kind: "list", facet: "workplace", labels: WORKPLACE_LABELS },
  { key: "company", title: "Companies", kind: "list", facet: "companies", search: "Search companies", remote: true },
  { key: "salary", title: "Monthly salary estimate", kind: "range" },
];

// The rail as an accordion of four, each holding one or two of the
// groups above as labelled sub-sections. The shape Welcome to the
// Jungle's filters take: a closed group is one line, and a closed group
// with something ticked says what under its name.
const RAIL_ICON = (d) => `<svg class="rail-acc-icon" viewBox="0 0 24 24" width="16" height="16" fill="none"
  stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const RAIL_ACCORDION = [
  { key: "role", title: "Role", parts: ["department", "seniority"],
    icon: RAIL_ICON('<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 13h18"/>') },
  { key: "place", title: "Location", parts: ["location", "workplace"],
    icon: RAIL_ICON('<path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21Z"/><circle cx="12" cy="9.5" r="2.5"/>') },
  { key: "pay", title: "Salary", parts: ["salary"],
    icon: RAIL_ICON('<rect x="2.5" y="5" width="19" height="14" rx="2"/><path d="M2.5 10h19M6.5 15h4"/>') },
  { key: "employer", title: "Company", parts: ["company"],
    icon: RAIL_ICON('<path d="M4 21V5a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v16M15 9h4a1 1 0 0 1 1 1v11M3 21h18M8 8h3M8 12h3M8 16h3"/>') },
];
const RAIL_CHEVRON = '<svg class="rail-acc-chev" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
const RAIL_OPEN_KEY = "iljobs_rail_open";

// Which accordion groups are open. Remembered per browser; the first
// visit opens the first group with a selection, or Role.
let railOpen = null;

function railOpenSet() {
  if (railOpen) return railOpen;
  try {
    const saved = JSON.parse(localStorage.getItem(RAIL_OPEN_KEY) || "null");
    if (Array.isArray(saved)) railOpen = new Set(saved);
  } catch { /* storage refused: fall through to the default */ }
  if (!railOpen) {
    const first = RAIL_ACCORDION.find((a) => railAccordionSummary(a));
    railOpen = new Set([(first || RAIL_ACCORDION[0]).key]);
  }
  return railOpen;
}

function railSaveOpen() {
  try { localStorage.setItem(RAIL_OPEN_KEY, JSON.stringify([...railOpenSet()])); } catch { /* per-browser nicety only */ }
}

// What a closed group is narrowing the board by, in the rail's own
// words: "Infrastructure · Senior". Empty when nothing is ticked.
function railAccordionSummary(acc) {
  const bits = [];
  for (const part of acc.parts) {
    if (part === "department") bits.push(...state.department);
    if (part === "seniority") bits.push(...state.seniority.map((v) => SENIORITY_LABELS[v] || v));
    if (part === "workplace") bits.push(...state.workplace.map((v) => WORKPLACE_LABELS[v] || v));
    if (part === "location") {
      const countries = railFacets.locations || [];
      bits.push(...state.country.map((c) => (countries.find((x) => x.value === c) || {}).label || c));
      bits.push(...state.city);
    }
    if (part === "company") bits.push(...state.company.map((d) => companyNameFor(d)));
    if (part === "salary") {
      if (state.salary_min || state.salary_max) {
        const s = railFacets.salary || {};
        const lo = Number(state.salary_min) || s.min;
        const hi = Number(state.salary_max) || s.max;
        bits.push(lo && hi ? `${fmtShekels(lo)}–${fmtShekels(hi)}` : "Range set");
      }
      if (state.salary_known) bits.push("With an estimate");
      if (state.salary_disclosed) bits.push("Disclosed only");
    }
  }
  return bits.join(" · ");
}

// Four, then a link. Long enough that the common answer is usually on
// screen (Israel's four biggest cities are 79% of its listings), short
// enough that six groups still fit a column without scrolling.
const RAIL_TOP_N = 4;

// What the facets endpoint last said, by group key. Kept whole rather
// than merged into the groups above so a refresh replaces it in one
// assignment and nothing can half-update.
let railFacets = {};
// Whether a facets response has ever landed. Until one has, an empty
// group means "not counted yet", not "nothing to count", and the two
// have to look different: the first shows bones, the second shows
// nothing at all. A filtered load asks the API rather than the
// precomputed file and that can take seconds, during which the rail
// used to be a blank column with one heading on it.
let railFacetsLoaded = false;
// Which groups are showing everything rather than their top four, and
// which countries are folded. View state: it never reaches the URL or
// localStorage, and Back has no business re-collapsing a group somebody
// just opened.
const railExpanded = new Set();
const railCollapsed = new Set();
const railQueries = new Map();
// Companies found by typing past the top 500 the facet carries, so a
// name searched for once stays tickable while the reader looks at it.
const railFound = new Map();
let railSearchSeq = 0;
let railSearchTimer = 0;

function railLabel(group, row) {
  if (group.labels) return group.labels[row.value] || row.value;
  // The company facet carries the resolved name beside the count now;
  // a domain stays the label only for a company that has none yet.
  return row.label || row.name || row.value;
}

// Every value currently ticked in this group, as a Set for the render.
function railSelected(group) {
  if (group.key === "location") return new Set(state.country);
  return new Set(state[group.key] || []);
}

function railRows(group) {
  const counted = railFacets[group.facet];
  if (counted && counted.length) return counted;
  // No counts for this group in this answer. A closed enum still knows
  // its own options, so the filter keeps working; only the number
  // beside it is missing, which is what the dropdowns it replaced
  // showed for their whole life.
  if (group.labels) return Object.keys(group.labels).map((value) => ({ value, n: null }));
  return [];
}

// The rows a group shows: its search box first, then the top four
// unless it has been expanded. Anything already ticked is always shown,
// wherever it sits in the order, because a filter you cannot see is one
// you cannot turn off. Reported on the old dropdowns: the Companies
// list is alphabetical over ten thousand names and a reader who had
// picked five opened it to find none of them.
function railVisibleRows(group) {
  const q = (railQueries.get(group.key) || "").trim().toLowerCase();
  const picked = railSelected(group);
  const extra = group.remote
    ? [...railFound.values()].filter((r) => !railRows(group).some((x) => x.value === r.value))
    : [];
  const pool = [...railRows(group), ...extra];
  if (q) return pool.filter((r) => railLabel(group, r).toLowerCase().includes(q));
  if (railExpanded.has(group.key)) return pool;
  const top = pool.slice(0, RAIL_TOP_N);
  const shown = new Set(top.map((r) => r.value));
  return [...top, ...pool.filter((r) => picked.has(r.value) && !shown.has(r.value))];
}

const RAIL_CHECK_SVG =
  '<svg class="rail-tick" viewBox="0 0 12 12" aria-hidden="true">'
  + '<path d="M2.5 6.3 4.9 8.7 9.5 3.4" fill="none" stroke="currentColor" stroke-width="2"'
  + ' stroke-linecap="round" stroke-linejoin="round"/></svg>';
// The indeterminate mark: a country narrowed to some of its cities is
// neither on nor off, and a half-filled box is the shape a reader
// already reads as "partly".
const RAIL_DASH_SVG =
  '<svg class="rail-tick" viewBox="0 0 12 12" aria-hidden="true">'
  + '<path d="M3 6h6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';

// The part of a label the reader has actually typed, wrapped so it can
// be set in bold. Escapes around the match rather than escaping the
// whole string and hunting for the needle in the markup, which breaks
// the moment a name contains an ampersand.
function railMark(label, q) {
  const text = String(label || "");
  if (!q) return escapeHtml(text);
  const i = text.toLowerCase().indexOf(q);
  if (i < 0) return escapeHtml(text);
  return escapeHtml(text.slice(0, i))
    + `<b class="rail-hit">${escapeHtml(text.slice(i, i + q.length))}</b>`
    + escapeHtml(text.slice(i + q.length));
}

function railOptionHtml({ kind, value, label, n, checked, mixed = false, cls = "", mark: hit = "" }) {
  // The count gets its own element so a long name is what truncates,
  // never the number. Reported live on the old dropdown: "United States
  // (71,7…" with the count cut off.
  const mark = mixed ? RAIL_DASH_SVG : RAIL_CHECK_SVG;
  const cnt = n == null ? "" : `<span class="rail-count">${fmtInt(n)}</span>`;
  return `
    <label class="rail-option ${cls}${checked || mixed ? " on" : ""}">
      <input type="checkbox" data-kind="${kind}" value="${escapeHtml(value)}" ${checked ? "checked" : ""} />
      <span class="rail-box" aria-hidden="true">${mark}</span>
      <span class="rail-option-label">${railMark(label, hit)}</span>${cnt}
    </label>`;
}

function railMoreHtml(group, total) {
  if (railQueries.get(group.key)) return ""; // typing is a search; a "show all" under it is noise
  if (total <= RAIL_TOP_N) return "";
  const open = railExpanded.has(group.key);
  return `<button type="button" class="rail-more" data-more="${group.key}">`
    + `${open ? "Show fewer" : `Show all (${fmtInt(total)})`}</button>`;
}

function railSearchHtml(group) {
  if (!group.search) return "";
  const q = railQueries.get(group.key) || "";
  return `<input type="search" class="rail-search" data-search="${group.key}"
            placeholder="${escapeHtml(group.search)}" value="${escapeHtml(q)}"
            aria-label="${escapeHtml(group.search)}" autocomplete="off" />`;
}

// Location: countries, each with its own cities under it
//
// Several countries can be on at once and each is its own block, so
// "Tel Aviv and Berlin" is one question rather than two filters that
// have to be reconciled. A ticked country with no cities means the
// whole country; ticking a city narrows it to that city and leaves the
// country's own box showing a dash, because it is no longer the whole
// of anything.
function railCountryBlocks() {
  const q = (railQueries.get("location") || "").trim().toLowerCase();
  const countries = railFacets.locations || [];
  const picked = new Set(state.country);
  const cities = new Set(state.city);
  const blocks = [];

  for (const c of countries) {
    const all = c.cities || [];
    const countryHit = !q || (c.label || c.value).toLowerCase().includes(q);
    const cityHits = q ? all.filter((t) => (t.label || t.value).toLowerCase().includes(q)) : all;
    if (q && !countryHit && !cityHits.length) continue;
    // Only a country that is on, or that the reader is actively looking
    // for, gets a block. The facet lists sixty and a rail that drew all
    // sixty would be a page of countries with the filters underneath.
    if (!q && !picked.has(c.value) && !all.some((t) => cities.has(t.value))) continue;

    const mine = all.filter((t) => cities.has(t.value));
    // A search opens what it found. Closing a country by hand still
    // wins, so typing does not fight a reader who just collapsed one.
    const open = q ? !railCollapsed.has(c.value) || cityHits.length > 0 : !railCollapsed.has(c.value);
    const pool = q && cityHits.length ? cityHits : all;
    const top = railExpanded.has(`city:${c.value}`) || q ? pool : pool.slice(0, RAIL_TOP_N);
    const shownValues = new Set(top.map((t) => t.value));
    const shown = [...top, ...all.filter((t) => cities.has(t.value) && !shownValues.has(t.value))];

    blocks.push(`
      <div class="rail-country${open ? " open" : ""}">
        <div class="rail-country-row">
          <button type="button" class="rail-caret" data-country="${escapeHtml(c.value)}"
                  aria-expanded="${open}"
                  aria-label="${open ? "Collapse" : "Expand"} ${escapeHtml(c.label || c.value)}">
            <svg viewBox="0 0 10 6" aria-hidden="true"><path d="M1 1l4 4 4-4" fill="none"
              stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          ${railOptionHtml({
            kind: "country", value: c.value, label: c.label || c.value, n: c.n,
            checked: picked.has(c.value) && !mine.length, mixed: mine.length > 0,
            cls: "rail-country-option", mark: q,
          })}
        </div>
        ${open
          ? `<div class="rail-cities">
               ${shown.map((t, i) => railOptionHtml({
                 kind: "city", value: t.value, label: t.label || t.value, n: t.n,
                 checked: cities.has(t.value), mark: q,
                 cls: "rail-city" + (i === shown.length - 1 ? " last" : ""),
               })).join("")}
               ${!q && pool.length > RAIL_TOP_N
                 ? `<button type="button" class="rail-more rail-more-city" data-more="city:${escapeHtml(c.value)}">`
                   + `${railExpanded.has(`city:${c.value}`) ? "Show fewer" : `Show all (${fmtInt(pool.length)} cities)`}</button>`
                 : ""}
             </div>`
          // Folded, a country still has to say it is doing something,
          // or the count above the list disagrees with a rail that
          // looks idle.
          : mine.length ? `<div class="rail-country-note">${mine.length} ${mine.length === 1 ? "city" : "cities"}</div>` : ""}
      </div>`);
  }

  // Typing is how a country gets added, so the box has to answer with
  // countries the rail is not already showing.
  if (q) {
    const offered = countries.filter(
      (c) => !picked.has(c.value)
        && !(c.cities || []).some((t) => cities.has(t.value))
        && ((c.label || c.value).toLowerCase().includes(q)
            || (c.cities || []).some((t) => (t.label || t.value).toLowerCase().includes(q)))
    );
    if (!blocks.length && !offered.length) return '<div class="rail-empty">No matches.</div>';
  }
  if (!blocks.length) return "";
  return blocks.join("");
}

// Salary
//
// Two handles over the range the current result set actually occupies,
// which the facet measures rather than the frontend guessing. The whole
// group is left out when there is nothing to measure: no shekel figures
// in this result set means no track, rather than a track that cannot
// move.
// The disclosed option sits under the track, and stands on its own where
// there is no track: outside Israel nothing is in shekels, but plenty of
// employers state their pay. Always drawn, count and all, even at zero:
// in Israel no employer discloses, and an option that vanished there
// read as a feature that was never built rather than as that fact.
function railSalaryHtml() {
  const disclosed = railOptionHtml({
    kind: "salary_disclosed", value: "1", label: "Disclosed salary only",
    n: railFacets.salary_disclosed || 0, checked: state.salary_disclosed,
  });
  return railSalaryTrackHtml() + disclosed;
}

function railSalaryTrackHtml() {
  const s = railFacets.salary;
  if (!s || s.min == null || s.max == null || s.max <= s.min) return "";
  const lo = Number(state.salary_min) || s.min;
  const hi = Number(state.salary_max) || s.max;
  const pct = (v) => ((v - s.min) / (s.max - s.min)) * 100;
  return `
    <div class="rail-range" data-min="${s.min}" data-max="${s.max}">
      <div class="rail-range-track">
        <div class="rail-range-fill" style="left:${pct(lo)}%;right:${100 - pct(hi)}%"></div>
      </div>
      <input type="range" class="rail-range-input lo" min="${s.min}" max="${s.max}" step="1000"
             value="${lo}" aria-label="Lowest monthly salary" />
      <input type="range" class="rail-range-input hi" min="${s.min}" max="${s.max}" step="1000"
             value="${hi}" aria-label="Highest monthly salary" />
      <div class="rail-range-ends">
        <span class="rail-range-lo">${fmtShekels(lo)}</span>
        <span class="rail-range-hi">${fmtShekels(hi)}${hi >= s.max ? "+" : ""}</span>
      </div>
    </div>
    <!-- Two in three Israeli listings quote nothing at all, so the
         handles leave those alone and this is how somebody asks for the
         other behaviour. Without it, nudging a handle would answer by
         deleting most of the board. -->
    ${railOptionHtml({
      kind: "salary_known", value: "1", label: "Has an estimate",
      n: s.known, checked: state.salary_known,
    })}`;
}

// ₪28K, ₪4.5K, ₪900. Thousands because that is how Israeli monthly pay
// is spoken; the exact shekel under it is noise on a filter handle.
function fmtShekels(v) {
  if (v == null) return "";
  if (v < 1000) return `₪${Math.round(v)}`;
  const k = v / 1000;
  return `₪${k >= 10 || Number.isInteger(k) ? Math.round(k) : k.toFixed(1)}K`;
}

// A group's shape while its counts are still being fetched: the
// heading it will have, over rows the size of the rows that are coming.
// Bones rather than a spinner, for the same reason the job list uses
// them: the rail does not change size when the answer lands.
function railGroupSkeleton(group) {
  return `
    <section class="rail-group" data-group="${group.key}" aria-busy="true">
      <h3 class="rail-title">${escapeHtml(group.title)}</h3>
      <div class="rail-body">
        ${'<div class="rail-option rail-bone"><span class="skeleton sk-line"></span></div>'.repeat(RAIL_TOP_N)}
      </div>
    </section>`;
}

function renderFilterRail() {
  const host = document.getElementById("rail-groups");
  if (!host) return;
  // Each inner group's HTML by key, wrapped into the accordion below.
  const parts = {};
  const push = (key, html) => { parts[key] = html; };

  for (const group of RAIL_GROUPS) {
    let body = "";

    if (group.kind === "tree") {
      if (!railFacetsLoaded && !(railFacets.locations || []).length) {
        push(group.key, railGroupSkeleton(group));
        continue;
      }
      body = railSearchHtml(group) + railCountryBlocks();
    } else if (group.kind === "range") {
      body = railSalaryHtml();
      if (!body) continue; // nothing measurable here, so no group at all
    } else {
      const rows = railVisibleRows(group);
      const picked = railSelected(group);
      const pool = railRows(group);
      // A facet this snapshot cannot answer gets no group at all,
      // rather than a heading over "No matches", which reads as a
      // result and not as an absence. Happens for one merge cycle after
      // a new facet ships, while the precomputed facets.json is still
      // the version written before it existed. Only once something has
      // actually been counted, though: before that the same emptiness
      // means the answer is still coming.
      if (!pool.length && !picked.size && !railQueries.get(group.key)) {
        if (railFacetsLoaded) continue;
        push(group.key, railGroupSkeleton(group));
        continue;
      }
      body = railSearchHtml(group)
        + (rows.length
          ? rows.map((r) => railOptionHtml({
              kind: group.key, value: r.value, label: railLabel(group, r), n: r.n,
              checked: picked.has(r.value),
            })).join("")
          : '<div class="rail-empty">No matches.</div>')
        + railMoreHtml(group, pool.length);
    }

    push(group.key, `
      <section class="rail-group" data-group="${group.key}">
        <h3 class="rail-title">${escapeHtml(group.title)}</h3>
        <div class="rail-body">${body}</div>
      </section>`);
  }

  const open = railOpenSet();
  host.innerHTML = RAIL_ACCORDION.map((acc) => {
    const inner = acc.parts.map((k) => parts[k] || "").join("");
    if (!inner) return "";
    const isOpen = open.has(acc.key);
    const summary = isOpen ? "" : railAccordionSummary(acc);
    // One inner group needs no label of its own under a header that
    // already names it, except salary, whose label says what the
    // numbers are.
    const single = acc.parts.length === 1 && acc.key !== "pay";
    return `
      <div class="rail-acc${isOpen ? " open" : ""}${single ? " single" : ""}" data-acc="${acc.key}">
        <button type="button" class="rail-acc-head" aria-expanded="${isOpen}" aria-controls="rail-acc-${acc.key}">
          ${acc.icon}
          <span class="rail-acc-text">
            <span class="rail-acc-name">${escapeHtml(acc.title)}</span>
            ${summary ? `<span class="rail-acc-sum">${escapeHtml(summary)}</span>` : ""}
          </span>
          ${RAIL_CHEVRON}
        </button>
        <div class="rail-acc-body" id="rail-acc-${acc.key}"${isOpen ? "" : " hidden"}>${inner}</div>
      </div>`;
  }).join("");
}

// One listener on the rail rather than one per control, because the
// rail redraws itself after every change and per-control listeners
// would have to be rebound each time.
function wireFilterRail() {
  const host = document.getElementById("rail-groups");
  if (!host) return;

  host.addEventListener("change", (e) => {
    const box = e.target.closest('input[type="checkbox"]');
    if (box) return railToggle(box.dataset.kind, box.value, box.checked);
    const slider = e.target.closest(".rail-range-input");
    if (slider) return railCommitRange(slider);
  });

  // Live while dragging: the ends and the filled span follow the handle
  // so the number under the thumb is the number being chosen. The board
  // only reloads on release (the change event above), because a request
  // per pixel is a request per pixel.
  host.addEventListener("input", (e) => {
    const slider = e.target.closest(".rail-range-input");
    if (slider) return railPaintRange(slider);
    const search = e.target.closest(".rail-search");
    if (search) return railSearch(search);
  });

  host.addEventListener("click", (e) => {
    const head = e.target.closest(".rail-acc-head");
    if (head) {
      const acc = head.closest(".rail-acc");
      const key = acc.dataset.acc;
      const set = railOpenSet();
      set.has(key) ? set.delete(key) : set.add(key);
      railSaveOpen();
      renderFilterRail();
      host.querySelector(`.rail-acc[data-acc="${key}"] .rail-acc-head`)?.focus();
      return;
    }
    const more = e.target.closest(".rail-more");
    if (more) {
      const key = more.dataset.more;
      railExpanded.has(key) ? railExpanded.delete(key) : railExpanded.add(key);
      renderFilterRail();
      return;
    }
    // The caret sits outside the label on purpose: a click anywhere in
    // a label toggles its checkbox, and folding a country away is not
    // the same act as filtering by it.
    const caret = e.target.closest(".rail-caret");
    if (caret) {
      const c = caret.dataset.country;
      railCollapsed.has(c) ? railCollapsed.delete(c) : railCollapsed.add(c);
      renderFilterRail();
    }
  });
}

function railToggle(kind, value, on) {
  if (kind === "salary_known") {
    state.salary_known = on;
  } else if (kind === "salary_disclosed") {
    state.salary_disclosed = on;
  } else if (kind === "country") {
    // Clicking a country's own box is a claim about the whole country,
    // so it clears whatever cities were narrowing it.
    const home = (railFacets.locations || []).find((c) => c.value === value);
    const theirs = new Set((home?.cities || []).map((t) => t.value));
    state.city = state.city.filter((t) => !theirs.has(t));
    state.country = on ? [...new Set([...state.country, value])] : state.country.filter((v) => v !== value);
  } else if (kind === "city") {
    state.city = on ? [...new Set([...state.city, value])] : state.city.filter((v) => v !== value);
    // A city belongs to its country, and the API ANDs the two, so
    // picking Tel Aviv without IL asks for nothing. The country goes on
    // with it and shows a dash rather than a tick.
    const home = (railFacets.locations || []).find((c) => (c.cities || []).some((t) => t.value === value));
    if (on && home && !state.country.includes(home.value)) state.country = [...state.country, home.value];
    if (!on && home) {
      const left = (home.cities || []).some((t) => state.city.includes(t.value));
      // The last city out of a country leaves the country itself on,
      // which is the honest reading of "I was looking here".
      if (!left && !state.country.includes(home.value)) state.country = [...state.country, home.value];
    }
  } else {
    const next = new Set(state[kind] || []);
    on ? next.add(value) : next.delete(value);
    state[kind] = [...next];
  }
  railApply();
}

function railApply() {
  state.offset = 0;
  renderFilterRail();
  renderActiveChips();
  loadJobs();
}

function railPaintRange(slider) {
  const box = slider.closest(".rail-range");
  const lo = box.querySelector(".lo");
  const hi = box.querySelector(".hi");
  // The handles cannot cross. Pushing rather than clamping, so a reader
  // dragging the low handle past the high one takes the high one along
  // instead of hitting an invisible wall.
  if (slider === lo && Number(lo.value) > Number(hi.value)) hi.value = lo.value;
  if (slider === hi && Number(hi.value) < Number(lo.value)) lo.value = hi.value;
  const min = Number(box.dataset.min);
  const max = Number(box.dataset.max);
  const pct = (v) => ((v - min) / (max - min)) * 100;
  const fill = box.querySelector(".rail-range-fill");
  fill.style.left = `${pct(Number(lo.value))}%`;
  fill.style.right = `${100 - pct(Number(hi.value))}%`;
  box.querySelector(".rail-range-lo").textContent = fmtShekels(Number(lo.value));
  box.querySelector(".rail-range-hi").textContent =
    fmtShekels(Number(hi.value)) + (Number(hi.value) >= max ? "+" : "");
}

function railCommitRange(slider) {
  railPaintRange(slider);
  const box = slider.closest(".rail-range");
  const min = Number(box.dataset.min);
  const max = Number(box.dataset.max);
  const lo = Number(box.querySelector(".lo").value);
  const hi = Number(box.querySelector(".hi").value);
  // A handle resting on its stop is not a filter. Sending it anyway
  // would put salary_min in the URL of every visit that so much as
  // brushed the track, and would drop every listing that quotes nothing
  // the moment the reader also ticked "only with an estimate".
  const wasSet = !!(state.salary_min || state.salary_max);
  state.salary_min = lo > min ? String(lo) : "";
  state.salary_max = hi < max ? String(hi) : "";
  // Dragging a salary handle means "show me jobs that pay this", and a
  // listing with no figure does not answer that. So the first move ticks
  // Only with an estimate, where the reader can see it and untick it to
  // bring the rest back. Reported live: a ₪32K-66K range still led with
  // listings that quote nothing.
  if (!wasSet && (state.salary_min || state.salary_max)) state.salary_known = true;
  railApply();
}

function railSearch(input) {
  const key = input.dataset.search;
  railQueries.set(key, input.value);
  const group = RAIL_GROUPS.find((g) => g.key === key);
  railRedrawGroup(key);

  if (!group || !group.remote) return;
  // The facet carries the 500 biggest employers. Typing a name past
  // that line used to say No matches while the company had listings, so
  // the box asks the API for the rest, counted under the other active
  // filters.
  clearTimeout(railSearchTimer);
  const q = input.value.trim();
  if (q.length < 2) return;
  railSearchTimer = setTimeout(async () => {
    const seq = ++railSearchSeq;
    try {
      const data = await getJSON(`/companies/search?${qs({ ...currentFilterParams(), company: "", name: q })}`);
      if (seq !== railSearchSeq) return; // a later keystroke has its own answer coming
      (data.companies || []).forEach((r) => railFound.set(r.value, { value: r.value, n: r.n, name: r.name }));
      railRedrawGroup(key);
    } catch {
      // Non-fatal: the group keeps whatever it already had.
    }
  }, 250);
}

// Only the one group's body, so the search box keeps focus and the
// caret keeps its place. Redrawing the whole rail on every keystroke
// took the focus with it and typing a company name was one character
// per click.
function railRedrawGroup(key) {
  const host = document.getElementById("rail-groups");
  const section = host?.querySelector(`.rail-group[data-group="${key}"] .rail-body`);
  if (!section) return renderFilterRail();
  const group = RAIL_GROUPS.find((g) => g.key === key);
  const search = section.querySelector(".rail-search");
  const focused = document.activeElement === search;
  const caret = search ? search.selectionStart : null;

  if (group.kind === "tree") {
    section.innerHTML = railSearchHtml(group) + railCountryBlocks();
  } else {
    const rows = railVisibleRows(group);
    const picked = railSelected(group);
    section.innerHTML = railSearchHtml(group)
      + (rows.length
        ? rows.map((r) => railOptionHtml({
            kind: group.key, value: r.value, label: railLabel(group, r), n: r.n,
            checked: picked.has(r.value),
          })).join("")
        : '<div class="rail-empty">No matches.</div>')
      + railMoreHtml(group, railRows(group).length);
  }
  if (focused) {
    const next = section.querySelector(".rail-search");
    next.focus();
    if (caret != null) next.setSelectionRange(caret, caret);
  }
}

// Active filter chips
//
// The rail says what is on, in six separate groups. This says it again
// in one line where the reader is already looking, and it is the only
// place that can drop a single value without opening the group it came
// from.
function activeChips() {
  const chips = [];
  const label = (group, value) => {
    const g = RAIL_GROUPS.find((x) => x.key === group);
    if (g?.labels) return g.labels[value] || value;
    return value;
  };
  // A city names its own place; its country is already implied by it,
  // so a country only gets a chip when no city has narrowed it.
  const narrowed = new Set();
  for (const city of state.city) {
    const home = (railFacets.locations || []).find((c) => (c.cities || []).some((t) => t.value === city));
    if (home) narrowed.add(home.value);
    chips.push({ kind: "city", value: city, text: city });
  }
  for (const code of state.country) {
    if (narrowed.has(code)) continue;
    const home = (railFacets.locations || []).find((c) => c.value === code);
    chips.push({ kind: "country", value: code, text: home?.label || countryLabel(code) });
  }
  for (const key of ["department", "seniority", "workplace", "company"]) {
    for (const v of state[key] || []) chips.push({ kind: key, value: v, text: label(key, v) });
  }
  if (state.max_age_days) {
    chips.push({ kind: "max_age_days", value: "", text: `Past ${state.max_age_days} days` });
  }
  if (state.salary_min || state.salary_max) {
    const s = railFacets.salary || {};
    const lo = Number(state.salary_min) || s.min;
    const hi = Number(state.salary_max) || s.max;
    chips.push({ kind: "salary", value: "", text: `${fmtShekels(lo)}–${fmtShekels(hi)}` });
  }
  if (state.salary_known) chips.push({ kind: "salary_known", value: "", text: "Has an estimate" });
  if (state.salary_disclosed) chips.push({ kind: "salary_disclosed", value: "", text: "Disclosed salary" });
  return chips;
}

function railToggleLabel() {
  const btn = document.getElementById("rail-toggle");
  if (!btn) return;
  let n = 0;
  for (const key of ["department", "seniority", "company", "workplace"]) {
    if ((state[key] || []).length) n += 1;
  }
  if (state.country.length || state.city.length) n += 1;
  if (state.max_age_days) n += 1;
  // The word alone, with the number as a badge the phone draws from
  // data-count. "Filters (3)" in a 44px button beside a search box is
  // most of the row spent on two brackets.
  btn.textContent = "Filters";
  btn.dataset.count = String(n);
}

function renderActiveChips() {
  railToggleLabel();
  const host = document.getElementById("active-chips");
  if (!host) return;
  const chips = activeChips();
  host.innerHTML = chips
    .map((c) => `<button type="button" class="chip" data-chip="${escapeHtml(c.kind)}"
                   data-value="${escapeHtml(c.value)}" title="Remove this filter">
                   <span class="chip-label">${escapeHtml(c.text)}</span>${CHIP_X_SVG}</button>`)
    .join("");
  // Reset appears with the first chip and goes away with the last. A
  // permanent Reset beside an unfiltered board offers to undo nothing.
  const reset = document.getElementById("f-reset");
  if (reset) reset.hidden = !chips.length && !state.search && state.roles === "tech" && !state.starred_only;
}

// Drawn, not typed. ✕ renders as a colour emoji tile inside a button on
// iOS unless the font is told otherwise, which is the same bug the
// Apply arrow hit (see EXTERNAL_ARROW_SVG) and the same fix.
const CHIP_X_SVG =
  '<svg class="chip-x" viewBox="0 0 10 10" width="10" height="10" aria-hidden="true">'
  + '<path d="M2 2l6 6M8 2l-6 6" fill="none" stroke="currentColor" stroke-width="1.6"'
  + ' stroke-linecap="round"/></svg>';

function wireActiveChips() {
  const host = document.getElementById("active-chips");
  if (!host) return;
  host.addEventListener("click", (e) => {
    const chip = e.target.closest("[data-chip]");
    if (!chip) return;
    const { chip: kind, value } = chip.dataset;
    if (kind === "max_age_days") {
      state.max_age_days = "";
      document.getElementById("f-date-posted").value = "";
    } else if (kind === "salary") {
      state.salary_min = "";
      state.salary_max = "";
    } else if (kind === "salary_known") {
      state.salary_known = false;
    } else if (kind === "salary_disclosed") {
      state.salary_disclosed = false;
    } else if (kind === "country" || kind === "city") {
      railToggle(kind, value, false);
      return; // railToggle applies on its own
    } else {
      state[kind] = (state[kind] || []).filter((v) => v !== value);
    }
    railApply();
  });
}


// the search box

// One box, two homes. Above 800px it is the first control in the topbar,
// where it stays put however far the list runs; below 800px there is no
// room up there, so it goes back to the filter bar the phone layout
// already put it in. The same element moves either way: two inputs that
// have to agree is how a search box ends up showing one thing and
// filtering by another.
function wireSearchHome() {
  const box = document.getElementById("topbar-search");
  const up = document.querySelector(".topbar-center");
  const down = document.querySelector(".board-bar-row-1");
  if (!box || !up || !down) return;
  const narrow = window.matchMedia("(max-width: 800px)");
  const place = () => {
    const home = narrow.matches ? down : up;
    if (box.parentElement === home) return;
    const held = document.activeElement === document.getElementById("f-search");
    if (narrow.matches) home.prepend(box);
    else home.insertBefore(box, document.querySelector(".topbar-sorts"));
    // Moving a node blurs whatever was focused inside it.
    if (held) document.getElementById("f-search").focus();
  };
  place();
  narrow.addEventListener("change", place);
}

// The / hint and the clear cross are two views of one thing: whether
// there is anything in the box.
function paintSearchBox() {
  const el = document.getElementById("f-search");
  if (!el) return;
  const has = el.value.length > 0;
  const box = el.closest(".topbar-search");
  if (box) box.classList.toggle("has-text", has);
  const clear = document.getElementById("f-search-clear");
  if (clear) clear.hidden = !has;
}

// Every writer goes through here, so the cross and the hint never
// describe a value the box no longer holds.
function setSearchBox(value) {
  const el = document.getElementById("f-search");
  if (!el) return;
  el.value = value;
  paintSearchBox();
}

let searchDebounced = null;

// Searches what is in the box now, without waiting out the typing timer.
// Enter, the Search button and the clear cross all land here.
function applySearchNow(raw) {
  if (searchDebounced) searchDebounced.cancel();
  const next = raw.trim();
  if (next === state.search) return;
  state.search = next;
  // A broadening applies to the search it was asked for, not to the
  // next one somebody types.
  state.search_mode = "";
  followSearchSort();
  state.offset = 0;
  loadJobs();
}

function wireSearchBox() {
  wireSearchHome();
  const el = document.getElementById("f-search");
  if (!el) return;
  paintSearchBox();

  // 500, not 300. At 300 a ten-character word fired eight requests, each
  // one a full search, and the later ones queued behind the earlier ones
  // until the gateway gave up at 29 seconds and the reader got a 500
  // instead of results. Typing settles inside 500ms between keys for
  // almost everyone, so this is one request per word rather than one per
  // keystroke.
  searchDebounced = debounce((value) => applySearchNow(value), 500);
  el.addEventListener("input", (e) => {
    paintSearchBox();
    searchDebounced(e.target.value);
  });

  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      applySearchNow(e.target.value);
      return;
    }
    // Escape empties the box before it reaches the handler that closes
    // the open listing. With nothing to empty it falls through.
    if (e.key === "Escape" && e.target.value) {
      e.stopPropagation();
      setSearchBox("");
      applySearchNow("");
    }
  });

  const clear = document.getElementById("f-search-clear");
  if (clear) {
    clear.addEventListener("click", () => {
      setSearchBox("");
      applySearchNow("");
      el.focus();
    });
  }

  // It had no handler at all: clicking it searched nothing and only
  // worked because the box had already searched as you typed.
  const go = document.getElementById("f-search-go");
  if (go) go.addEventListener("click", () => applySearchNow(el.value));

  // / focuses the box, the shortcut every list on the web has. Ignored
  // while the reader is already typing somewhere, and while a modifier
  // is down, where it belongs to the browser.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || ""))) return;
    e.preventDefault();
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  });
}

// filter wiring

function wireFilters() {
  wireSearchBox();

  renderFilterRail();
  wireFilterRail();
  wireActiveChips();

  document.getElementById("f-date-posted").addEventListener("change", (e) => {
    if (!e.target.value) return; // the blank "Date posted" placeholder, not a real choice
    state.max_age_days = e.target.value === "any" ? "" : e.target.value;
    state.offset = 0;
    loadJobs();
  });

  // Newest/Oldest are both just the existing age-column sort under a
  // clearer label -- the th[data-sort="age"] click handler below does the
  // same thing, this is a second, more discoverable way in. Salary and
  // Relevance sorting aren't here: neither has real backing yet (salary
  // estimates are computed but never persisted as a sortable number, and
  // there's no ranking/search-relevance signal in the schema at all).
  document.getElementById("f-sort").addEventListener("change", (e) => {
    if (!e.target.value) return; // the blank "Sort" placeholder, not a real choice
    const [key, dir] = e.target.value.split(":");
    state.sortExplicit = true;
    setActiveSortHeader(key, dir);
    state.offset = 0;
    loadJobs();
  });

  document.getElementById("view-switch").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-view]");
    if (btn) setView(btn.dataset.view);
  });

  document.getElementById("f-reset").addEventListener("click", () => {
    state.search = "";
    state.search_mode = "";
    state.sortExplicit = false;
    state.department = [];
    state.seniority = [];
    state.company = [];
    state.country = [];
    state.city = [];
    state.workplace = [];
    state.skills = [];
    state.max_age_days = "";
    state.salary_min = "";
    state.salary_max = "";
    state.salary_known = false;
    state.salary_disclosed = false;
    state.starred_only = false;
    state.roles = "tech";
    state.sort = "age";
    state.dir = "asc";
    state.offset = 0;
    setSearchBox("");
    document.getElementById("f-date-posted").value = "";
    // What the rail was showing, not what it was filtering by: a reader
    // who reset while three countries were folded open should get them
    // folded open and empty, not re-collapsed.
    railQueries.clear();
    railFound.clear();
    setActiveSortHeader("age", "asc");
    renderFilterRail();
    renderActiveChips();
    loadJobs();
  });

  document.getElementById("match-panel").addEventListener("click", (e) => {
    const toggle = e.target.closest(".match-panel-toggle");
    if (toggle) {
      const open = toggle.getAttribute("aria-expanded") !== "true";
      try { localStorage.setItem(MATCH_SKILLS_OPEN_KEY, open ? "1" : "0"); } catch {}
      toggle.setAttribute("aria-expanded", String(open));
      document.getElementById("match-skills").classList.toggle("open", open);
      document.querySelectorAll("#match-skills .match-skill").forEach((b) => {
        if (open) b.removeAttribute("tabindex");
        else b.setAttribute("tabindex", "-1");
      });
      return;
    }
    const chip = e.target.closest(".match-skill");
    if (!chip) return;
    state.skills = state.skills.filter((s) => s !== chip.dataset.matchSkill);
    if (!state.skills.length && state.sort === "match") setActiveSortHeader("age", "asc");
    state.offset = 0;
    loadJobs();
  });

  document.querySelectorAll("[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      const isSameColumn = state.sort === key;
      const dir = isSameColumn && state.dir === "desc" ? "asc" : "desc";
      state.sortExplicit = true;
      setActiveSortHeader(key, dir);
      state.offset = 0;
      loadJobs();
    });
  });

  wireRailSheet();
  wireInfiniteList();
}

function setActiveSortHeader(key, dir) {
  state.sort = key;
  state.dir = dir;
  document.querySelectorAll("[data-sort]").forEach((h) => {
    h.classList.remove("active");
    h.removeAttribute("data-dir");
  });
  // No header for this key: the board sorts by two columns and anything
  // else is a stale link or a typo. Threw a TypeError here before,
  // after the rows had already rendered, so the board looked fine while
  // the sort UI was left with nothing marked active.
  const th = document.querySelector(`[data-sort="${key}"]`);
  if (th) {
    th.classList.add("active");
    th.setAttribute("data-dir", dir === "desc" ? "↓" : "↑");
  }
  // Keep the "Sort:" dropdown in step -- it only offers the two age-column
  // sorts (Newest/Oldest), so clicking the Age header updates it and
  // clicking the Listing header falls back to its blank "Sort" placeholder
  // rather than showing a now-wrong stale option.
  document.getElementById("f-sort").value =
    key === "age" ? `age:${dir}` : key === "match" ? "match:asc" : key === "relevance" ? "relevance:asc" : "";
}

// boot

// Board-scoped (see refreshFacetOptions) -- narrows with the board's own
// active filters, same as msDepartment/msLocation. NOT what the
// alert-creation form uses (see globalCompanyOptions below): an alert
// you're setting up for the future shouldn't be limited to whatever
// company filter happens to be active on the board right now.
let latestCompanyOptions = null;

// The alert-creation form's own company list: every resolved company,
// unscoped, fetched once and reused (populateAlertFilterOptions runs on
// every refreshStats() tick, but there's no reason to re-fetch the same
// global list that often -- companies.length changes maybe once a day).
let globalCompanyOptions = null;
async function loadGlobalCompanyOptions() {
  if (globalCompanyOptions) return globalCompanyOptions;
  try {
    const companies = await getJSON("/companies?resolved_only=1");
    globalCompanyOptions = [...companies.companies]
      .sort((a, b) => a.domain.localeCompare(b.domain))
      .map((c) => ({ value: c.domain, label: c.domain }));
  } catch {
    // Non-fatal: the alert form's company picker just stays empty/stale.
  }
  return globalCompanyOptions;
}

// Category/Location/Company dropdown counts, scoped to whatever ELSE is
// currently selected -- reported live: picking Security then Israel-only
// still showed Security's GLOBAL count (369) in the dropdown, not the 98
// that combination actually returns, because these used to come from
// /api/stats' top_departments/top_locations, which are deliberately
// global (that's right for the Market Stats dashboard and the
// alert-creation form -- see populateAlertFilterOptions, which still
// reads latestStats directly for exactly that reason -- but wrong for a
// live filter's own option counts). /api/facets computes each facet
// with every OTHER active filter applied but that facet's own key
// excluded (server-side, see handler.py's route_facets), so this is one
// query per call, not a client-side recount. Called from loadJobs() on
// every filter change, and from refreshStats()'s periodic tick so counts
// still track new postings without waiting for the next filter click.
// The locations facet, sanity-checked before it reaches the dropdown.
//
// Two different files answer that key. The filtered path asks the API,
// which is being deployed with the nested country/cities shape. The
// unfiltered path (every page load, every timer tick) reads the
// precomputed /facets.json, which the merge only rewrites on its next
// run, so for one cycle after deploy it is still the old flat list of
// raw location strings. Those entries have no `label` and no `cities`,
// so rendering them as countries would put "Tel Aviv, Israel" at the top
// level and then send it to the API as country=Tel Aviv, Israel.
//
// The `label` is what tells the two shapes apart: the new contract is
// the only one that carries a country name separate from its code.
// Anything else degrades to an empty list, which setOptions treats as
// no news rather than as a reason to clear the reader's picks. Nothing
// in here can throw either -- refreshFacetOptions wraps every facet in
// one try/catch, so a single bad key takes the categories and companies
// dropdowns down with it.
// Fills COUNTRY_LABELS_SEEN as a side effect, so describeAlertFilter can
// name a code without the frontend carrying its own country list.
function normalizeLocationFacets(rows) {
  if (!Array.isArray(rows)) return [];
  return rows
    .filter((r) => r && typeof r.value === "string" && typeof r.label === "string")
    .map((r) => {
      COUNTRY_LABELS_SEEN.set(r.value, r.label);
      return {
        value: r.value,
        label: r.label,
        n: r.n,
        cities: (Array.isArray(r.cities) ? r.cities : [])
          .filter((c) => c && typeof c.value === "string")
          .map((c) => ({ value: c.value, label: c.value, n: c.n })),
      };
    });
}

// What the counting endpoints should be asked about. confidence rides on
// every request, so it is not a filter. skills is not one either: in Best
// matches it orders what the filters already left, it does not narrow it
// (see setView). Sending it anyway asked /facets and /stats a question no
// precomputed variant answers, so both computed live -- measured on the
// box at 7.2s and 4.2s -- for counts that are identical to the ones
// without it. Those two requests held gunicorn slots for seven seconds,
// which is what made opening a listing take five: the description was
// queued behind them.
function countingParams() {
  return { ...currentFilterParams(), confidence: "", skills: "" };
}

let facetsRequestSeq = 0;

async function refreshFacetOptions() {
  const seq = ++facetsRequestSeq;
  try {
    // Only the unfiltered case has a static answer, which is also the
    // one every page load and every timer tick asks for. Any active
    // filter goes to the API, where the counts are scoped to it.
    //
    // confidence is the exception: the board always sends one, so it is
    // not a filter in the sense that matters here. The static file holds
    // a variant per value.
    const active = countingParams();
    const facets = qs(active)
      ? await getJSON(`/facets?${qs({ ...currentFilterParams(), skills: "" })}`)
      : await getStaticFacets(state.confidence || "verified");
    if (seq !== facetsRequestSeq) return; // a newer filter is already being counted
    // By count, every group, because the rail's whole rule is that the
    // top four are the four biggest. The old dropdowns sorted Companies
    // alphabetically, which was right for a list you type into and
    // wrong for one that shows you four and hides the rest.
    railFacetsLoaded = true;
    railFacets = {
      categories: facets.categories || [],
      seniority: facets.seniority || [],
      workplace: facets.workplace || [],
      companies: facets.companies || [],
      locations: normalizeLocationFacets(facets.locations),
      // Absent on a snapshot written before the columns existed, and on
      // a result set with no shekel figures in it. Either way the rail
      // leaves the group out rather than drawing a dead track.
      salary: facets.salary || null,
      salary_disclosed: facets.salary_disclosed || 0,
    };
    latestCompanyOptions = [...(facets.companies || [])]
      .sort((a, b) => a.value.localeCompare(b.value))
      .map((r) => ({ value: r.value, label: `${r.value} (${r.n})` }));
    renderFilterRail();
    renderActiveChips();
    populateAlertFilterOptions();
  } catch {
    // Non-fatal: worst case the dropdowns keep their previous option set.
  }
}

// theme (light/dark)

const THEME_KEY = "iljobs_theme";

function isDarkTheme() {
  return document.documentElement.getAttribute("data-theme") === "dark";
}

// Set by wireThemeToggle. The account menu's own theme row calls it, so
// the crossfade and the storage write stay in one place rather than
// being written a second time next to the menu.
let runThemeSwap = null;

// Returns a promise that settles once the attribute has actually
// changed. A view transition runs the callback asynchronously, so a
// caller that reads the theme straight after this still sees the old
// one: the account menu's row said "Dark mode" after switching to dark.
function toggleTheme() {
  if (!runThemeSwap) return Promise.resolve();
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced || typeof document.startViewTransition !== "function") {
    runThemeSwap();
    return Promise.resolve();
  }
  return document.startViewTransition(runThemeSwap).updateCallbackDone;
}

function wireThemeToggle() {
  const btn = document.getElementById("theme-toggle");
  const sync = () => {
    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    // A switch, not a labelled button: the CSS draws the side from
    // :root[data-theme], so the state is all this has to say.
    btn.setAttribute("aria-checked", String(isDark));
  };
  sync(); // index.html's inline head script already applied the saved theme before this ran

  function swapTheme() {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    if (next === "dark") {
      document.documentElement.setAttribute("data-theme", "dark");
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      // Private browsing or a full quota. The theme still switches; it
      // just will not be remembered, which is not worth failing over.
    }
    sync();
  }

  // One crossfade of the whole page, run by the compositor, instead of a
  // transition on every element.
  //
  // The old version put `transition: background-color, color,
  // border-color, fill` on .theme-transition * !important for 450ms.
  // That is 2,430 elements on a full board, four properties each: around
  // 9,700 transitions, none of which the compositor can help with,
  // because changing a colour means repainting. Hence the lag on a
  // phone, reported live.
  //
  // And it never animated anything anyway. Measured with a
  // transitionstart counter on a real page: zero started, and the colours
  // were at their new values in the same frame as the click. The class
  // and the theme attribute were set in one go, so no style was ever
  // resolved with the transition in place and nothing had a value to
  // animate from.
  //
  // A view transition has neither problem. The browser snapshots the
  // page before and after and crossfades the two images on the
  // compositor: one animation, no repaint per frame, and it cannot fail
  // to start because the snapshot is taken before the callback runs.
  runThemeSwap = swapTheme;
  btn.addEventListener("click", toggleTheme);
}

// Refreshes everything driven by /api/stats -- metrics, market panels,
// filter option counts. Not the job table/pagination itself: a listing
// re-rendering under a user mid-scroll or mid-page would be more
// disruptive than useful, so that stays a manual reload.
// Cached so the alert-creation form can reuse the board's own last-fetched
// stats (top_departments/top_locations) instead of a duplicate fetch.
let latestStats = null;

const STATS_CACHE_KEY = "iljobs_stats_cache";

function getCachedStats() {
  try {
    const raw = localStorage.getItem(STATS_CACHE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && parsed.totals ? parsed : null;
  } catch {
    return null;
  }
}

// The applier publishes /stats.json and /facets.json to the frontend
// bucket on every write cycle, so CloudFront can hand the browser the
// same answer the API would compute without invoking anything. Same
// pattern bootstrap.json has always used.
//
// This is where the API's compute bill actually was: the page polls
// these on a timer per open tab, and each poll was a Lambda cold start
// downloading a 308MB snapshot to hand back JSON already sitting in S3.
//
// The API route stays exactly as it was, both as the fallback here and
// as the answer anyone calling /api/stats directly still gets. A missing
// or unreachable static copy is a normal state, not an error: it is what
// every deploy looks like until the next merge runs.
// Facets come keyed by confidence, since the board always sends one.
// An older file without the variant map, or none at all, falls back to
// the API rather than guessing.
async function getStaticFacets(confidence) {
  try {
    const r = await fetch("/facets.json", { cache: "no-store" });
    if (r.ok) {
      const byConfidence = await r.json();
      if (byConfidence && byConfidence[confidence]) return byConfidence[confidence];
    }
  } catch {
    // Falls through to the API, which is the authority anyway.
  }
  return getJSON(`/facets?${qs(currentFilterParams())}`);
}

async function getStaticOrApi(staticPath, apiPath) {
  try {
    const r = await fetch(staticPath, { cache: "no-store" });
    if (r.ok) return await r.json();
  } catch {
    // Falls through to the API, which is the authority anyway.
  }
  return getJSON(apiPath);
}

async function refreshStats() {
  // Cache-first, matching the job list. Without this the Statistics
  // column sat on skeletons for the whole request on every single visit,
  // even though /api/stats changes at most once an hour (the merge) and
  // the previous answer is almost always still correct. The request
  // still always runs and silently replaces this.
  const cachedStats = getCachedStats();
  if (cachedStats) {
    latestStats = cachedStats;
    renderMetrics(cachedStats);
    renderPanels(cachedStats);
    renderScopedPanels(cachedStats);
  }

  try {
    const stats = await getStaticOrApi("/stats.json", "/stats");
    latestStats = stats;
    renderMetrics(stats);
    renderPanels(stats);
    renderScopedPanels(stats);
    refreshFacetOptions();
    // Not awaited, and forced: a filtered board's tiles go stale on the
    // same 2-minute clock the global ones do, and this tick is also the
    // catch-up a tab gets when it becomes visible again.
    refreshScopedStats({ force: true });
    populateAlertFilterOptions();
    try {
      localStorage.setItem(STATS_CACHE_KEY, JSON.stringify(stats));
    } catch {
      // Quota or private browsing. Purely a speed-up, never load-bearing.
    }
  } catch (err) {
    // Don't bury a perfectly good cached dashboard under an error banner
    // over one failed refresh.
    if (!cachedStats) {
      const msg = `<div class="error-state" style="grid-column:1/-1">Could not load /api/stats: ${escapeHtml(err.message)}</div>`;
      // Each guarded, because /board has none of these any more (see
      // renderScopedMetrics) while /stats has all four. The other two
      // grids are the same failure, and four copies of one message are
      // not four times the information, so their bones are cleared
      // rather than repeating it.
      const paint = (id, html) => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = html;
      };
      paint("metrics-grid", msg);
      paint("panel-grid", msg);
      paint("scoped-panel-grid", "");
      paint("pipeline-grid", "");
      // The board's own failure surface for this is the empty state,
      // which says what it could not count rather than staying blank.
      renderDetailEmpty();
    }
  }
}

// The scoped half of the Statistics column: the five tiles and Top
// hirers, asked for with whatever the filter bar currently says.
//
// Sequenced the way loadJobs is, and for the same reason it had to be. A
// scoped /stats was measured at up to 2.9 seconds, which is several
// filter changes' worth of typing, and without the counter a slow answer
// for an abandoned filter paints straight over a fast one for the
// current filter. Abort rather than ignore, too: an answer nobody will
// read still costs the reader's bandwidth and us a Lambda invocation.
let scopedStatsSeq = 0;
let scopedStatsInFlight = null;
// The filter string currently on the wire, so loadJobs' calls for a page
// turn or a sort flip (neither of which changes what is counted) do not
// each fire a duplicate of the request already running.
let scopedStatsPending = null;

async function refreshScopedStats({ force = false } = {}) {
  // refreshFacetOptions' test, not a second definition of it: confidence
  // rides along on every request the board makes, so it is not a filter
  // in the sense that decides between the static artifact and the API.
  const params = qs(countingParams());

  if (!params) {
    // Unfiltered, which is the path that must stay on the precomputed
    // /stats.json artifact. refreshStats is already holding that answer,
    // so there is nothing to ask anyone.
    if (scopedStatsInFlight) scopedStatsInFlight.abort();
    scopedStatsInFlight = null;
    scopedStatsPending = null;
    scopedStatsSeq++; // retires anything still resolving
    latestScoped = null;
    renderScopeDependent();
    return;
  }

  const held = latestScoped !== null && latestScoped.params === params;
  // "already being asked" has to mean a request that is actually in
  // flight. scopedStatsPending is a string, and an aborted request's
  // finally only clears it when its own sequence is still current, so
  // the string can outlive the request that set it. Once that happened,
  // every later call for the same filters took this early return and
  // never asked anyone, and currentScopeMode stayed "pending" for the
  // rest of the session: the overview's tiles hold skeletons forever,
  // which looks exactly like a board that is still loading.
  const asking = scopedStatsPending === params && scopedStatsInFlight !== null;
  if (!force && (held || asking)) {
    renderScopeDependent(); // already answered, or already being asked
    return;
  }

  const seq = ++scopedStatsSeq;
  if (scopedStatsInFlight) scopedStatsInFlight.abort();
  const inFlight = new AbortController();
  scopedStatsInFlight = inFlight;
  scopedStatsPending = params;
  // Only when the question actually changed. A forced refresh of the
  // same filters keeps its numbers on screen while it revalidates; a new
  // filter drops them, because the previous filter's count sitting under
  // a new search reads as the new search's count.
  if (!held) latestScoped = null;
  renderScopeDependent();

  try {
    const data = await getJSON(`/stats?${qs({ ...currentFilterParams(), skills: "" })}`, { signal: inFlight.signal });
    if (seq !== scopedStatsSeq) return;
    const scoped = data && data.scoped;
    // No `scoped` key is a normal answer, not a broken one: it is what
    // the unfiltered route returns and what a stale artifact or a
    // pre-deploy API looks like. Fall back to whole-board numbers, under
    // whole-board labels, and let the scope line say what happened.
    latestScoped =
      scoped && typeof scoped.open_jobs === "number"
        ? { params, data: scopeShape(scoped) }
        : { params, data: null, degraded: true };
    renderScopeDependent();
  } catch (err) {
    if (seq !== scopedStatsSeq || err.name === "AbortError") return;
    // One failed sidebar refresh does not earn an error banner. Same
    // fallback as a payload with no `scoped` key.
    latestScoped = { params, data: null, degraded: true };
    renderScopeDependent();
  } finally {
    if (seq === scopedStatsSeq) {
      scopedStatsPending = null;
      scopedStatsInFlight = null;
    }
  }
}

// Everything in the sidebar that has to agree about the board's current
// filters, repainted together so the tiles, Top hirers and the scope
// line can never disagree about which question they are answering.
function renderScopeDependent() {
  if (!latestStats) {
    // No stats in hand yet, so the markup's own skeletons stay up. The
    // scope line still paints: it is built from state, not from a
    // response, and it is the one thing that can be right immediately.
    renderScopeLine();
    renderDetailEmpty();
    return;
  }
  renderScopedMetrics(latestStats); // paints the scope line with them
  renderScopedPanels(latestStats);
  renderDetailEmpty();
}

// /api/health is a tiny, cheap endpoint built for exactly this: a
// freshness ping without hauling the whole /stats payload over again.
// Polled far more often than the 2-min full stats refresh so the "X AGO"
// text and the sync countdown don't sit on data up to a full 2-min-poll
// -plus-2-min-CDN-cache stale (reported live: the countdown was landing
// a few minutes off). Doesn't touch latestStats or re-render panels --
// setLastCheckedAt's monotonic guard means this can only ever pull the
// countdown's anchor forward, never regress it against a fresher value
// the last /stats poll already saw.
// Was 30s, which predates the 5-minute write cycle: it asked the same
// question ten times per available answer, and each miss past
// CloudFront's window was a Lambda invocation. 120s still sees every
// cycle twice.
const HEALTH_POLL_MS = 120_000;

async function refreshFreshness() {
  try {
    const health = await getJSON("/health");
    setLastCheckedAt(health.last_checked);
    tickApiStatus();
  } catch {
    // Non-fatal: the next /stats or /health poll will catch up.
  }
}

// Reported live: the "next sync" line just said "syncing" once the
// countdown hit zero, with no way to tell whether anything was actually
// happening versus a missed/failed cycle. Read from /api/pipeline-status
// (api/handler.py's route_pipeline_status), which now reports BOTH
// halves of the pipeline separately -- {scrape: {...}, merge: {...}} --
// since they run on genuinely different schedules (fast-poll/workday
// every 5-10 min, the merge every 5). pipelineActivityText() above reads
// .merge: see its own comment for why the scrape half, active almost
// continuously, wouldn't actually answer "when does the site next
// update."
let pipelinePhase = null;

async function refreshPipelineStatus() {
  try {
    pipelinePhase = await getJSON("/pipeline-status");
    tickApiStatus();
  } catch {
    // Non-fatal: the activity line simply stays empty when this is null.
  }
}

// Auth lives in auth.js now, loaded before this file, so the landing
// and contact pages can offer sign-in too. This page says where a
// problem is shown.
setAuthErrorSink((msg) => showAuthError(msg));
setAuthRenderSink(() => {
  renderAuthState();
  // Signing out clears the stored skills, but the board is already
  // running with them in memory and on screen. Reported live: log out
  // on Best matches and the view stayed, still labelled "matching your
  // CV", until something forced a reload.
  //
  // Best matches with nobody signed in has nothing to rank against, so
  // the board falls back to the view a reader arriving fresh would get.
  if (!getAuthTokens() && (state.sort === "match" || state.skills.length)) {
    state.skills = [];
    setView(state.roles === "all" ? "all" : "tech");
  }
});

// The sign-in dialog registers its own sink while it is open, so this
// is the fallback for an error raised with no dialog on screen: a token
// refresh failing in the background, for instance. There is no element
// to write into then, and the console is the honest place for it.
function showAuthError(msg) {
  const el = document.getElementById("auth-error");
  if (!el) {
    console.error("Sign-in:", msg);
    return;
  }
  el.textContent = msg;
  el.hidden = false;
}

function renderAuthState() {
  const area = document.getElementById("auth-area");
  // Pages other than the board load this file for authedFetch and the
  // token helpers but have no topbar auth slot to paint into. The
  // account page cannot simply grow one either: this panel carries the
  // alert form's ids, and that page already has them.
  if (!area) return;
  const tokens = getAuthTokens();
  if (!tokens) {
    // One Sign in button opening the dialog every other page opens.
    // This used to be a panel hanging under the button: the same four
    // controls in a different shape and a different place, which is two
    // implementations of one screen.
    area.innerHTML = '<button class="auth-trigger" id="auth-trigger" type="button">Sign in</button>';
    document.getElementById("auth-trigger").addEventListener("click", () => window.openSignIn());
    return;
  }
  const email = decodeJwtEmail(tokens.id_token) || "signed in";
  // An icon, not the address. The topbar is the one place on the site a
  // reader's own email was on screen permanently, including over a
  // shoulder and in any screenshot they take of the board. The address
  // is still there for anyone who wants it, as the link's title and its
  // accessible name, which is also where a screen reader reads it.
  //
  // A link to /account rather than a button that opened the alerts panel
  // here. That panel was a second, smaller copy of what /account already
  // shows: account.html carries the same alert ids on purpose, so
  // renderAlertsList and wireAlertCreateForm drive both, and it has its
  // own sign-out. Clicking your own account now goes to your account.
  // Their Google photo when they signed in that way, the first letter
  // of the address otherwise. avatarHtml in auth.js decides which.
  const avatar = avatarHtml(email, tokens.id_token);
  // The alerts panel is the board's. A page that carries the alert ids
  // itself asks for the menu alone, because two elements with one id put
  // the second one out of reach: getElementById returns the first in the
  // document, and the header comes before the page. See account.html.
  const menuOnly = area.hasAttribute("data-menu-only");
  const alertsPanel = menuOnly ? "" : `
    <div class="auth-panel alerts-panel" id="auth-panel" hidden>
      <div class="alerts-header alerts-header-row">
        <span>My Alerts</span>
        <a class="link account-link" href="/account">Account</a>
      </div>
      <div id="alerts-list"><p class="alerts-empty">Loading…</p></div>

      <div class="alert-create" id="alert-create">
        <div class="alerts-header" id="alert-form-title">New Alert</div>
        <div class="alert-create-fields">
          <input type="text" id="alert-f-search" placeholder="SEARCH" />
          <div class="ms" id="alert-ms-department"></div>
          <div class="ms" id="alert-ms-seniority"></div>
          <div class="ms" id="alert-ms-company"></div>
          <div class="ms" id="alert-ms-location"></div>
          <div class="ms" id="alert-ms-workplace"></div>
        </div>
        <div class="alert-form-actions">
          <button class="btn" id="create-alert-btn" type="button">Create Alert</button>
          <button class="btn btn-quiet" id="cancel-edit-btn" type="button" hidden>Cancel</button>
        </div>
        <p class="create-alert-feedback" id="create-alert-feedback" hidden></p>
      </div>

      <button class="auth-signout" id="auth-panel-signout" type="button">Sign Out</button>
    </div>`;
  area.innerHTML = `
    <button class="hero-account-btn" id="topbar-account-btn" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="topbar-menu">
      ${avatar}My Account
    </button>
    <div class="hero-menu" id="topbar-menu" role="menu" hidden>
      <div class="hero-menu-head">${avatar}<span class="hero-menu-email" title="${escapeHtml(email)}">${escapeHtml(email)}</span></div>
      <a role="menuitem" href="/account">${MENU_ICONS.person}My Profile</a>
      <a role="menuitem" href="/board?starred=1">${MENU_ICONS.bookmark}Saved Jobs</a>
      <button role="menuitem" type="button" id="topbar-alert-btn">${MENU_ICONS.bell}Alerts</button>
      <a role="menuitem" href="/stats">${MENU_ICONS.chart}Statistics</a>
      <a role="menuitem" href="/api/help">${MENU_ICONS.code}API reference</a>
      <a role="menuitem" href="/contact">${MENU_ICONS.chat}Contact</a>
      <button role="menuitem" type="button" id="topbar-menu-theme">${isDarkTheme() ? MENU_ICONS.sun : MENU_ICONS.moon}<span>${isDarkTheme() ? "Light mode" : "Dark mode"}</span></button>
      <button role="menuitem" type="button" class="hero-menu-out" id="auth-signout">${MENU_ICONS.out}Log Out</button>
    </div>
    ${alertsPanel}`;
  wireMenu("topbar-account-btn", "topbar-menu");
  wireTopbarAlertButton(menuOnly);
  // Repaints its own row rather than re-rendering the menu, which would
  // shut it on the click that opened the change.
  const themeRow = document.getElementById("topbar-menu-theme");
  if (themeRow) {
    themeRow.addEventListener("click", () => {
      toggleTheme().then(() => {
        themeRow.innerHTML = `${isDarkTheme() ? MENU_ICONS.sun : MENU_ICONS.moon}<span>${isDarkTheme() ? "Light mode" : "Dark mode"}</span>`;
      });
    });
  }
  document.getElementById("auth-signout").addEventListener("click", signOut);
  const panelOut = document.getElementById("auth-panel-signout");
  if (panelOut) panelOut.addEventListener("click", signOut);
  // The account page wires its own form and fetches its own list, and
  // doing it here as well was a second /me/alerts on every load.
  if (menuOnly) return;
  wireAlertCreateForm();
  loadMyAlerts().then(renderAlertsList);
}

// The one menu the topbar holds, on the board and on every page that
// carries the nav. Everything except the theme toggle and the source
// link lives under it, so the bar is three things wide however many
// features grow behind it.
const MENU_ICONS = {
  person: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><circle cx="8" cy="5" r="3" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M2.5 14c.6-3 2.7-4.5 5.5-4.5s4.9 1.5 5.5 4.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  bookmark: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M4 2h8v12l-4-3-4 3z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
  chart: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M2 13.5h12M4 13V8M7.3 13V4.5M10.6 13V9.5M13.9 13V6.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  bell: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M4 11V7a4 4 0 0 1 8 0v4l1 1.5H3z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M6.5 14a1.5 1.5 0 0 0 3 0" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>',
  code: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M5.5 4.5L2 8l3.5 3.5M10.5 4.5L14 8l-3.5 3.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  chat: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M2.5 3h11v8h-6l-3 2.5V11h-2z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
  sun: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><circle cx="8" cy="8" r="3.2" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 1v1.6M8 13.4V15M1 8h1.6M13.4 8H15M3 3l1.1 1.1M11.9 11.9L13 13M13 3l-1.1 1.1M4.1 11.9L3 13" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  moon: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M13.5 9.5A5.6 5.6 0 0 1 6.5 2.5a5.6 5.6 0 1 0 7 7z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
  out: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M6 2.5H3v11h3M10 5l3 3-3 3M13 8H6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

// Opens and closes one menu, and closes it on a click outside or Escape.
function wireMenu(btnId, menuId) {
  const btn = document.getElementById(btnId);
  const menu = document.getElementById(menuId);
  if (!btn || !menu) return;
  const open = (on) => { menu.hidden = !on; btn.setAttribute("aria-expanded", String(on)); };
  btn.addEventListener("click", (e) => { e.stopPropagation(); open(menu.hidden); });
  document.addEventListener("click", (e) => { if (!menu.contains(e.target) && e.target !== btn) open(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") open(false); });
}

// The only way into the alerts panel now that the account icon is a link
// to /account. It carries its own active state rather than borrowing the
// account control's, which is no longer a button and no longer toggles
// anything. Toggles rather than only opening, so the button that showed
// the panel can also put it away. Rewired on every renderAuthState()
// re-render like the rest of this panel's internals, since sign-in/out
// replaces the whole subtree.
function wireTopbarAlertButton(menuOnly) {
  const btn = document.getElementById("topbar-alert-btn");
  if (!btn) return;
  // With no panel to open, the row is a link to the page's own section.
  if (menuOnly) {
    btn.addEventListener("click", () => { location.hash = "#alerts"; });
    return;
  }
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const panel = document.getElementById("auth-panel");
    panel.hidden = !panel.hidden;
    btn.classList.toggle("active", !panel.hidden);
    // It lives in the account menu now: the menu steps out of the way
    // of the panel it just opened.
    const menu = document.getElementById("topbar-menu");
    if (menu) {
      menu.hidden = true;
      document.getElementById("topbar-account-btn").setAttribute("aria-expanded", "false");
    }
  });
}

function wireAuth() {
  renderAuthState();

  // Once, not inside wireAuthPanel (which reruns on every sign-in-state
  // re-render and was stacking up a fresh document-level listener each
  // time, each one closing over that render's now-detached panel/trigger
  // elements -- harmless-looking but wasteful). Looks up the current
  // panel live rather than closing over a specific render's elements.
  //
  // mousedown, not click: reported live -- drag-selecting the pasted OTP
  // code inside the input closed the panel mid-selection. A selection
  // drag's mouseup (and the click it generates) can land outside the
  // input if the drag overshoots, so a click-based outside-check treats
  // a normal text selection as a dismiss. mousedown fires on press,
  // before any drag happens, so it isn't fooled by where the drag ends.
  // Both controls are looked up optionally: auth-trigger only exists
  // signed out now (signed in, the account control is a plain link), and
  // topbar-alert-btn only exists signed in, so on any given render one of
  // the two is absent. Reaching for .classList on the missing one would
  // throw here, on a document-level listener, every time the panel was
  // dismissed.
  document.addEventListener("mousedown", (e) => {
    const panel = document.getElementById("auth-panel");
    if (panel && !panel.hidden && !e.target.closest("#auth-area")) {
      panel.hidden = true;
      document.getElementById("auth-trigger")?.classList.remove("active");
      document.getElementById("topbar-alert-btn")?.classList.remove("active");
    }
  });
}

// /me/alerts: the one authenticated area of the app. authedFetch()
// refreshes the id_token first if it's about to expire (Cognito's
// public, unsigned REFRESH_TOKEN_AUTH -- same pattern as the other
// direct cognitoRequest() calls), so a signed-in tab doesn't silently
// start failing an hour in.

async function ensureFreshTokens() {
  const tokens = getAuthTokens();
  if (!tokens) return null;
  const payload = JSON.parse(atob(tokens.id_token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
  if (payload.exp * 1000 - Date.now() > 60_000) return tokens; // still good for at least another minute
  try {
    const result = await cognitoRequest("InitiateAuth", {
      ClientId: COGNITO_CLIENT_ID,
      AuthFlow: "REFRESH_TOKEN_AUTH",
      AuthParameters: { REFRESH_TOKEN: tokens.refresh_token },
    });
    const t = result.AuthenticationResult;
    const refreshed = { id_token: t.IdToken, access_token: t.AccessToken, refresh_token: tokens.refresh_token };
    setAuthTokens(refreshed);
    return refreshed;
  } catch (err) {
    // A refresh Cognito actively refused is a dead session and the
    // reader has to sign in again. A timeout or a dropped connection is
    // not, and signing someone out because their train went into a
    // tunnel throws away a session that was still good. Fail this one
    // request instead and leave the tokens where they are.
    // TimeoutError is what AbortSignal.timeout aborts with; AbortError
    // is the AbortController fallback; TypeError is fetch's own network
    // failure.
    const transient = err && ["TimeoutError", "AbortError", "TypeError"].includes(err.name);
    if (transient) {
      throw new Error("Could not reach the sign-in service");
    }
    signOut();
    return null;
  }
}

async function authedFetch(path, options = {}) {
  const tokens = await ensureFreshTokens();
  if (!tokens) throw new Error("Signed out");
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    // Same reason as the refresh above. 20s is far longer than any of
    // these routes takes (they are one DynamoDB query) and still finite.
    signal: options.signal || abortAfter(20000),
    headers: { "Content-Type": "application/json", ...(options.headers || {}), Authorization: `Bearer ${tokens.id_token}` },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

async function loadMyAlerts() {
  try {
    const data = await authedFetch("/me/alerts");
    return data.alerts || [];
  } catch {
    return [];
  }
}

// Short, readable summary of a saved filter -- not every possible key,
// just the ones a user is likely to have actually set.
// A country code as the facets name it. The board never ships a country
// list of its own: the codes it shows came from /api/facets, and this
// remembers those labels so a saved alert reads "Israel" rather than
// "IL". Falls back to the code, which is what an alert saved against a
// country the facet no longer lists will show.
const COUNTRY_LABELS_SEEN = new Map();

// The browser's own region names cover a code the facets never listed
// this visit, such as Israel on a link that also asks for Remote.
let REGION_NAMES = null;
try {
  REGION_NAMES = new Intl.DisplayNames(["en"], { type: "region" });
} catch { /* old browser: the code itself is the fallback */ }

function countryLabel(code) {
  if (COUNTRY_LABELS_SEEN.has(code)) return COUNTRY_LABELS_SEEN.get(code);
  try {
    const name = REGION_NAMES && REGION_NAMES.of(code);
    if (name && name !== code) return name;
  } catch { /* not a region code */ }
  return code;
}

// Trimmed, because a saved value like "Tel Aviv, Israel" splits into a
// second part with a leading space and rendered as a double one.
function csv(value) {
  return String(value).split(",").map((v) => v.trim()).filter(Boolean);
}

function describeAlertFilter(filter) {
  const parts = [];
  if (filter.search || filter.q) parts.push(`"${filter.search || filter.q}"`);
  if (filter.keywords) parts.push(filter.keywords.split(";").map((t) => t.trim()).filter(Boolean).join(", "));
  if (filter.department) parts.push(csv(filter.department).join(", "));
  if (filter.seniority) parts.push(csv(filter.seniority).join(", "));
  if (filter.company) parts.push(csv(filter.company).join(", "));
  if (filter.location) parts.push(csv(filter.location).join(", "));
  if (filter.workplace) parts.push(csv(filter.workplace).join(", "));
  // Place, as the form now saves it. A code is shown by the name the
  // facets gave it, so a saved alert reads Israel rather than IL.
  if (filter.country) parts.push(csv(filter.country).map(countryLabel).join(", "));
  if (filter.city) parts.push(csv(filter.city).join(", "));
  // And as it used to. These alerts still match, because the API still
  // answers both keys, so they are still described.
  if (filter.israel_only === "1") parts.push("Israel only");
  return parts.length ? parts.join(" · ") : "All jobs";
}

function renderAlertsList(alerts) {
  const container = document.getElementById("alerts-list");
  if (!container) return; // signed out (or panel re-rendered) before this resolved
  myAlerts = alerts;
  // An alert deleted from under an open edit leaves nothing to save to.
  if (editingAlertId && !alerts.some((a) => a.alert_id === editingAlertId)) resetAlertForm();
  if (!alerts.length) {
    container.innerHTML = `<p class="alerts-empty">No alerts yet. Pick some filters below and create one.</p>`;
    return;
  }
  container.innerHTML = alerts
    .map(
      (a) => `
      <div class="alert-row ${a.active ? "" : "paused"} ${a.alert_id === editingAlertId ? "editing" : ""}">
        <button type="button" class="alert-summary" data-id="${a.alert_id}" title="Edit this alert">${escapeHtml(describeAlertFilter(a.filter))}</button>
        <span class="alert-actions">
          <button class="alert-toggle" data-id="${a.alert_id}" data-active="${a.active}">${a.active ? "Pause" : "Resume"}</button>
          <button class="alert-delete" data-id="${a.alert_id}" aria-label="Delete alert" title="Delete alert">✕</button>
        </span>
      </div>`
    )
    .join("");

  container.querySelectorAll(".alert-summary").forEach((btn) => {
    btn.addEventListener("click", () => startEditAlert(btn.dataset.id));
  });
  container.querySelectorAll(".alert-toggle").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await authedFetch(`/me/alerts/${btn.dataset.id}`, {
          method: "PATCH",
          body: JSON.stringify({ active: btn.dataset.active !== "true" }),
        });
        renderAlertsList(await loadMyAlerts());
      } catch {
        btn.disabled = false;
      }
    });
  });
  container.querySelectorAll(".alert-delete").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await authedFetch(`/me/alerts/${btn.dataset.id}`, { method: "DELETE" });
        renderAlertsList(await loadMyAlerts());
      } catch {
        btn.disabled = false;
      }
    });
  });
}

function showCreateAlertFeedback(msg, isError) {
  const el = document.getElementById("create-alert-feedback");
  if (!el) return; // panel got torn down (sign out) while a request was in flight
  el.textContent = msg;
  el.classList.toggle("error", !!isError);
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 3000);
}

// New-alert form, lives inside the My Alerts panel with its own filter
// pickers -- deliberately independent of `state` (the board's live
// filters). Reported live: tying "+ Alert" to whatever the board
// happened to be showing meant you had to first go set up the board
// exactly right, create the alert, then undo it to keep browsing.
// Wired fresh each time the signed-in panel renders (sign in/out
// recreates the underlying DOM), same as wireAuthPanel().
let alertMsDepartment, alertMsSeniority, alertMsCompany, alertMsLocation, alertMsWorkplace;

const alertFormState = {
  // Same single field as the board's box, so an alert matches what you
  // were looking at when you saved it. Alerts saved before this still
  // carry q and the API still answers it.
  search: "",
  department: [],
  seniority: [],
  company: [],
  country: [],
  city: [],
  workplace: [],
};

// The alert being edited, or null for "create a new one". The form below
// is the same set of controls either way, so this is the only thing that
// distinguishes the two modes.
let editingAlertId = null;

// The reader's workplace and places from /account#preferences, read once
// when the form is wired. A new alert starts from them, and they can be
// changed in the form like anything else. Null until read, or signed
// out, or with nothing set.
let alertPrefs = null;

async function profilePrefs() {
  if (!getAuthTokens()) return null;
  try {
    const data = await authedFetch("/me/profile");
    const p = (data && data.profile) || {};
    const prefs = { workplace: p.workplace || [], country: p.country || [], city: p.city || [] };
    return prefs.workplace.length || prefs.country.length || prefs.city.length ? prefs : null;
  } catch {
    return null;
  }
}

// Only onto a blank form: the reader may have started filling it in
// while the profile was still on its way, and the options for places
// arrive on their own schedule (setOptions drops any selection it does
// not know), so this runs from each of those arrivals and from every
// reset, and does nothing when there is already something in the form.
function applyAlertPrefsIfBlank() {
  if (!alertPrefs || !alertMsWorkplace || editingAlertId) return;
  if (alertFormState.workplace.length || alertFormState.country.length || alertFormState.city.length) return;
  alertFormState.workplace = [...alertPrefs.workplace];
  alertFormState.country = [...alertPrefs.country];
  alertFormState.city = [...alertPrefs.city];
  alertMsWorkplace.setSelected(alertFormState.workplace);
  alertMsLocation.setSelected(alertFormState.country, alertFormState.city);
}

// The last list we rendered, so clicking a row can reload that alert's
// filter without asking the API for it again.
let myAlerts = [];

// Turns a saved filter back into the form's own state shape. The stored
// form is what /api/jobs takes, where the multi-value fields are
// comma-joined strings.
function fillAlertForm(filter) {
  const list = (v) => (v ? String(v).split(",").filter(Boolean) : []);
  alertFormState.search = filter.search || filter.q || "";
  alertFormState.department = list(filter.department);
  alertFormState.seniority = list(filter.seniority);
  alertFormState.company = list(filter.company);
  alertFormState.country = list(filter.country);
  alertFormState.city = list(filter.city);
  // An alert saved before the board had countries. israel_only maps
  // exactly onto country=IL now that the two return the same listings,
  // so it is translated rather than dropped; the old raw-string location
  // is not, because its values were never canonical names and any
  // mapping would be a guess. Editing such an alert loses only the part
  // this form can no longer express.
  if (filter.israel_only === "1" && !alertFormState.country.includes("IL")) {
    alertFormState.country = ["IL", ...alertFormState.country];
  }
  alertFormState.workplace = list(filter.workplace);

  document.getElementById("alert-f-search").value = alertFormState.search;
  alertMsDepartment.setSelected(alertFormState.department);
  alertMsSeniority.setSelected(alertFormState.seniority);
  alertMsCompany.setSelected(alertFormState.company);
  alertMsLocation.setSelected(alertFormState.country, alertFormState.city);
  alertMsWorkplace.setSelected(alertFormState.workplace);
}

// Both modes paint from here, so the heading, the button and the Cancel
// affordance can never disagree about which one we are in.
function paintAlertFormMode() {
  const title = document.getElementById("alert-form-title");
  const submit = document.getElementById("create-alert-btn");
  const cancel = document.getElementById("cancel-edit-btn");
  if (!title || !submit || !cancel) return;
  title.textContent = editingAlertId ? "Edit Alert" : "New Alert";
  submit.textContent = editingAlertId ? "Save Changes" : "Create Alert";
  cancel.hidden = !editingAlertId;
  document.getElementById("alert-create").classList.toggle("editing", !!editingAlertId);
}

function startEditAlert(alertId) {
  const alert = myAlerts.find((a) => a.alert_id === alertId);
  if (!alert) return;
  editingAlertId = alertId;
  fillAlertForm(alert.filter || {});
  paintAlertFormMode();
  renderAlertsList(myAlerts);
  document.getElementById("alert-create").scrollIntoView({ block: "nearest" });
}

function cancelEditAlert() {
  resetAlertForm();
  renderAlertsList(myAlerts);
}

function resetAlertForm() {
  alertFormState.search = "";
  alertFormState.department = [];
  alertFormState.seniority = [];
  alertFormState.company = [];
  alertFormState.country = [];
  alertFormState.city = [];
  alertFormState.workplace = [];
  document.getElementById("alert-f-search").value = "";
  alertMsDepartment.reset();
  alertMsSeniority.reset();
  alertMsCompany.reset();
  alertMsLocation.reset();
  alertMsWorkplace.reset();
  editingAlertId = null;
  paintAlertFormMode();
  applyAlertPrefsIfBlank();
}

// Deliberately global, not board-scoped: an alert is a standing filter
// for FUTURE postings, not a snapshot of whatever the board's own
// filters happen to show right now, so this reads latestStats'
// unscoped top_departments/top_locations (same ones the Market Stats
// panel uses) and its own globalCompanyOptions, never
// latestCompanyOptions/msDepartment/msLocation's board-scoped values.
function populateAlertFilterOptions() {
  if (!alertMsDepartment) return; // signed out, or panel not built yet
  // Not while someone is editing. setOptions drops any selection missing
  // from the new list and reports it through onChange, which would
  // rewrite the form under the user mid-edit. These lists barely move in
  // the two minutes between polls, and the next save repaints anyway.
  if (editingAlertId) return;
  if (latestStats) {
    alertMsDepartment.setOptions(
      latestStats.top_departments.map((r) => ({ value: r.department, label: `${r.department} (${r.n})` }))
    );
  }
  // Locations come from the facets tree, not latestStats.top_locations.
  // Those are raw strings, which is what the board stopped offering:
  // "Tel Aviv", "Tel Aviv-Yafo, Tel Aviv, ISR" and "tel-aviv" were three
  // choices for one place, and none of them named a country.
  getStaticFacets("verified")
    .then((facets) => {
      alertMsLocation.setOptions(normalizeLocationFacets(facets.locations));
      applyAlertPrefsIfBlank();
    })
    .catch(() => {});
  loadGlobalCompanyOptions().then((opts) => {
    if (opts) alertMsCompany.setOptions(opts);
  });
}

function wireAlertCreateForm() {
  // Sign-out tears the panel down and sign-in builds a fresh one, but
  // editingAlertId lives above both. Left set, the rebuilt form would
  // read "New Alert" while still holding a pointer to someone's
  // existing alert, and Create would quietly overwrite it.
  editingAlertId = null;

  document.getElementById("alert-f-search").addEventListener("input", (e) => {
    alertFormState.search = e.target.value.trim();
  });

  alertMsDepartment = createMultiSelect("alert-ms-department", {
    placeholder: "Categories",
    onChange: (values) => { alertFormState.department = values; },
  });
  alertMsSeniority = createMultiSelect("alert-ms-seniority", {
    placeholder: "Levels",
    options: Object.entries(SENIORITY_LABELS).map(([value, label]) => ({ value, label })),
    onChange: (values) => { alertFormState.seniority = values; },
  });
  alertMsCompany = createMultiSelect("alert-ms-company", {
    placeholder: "Companies",
    searchable: true,
    onChange: (values) => { alertFormState.company = values; },
  });
  alertMsLocation = createLocationSelect("alert-ms-location", {
    placeholder: "Locations",
    searchable: true,
    onChange: ({ countries, cities }) => {
      alertFormState.country = countries;
      alertFormState.city = cities;
    },
  });
  alertMsWorkplace = createMultiSelect("alert-ms-workplace", {
    placeholder: "Workplace",
    options: Object.entries(WORKPLACE_LABELS).map(([value, label]) => ({ value, label })),
    onChange: (values) => { alertFormState.workplace = values; },
  });
  populateAlertFilterOptions();
  profilePrefs().then((prefs) => {
    alertPrefs = prefs;
    applyAlertPrefsIfBlank();
  });

  document.getElementById("cancel-edit-btn").addEventListener("click", cancelEditAlert);
  paintAlertFormMode();

  document.getElementById("create-alert-btn").addEventListener("click", async (e) => {
    // Localized to the button, not a page-level spinner: the user
    // clicked one control and that control is what should look busy.
    // .btn-busy hides the label with color:transparent rather than
    // replacing it, so the button keeps its exact width mid-request.
    const btn = e.currentTarget;
    btn.classList.add("btn-busy");
    const raw = {
      search: alertFormState.search,
      department: alertFormState.department.join(","),
      seniority: alertFormState.seniority.join(","),
      company: alertFormState.company.join(","),
      country: alertFormState.country.join(","),
      city: alertFormState.city.join(","),
      workplace: alertFormState.workplace.join(","),
    };
    const filter = Object.fromEntries(Object.entries(raw).filter(([, v]) => v !== "" && v != null));
    // Read once: the request is awaited below, and a click on another row
    // in the meantime would otherwise redirect the save.
    const editing = editingAlertId;
    try {
      if (editing) {
        await authedFetch(`/me/alerts/${editing}`, { method: "PATCH", body: JSON.stringify({ filter }) });
      } else {
        await authedFetch("/me/alerts", { method: "POST", body: JSON.stringify({ filter }) });
      }
      showCreateAlertFeedback(editing ? "Alert saved." : "Alert created.");
      resetAlertForm();
      renderAlertsList(await loadMyAlerts());
    } catch (err) {
      const fallback = editing ? "Could not save alert." : "Could not create alert.";
      showCreateAlertFeedback(err.message || fallback, true);
    } finally {
      // The panel is torn down and rebuilt on sign-out, so this button
      // can be gone by the time the request settles.
      btn.classList.remove("btn-busy");
    }
  });
}

// The scrape-fast Lambda (EventBridge, not scrape-fast.yml -- see its own
// header comment) re-polls every 5 min; 2 min keeps an open tab
// reasonably current without hammering the API, and lines up with
// CloudFront's own 120s cache on /api/* so most polls never even reach
// the Lambda.
const STATS_POLL_MS = 120_000;

// The Statistics column folds away to the right (see .stats-toggle in
// style.css). Remembered per browser; the head script in index.html
// applies a saved collapse before first paint.
const STATS_COLLAPSED_KEY = "iljobs_stats_collapsed";

// Asked once, ever. Its own key rather than anything derived from the
// filters: someone who accepts the suggestion and later clears their
// location is not asking to be asked again, and a flag that read the
// filter state would do exactly that every time the board emptied.
const GEO_ASKED_KEY = "iljobs_geo_asked";

// Offers the visitor's own country as a filter, and never mentions it
// again. Resolves true only when the visitor accepted, which is the
// caller's signal to re-query; every other outcome, including skip, is
// false and leaves the board showing what it already drew.
//
// This used to gate the first load. It doesn't any more: boot() renders
// the global board behind the prompt, because holding the listings until
// a human reads a dialog left first-time visitors looking at an empty
// table for three to five seconds. The old ordering was worth its cost
// when the default view meant a Lambda round trip and answering after
// the fact would have fetched the whole board twice. bootstrap.json made
// that first view a 4KB static file, so the second fetch is now the
// cheap half and the wait was the expensive one.
//
// Silent in three cases, all of which mean the question is not worth
// asking: it has been asked before, the visitor already has a country
// filter (from a link or a previous visit, so they have said where they
// want to look), or /api/geo declines to guess. That last one answers
// null rather than guessing when Cloudflare sends XX or T1, and the
// right response to "we do not know" is to say nothing at all.
async function maybeAskCountry() {
  try {
    if (localStorage.getItem(GEO_ASKED_KEY)) return false;
  } catch {
    return false; // private browsing: no way to remember the answer, so never ask
  }
  if (state.country.length) return false;

  let country = null;
  let source = null;
  try {
    const geo = await fetch("/api/geo").then((r) => (r.ok ? r.json() : null));
    country = geo && geo.country;
    source = geo && geo.source;
  } catch {
    return false; // offline or the endpoint is down; the board is what matters
  }
  if (!country) return false;

  // The country's name, fetched here rather than inherited. Labels live
  // in COUNTRY_LABELS_SEEN, which normalizeLocationFacets fills from
  // /facets.json, and nothing guarantees that has happened yet: the call
  // that does it sits inside refreshStats' try block behind three
  // render functions, so one bad stats payload skips it silently and
  // this prompt would render a bare ISO code. Awaiting that call instead
  // would put a fetch on every visitor's boot to serve a prompt only
  // first-timers ever see. So it is asked for here, once, by the only
  // code that needs it.
  let where = countryLabel(country);
  if (where === country) {
    try {
      const r = await fetch("/facets.json", { cache: "no-store" });
      if (r.ok) {
        const byConfidence = await r.json();
        const facets = byConfidence && (byConfidence[state.confidence || "verified"] || byConfidence.verified);
        if (facets && Array.isArray(facets.locations)) normalizeLocationFacets(facets.locations);
        where = countryLabel(country);
      }
    } catch {
      // Falls through to the check below, which says nothing at all.
    }
  }
  // Still no name for it. Saying "Are you in IL?" is worse than staying
  // quiet, the same reasoning route_geo uses when it answers null rather
  // than guessing at XX or T1.
  if (where === country) return false;

  const remember = () => {
    try { localStorage.setItem(GEO_ASKED_KEY, "1"); } catch {}
  };

  return await new Promise((resolve) => {
    const scrim = document.createElement("div");
    scrim.className = "geo-scrim";
    const box = document.createElement("div");
    box.className = "geo-prompt";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-labelledby", "geo-prompt-title");
    box.innerHTML = `
      <h2 id="geo-prompt-title">Are you in ${escapeHtml(where)}?</h2>
      <p>To improve user experience we detect the client's location via AWS CloudFront to tailor your interface accordingly. If you do not wish to share your location please press skip, which will show the global job board.</p>
      <div class="geo-evidence">
        country <b>${escapeHtml(country)}</b><br />
        read from <b>${escapeHtml(source || "unknown")}</b>
      </div>
      <div class="geo-actions">
        <button class="btn" id="geo-accept" type="button">Show ${escapeHtml(where)} jobs</button>
        <button class="btn btn-quiet" id="geo-skip" type="button">Skip</button>
      </div>`;

    // chose: the visitor picked their country, so the caller re-queries.
    // Skip, Escape and the scrim all answer false and change nothing.
    const close = (chose = false) => {
      remember();
      scrim.remove();
      box.remove();
      document.removeEventListener("keydown", onKey);
      resolve(chose);
    };
    // Escape and the scrim both mean skip. A prompt with no way out
    // other than answering it is a dialog nobody thanks you for.
    const onKey = (e) => { if (e.key === "Escape") close(); };

    box.querySelector("#geo-accept").addEventListener("click", () => {
      state.country = [country];
      // The control has to agree with the state, or the filter is on
      // with nothing on screen saying so.
      applyStateToFilterUI();
      saveFiltersToStorage();
      close(true);
    });
    // Wrapped, not passed directly: an event listener calls its handler
    // with the event, and close() reads its first argument as the
    // visitor's answer. Handing it a MouseEvent made Skip look like a
    // choice and re-ran the query it is supposed to avoid.
    box.querySelector("#geo-skip").addEventListener("click", () => close());
    scrim.addEventListener("click", () => close());
    document.addEventListener("keydown", onKey);

    document.body.append(scrim, box);
    box.querySelector("#geo-accept").focus();
  });
}

function wireStatsToggle() {
  const btn = document.getElementById("stats-toggle");
  if (!btn) return;
  const paint = () => {
    const collapsed = document.documentElement.classList.contains("stats-collapsed");
    btn.setAttribute("aria-expanded", String(!collapsed));
    btn.title = collapsed ? "Show statistics" : "Hide statistics";
    btn.setAttribute("aria-label", btn.title);
  };
  paint();
  btn.addEventListener("click", () => {
    const collapsed = document.documentElement.classList.toggle("stats-collapsed");
    try { localStorage.setItem(STATS_COLLAPSED_KEY, collapsed ? "1" : "0"); } catch {}
    paint();
  });
}

async function boot() {
  // Pages other than the board load this file for its auth helpers and
  // createMultiSelect, and have none of the board's markup. Everything
  // below assumes #jobs-body and the filter controls exist, so stop here
  // rather than throwing through a dozen null lookups. handleAuthRedirect
  // still runs: a sign-in can land on any page.
  await handleAuthRedirect();
  if (!document.getElementById("jobs-body")) {
    wireAuth();
    // The theme control is on every page that loads this file and only
    // the board was ever wiring it, because the call sits below this
    // return. Found live on /account: the button was there, said Dark,
    // and did nothing. The board is the only other page here, so this
    // reaches exactly one more button.
    if (document.getElementById("theme-toggle")) wireThemeToggle();
    return;
  } // before wireAuth: a fresh token from a redirect must be in localStorage before the initial render; also before applyStateFromUrl below, since a code-exchange redirect strips the URL down to location.pathname first

  // localStorage first, as the new baseline, THEN the URL on top -- an
  // explicit query param always overrides a saved filter, never the
  // other way around (see applyStoredFilters' own comment). Both need to
  // run before wireFilters() creates the actual controls -- state.israel_only
  // (read at creation time by ms-location's pinned "Israel (only)"
  // checkbox) needs to already be right by then. applyStateToFilterUI()
  // below handles the rest (the multi-selects/#f-search/the view switch),
  // which all need wireFilters() to have already assigned msDepartment
  // etc. first.
  // The address as the visitor typed it, read once before anything can
  // rewrite it. loadJobs() below runs syncUrl(), which rebuilds the URL
  // from state, and ?job= and ?view= are not state until they have been
  // acted on. Reading location.search after that found them already gone,
  // so a deep link to a job opened nothing.
  const bootParams = new URLSearchParams(location.search);
  applyStoredFilters();
  applyStateFromUrl(location.search);
  // Skills saved from an earlier visit bring the saved sort back with
  // them, and that was usually Newest. Best matches then came back in
  // plain date order, listings with no matching skill mixed in, looking
  // exactly like Show all. Reported live from a screenshot. Only a
  // link that names its own sort keeps it.
  if (state.skills.length && !state.starred_only && !bootParams.has("sort")) state.sort = "match";

  wireAuth();
  // Not awaited. The board renders from localStorage the moment it can,
  // signed in or out, and the merged-in stars from other devices flip
  // themselves on whenever /me/saved gets back.
  syncSavedFromServer();
  wireFilters();
  applyStateToFilterUI();
  wireJobDetail();
  renderDetailEmpty();
  wireThemeToggle();
  wireStatsToggle();

  // Both started together, and neither waits for the other. The prompt
  // renders over a board that is already filling in rather than over an
  // empty table, and answering it re-queries. Only an accepted country
  // re-queries: skip leaves the global view that is already on screen,
  // which is the same board the visitor would have got anyway.
  //
  // The re-query replaces the rows wholesale rather than merging, which
  // is what loadJobs does anyway. It also cancels the global request
  // still in flight, so choosing Israel costs one aborted fetch and not
  // a second full render.
  const asked = maybeAskCountry();
  loadJobs();
  asked.then((chose) => { if (chose) loadJobs(); });

  // Behind the listings, both of them, because neither is what anybody
  // opened the board for.
  //
  // The comment that used to sit above refreshStats already knew this:
  // "measured over 50 cold visits, /stats.json cost the first
  // /api/jobs request 232ms at p90 purely by being ahead of it in the
  // queue". It was left in front anyway on the grounds that it is not
  // awaited, which is the same mistake the rail's counts made. Not
  // awaited is not free: /stats.json is 777KB and on a hard reload it
  // competes for the same bandwidth as the rows, on a page that now
  // reads four small numbers out of it.
  refreshStats();
  // /health at boot, not only on the two-minute refresh. refreshStats
  // seeds lastCheckedAt from /stats.json, whose freshness block is
  // frozen at the moment that file was built, so until the first health
  // poll landed the board reported the artifact's age as its own. It is
  // the cheapest endpoint on the box and it is the only live answer.
  refreshFreshness();

  // ?view=matches opens Best matches directly, for the 404 page and any
  // other link that wants it. Not filter state: setView works out the
  // skills, and the next syncUrl drops the param.
  if (bootParams.get("view") === "matches") setView("matches");

  // A deep link to one specific job (see jobPermalink) opens after the
  // above, not folded into applyStateToFilterUI -- it's not a filter
  // control, and openJobDetail needs the DOM/state from everything above
  // to already be in place. loadJobs() just ran its own syncUrl() with
  // no job selected yet, so this needs its own explicit re-sync after.
  const deepLinkJobId = bootParams.get("job");
  if (deepLinkJobId) {
    await openJobDetail(deepLinkJobId);
    syncUrl();
  }

  // Back/Forward: the browser already changed the URL, so this only
  // ever reads it back into state/UI -- never pushes or replaces itself
  // (that would corrupt the very history entry the user just navigated
  // to). loadJobs() at the end still does its own replaceState, but
  // that's just re-serializing the same state to the same canonical
  // form, a no-op in practice.
  window.addEventListener("popstate", () => {
    applyStateFromUrl(location.search);
    applyStateToFilterUI();
    const jobId = new URLSearchParams(location.search).get("job");
    if (jobId) {
      if (jobId !== selectedJobId) openJobDetail(jobId);
    } else if (selectedJobId) {
      closeJobDetail();
    }
    loadJobs();
  });

  // Every poller below goes through this. A tab nobody is looking at
  // still ran all three of them, forever, and a backgrounded tab left
  // open overnight was quietly the largest single source of API Lambda
  // invocations on the whole system. document.hidden costs one property
  // read and gives back everything that tab was spending.
  //
  // The catch-up on becoming visible is the part that keeps this from
  // being a downgrade: you come back to the tab and it refreshes, rather
  // than showing you whatever was on screen when you left.
  function whenVisible(fn) {
    return () => {
      if (!document.hidden) fn();
    };
  }

  // The board's data refreshes as one: listings, statistics and the
  // status line. The return from a hidden tab used to refresh only
  // the statistics, so for up to two minutes fresh numbers sat beside an
  // old list. loadJobs() still skips the re-render when nothing changed,
  // and keeps the reader's place when something did (holdForReader).
  //
  // One timer, restarted by every refresh, rather than an interval: a
  // return refresh is then never followed seconds later by a scheduled
  // one, and two refreshes never overlap.
  let boardTimer = 0;
  let boardRefreshing = false;
  let hiddenAt = null;

  async function refreshBoard() {
    // The next one is scheduled from the start of this one, not its end:
    // a request that never settles must not stop the polling for good.
    scheduleBoardRefresh();
    if (boardRefreshing) return;
    boardRefreshing = true;
    try {
      await Promise.allSettled([
        loadJobs({ background: true }),
        refreshStats(),
        refreshFreshness(),
        refreshPipelineStatus(),
      ]);
    } finally {
      boardRefreshing = false;
    }
  }

  function scheduleBoardRefresh() {
    clearTimeout(boardTimer);
    // A hidden tab stops here; the return below starts it again.
    if (document.hidden) return;
    boardTimer = setTimeout(refreshBoard, STATS_POLL_MS);
  }

  // Background tabs get throttled or frozen, so nothing is assumed to have
  // run while hidden. Coming back is the moment to catch up. A glance away
  // of under RETURN_REFRESH_MS refreshes nothing and keeps the schedule.
  const RETURN_REFRESH_MS = 30_000;
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      hiddenAt = Date.now();
      clearTimeout(boardTimer);
      return;
    }
    const away = hiddenAt === null ? 0 : Date.now() - hiddenAt;
    hiddenAt = null;
    if (away > RETURN_REFRESH_MS) refreshBoard();
    else scheduleBoardRefresh();
  });

  document.getElementById("new-listings").addEventListener("click", () => showHeldJobs());

  // The ways out of a search that found nothing, and back out of a
  // broadened one. Each sets the filter and reloads, so the URL, the
  // saved filters and the stats panel all follow as they do for any
  // other filter change.
  function setSearch({ search, mode }) {
    if (search !== undefined) {
      state.search = search;
      setSearchBox(search);
    }
    if (mode !== undefined) state.search_mode = mode;
    state.offset = 0;
    loadJobs();
    refreshStats();
  }
  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-search-any], [data-search-all], [data-clear-search], [data-drop-term]");
    if (!el) return;
    if (el.hasAttribute("data-search-any")) setSearch({ mode: "any" });
    else if (el.hasAttribute("data-search-all")) setSearch({ mode: "" });
    else if (el.hasAttribute("data-clear-search")) setSearch({ search: "", mode: "" });
    else {
      const drop = el.getAttribute("data-drop-term").toLowerCase();
      const kept = searchTermsInPlay().filter((t) => t !== drop)
        .map((t) => (t.includes(" ") ? `"${t}"` : t)).join(" ");
      setSearch({ search: kept });
    }
  });
  // Scrolling back to the top of the list shows held rows without a click.
  window.addEventListener("scroll", () => {
    if (heldJobs && jobsListTop() >= topbarBottom()) showHeldJobs({ scroll: false });
  }, { passive: true });

  scheduleBoardRefresh();
  // The countdown tick stays unconditional: it reads no network, it only
  // recomputes a number already in memory.
  setInterval(tickApiStatus, API_STATUS_TICK_MS);
  setInterval(whenVisible(refreshFreshness), HEALTH_POLL_MS);
  refreshPipelineStatus();
  setInterval(whenVisible(refreshPipelineStatus), HEALTH_POLL_MS);
}

boot();
