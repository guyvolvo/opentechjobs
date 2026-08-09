// OpenMarketIL frontend. No framework, no build step -- this is a static file
// served straight from S3/CloudFront (see infra/cloudfront.tf), so it has
// to run as-shipped. Talks to the real API (api/handler.py) at /api/*,
// same origin, no CORS needed in production (CloudFront routes /api/*
// to the Lambda -- see cloudfront.tf's header comment).
//
// "CRUD" for an individual visitor -- starring a listing -- is
// localStorage-only, on purpose: the API has no write endpoints and no
// accounts (see api/handler.py's module docstring), so there's no server
// side to hang that state off of.

const API_BASE = "/api";
const STAR_KEY = "iljobs_starred";
const PAGE_SIZE = 50;

// Fixed vocabulary -- matches probe.py's _classify_seniority/Job.seniority
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

// Matches probe.py's Job.workplace_type exactly -- see its docstring.
const WORKPLACE_LABELS = {
  remote: "Remote",
  hybrid: "Hybrid",
  onsite: "Onsite",
};

const state = {
  q: "",
  keywords: "", // ';'-separated, ALL must appear -- see api/handler.py's route_jobs comment on why AND not OR
  department: [], // labeled "Role" in the UI -- see route_stats()'s comment on why the backend field stays "department". Multi-select -> joined with "," for the API (route_jobs' _add_in_filter).
  seniority: [],
  company: [], // multi-select; also set via clicking a company in the market panels
  location: [], // multi-select of curated top raw location strings -- see route_stats()'s comment on why this isn't a real geocoded facet. israel_only stays the right tool for "just IL."
  workplace: [], // remote|hybrid|onsite -- fixed vocabulary like seniority, see probe.py's Job.workplace_type docstring
  confidence: "all", // no confidence filter in the UI -- verified vs best_effort is shown inline (the badge) instead of gating
  israel_only: true, // this is an Israel-focused board -- global-by-default would bury the point of it
  starred_only: false,
  sort: "age",
  dir: "asc", // newest first -- see api/handler.py's route_jobs comment on the age/dir flip
  offset: 0,
};

// Assigned once in wireFilters() -- referenced from loadFilterOptions()
// (populating Role/Company's options once they're known) and from the
// market panels' "click a company" handler (syncing that pick back into
// the Company multi-select).
let msDepartment, msSeniority, msCompany, msLocation, msWorkplace;

// ---------------------------------------------------------------------------
// storage
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// fetch helpers
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// formatting
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// metrics dashboard
// ---------------------------------------------------------------------------

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

// Market-insight panels -- who's hiring, what for, where. No ATS-vendor
// breakdown here on purpose: which API a listing was polled from is
// plumbing, not a market signal (still in the raw /api/stats response
// as open_jobs_by_ats for anyone polling directly who wants it).
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
// overlaid on its OWN scale (max of `closed`, not max of `n`) -- closed
// counts run far smaller than new-listing counts, so sharing one scale
// would flatline the line near zero. That means the two series aren't
// pixel-comparable to each other, just each legible in its own right --
// the legend says so explicitly rather than implying a shared axis.
// viewBox-scaled so it's responsive without JS recalculating on resize;
// native <title> gives a free per-point tooltip with no extra markup.
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
  const closedLine = closedXY.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const closedDots = daily
    .map(
      (d, i) =>
        `<circle class="trend-dot" cx="${closedXY[i][0].toFixed(1)}" cy="${closedXY[i][1].toFixed(1)}" r="2"><title>${d.date}: ${d.closed || 0} closed</title></circle>`
    )
    .join("");

  return `
    <svg class="trend-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      ${bars}
      <polyline class="trend-line" points="${closedLine}" />
      ${closedDots}
    </svg>
    <div class="trend-legend">
      <span><span class="dot new"></span>New</span>
      <span><span class="dot closed"></span>Closed (own scale)</span>
    </div>
    <div class="trend-axis"><span>${daily[0].date.slice(5)}</span><span>${daily[daily.length - 1].date.slice(5)}</span></div>`;
}

