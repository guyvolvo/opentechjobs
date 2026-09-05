// OpenMarketIL frontend. No framework, no build step, served straight
// from S3/CloudFront, runs as-shipped. Talks to the API at /api/*,
// same-origin (CloudFront routes /api/* to the Lambda).
//
// Starring a listing is localStorage-only: the API has no write
// endpoints or accounts, so there's no server side to hang that state off.

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
  q: "",
  keywords: "", // ';'-separated, ALL must appear (AND, not OR)
  department: [], // labeled "Category" in the UI; backend field stays "department"
  seniority: [],
  company: [], // multi-select; also set via clicking a company in the market panels
  location: [], // curated top raw location strings, not a geocoded facet
  workplace: [], // remote|hybrid|onsite
  confidence: "all", // no confidence filter in the UI; shown inline via badge instead
  israel_only: true,
  starred_only: false,
  sort: "age",
  dir: "asc", // newest first by default
  offset: 0,
};

// Assigned once in wireFilters(); referenced by loadFilterOptions() and
// the market panels' "click a company" handler.
let msDepartment, msSeniority, msCompany, msLocation, msWorkplace;

// The job detail panel's current selection. Not part of `state` above
// since it's ephemeral UI, never an API param. Survives filter changes
// (doesn't get yanked closed), just loses its row highlight if that job
// scrolls off the current page.
let selectedJobId = null;

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

// fetch helpers

