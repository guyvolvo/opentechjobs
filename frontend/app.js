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

function renderMetrics(stats) {
  const el = document.getElementById("metrics-grid");
  const fresh = (stats.freshness.minutes_since_update ?? 9999) <= 15;

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
      label: "New Listings",
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
      label: "Data Freshness",
      value: fresh ? "LIVE" : fmtMinutesAgo(stats.freshness.minutes_since_update),
      sub: fresh ? fmtMinutesAgo(stats.freshness.minutes_since_update) : "no recent updates",
      hl: fresh,
    },
  ];

  el.innerHTML = cards
    .map(
      (c) => `
      <div class="metric-card ${c.hl ? "highlight" : ""}">
        <div class="label">${c.label}</div>
        <div>
          <div class="value">${c.value}</div>
          <div class="sub">${c.sub}</div>
        </div>
      </div>`
    )
    .join("");

  document.getElementById("status-dot").classList.toggle("stale", !fresh);
  document.getElementById("status-text").textContent = fresh ? "LIVE" : "OFFLINE";
}

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
        <div class="name">${name}</div>
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
// Clamps the neighbor lookup at both ends so the curve doesn't overshoot
// past the first/last point.
function smoothPath(points) {
  if (points.length < 2) return "";
  const p = points;
  let d = `M${p[0][0].toFixed(1)},${p[0][1].toFixed(1)}`;
  for (let i = 0; i < p.length - 1; i++) {
    const p0 = p[i - 1] || p[i];
    const p1 = p[i];
    const p2 = p[i + 1];
    const p3 = p[i + 2] || p2;
    const c1x = p1[0] + (p2[0] - p0[0]) / 6;
    const c1y = p1[1] + (p2[1] - p0[1]) / 6;
    const c2x = p2[0] - (p3[0] - p1[0]) / 6;
    const c2y = p2[1] - (p3[1] - p1[1]) / 6;
    d += ` C${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
  }
  return d;
}

function renderTrendChart(daily) {
  const maxNew = Math.max(1, ...daily.map((d) => d.n));
  const maxClosed = Math.max(1, ...daily.map((d) => d.closed || 0));
  const w = 320;
  const h = 64;
  const gap = 3;
  const barW = (w - gap * (daily.length - 1)) / daily.length;

  const bars = daily
    .map((d, i) => {
      const barH = d.n ? Math.max(2, (d.n / maxNew) * h) : 0;
      const x = i * (barW + gap);
      const y = h - barH;
      const isLast = i === daily.length - 1;
      return `<rect class="trend-bar ${isLast ? "latest" : ""}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${barH.toFixed(1)}"><title>${d.date}: ${d.n} new</title></rect>`;
    })
    .join("");

  const closedXY = daily.map((d, i) => [i * (barW + gap) + barW / 2, h - ((d.closed || 0) / maxClosed) * h]);
  const closedLine = smoothPath(closedXY);
  const closedDots = daily
    .map(
      (d, i) =>
        `<circle class="trend-dot" cx="${closedXY[i][0].toFixed(1)}" cy="${closedXY[i][1].toFixed(1)}" r="2"><title>${d.date}: ${d.closed || 0} closed</title></circle>`
    )
    .join("");

  return `
    <svg class="trend-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      ${bars}
      <path class="trend-line" d="${closedLine}" />
      ${closedDots}
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
  const dots = history
    .map((d, i) => {
      const isLast = i === history.length - 1;
      return `<circle class="trend-dot open ${isLast ? "latest" : ""}" cx="${xy[i][0].toFixed(1)}" cy="${xy[i][1].toFixed(1)}" r="2.5"><title>${d.date}: ${fmtInt(d.n)} open</title></circle>`;
    })
    .join("");
  return `
    <svg class="trend-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <path class="trend-line open" d="${line}" />
      ${dots}
    </svg>
    <div class="trend-axis"><span>${history[0].date.slice(5)}</span><span>${history[history.length - 1].date.slice(5)}</span></div>`;
}

// One headline percentage: how much of the open board looks like it's
// stopped moving. threshold_days comes from the API, not hardcoded here,
// so a backend change to the cutoff doesn't need a matching frontend edit.
function renderGhostStat(ghost, openJobs) {
  const pct = Math.round(ghost.stale_pct * 1000) / 10;
  return `
    <div class="ghost-pct">${pct}%</div>
    <div class="ghost-sub">${fmtInt(ghost.stale_count)} of ${fmtInt(ghost.sample_size)} open listings with a known post date haven't moved in over ${ghost.threshold_days} days (${fmtInt(openJobs)} open in total).</div>`;
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
      <div class="panel-title">Stale Listings</div>
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

async function loadJobs() {
  const tbody = document.getElementById("jobs-body");
  const starred = getStarred();
  renderCompanyChip();

  if (state.starred_only) {
    renderStarredOnly(starred);
    return;
  }

  tbody.closest("table").style.display = "";
  document.getElementById("jobs-loading").style.display = "block";
  document.getElementById("jobs-error").style.display = "none";
  document.getElementById("jobs-empty").style.display = "none";

  const params = qs({
    ...currentFilterParams(),
    sort: state.sort,
    dir: state.dir,
    limit: PAGE_SIZE,
    offset: state.offset,
  });

  try {
    const data = await getJSON(`/jobs?${params}`);
    lastJobsResponse = data;
    document.getElementById("jobs-loading").style.display = "none";
    renderJobs(data, starred);
    renderPagination(data);
  } catch (err) {
    document.getElementById("jobs-loading").style.display = "none";
    const errEl = document.getElementById("jobs-error");
    errEl.textContent = `Could not load jobs: ${err.message}`;
    errEl.style.display = "block";
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
          <a href="${escapeHtml(j.url || "#")}" target="_blank" rel="noopener">${escapeHtml(j.title)}</a>
          ${j.seniority ? `<span class="badge seniority">${escapeHtml(SENIORITY_LABELS[j.seniority] || j.seniority)}</span>` : ""}
          ${j.workplace_type ? `<span class="badge workplace">${escapeHtml(WORKPLACE_LABELS[j.workplace_type] || j.workplace_type)}</span>` : ""}
          ${j.confidence === "best_effort" ? '<span class="badge best-effort" title="Scraped from the company\'s own page, not a live ATS API">best_effort</span>' : ""}
          <div class="company">${escapeHtml(j.company_domain)}</div>
          <div class="job-links">
            <a class="apply-link" href="${escapeHtml(j.url || "#")}" target="_blank" rel="noopener" title="Open the original listing to apply">Apply ↗</a>
            <button class="copy-link-btn" data-copy-url="${escapeHtml(j.url || "")}" title="Copy the application link">Save link</button>
          </div>
        </td>
        <td data-label="Location">${escapeHtml(j.location || "-")}</td>
        <td data-label="Category">${escapeHtml(j.department || "-")}</td>
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
        <div class="job-detail-company">${escapeHtml(job.company_domain)}</div>
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
    msCompany.setOptions(sorted.map((c) => ({ value: c.domain, label: c.domain })));
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
          <span class="ticker-company">${escapeHtml(j.company_domain)}</span>
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

async function boot() {
  wireFilters();
  wireJobDetail();
  wireThemeToggle();
  setActiveSortHeader("age", "asc");
  loadTicker();

  try {
    const stats = await getJSON("/stats");
    renderMetrics(stats);
    renderPanels(stats);
    loadFilterOptions(stats);
  } catch (err) {
    const msg = `<div class="error-state" style="grid-column:1/-1">Could not load /api/stats: ${escapeHtml(err.message)}</div>`;
    document.getElementById("metrics-grid").innerHTML = msg;
    document.getElementById("panel-grid").innerHTML = msg;
  }

  loadJobs();
}

boot();