// Standalone line chart -- open_jobs_history, reconstructed retroactively
// from first_seen/closed_at rather than a real snapshot (see
// route_stats()'s comment on why that's honest, not a shortcut).
function renderOpenJobsChart(history) {
  const max = Math.max(1, ...history.map((d) => d.n));
  const w = 320;
  const h = 64;
  const stepX = history.length > 1 ? w / (history.length - 1) : 0;
  const xy = history.map((d, i) => [i * stepX, h - (d.n / max) * h]);
  const line = xy.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const dots = history
    .map((d, i) => {
      const isLast = i === history.length - 1;
      return `<circle class="trend-dot open ${isLast ? "latest" : ""}" cx="${xy[i][0].toFixed(1)}" cy="${xy[i][1].toFixed(1)}" r="2.5"><title>${d.date}: ${fmtInt(d.n)} open</title></circle>`;
    })
    .join("");
  return `
    <svg class="trend-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <polyline class="trend-line open" points="${line}" />
      ${dots}
    </svg>
    <div class="trend-axis"><span>${history[0].date.slice(5)}</span><span>${history[history.length - 1].date.slice(5)}</span></div>`;
}

// One headline percentage -- how much of the open board looks like it's
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
      <div class="panel-title">Top Roles</div>
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

// ---------------------------------------------------------------------------
// job board
// ---------------------------------------------------------------------------

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