async function getJSON(path) {
  const res = await fetch(`${API_BASE}${path}`);
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
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="#40513b"/><text x="32" y="33" font-family="Helvetica Neue, Helvetica, Arial, sans-serif" font-size="30" font-weight="700" fill="#f3f6e4" text-anchor="middle" dominant-baseline="central">${letter}</text></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

// Reported live: Overwolf's real favicon.ico is a genuine 16x16 (verified
// via a direct curl, not assumed) with no apple-touch-icon anywhere on
// the site either -- Google's service was never the bug there, it was
// already showing the best real source, just upscaled. Four tiers, each
// one a same-origin/no-key image request or a local fallback, cheapest
// and most honest first:
//   1. apple-touch-icon.png -- the high-res convention, when it exists.
//   2. the domain's own favicon.ico, fetched directly (not through a
//      resizing proxy) specifically so naturalWidth reflects the real
//      file -- a genuinely tiny one (<=32px) skips straight to the
//      monogram instead of rendering blurry, rather than pretending an
//      upscale is a real logo.
//   3. Google's favicon service -- catches the case a site's icon isn't
//      at a guessable path at all (declared via a <link> tag instead);
//      that's the one failure mode a direct path guess can't solve.
//   4. the generated monogram, unconditionally available.
function companyLogoImg(domain, size, extraClass = "") {
  const touchIcon = escapeHtml(`https://${domain}/apple-touch-icon.png`);
  const directFavicon = escapeHtml(`https://${domain}/favicon.ico`);
  const googleFavicon = escapeHtml(companyLogoUrl(domain, size));
  const monogram = escapeHtml(monogramLogoSvg(domain));
  const cls = extraClass ? `company-logo ${extraClass}` : "company-logo";
  return `<img class="${cls}" src="${touchIcon}" alt="" loading="lazy"
    data-stage="1" data-direct-favicon="${directFavicon}" data-google-favicon="${googleFavicon}" data-monogram="${monogram}"
    onerror="if(this.dataset.stage==='1'){this.dataset.stage='2';this.src=this.dataset.directFavicon;}else if(this.dataset.stage==='2'){this.dataset.stage='3';this.src=this.dataset.googleFavicon;}else{this.onerror=null;this.src=this.dataset.monogram;}"
    onload="if(this.dataset.stage==='2'&&this.naturalWidth&&this.naturalWidth<=32){this.onerror=null;this.src=this.dataset.monogram;}" />`;
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

function fmtMinutesAgo(mins) {
  if (mins === null || mins === undefined) return "UNKNOWN";
  if (mins < 60) return `${Math.round(mins)}M AGO`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)}H AGO`;
  return `${Math.round(mins / (60 * 24))}D AGO`;
}

function escapeHtml(s) {
  return (s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

// metrics dashboard

// Set on every renderMetrics() call, read by tickApiStatus() so the "X
// AGO" text can keep counting up client-side between the 2-min stats
// polls instead of sitting frozen at whatever the last poll said.
let lastCheckedAt = null;

// Shared by the topbar status dot/text and the API Status card: past
// this, both flip from LIVE (green) to OFFLINE (red) together.
const FRESH_THRESHOLD_MINUTES = 16;

// EventBridge fires the scrape-fast Lambda on a flat rate(10 minutes)
// schedule (infra/scrape_lambda.tf), and each run's start time is what
// gets written to companies.last_checked -- so lastCheckedAt + 10m is a
// close read on when the next run should land. Not a guarantee: a run
// that takes longer than usual, or a rare failed invocation, pushes the
// real next update later than this estimate says.
const SYNC_INTERVAL_MINUTES = 10;

function apiStatusFields() {
  const minutesSince = lastCheckedAt === null ? null : (Date.now() - lastCheckedAt) / 60000;
  const fresh = (minutesSince ?? 9999) <= FRESH_THRESHOLD_MINUTES;
  return {
    fresh,
    value: fresh ? "LIVE" : fmtMinutesAgo(minutesSince),
    sub: fresh ? fmtMinutesAgo(minutesSince) : "no recent updates",
  };
}

function nextSyncText() {
  if (lastCheckedAt === null) return null;
  const remainingMs = lastCheckedAt + SYNC_INTERVAL_MINUTES * 60_000 - Date.now();
  if (remainingMs <= 0) return "next sync any moment";
  const totalSeconds = Math.floor(remainingMs / 1000);
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `next sync in ${m}:${String(s).padStart(2, "0")}`;
}

function tickApiStatus() {
  const card = document.getElementById("metric-api-status");
  if (!card) return;
  const { fresh, value, sub } = apiStatusFields();
  card.classList.toggle("highlight", fresh);
  card.querySelector(".value").textContent = value;
  card.querySelector(".sub").textContent = sub;
  const syncEl = card.querySelector(".sync-countdown");
  if (syncEl) syncEl.textContent = nextSyncText() ?? "";
  document.getElementById("status-dot").classList.toggle("offline", !fresh);
  document.getElementById("status-text").textContent = fresh ? "LIVE" : "OFFLINE";
}

function renderMetrics(stats) {
  const el = document.getElementById("metrics-grid");
  lastCheckedAt = stats.freshness.last_checked ? new Date(stats.freshness.last_checked).getTime() : null;
  const { fresh } = apiStatusFields();

  const cards = [
    {
      label: "Open Jobs",
      value: fmtInt(stats.totals.open_jobs),
      sub: `${fmtInt(stats.meta.open_jobs_best_effort)} more unverified`,
      hl: true,
    },
    {
      label: "Companies Hiring",
      value: fmtInt(stats.totals.companies_hiring),
      sub: "with a fresh open role",
    },
    {
      label: "New Listings in 7d",
      value: `+${fmtInt(stats.throughput.new_jobs_24h)}`,
      sub: `${fmtInt(stats.throughput.new_jobs_7d)} in 7d`,
      hl: stats.throughput.new_jobs_24h > 0,
    },
    {
      label: "Closed / Filled",
      value: `-${fmtInt(stats.throughput.closed_jobs_24h)}`,
      sub: `${fmtInt(stats.throughput.closed_jobs_7d)} in 7d`,
    },
    {
      label: "Median Open Age",
      value: fmtAge(stats.age.median_open_days),
      sub: `oldest ${fmtAge(stats.age.oldest_open_days)}`,
    },
    {
      id: "metric-api-status",
      label: "API Status",
      value: apiStatusFields().value,
      sub: apiStatusFields().sub,
      sub2: nextSyncText(),
      hl: fresh,
    },
  ];

  el.innerHTML = cards
    .map(
      (c) => `
      <div class="metric-card ${c.hl ? "highlight" : ""}" ${c.id ? `id="${c.id}"` : ""}>
        <div class="label">${c.label}</div>
        <div>
          <div class="value">${c.value}</div>
          <div class="sub">${c.sub}</div>
          ${c.sub2 ? `<div class="sync-countdown">${c.sub2}</div>` : ""}
        </div>
      </div>`
    )
    .join("");

  document.getElementById("status-dot").classList.toggle("offline", !fresh);
  document.getElementById("status-text").textContent = fresh ? "LIVE" : "OFFLINE";
}

// Recomputes from lastCheckedAt every 1s -- the sync countdown needs a
// real per-second tick to read as "live"; the AGO text along for the
// ride is a no-op most seconds, negligible cost either way.
const API_STATUS_TICK_MS = 1_000;

// Market-insight panels: who's hiring, what for, where. No ATS-vendor
// breakdown here; that's plumbing, not a market signal (still available
// as open_jobs_by_ats for anyone polling the raw API).
function renderBarList(rows, nameKey, { clickable = false } = {}) {
  const max = Math.max(1, ...rows.map((r) => r.n));
  return rows
    .map((r) => {
      const name = escapeHtml(r[nameKey]);
      return `
      <div class="bar-row ${clickable ? "clickable" : ""}" ${clickable ? `data-company="${name}"` : ""}>
        <div class="name">${clickable ? companyLogoImg(r[nameKey], 16) : ""}${name}</div>
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

function renderPanels(stats) {
  const el = document.getElementById("panel-grid");

  el.innerHTML = `
    <div class="panel">
      <div class="panel-title">New Listings, Last 14 Days</div>
      ${renderTrendChart(stats.daily_new_jobs)}
    </div>
    <div class="panel">
      <div class="panel-title">Open Jobs Over Time</div>
      ${renderOpenJobsChart(stats.open_jobs_history)}
    </div>
    <div class="panel">
      <div class="panel-title">Top Hiring Companies</div>
      ${renderBarList(stats.top_companies, "domain", { clickable: true })}
    </div>
    <div class="panel">
      <div class="panel-title">Fastest Growing (New Reqs, 7D)</div>
      ${
        stats.top_movers_7d.length
          ? renderBarList(stats.top_movers_7d, "domain", { clickable: true })
          : '<div class="sub" style="color:var(--grey)">Nothing new in the last 7 days.</div>'
      }
    </div>
    <div class="panel">
      <div class="panel-title">Top Categories</div>
      ${renderBarList(stats.top_departments, "department")}
    </div>
    <div class="panel">
      <div class="panel-title">Seniority (of Postings That State One)</div>
      ${renderBarList(
        stats.seniority_breakdown.map((r) => ({ seniority: SENIORITY_LABELS[r.seniority] || r.seniority, n: r.n })),
        "seniority"
      )}
    </div>
    <div class="panel">
      <div class="panel-title">Dormant Listings</div>
      ${renderGhostStat(stats.ghost, stats.totals.open_jobs)}
    </div>`;

  el.querySelectorAll("[data-company]").forEach((row) => {
    row.addEventListener("click", () => {
      state.company = [row.dataset.company];
      state.starred_only = false;
      state.offset = 0;
      document.getElementById("f-starred").checked = false;
      msCompany.setSelected(state.company);
      loadJobs();
      loadTicker();
      window.scrollTo({ top: document.getElementById("board").offsetTop - 60, behavior: "smooth" });
    });
  });
}

// job board

let lastJobsResponse = null;

function renderCompanyChip() {
  const chip = document.getElementById("company-chip");
  if (state.company.length === 0) {
    chip.style.display = "none";
    return;
  }
  chip.style.display = "inline-flex";
  chip.querySelector(".chip-label").textContent =
    state.company.length === 1 ? state.company[0] : `${state.company.length} companies`;
}

// The filter (not sort/pagination) portion of state. Shared by loadJobs
// and the ticker, so "10 most recent" respects the active filters too.
function currentFilterParams() {
  return {
    q: state.q,
    keywords: state.keywords,
    department: state.department.join(","),
    seniority: state.seniority.join(","),
    company: state.company.join(","),
    location: state.location.join(","),
    workplace: state.workplace.join(","),
    confidence: state.confidence,
    israel_only: state.israel_only ? "1" : "",
  };
}

// Stale-while-revalidate for the job list: keyed by the exact
// filter/sort/page combo, so a revisit with the same view renders
// instantly from whatever was cached last time instead of sitting on a
// loading screen, while the real fetch still always runs in the
// background and silently replaces it the moment fresh data lands.
// Reported live: every visit meant a few seconds of "Loading
// listings..." even though the underlying data rarely changes that
// fast (scrape-fast.yml re-polls every 10 min, most visits land well
// inside that window).
const JOBS_CACHE_PREFIX = "iljobs_jobs_cache:";

function getCachedJobs(params) {
  try {
    const raw = localStorage.getItem(JOBS_CACHE_PREFIX + params);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function setCachedJobs(params, data) {
  try {
    localStorage.setItem(JOBS_CACHE_PREFIX + params, JSON.stringify(data));
  } catch {
    // Full quota or unavailable (private browsing) -- this is a pure UX
    // nicety, silently skip rather than break the real fetch over it.
  }
}

async function loadJobs() {
  const tbody = document.getElementById("jobs-body");
  const starred = getStarred();
  renderCompanyChip();

  if (state.starred_only) {
    renderStarredOnly(starred);
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

  const cached = getCachedJobs(params);
  tbody.closest("table").style.display = "";
  if (cached) {
    document.getElementById("jobs-loading").style.display = "none";
    lastJobsResponse = cached;
    renderJobs(cached, starred);
    renderPagination(cached);
  } else {
    document.getElementById("jobs-loading").style.display = "block";
  }

  try {
    const data = await getJSON(`/jobs?${params}`);
    document.getElementById("jobs-loading").style.display = "none";
    // Skip the re-render when the background refresh just confirms
    // nothing changed -- avoids a jarring flicker/scroll-reset for what
    // will be the common case (revisiting within the same 10-min window).
    const changed = !cached || JSON.stringify(data) !== JSON.stringify(cached);
    lastJobsResponse = data;
    if (changed) {
      renderJobs(data, starred);
      renderPagination(data);
    }
    setCachedJobs(params, data);
  } catch (err) {
    document.getElementById("jobs-loading").style.display = "none";
    // A cached render is still on screen and still useful -- don't bury
    // it under an error banner over a transient fetch failure.
    if (!cached) {
      const errEl = document.getElementById("jobs-error");
      errEl.textContent = `Could not load jobs: ${err.message}`;
      errEl.style.display = "block";
    }
  }
}

function renderStarredOnly(starred) {
  document.getElementById("jobs-loading").style.display = "none";
  document.getElementById("jobs-error").style.display = "none";
  const rows = lastJobsResponse?.jobs?.filter((j) => starred.has(j.id)) || [];
  if (!rows.length) {
    document.getElementById("jobs-empty").textContent = "No starred jobs.";
    document.getElementById("jobs-empty").style.display = "block";
    document.getElementById("jobs-body").innerHTML = "";
    document.getElementById("result-count").innerHTML = "";
    return;
  }
  document.getElementById("jobs-empty").style.display = "none";
  renderJobRows(rows, starred);
  document.getElementById("result-count").innerHTML = `<b>${rows.length}</b> starred`;
  document.getElementById("pagination").style.display = "none";
}

function renderJobs(data, starred) {
  document.getElementById("pagination").style.display = "flex";
  if (!data.jobs.length) {
    document.getElementById("jobs-empty").style.display = "block";
    document.getElementById("jobs-body").innerHTML = "";
    document.getElementById("result-count").innerHTML = "";
    return;
  }
  document.getElementById("jobs-empty").style.display = "none";
  renderJobRows(data.jobs, starred);
  const from = state.offset + 1;
  const to = Math.min(state.offset + data.jobs.length, data.total);
  document.getElementById("result-count").innerHTML =
    `<b>${from}–${to}</b> of <b>${fmtInt(data.total)}</b> open listings`;
}

// "Company · Department · Location (Workplace)" -- one scannable line
// under the title, LinkedIn-card style, standing in for what used to be
// three separate table columns (Company was already folded in as the
// row's ".company" div; Location and Category had their own <td>s).
function jobMetaLine(j) {
  const parts = [escapeHtml(j.company_domain)];
  if (j.department) parts.push(escapeHtml(j.department));
  if (j.location) parts.push(escapeHtml(j.location));
  let line = parts.join(" · ");
  if (j.workplace_type) line += ` (${escapeHtml(WORKPLACE_LABELS[j.workplace_type] || j.workplace_type)})`;
  return line;
}

// Real disclosed comp (currently Ashby only) shown plainly; an estimate
// (probe.py's _estimate_salary, Israeli role x seniority market data,
// not this listing's own figure) prefixed "Est." and never styled with
// the same weight as a real number -- see .job-salary.estimate. Neither
// present just reads "Undisclosed," matching this board's own principle
// of showing an absence as an absence, not hiding it.
function jobSalaryHtml(j) {
  if (!j.salary_text) return `<span class="job-salary undisclosed">Undisclosed</span>`;
  const cls = j.salary_is_estimate ? "job-salary estimate" : "job-salary";
  const prefix = j.salary_is_estimate ? "Est. " : "";
  return `<span class="${cls}">${prefix}${escapeHtml(j.salary_text)}</span>`;
}

// Plain text, no chip/badge container (confirmed live) -- each one
// still a real button, clicking sets the board's existing `keywords`
// filter (the same AND-match param /api/jobs already supports) to that
// one term and reloads, rather than adding a second, parallel filter
// mechanism just for this.
function jobSkillsHtml(j) {
  const skills = (j.skills || "").split(",").filter(Boolean);
  if (!skills.length) return "";
  const chips = skills
    .map((s) => `<button class="skill-chip" data-skill="${escapeHtml(s)}" type="button">${escapeHtml(s)}</button>`)
    .join("");
  return `<span class="skill-bracket">[</span>${chips}<span class="skill-bracket">]</span>`;
}

function renderJobRows(jobs, starred) {
  document.getElementById("jobs-body").innerHTML = jobs
    .map((j) => {
      const age = j.posted_at
        ? (Date.now() - new Date(j.posted_at).getTime()) / 86400000
        : null;
      const fresh = age !== null && age <= 3;
      const isStarred = starred.has(j.id);
      return `
      <tr data-id="${j.id}" class="${j.id === selectedJobId ? "selected" : ""}">
        <td>
          <button class="star-btn ${isStarred ? "on" : ""}" data-star="${j.id}" title="Star (saved in this browser only)">
            ${isStarred ? "★" : "☆"}
          </button>
        </td>
        <td class="title-cell">
          ${companyLogoImg(j.company_domain, 64, "listing")}
          <div class="job-card-body">
            <div class="job-card-title">
              <a href="${escapeHtml(j.url || "#")}" target="_blank" rel="noopener">${escapeHtml(j.title)}</a>
              ${j.seniority ? `<span class="badge seniority">${escapeHtml(SENIORITY_LABELS[j.seniority] || j.seniority)}</span>` : ""}
              ${j.confidence === "best_effort" ? '<span class="badge best-effort" title="Scraped from the company\'s own page, not a live ATS API">best_effort</span>' : ""}
            </div>
            <div class="job-meta">${jobMetaLine(j)}</div>
            <div class="job-links">
              <a class="apply-link" href="${escapeHtml(j.url || "#")}" target="_blank" rel="noopener" title="Open the original listing to apply">Apply ↗</a>
              <button class="copy-link-btn" data-copy-url="${escapeHtml(j.url || "")}" title="Copy the application link">Save link</button>
            </div>
          </div>
          <div class="job-salary-col">${jobSalaryHtml(j)}</div>
          <!-- Skills chips pulled from the UI for now, per request, while
               salary gets more attention -- jobSkillsHtml/.skill-chip and
               its click-to-filter wiring are still intact below, just
               unused, so this is a one-line change to bring back. -->
        </td>
        <td data-label="Age" class="age-cell ${fresh ? "fresh" : ""}">${fmtAge(age)}</td>
      </tr>`;
    })
    .join("");

  document.querySelectorAll("[data-star]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation(); // inside a now-clickable <tr> (opens the detail panel); starring shouldn't also open it
      const s = toggleStar(btn.dataset.star);
      btn.classList.toggle("on", s.has(btn.dataset.star));
      btn.textContent = s.has(btn.dataset.star) ? "★" : "☆";
      syncDetailStarButton(btn.dataset.star, s);
      if (state.starred_only) loadJobs();
    });
  });

  document.querySelectorAll("[data-copy-url]").forEach((btn) => {
    btn.addEventListener("click", () => copyToClipboard(btn, btn.dataset.copyUrl));
  });

  document.querySelectorAll("[data-skill]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation(); // same reasoning as the star button above
      state.keywords = btn.dataset.skill;
      state.offset = 0;
      document.getElementById("f-keywords").value = state.keywords;
      loadJobs();
      loadTicker();
      window.scrollTo({ top: document.getElementById("board").offsetTop - 60, behavior: "smooth" });
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
  const original = btn.textContent;
  btn.textContent = "Copied";
  btn.classList.add("copied");
  setTimeout(() => {
    btn.textContent = original;
    btn.classList.remove("copied");
  }, 1200);
}

function renderPagination(data) {
  const el = document.getElementById("pagination-pages");
  const totalPages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
  const current = Math.floor(state.offset / PAGE_SIZE) + 1;
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
  const on = starredSet.has(id);
  btn.classList.toggle("on", on);
  btn.textContent = on ? "★ Saved" : "☆ Save";
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

function renderJobDetailBody(job, { descriptionLoading = false, descriptionError = null } = {}) {
  const age = job.posted_at ? (Date.now() - new Date(job.posted_at).getTime()) / 86400000 : null;
  const starred = getStarred().has(job.id);

  let descriptionHtml;
  if (descriptionError) {
    descriptionHtml = `<div class="error-state">Could not load the full description: ${escapeHtml(descriptionError)}</div>`;
  } else if (descriptionLoading) {
    descriptionHtml = `<div class="loading-state">Loading description…</div>`;
  } else if (job.description) {
    descriptionHtml = `<div class="job-detail-description">${renderDescriptionLines(job.description)}</div>`;
  } else {
    descriptionHtml = `<div class="job-detail-description empty">No description provided by this listing.</div>`;
  }

  return `
    <div class="job-detail-header">
      <div>
        <div class="job-detail-company">${companyLogoImg(job.company_domain, 64, "detail")}${escapeHtml(job.company_domain)}</div>
        <h3 class="job-detail-title">${escapeHtml(job.title)}</h3>
        <div class="job-detail-badges">
          ${job.seniority ? `<span class="badge seniority">${escapeHtml(SENIORITY_LABELS[job.seniority] || job.seniority)}</span>` : ""}
          ${job.workplace_type ? `<span class="badge workplace">${escapeHtml(WORKPLACE_LABELS[job.workplace_type] || job.workplace_type)}</span>` : ""}
          ${job.confidence === "best_effort" ? '<span class="badge best-effort" title="Scraped from the company\'s own page, not a live ATS API">best_effort</span>' : ""}
          ${job.closed_at ? '<span class="badge">Closed</span>' : ""}
        </div>
      </div>
      <button type="button" class="job-detail-close" title="Close" aria-label="Close job detail">✕</button>
    </div>

    <div class="job-detail-actions">
      <a class="job-detail-apply" href="${escapeHtml(job.url || "#")}" target="_blank" rel="noopener" title="Open the original listing to apply">Apply ↗</a>
      <button type="button" class="job-detail-star ${starred ? "on" : ""}" data-star="${job.id}">${starred ? "★ Saved" : "☆ Save"}</button>
      <button type="button" class="copy-link-btn" data-copy-url="${escapeHtml(job.url || "")}" title="Copy the application link">Save link</button>
    </div>

    <div class="job-detail-meta">
      <div class="job-detail-meta-row"><span class="label">Location</span><span class="value">${escapeHtml(job.location || "-")}</span></div>
      <div class="job-detail-meta-row"><span class="label">Category</span><span class="value">${escapeHtml(job.department || "-")}</span></div>
      <div class="job-detail-meta-row"><span class="label">Seniority</span><span class="value">${escapeHtml(SENIORITY_LABELS[job.seniority] || job.seniority || "-")}</span></div>
      <div class="job-detail-meta-row"><span class="label">Workplace</span><span class="value">${escapeHtml(WORKPLACE_LABELS[job.workplace_type] || job.workplace_type || "-")}</span></div>
      <div class="job-detail-meta-row"><span class="label">Posted</span><span class="value">${age !== null ? `${fmtAge(age)} ago` : "-"}</span></div>
      <div class="job-detail-meta-row"><span class="label">Via</span><span class="value">${escapeHtml(job.ats || "-")}</span></div>
    </div>

    <div class="job-detail-description-title">Description</div>
    ${descriptionHtml}`;
}

function wireJobDetailPanel(job) {
  const panel = document.getElementById("job-detail");
  panel.querySelector(".job-detail-close").addEventListener("click", closeJobDetail);
  panel.querySelector(".job-detail-star").addEventListener("click", (e) => {
    const s = toggleStar(job.id);
    const on = s.has(job.id);
    e.currentTarget.classList.toggle("on", on);
    e.currentTarget.textContent = on ? "★ Saved" : "☆ Save";
    const rowBtn = document.querySelector(`[data-star="${job.id}"].star-btn`);
    if (rowBtn) {
      rowBtn.classList.toggle("on", on);
      rowBtn.textContent = on ? "★" : "☆";
    }
    if (state.starred_only) loadJobs();
  });
  const copyBtn = panel.querySelector("[data-copy-url]");
  if (copyBtn) copyBtn.addEventListener("click", () => copyToClipboard(copyBtn, copyBtn.dataset.copyUrl));
}

async function openJobDetail(id) {
  const panel = document.getElementById("job-detail");
  const known = findKnownJob(id);
  if (!known) return; // row's own data is the minimum we need to render anything at all

  const previousId = selectedJobId;
  selectedJobId = id;
  document.querySelector(`tr[data-id="${previousId}"]`)?.classList.remove("selected");
  document.querySelector(`tr[data-id="${id}"]`)?.classList.add("selected");

  panel.hidden = false;
  panel.innerHTML = renderJobDetailBody(known, { descriptionLoading: true });
  wireJobDetailPanel(known);

  // On the stacked layout (<=1300px, see style.css) the panel renders
  // below the *entire* list, not beside it, invisible without this. On
  // the desktop side-by-side layout it's already sticky-positioned into
  // view, so skip the scroll there rather than yank the page around.
  // -60 clears the sticky topbar, same offset renderPanels()'s company
  // click and renderPagination()'s page-button handlers already use.
  const rect = panel.getBoundingClientRect();
  if (rect.top < 0 || rect.top > window.innerHeight * 0.8) {
    window.scrollTo({ top: window.scrollY + rect.top - 60, behavior: "smooth" });
  }

  try {
    const full = await getJSON(`/jobs/${encodeURIComponent(id)}`);
    if (selectedJobId !== id) return; // a different row was picked while this was in flight
    panel.innerHTML = renderJobDetailBody(full);
    wireJobDetailPanel(full);
  } catch (err) {
    if (selectedJobId !== id) return;
    panel.innerHTML = renderJobDetailBody(known, { descriptionError: err.message });
    wireJobDetailPanel(known);
  }
}

function closeJobDetail() {
  const panel = document.getElementById("job-detail");
  panel.hidden = true;
  panel.innerHTML = "";
  document.querySelector(`tr[data-id="${selectedJobId}"]`)?.classList.remove("selected");
  selectedJobId = null;
}

function wireJobDetail() {
  document.getElementById("jobs-body").addEventListener("click", (e) => {
    if (e.target.closest("a, button")) return; // Apply/Save link/star handle their own click
    const row = e.target.closest("tr[data-id]");
    if (row) openJobDetail(row.dataset.id);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && selectedJobId !== null) closeJobDetail();
  });
}

// multi-select filter dropdown (Category / Level / Company)

// One open dropdown at a time. Opening a second one closes whichever
// was already open, same as a native <select> would behave.
const OPEN_MULTISELECTS = new Set();

function createMultiSelect(containerId, { placeholder, options = [], searchable = false, onChange, pinnedOption = null }) {
  const container = document.getElementById(containerId);
  const selected = new Set();
  let currentOptions = options;

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

  function renderOptions(filterText = "") {
    const q = filterText.trim().toLowerCase();
    const visible = q ? currentOptions.filter((o) => o.label.toLowerCase().includes(q)) : currentOptions;
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
      const opt = currentOptions.find((o) => o.value === [...selected][0]);
      toggle.textContent = opt ? opt.label : [...selected][0];
    } else {
      toggle.textContent = `${selected.size} SELECTED`;
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

  if (searchEl) searchEl.addEventListener("input", () => renderOptions(searchEl.value));

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
      // Location narrowing to IL-only, dropping a non-IL pick).
      selected.clear();
      current.filter((v) => opts.some((o) => o.value === v)).forEach((v) => selected.add(v));
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

document.addEventListener("click", () => OPEN_MULTISELECTS.forEach((closeOther) => closeOther()));

// filter wiring

function wireFilters() {
  document.getElementById("f-q").addEventListener(
    "input",
    debounce((e) => {
      state.q = e.target.value.trim();
      state.offset = 0;
      loadJobs();
      loadTicker();
    }, 300)
  );

  document.getElementById("f-keywords").addEventListener(
    "input",
    debounce((e) => {
      state.keywords = e.target.value.trim();
      state.offset = 0;
      loadJobs();
      loadTicker();
    }, 300)
  );

  msDepartment = createMultiSelect("ms-department", {
    placeholder: "CATEGORIES",
    onChange: (values) => {
      state.department = values;
      state.offset = 0;
      loadJobs();
      loadTicker();
    },
  });

  msSeniority = createMultiSelect("ms-seniority", {
    placeholder: "LEVELS",
    options: Object.entries(SENIORITY_LABELS).map(([value, label]) => ({ value, label })),
    onChange: (values) => {
      state.seniority = values;
      state.offset = 0;
      loadJobs();
      loadTicker();
    },
  });

  msCompany = createMultiSelect("ms-company", {
    placeholder: "COMPANIES",
    searchable: true,
    onChange: (values) => {
      state.company = values;
      state.offset = 0;
      loadJobs();
      loadTicker();
    },
  });

  msLocation = createMultiSelect("ms-location", {
    placeholder: "LOCATIONS",
    searchable: true,
    pinnedOption: {
      id: "f-israel",
      label: "IL Only",
      checked: state.israel_only,
      onChange: (checked) => {
        state.israel_only = checked;
        state.offset = 0;
        refreshLocationOptions();
        loadJobs();
        loadTicker();
      },
    },
    onChange: (values) => {
      state.location = values;
      state.offset = 0;
      loadJobs();
      loadTicker();
    },
  });

  msWorkplace = createMultiSelect("ms-workplace", {
    placeholder: "WORKPLACE",
    options: Object.entries(WORKPLACE_LABELS).map(([value, label]) => ({ value, label })),
    onChange: (values) => {
      state.workplace = values;
      state.offset = 0;
      loadJobs();
      loadTicker();
    },
  });

  document.getElementById("f-starred").addEventListener("change", (e) => {
    state.starred_only = e.target.checked;
    loadJobs();
    // Not loadTicker(): "starred" is a client-local view, not an API filter.
  });

  document.getElementById("f-reset").addEventListener("click", () => {
    state.q = "";
    state.keywords = "";
    state.department = [];
    state.seniority = [];
    state.company = [];
    state.location = [];
    state.workplace = [];
    state.israel_only = true;
    state.starred_only = false;
    state.sort = "age";
    state.dir = "asc";
    state.offset = 0;
    document.getElementById("f-q").value = "";
    document.getElementById("f-keywords").value = "";
    msDepartment.reset();
    msSeniority.reset();
    msCompany.reset();
    msLocation.reset();
    msWorkplace.reset();
    document.getElementById("f-israel").checked = true;
    document.getElementById("f-starred").checked = false;
    setActiveSortHeader("age", "asc");
    loadJobs();
    loadTicker();
  });

  document.getElementById("company-chip").addEventListener("click", () => {
    state.company = [];
    msCompany.reset();
    state.offset = 0;
    loadJobs();
    loadTicker();
  });

  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      const isSameColumn = state.sort === key;
      const dir = isSameColumn && state.dir === "desc" ? "asc" : "desc";
      setActiveSortHeader(key, dir);
      state.offset = 0;
      loadJobs();
    });
  });
}

function setActiveSortHeader(key, dir) {
  state.sort = key;
  state.dir = dir;
  document.querySelectorAll("th[data-sort]").forEach((h) => {
    h.classList.remove("active");
    h.removeAttribute("data-dir");
  });
  const th = document.querySelector(`th[data-sort="${key}"]`);
  th.classList.add("active");
  th.setAttribute("data-dir", dir === "desc" ? "↓" : "↑");
}

// boot

// Cached so the alert-creation form (populateAlertFilterOptions, wired
// independently of the board) can reuse whatever the board's own
// filters last fetched instead of issuing its own duplicate requests.
let latestCompanyOptions = null;

async function loadFilterOptions(stats) {
  // Reuse /api/stats' curated top_departments rather than every distinct
  // string in the DB, which would be a hundreds-long noisy dropdown.
  msDepartment.setOptions(
    stats.top_departments.map((r) => ({ value: r.department, label: `${r.department} (${r.n})` }))
  );

  // Location comes from refreshLocationOptions() instead, since it needs
  // to be re-scoped to the IL-only toggle's current value.
  refreshLocationOptions();

  try {
    const companies = await getJSON("/companies?resolved_only=1");
    const sorted = [...companies.companies].sort((a, b) => a.domain.localeCompare(b.domain));
    latestCompanyOptions = sorted.map((c) => ({ value: c.domain, label: c.domain }));
    msCompany.setOptions(latestCompanyOptions);
    populateAlertFilterOptions();
  } catch {
    // Non-fatal: the board itself doesn't depend on this list, and
    // clicking a company in the market panels still works either way.
  }
}