// The filter (not sort/pagination) portion of the current state -- shared
// between loadJobs and the topbar ticker, so "10 most recent" in the
// ticker means "10 most recent matching what you've actually filtered
// for," not a fixed sitewide list that ignores your selections.
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
      <tr data-id="${j.id}">
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
            <a class="api-link" href="${API_BASE}/jobs/${j.id}" target="_blank" rel="noopener" title="Raw API record for this job -- not the application link">Raw JSON</a>
          </div>
        </td>
        <td data-label="Location">${escapeHtml(j.location || "-")}</td>
        <td data-label="Role">${escapeHtml(j.department || "-")}</td>
        <td data-label="Age" class="age-cell ${fresh ? "fresh" : ""}">${fmtAge(age)}</td>
      </tr>`;
    })
    .join("");

  document.querySelectorAll("[data-star]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const s = toggleStar(btn.dataset.star);
      btn.classList.toggle("on", s.has(btn.dataset.star));
      btn.textContent = s.has(btn.dataset.star) ? "★" : "☆";
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
    // isn't guaranteed everywhere -- fall back to a hidden textarea copy
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

// ---------------------------------------------------------------------------
// multi-select filter dropdown (Role / Level / Company)
// ---------------------------------------------------------------------------

// One open dropdown at a time -- opening a second one closes whichever
// was already open, same as a native <select> would behave.
const OPEN_MULTISELECTS = new Set();

function createMultiSelect(containerId, { placeholder, options = [], searchable = false, onChange }) {
  const container = document.getElementById(containerId);
  const selected = new Set();
  let currentOptions = options;

  container.innerHTML = `
    <button type="button" class="ms-toggle" aria-haspopup="listbox" aria-expanded="false">${escapeHtml(placeholder)}</button>
    <div class="ms-menu" hidden>
      ${searchable ? '<input type="text" class="ms-search" placeholder="Filter…" />' : ""}
      <div class="ms-options" role="listbox"></div>
      <button type="button" class="ms-clear">Clear</button>
    </div>
  `;
  const toggle = container.querySelector(".ms-toggle");
  const menu = container.querySelector(".ms-menu");
  const optionsEl = container.querySelector(".ms-options");
  const searchEl = container.querySelector(".ms-search");

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
      // Drop any selected value that no longer exists in the new option
      // set (e.g. Company's list arrives async, after boot -- nothing
      // should stay "selected" that was never a valid choice; or
      // Location's list narrowing to IL-only options when that toggle
      // flips, dropping a non-IL pick that's no longer valid there).
      selected.clear();
      current.filter((v) => opts.some((o) => o.value === v)).forEach((v) => selected.add(v));
      renderOptions(searchEl ? searchEl.value : "");
      updateLabel();
      // If something was silently dropped, the caller's state needs to
      // know -- otherwise it still holds a value this widget no longer
      // shows as selected, and would keep sending it to the API.
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

// ---------------------------------------------------------------------------
// filter wiring
// ---------------------------------------------------------------------------

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
    placeholder: "ROLES",
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

  document.getElementById("f-israel").addEventListener("change", (e) => {
    state.israel_only = e.target.checked;
    state.offset = 0;
    refreshLocationOptions();
    loadJobs();
    loadTicker();
  });

  document.getElementById("f-starred").addEventListener("change", (e) => {
    state.starred_only = e.target.checked;
    loadJobs();
    // Not loadTicker() -- "starred" is a personal, client-local view of
    // the board (see toggleStar's docstring), not an API filter
    // criterion; currentFilterParams() doesn't include it, on purpose.
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

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------

async function loadFilterOptions(stats) {
  // Roles: reuse /api/stats' top_departments (curated to the most common
  // ~20 raw labels -- see route_stats()'s comment) rather than every
  // distinct string in the DB, which would be a hundreds-long dropdown of
  // noisy one-off ATS-specific labels.
  msDepartment.setOptions(
    stats.top_departments.map((r) => ({ value: r.department, label: `${r.department} (${r.n})` }))
  );

  // Location's options come from refreshLocationOptions() instead of this
  // stats snapshot -- it needs to be re-scoped to the IL-only toggle's
  // *current* value (true by default), not whatever this unscoped fetch
  // happened to return.
  refreshLocationOptions();

  try {
    const companies = await getJSON("/companies?resolved_only=1");
    const sorted = [...companies.companies].sort((a, b) => a.domain.localeCompare(b.domain));
    msCompany.setOptions(sorted.map((c) => ({ value: c.domain, label: c.domain })));
  } catch {
    // Non-fatal -- the board itself doesn't depend on this list, and
    // clicking a company in the market panels still works either way.
  }
}

// Re-fetches just the Location dropdown's options scoped to IL-only or
// not, whenever that toggle flips -- so picking Israel only offers
// Israeli locations instead of 40 mostly-irrelevant global offices. Does
// NOT touch the Market Stats dashboard (metrics-grid/panel-grid): that's
// a deliberately global, permanent view independent of the board's local
// filters (see route_stats()'s docstring) -- this only reads
// top_locations back out of that same endpoint and throws the rest away.
async function refreshLocationOptions() {
  try {
    const stats = await getJSON(`/stats${state.israel_only ? "?israel_only=1" : ""}`);
    msLocation.setOptions(stats.top_locations.map((r) => ({ value: r.location, label: `${r.location} (${r.n})` })));
  } catch {
    // Non-fatal -- worst case the dropdown keeps its previous option set.
  }
}

// ---------------------------------------------------------------------------
// theme (light/dark)
// ---------------------------------------------------------------------------

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

// Topbar ticker -- 10 most recent listings matching whatever's currently
// filtered on the board (currentFilterParams(), shared with loadJobs --
// same Role/Level/Company/Location/Workplace/IL-only/search/keywords
// selections apply here too), not a fixed sitewide list. Called from
// every filter-changing handler in wireFilters(), same as loadJobs, but
// deliberately NOT from pagination/sort-column clicks -- which page
// you're on or how it's sorted doesn't change what "recent" means. Each
// item links straight to the application, same as the board's own Apply
// link. Duplicated once in the DOM so the CSS animation (.ticker-track,
// -50% translate) loops seamlessly -- see that rule's comment for why
// exactly half, not some other fraction.
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
    // Non-fatal -- purely decorative, the board itself doesn't depend on it.
  }
}

async function boot() {
  wireFilters();
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