// Re-fetches the Location dropdown's options scoped to IL-only or not,
// whenever that toggle flips. Doesn't touch the Market Stats dashboard,
// which stays a global view independent of the board's local filters.
async function refreshLocationOptions() {
  try {
    const stats = await getJSON(`/stats${state.israel_only ? "?israel_only=1" : ""}`);
    msLocation.setOptions(stats.top_locations.map((r) => ({ value: r.location, label: `${r.location} (${r.n})` })));
  } catch {
    // Non-fatal: worst case the dropdown keeps its previous option set.
  }
}

// theme (light/dark)

const THEME_KEY = "iljobs_theme";

function wireThemeToggle() {
  const btn = document.getElementById("theme-toggle");
  const sync = () => {
    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    btn.textContent = isDark ? "Light" : "Dark";
  };
  sync(); // index.html's inline head script already applied the saved theme before this ran
  btn.addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    if (next === "dark") {
      document.documentElement.setAttribute("data-theme", "dark");
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    localStorage.setItem(THEME_KEY, next);
    sync();
  });
}

// Topbar ticker: 10 most recent listings matching the board's current
// filters, not a fixed sitewide list. Called from every filter-changing
// handler, but not pagination/sort (those don't change what "recent"
// means). Duplicated once in the DOM so the CSS marquee loops seamlessly.
async function loadTicker() {
  const track = document.getElementById("ticker-track");
  try {
    const params = qs({ ...currentFilterParams(), limit: 10, sort: "age", dir: "asc" });
    const data = await getJSON(`/jobs?${params}`);
    if (!data.jobs.length) {
      track.innerHTML = "";
      return;
    }
    const itemsHtml = data.jobs
      .map(
        (j) => `
        <a class="ticker-item" href="${escapeHtml(j.url || "#")}" target="_blank" rel="noopener">
          <span class="bullet">●</span>${escapeHtml(j.title)}
          <span class="ticker-company">@${escapeHtml(j.company_domain)}</span>
        </a>`
      )
      .join("");
    track.innerHTML = itemsHtml + itemsHtml;
    // Roughly constant per-item reading speed regardless of list length,
    // rather than a fixed duration that'd crawl for 3 items and race for 10.
    track.style.animationDuration = `${data.jobs.length * 4}s`;
  } catch {
    // Non-fatal: purely decorative, the board itself doesn't depend on it.
  }
}

// Refreshes everything driven by /api/stats -- metrics, market panels,
// filter option counts. Not the job table/pagination itself: a listing
// re-rendering under a user mid-scroll or mid-page would be more
// disruptive than useful, so that stays a manual reload.
// Cached so the alert-creation form can reuse the board's own last-fetched
// stats (top_departments/top_locations) instead of a duplicate fetch.
let latestStats = null;

async function refreshStats() {
  try {
    const stats = await getJSON("/stats");
    latestStats = stats;
    renderMetrics(stats);
    renderPanels(stats);
    loadFilterOptions(stats);
    populateAlertFilterOptions();
  } catch (err) {
    const msg = `<div class="error-state" style="grid-column:1/-1">Could not load /api/stats: ${escapeHtml(err.message)}</div>`;
    document.getElementById("metrics-grid").innerHTML = msg;
    document.getElementById("panel-grid").innerHTML = msg;
  }
}

// Auth. Three passwordless sign-in paths, no accounts endpoint on
// this API beyond what a Cognito JWT authorizer will eventually protect
// (/me/alerts). See infra/cognito.tf and github_auth_handler.py for the
// backend half of each of these.

const COGNITO_DOMAIN = "iljobs-auth-876913698688.auth.il-central-1.amazoncognito.com";
const COGNITO_REGION = "il-central-1";
const COGNITO_CLIENT_ID = "5021pv23cp3udp1uaq34tp38mb";
// Filled in once the GitHub OAuth App exists (github.com/settings/developers) --
// OAuth client IDs aren't secret, safe to ship in frontend JS same as Google's.
const GITHUB_OAUTH_CLIENT_ID = "";
// Flips to true once infra/cognito.tf's aws_cognito_identity_provider.google
// actually exists (real Google Cloud Console credentials set). Until then,
// redirecting to Cognito's /oauth2/authorize?identity_provider=Google lands
// on Cognito's own generic "Login option is not available" hosted-UI error
// page instead -- confusing, and a full navigation away from this app's own
// error display. Guarded the same way GitHub is instead.
const GOOGLE_CONFIGURED = false;
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

function signOut() {
  localStorage.removeItem(AUTH_TOKENS_KEY);
  renderAuthState();
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
    showAuthError("Google sign-in isn't wired up yet.");
    return;
  }
  const verifier = randomUrlSafe(64);
  sessionStorage.setItem(PKCE_VERIFIER_KEY, verifier);
  const challenge = await base64UrlDigest(verifier);
  const redirectUri = `${location.origin}/`;
  const url = `https://${COGNITO_DOMAIN}/oauth2/authorize?${qs({
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
    showAuthError("GitHub sign-in isn't wired up yet.");
    return;
  }
  const url = `https://github.com/login/oauth/authorize?${qs({
    client_id: GITHUB_OAUTH_CLIENT_ID,
    redirect_uri: `${location.origin}/api/auth/github/callback`,
    scope: "read:user user:email",
  })}`;
  location.href = url;
}

// Cognito's InitiateAuth/RespondToAuthChallenge are deliberately public,
// unsigned operations for a user-pool app client -- callable directly
// from the browser, no backend proxy or AWS SDK needed for this part.
async function cognitoRequest(target, body) {
  const res = await fetch(`https://cognito-idp.${COGNITO_REGION}.amazonaws.com/`, {
    method: "POST",
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
  const res = await fetch(`${API_BASE}/auth/email/start`, {
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
    body: qs({
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

// Two unrelated redirect shapes land here, both back at "/": Google's
// via Cognito's own authorization-code flow (?code=... query param,
// exchanged client-side above) and GitHub's via github_auth_handler.py's
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
    console.error("Sign-in failed:", hash.get("auth_error"));
    history.replaceState(null, "", location.pathname + location.search);
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

function showAuthError(msg) {
  const el = document.getElementById("auth-error");
  el.textContent = msg;
  el.hidden = false;
}

// Reported live as "OTP isn't sending" -- it was sending fine, but a
// stale error from an earlier attempt (e.g. clicking GitHub first) never
// got cleared, so it sat on screen looking like the *current* action had
// just failed. Called at the start of every provider/form action below.
function clearAuthError() {
  document.getElementById("auth-error").hidden = true;
}

function renderAuthState() {
  const area = document.getElementById("auth-area");
  const tokens = getAuthTokens();
  if (!tokens) {
    area.innerHTML = `
      <button class="auth-trigger" id="auth-trigger" type="button">Sign In</button>
      <div class="auth-panel" id="auth-panel" hidden>
        <button class="auth-provider-btn" id="auth-google" type="button">
          <svg viewBox="0 0 18 18" width="16" height="16" aria-hidden="true"><path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.874 2.684-6.615z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332C2.438 15.983 5.482 18 9 18z"/><path fill="#FBBC05" d="M3.964 10.71c-.18-.54-.282-1.117-.282-1.71s.102-1.17.282-1.71V4.958H.957C.348 6.173 0 7.548 0 9s.348 2.827.957 4.042l3.007-2.332z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0 5.482 0 2.438 2.017.957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"/></svg>
          <span>Continue with Google</span>
        </button>
        <button class="auth-provider-btn" id="auth-github" type="button">
          <svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
          <span>Continue with GitHub</span>
        </button>
        <div class="auth-divider">or</div>
        <form class="auth-email-form" id="auth-email-form">
          <input type="email" id="auth-email-input" placeholder="you@example.com" required autocomplete="email" />
          <button class="btn" type="submit">Send code</button>
        </form>
        <form class="auth-email-form" id="auth-otp-form" hidden>
          <input type="text" id="auth-otp-input" placeholder="Verification code" inputmode="numeric" pattern="[0-9]{4,10}" required autocomplete="one-time-code" />
          <button class="btn" type="submit">Verify</button>
        </form>
        <p class="auth-error" id="auth-error" hidden></p>
      </div>`;
    wireAuthTrigger();
    wireAuthPanel();
    return;
  }
  const email = decodeJwtEmail(tokens.id_token) || "signed in";
  area.innerHTML = `
    <button class="auth-trigger" id="topbar-alert-btn" type="button">+ Alert</button>
    <button class="auth-trigger" id="auth-trigger" type="button">${escapeHtml(email)}</button>
    <div class="auth-panel alerts-panel" id="auth-panel" hidden>
      <div class="alerts-header">My Alerts</div>
      <div id="alerts-list"><p class="alerts-empty">Loading…</p></div>

      <div class="alert-create">
        <div class="alerts-header">New Alert</div>
        <div class="alert-create-fields">
          <input type="text" id="alert-f-q" placeholder="SEARCH TITLE, COMPANY, LOCATION…" />
          <div class="ms" id="alert-ms-department"></div>
          <div class="ms" id="alert-ms-seniority"></div>
          <div class="ms" id="alert-ms-company"></div>
          <div class="ms" id="alert-ms-location"></div>
          <div class="ms" id="alert-ms-workplace"></div>
          <label class="toggle"><input type="checkbox" id="alert-f-israel" checked /> Israel only</label>
        </div>
        <button class="btn" id="create-alert-btn" type="button">Create Alert</button>
        <p class="create-alert-feedback" id="create-alert-feedback" hidden></p>
      </div>

      <button class="auth-signout" id="auth-signout" type="button">Sign Out</button>
    </div>`;
  wireAuthTrigger();
  wireTopbarAlertButton();
  document.getElementById("auth-signout").addEventListener("click", signOut);
  wireAlertCreateForm();
  loadMyAlerts().then(renderAlertsList);
}

function wireAuthTrigger() {
  const trigger = document.getElementById("auth-trigger");
  const panel = document.getElementById("auth-panel");
  trigger.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    trigger.classList.toggle("active", !panel.hidden);
  });
}

// A second trigger next to the email one, signed-in only -- opens the
// same panel (My Alerts + New Alert form), just a more discoverable
// entry point than clicking your own email. Doesn't create anything
// itself. Rewired on every renderAuthState() re-render like the rest of
// this panel's internals, since sign-in/out replaces the whole subtree.
function wireTopbarAlertButton() {
  document.getElementById("topbar-alert-btn").addEventListener("click", () => {
    const trigger = document.getElementById("auth-trigger");
    const panel = document.getElementById("auth-panel");
    panel.hidden = false;
    trigger.classList.add("active");
  });
}

function wireAuthPanel() {
  document.getElementById("auth-google").addEventListener("click", () => { clearAuthError(); startGoogleSignIn(); });
  document.getElementById("auth-github").addEventListener("click", () => { clearAuthError(); startGithubSignIn(); });

  document.getElementById("auth-email-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clearAuthError();
    const email = document.getElementById("auth-email-input").value.trim();
    const btn = e.target.querySelector("button");
    btn.disabled = true;
    try {
      await startEmailSignIn(email);
      document.getElementById("auth-email-form").hidden = true;
      document.getElementById("auth-otp-form").hidden = false;
      document.getElementById("auth-otp-input").focus();
    } catch (err) {
      showAuthError(err.message || "Could not send a code. Try again.");
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("auth-otp-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clearAuthError();
    const code = document.getElementById("auth-otp-input").value.trim();
    const btn = e.target.querySelector("button");
    btn.disabled = true;
    try {
      await verifyEmailOtp(code);
      renderAuthState();
    } catch (err) {
      showAuthError(err.message === "CodeMismatchException" ? "Wrong code, try again." : err.message || "Could not verify that code.");
    } finally {
      btn.disabled = false;
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
  document.addEventListener("mousedown", (e) => {
    const panel = document.getElementById("auth-panel");
    const trigger = document.getElementById("auth-trigger");
    if (panel && !panel.hidden && !e.target.closest("#auth-area")) {
      panel.hidden = true;
      trigger.classList.remove("active");
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
  } catch {
    signOut();
    return null;
  }
}

async function authedFetch(path, options = {}) {
  const tokens = await ensureFreshTokens();
  if (!tokens) throw new Error("Signed out");
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
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
function describeAlertFilter(filter) {
  const parts = [];
  if (filter.q) parts.push(`"${filter.q}"`);
  if (filter.department) parts.push(filter.department.split(",").join(", "));
  if (filter.seniority) parts.push(filter.seniority.split(",").join(", "));
  if (filter.company) parts.push(filter.company.split(",").join(", "));
  if (filter.location) parts.push(filter.location.split(",").join(", "));
  if (filter.workplace) parts.push(filter.workplace.split(",").join(", "));
  if (filter.israel_only === "1") parts.push("Israel only");
  return parts.length ? parts.join(" · ") : "All jobs";
}

function renderAlertsList(alerts) {
  const container = document.getElementById("alerts-list");
  if (!container) return; // signed out (or panel re-rendered) before this resolved
  if (!alerts.length) {
    container.innerHTML = `<p class="alerts-empty">No alerts yet. Pick some filters below and create one.</p>`;
    return;
  }
  container.innerHTML = alerts
    .map(
      (a) => `
      <div class="alert-row ${a.active ? "" : "paused"}">
        <span class="alert-summary" title="${escapeHtml(describeAlertFilter(a.filter))}">${escapeHtml(describeAlertFilter(a.filter))}</span>
        <span class="alert-actions">
          <button class="alert-toggle" data-id="${a.alert_id}" data-active="${a.active}">${a.active ? "Pause" : "Resume"}</button>
          <button class="alert-delete" data-id="${a.alert_id}" aria-label="Delete alert" title="Delete alert">✕</button>
        </span>
      </div>`
    )
    .join("");

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
  q: "",
  department: [],
  seniority: [],
  company: [],
  location: [],
  workplace: [],
  israel_only: true,
};

function resetAlertForm() {
  alertFormState.q = "";
  alertFormState.department = [];
  alertFormState.seniority = [];
  alertFormState.company = [];
  alertFormState.location = [];
  alertFormState.workplace = [];
  alertFormState.israel_only = true;
  document.getElementById("alert-f-q").value = "";
  document.getElementById("alert-f-israel").checked = true;
  alertMsDepartment.reset();
  alertMsSeniority.reset();
  alertMsCompany.reset();
  alertMsLocation.reset();
  alertMsWorkplace.reset();
}

// Options come from whatever /api/stats and /api/companies last
// returned for the board's own filters (see refreshStats/
// loadFilterOptions, which cache into latestStats/latestCompanyOptions
// and call this again on every refresh) -- no separate fetch just for
// this form. Location uses the unscoped, global top_locations (the
// refreshStats() call, not the board's IL-only-rescoped one), so it
// doesn't need its own re-fetch when the form's own Israel-only
// checkbox is toggled.
function populateAlertFilterOptions() {
  if (!alertMsDepartment) return; // signed out, or panel not built yet
  if (latestStats) {
    alertMsDepartment.setOptions(
      latestStats.top_departments.map((r) => ({ value: r.department, label: `${r.department} (${r.n})` }))
    );
    alertMsLocation.setOptions(
      latestStats.top_locations.map((r) => ({ value: r.location, label: `${r.location} (${r.n})` }))
    );
  }
  if (latestCompanyOptions) alertMsCompany.setOptions(latestCompanyOptions);
}

function wireAlertCreateForm() {
  document.getElementById("alert-f-q").addEventListener("input", (e) => {
    alertFormState.q = e.target.value.trim();
  });

  alertMsDepartment = createMultiSelect("alert-ms-department", {
    placeholder: "CATEGORIES",
    onChange: (values) => { alertFormState.department = values; },
  });
  alertMsSeniority = createMultiSelect("alert-ms-seniority", {
    placeholder: "LEVELS",
    options: Object.entries(SENIORITY_LABELS).map(([value, label]) => ({ value, label })),
    onChange: (values) => { alertFormState.seniority = values; },
  });
  alertMsCompany = createMultiSelect("alert-ms-company", {
    placeholder: "COMPANIES",
    searchable: true,
    onChange: (values) => { alertFormState.company = values; },
  });
  alertMsLocation = createMultiSelect("alert-ms-location", {
    placeholder: "LOCATIONS",
    searchable: true,
    onChange: (values) => { alertFormState.location = values; },
  });
  alertMsWorkplace = createMultiSelect("alert-ms-workplace", {
    placeholder: "WORKPLACE",
    options: Object.entries(WORKPLACE_LABELS).map(([value, label]) => ({ value, label })),
    onChange: (values) => { alertFormState.workplace = values; },
  });
  populateAlertFilterOptions();

  document.getElementById("alert-f-israel").addEventListener("change", (e) => {
    alertFormState.israel_only = e.target.checked;
  });

  document.getElementById("create-alert-btn").addEventListener("click", async () => {
    const raw = {
      q: alertFormState.q,
      department: alertFormState.department.join(","),
      seniority: alertFormState.seniority.join(","),
      company: alertFormState.company.join(","),
      location: alertFormState.location.join(","),
      workplace: alertFormState.workplace.join(","),
      israel_only: alertFormState.israel_only ? "1" : "",
    };
    const filter = Object.fromEntries(Object.entries(raw).filter(([, v]) => v !== "" && v != null));
    try {
      await authedFetch("/me/alerts", { method: "POST", body: JSON.stringify({ filter }) });
      showCreateAlertFeedback("Alert created.");
      resetAlertForm();
      renderAlertsList(await loadMyAlerts());
    } catch (err) {
      showCreateAlertFeedback(err.message || "Could not create alert.", true);
    }
  });
}

// scrape-fast.yml re-polls every 10 min; 2 min keeps an open tab
// reasonably current without hammering the API, and lines up with
// CloudFront's own 120s cache on /api/* so most polls never even reach
// the Lambda.
const STATS_POLL_MS = 120_000;

async function boot() {
  await handleAuthRedirect(); // before wireAuth: a fresh token from a redirect must be in localStorage before the initial render
  wireAuth();
  wireFilters();
  wireJobDetail();
  wireThemeToggle();
  setActiveSortHeader("age", "asc");
  loadTicker();
  await refreshStats();
  loadJobs();

  setInterval(() => {
    refreshStats();
    loadTicker();
  }, STATS_POLL_MS);
  setInterval(tickApiStatus, API_STATUS_TICK_MS);
}

boot();
