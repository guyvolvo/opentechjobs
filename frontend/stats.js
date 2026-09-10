// The Explore page: SQL over every listing, run in the browser.
//
// The database is /explore.db, a static file the applier rebuilds hourly
// (loader/build_explore.py). SQLite compiled to WebAssembly opens it
// through a virtual filesystem that fetches only the 4KB pages a query
// touches, by HTTP range request. So an indexed query reads a few dozen
// pages whether the file is 78MB or 780, a full scan reads the whole
// thing once, and either way no server runs anything.
//
// That is also the whole security model. A query can only exhaust this
// tab. There is no backend to protect from it.
//
// Its own script, not a mode of app.js, for the same reason the stats
// page was: three thousand lines of board machinery this page has no
// use for.

"use strict";

const DB_URL = "/explore.db";
const WORKER_URL = "vendor/httpvfs/sqlite.worker.js";
const WASM_URL = "vendor/httpvfs/sql-wasm.wasm";

// Matches PAGE_SIZE in the builder. Smaller means more requests for a
// scan; larger means more waste on an index lookup.
const CHUNK_SIZE = 4096;

// A ceiling on what one page load may fetch, in bytes. A single full
// scan of the file fits under it; a runaway query that keeps scanning
// does not, and gets a disk I/O error rather than an unbounded bill for
// the visitor's own bandwidth.
const MAX_BYTES = 256 * 1024 * 1024;

// Rows rendered, not rows computed. The query runs to completion in the
// worker; this only bounds what the page tries to paint.
const MAX_ROWS = 2000;

const TEMPLATES = [
  {
    name: "Open jobs by category and seniority",
    sql: `SELECT COALESCE(category, 'other') AS category,
       COALESCE(seniority, 'unstated') AS seniority,
       COUNT(*) AS jobs
FROM jobs
WHERE closed_at IS NULL
GROUP BY 1, 2
ORDER BY jobs DESC
LIMIT 40`,
  },
  {
    name: "Most-asked-for skills",
    sql: `SELECT s.skill, COUNT(*) AS listings
FROM job_skills s
JOIN jobs j ON j.id = s.job_id
WHERE j.closed_at IS NULL
GROUP BY s.skill
ORDER BY listings DESC
LIMIT 25`,
  },
  {
    name: "Skills asked of senior engineers",
    sql: `SELECT s.skill, COUNT(*) AS listings
FROM job_skills s
JOIN jobs j ON j.id = s.job_id
WHERE j.closed_at IS NULL
  AND j.seniority = 'senior'
  AND j.category = 'Software Engineering'
GROUP BY s.skill
ORDER BY listings DESC
LIMIT 20`,
  },
  {
    name: "Remote, hybrid, on site, or unsaid",
    sql: `SELECT COALESCE(workplace, 'unstated') AS workplace,
       COUNT(*) AS jobs,
       ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM jobs WHERE closed_at IS NULL), 1) AS pct
FROM jobs
WHERE closed_at IS NULL
GROUP BY 1
ORDER BY jobs DESC`,
  },
  {
    name: "Who discloses pay, by ATS",
    sql: `SELECT ats,
       COUNT(*) AS jobs,
       SUM(salary_source = 'disclosed') AS disclosed,
       ROUND(100.0 * SUM(salary_source = 'disclosed') / COUNT(*), 1) AS pct_disclosed
FROM jobs
WHERE closed_at IS NULL
GROUP BY ats
HAVING jobs > 300
ORDER BY pct_disclosed DESC`,
  },
  {
    name: "New listings per day",
    sql: `SELECT substr(first_seen, 1, 10) AS day, COUNT(*) AS new_listings
FROM jobs
GROUP BY day
ORDER BY day`,
  },
  {
    name: "Days open before closing, by category",
    sql: `SELECT COALESCE(category, 'other') AS category,
       COUNT(*) AS closed,
       ROUND(AVG(days_open), 1) AS avg_days_open
FROM jobs
WHERE closed_at IS NOT NULL
GROUP BY 1
HAVING closed > 100
ORDER BY avg_days_open DESC`,
  },
  {
    name: "Companies hiring the most right now",
    sql: `SELECT COALESCE(name, domain) AS company, domain, open_jobs
FROM companies
ORDER BY open_jobs DESC
LIMIT 30`,
  },
  {
    name: "Senior backend roles in Israel",
    sql: `SELECT title, company, location, salary_text, url
FROM jobs
WHERE closed_at IS NULL
  AND seniority = 'senior'
  AND category = 'Software Engineering'
  AND (location LIKE '%Israel%' OR location LIKE '%Tel Aviv%')
ORDER BY first_seen DESC
LIMIT 100`,
  },
  {
    name: "Listings that state a real salary",
    sql: `SELECT title, company, location, salary_text, url
FROM jobs
WHERE closed_at IS NULL
  AND salary_source = 'disclosed'
ORDER BY first_seen DESC
LIMIT 100`,
  },
];

const SCHEMA = [
  {
    table: "jobs",
    note: "One row per listing, open and closed.",
    columns: [
      ["id", "stable id"],
      ["company", "domain; joins to companies"],
      ["ats", "which system it was scraped from"],
      ["title", ""],
      ["category", "board's normalisation; NULL when unsure"],
      ["department", "the employer's own label"],
      ["seniority", "intern … exec, or NULL"],
      ["workplace", "remote, hybrid, onsite, or NULL"],
      ["location", "as written"],
      ["salary_text", "as shown on the board"],
      ["salary_source", "disclosed, table, estimated, or NULL"],
      ["url", ""],
      ["posted_at", "employer's date, if given"],
      ["first_seen", "when we first saw it"],
      ["last_seen", ""],
      ["closed_at", "NULL while open"],
      ["days_open", "closed minus first seen, or age so far"],
    ],
  },
  {
    table: "job_skills",
    note: "One row per skill per listing. Join on job_id.",
    columns: [["job_id", ""], ["skill", "lower-cased"]],
  },
  {
    table: "companies",
    note: "One row per tracked company.",
    columns: [["domain", ""], ["name", "as the ATS reports it"], ["ats", ""], ["open_jobs", ""]],
  },
  {
    table: "meta",
    note: "When this file was built, and how much is in it.",
    columns: [["key", ""], ["value", ""]],
  },
];

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmtInt(n) {
  return Number(n).toLocaleString("en-US");
}

function fmtBytes(b) {
  if (b < 1024 * 1024) return `${Math.round(b / 1024)} KB`;
  return `${(b / (1024 * 1024)).toFixed(1)} MB`;
}

const $ = (id) => document.getElementById(id);

let worker = null;
let running = false;

// Comlink cannot cancel a running query, so this is a bound on what the
// page waits for, not on what the worker does. The byte cap is the real
// backstop.
const QUERY_TIMEOUT_MS = 60_000;

async function openDatabase() {
  const status = $("explore-status");
  status.textContent = "Opening the database…";
  const config = {
    from: "inline",
    config: {
      serverMode: "full",
      url: DB_URL,
      requestChunkSize: CHUNK_SIZE,
    },
  };
  // eslint-disable-next-line no-undef
  worker = await createDbWorker([config], WORKER_URL, WASM_URL, MAX_BYTES);
  const meta = await worker.db.exec("SELECT key, value FROM meta");
  const m = Object.fromEntries(meta[0] ? meta[0].values : []);
  const built = m.built_at ? new Date(m.built_at) : null;
  const age = built ? Math.round((Date.now() - built.getTime()) / 60000) : null;
  status.textContent = `${fmtInt(m.jobs || 0)} listings, ${fmtInt(m.companies || 0)} companies`
    + (age == null ? "" : `, built ${age < 2 ? "just now" : age + " min ago"}`);
  $("explore-note").textContent =
    `The file covers every verified listing seen since ${m.corpus_since || "1 September 2026"} and is rebuilt every hour. `
    + `Your queries fetch only what they touch; a page load may read up to ${fmtBytes(MAX_BYTES)} before it stops itself.`;
}

function currentSql() {
  return $("explore-sql").value.trim();
}

function setSql(sql) {
  $("explore-sql").value = sql;
}

// Shareable state: the query in the URL, so a link reproduces the view.
// Base64 of UTF-8, because SQL has every character URLs dislike.
function readSqlFromUrl() {
  const q = new URLSearchParams(location.search).get("q");
  if (!q) return null;
  try {
    return decodeURIComponent(escape(atob(q)));
  } catch {
    return null;
  }
}

function writeSqlToUrl(sql) {
  const q = btoa(unescape(encodeURIComponent(sql)));
  history.replaceState(null, "", `${location.pathname}?q=${q}`);
}

async function runQuery() {
  if (!worker || running) return;
  const sql = currentSql();
  if (!sql) return;
  running = true;
  const status = $("explore-status");
  const err = $("explore-error");
  const out = $("explore-result");
  err.hidden = true;
  status.textContent = "Running…";
  document.body.classList.add("explore-busy");
  const t0 = performance.now();
  const before = await worker.worker.bytesRead;
  try {
    const result = await Promise.race([
      worker.db.exec(sql),
      new Promise((_, reject) => setTimeout(() => reject(new Error(
        `Still running after ${QUERY_TIMEOUT_MS / 1000}s. The query continues in the background; `
        + "narrow it or add an indexed WHERE clause.")), QUERY_TIMEOUT_MS)),
    ]);
    const ms = Math.round(performance.now() - t0);
    const read = (await worker.worker.bytesRead) - before;
    writeSqlToUrl(sql);
    render(result, ms, read);
  } catch (e) {
    err.textContent = String(e.message || e);
    err.hidden = false;
    out.innerHTML = "";
    status.textContent = "Query failed";
  } finally {
    running = false;
    document.body.classList.remove("explore-busy");
  }
}

function render(result, ms, read) {
  const status = $("explore-status");
  const out = $("explore-result");
  if (!result.length) {
    out.innerHTML = '<div class="explore-empty">No rows.</div>';
    status.textContent = `0 rows in ${ms} ms, ${fmtBytes(read)} fetched`;
    return;
  }
  const { columns, values } = result[result.length - 1];
  const shown = values.slice(0, MAX_ROWS);
  const viz = pickViz(columns, shown);
  status.textContent = `${fmtInt(values.length)} row${values.length === 1 ? "" : "s"} in ${ms} ms, ${fmtBytes(read)} fetched`
    + (values.length > MAX_ROWS ? `, showing ${fmtInt(MAX_ROWS)}` : "");
  if (viz === "bar") out.innerHTML = renderBars(columns, shown);
  else if (viz === "line") out.innerHTML = renderLine(columns, shown);
  else out.innerHTML = renderTable(columns, shown);
}

// Auto picks the shape the data is already in. Two columns where the
// first is a label and the second a number is a bar chart; where the
// first is a date it is a line. Anything else is a table, which is
// never wrong, only sometimes less useful.
function pickViz(columns, rows) {
  const chosen = $("explore-viz").value;
  if (chosen !== "auto") return chosen;
  if (columns.length < 2 || rows.length < 2 || rows.length > 60) return "table";
  const firstIsDate = rows.every((r) => /^\d{4}-\d{2}(-\d{2})?/.test(String(r[0] ?? "")));
  const secondIsNumber = rows.every((r) => typeof r[1] === "number");
  if (!secondIsNumber) return "table";
  return firstIsDate ? "line" : "bar";
}

function renderTable(columns, rows) {
  const head = columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("");
  const body = rows.map((r) => `<tr>${r.map((v, i) => cell(v, columns[i])).join("")}</tr>`).join("");
  return `<div class="explore-table-wrap"><table class="explore-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function cell(v, col) {
  if (v == null) return '<td class="null">NULL</td>';
  if (typeof v === "number") return `<td class="num">${Number.isInteger(v) ? fmtInt(v) : v}</td>`;
  const s = String(v);
  if (col === "url" && /^https?:\/\//.test(s)) {
    return `<td><a class="link" href="${escapeHtml(s)}" target="_blank" rel="noopener">open</a></td>`;
  }
  return `<td>${escapeHtml(s)}</td>`;
}

// Horizontal bars in the board's own bar-row markup, so a query result
// looks like the panels it replaced. First row green, One Voice Rule.
function renderBars(columns, rows) {
  const max = Math.max(1, ...rows.map((r) => Number(r[1]) || 0));
  return `<div class="explore-bars">${rows.map((r) => `
    <div class="bar-row">
      <div class="name" title="${escapeHtml(r[0])}">${escapeHtml(r[0] ?? "NULL")}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${((Number(r[1]) || 0) / max) * 100}%"></div></div>
      <div class="n">${Number.isInteger(r[1]) ? fmtInt(r[1]) : r[1]}</div>
    </div>`).join("")}
    <div class="explore-axis"><span>${escapeHtml(columns[0])}</span><span>${escapeHtml(columns[1])}</span></div>
  </div>`;
}

// One line, hand-drawn. Same visual voice as the homepage's trend
// chart: a single stroke, no fill, no markers, the ends labelled.
function renderLine(columns, rows) {
  const w = 640;
  const h = 140;
  const pad = 4;
  const ys = rows.map((r) => Number(r[1]) || 0);
  const max = Math.max(1, ...ys);
  const step = rows.length > 1 ? (w - pad * 2) / (rows.length - 1) : 0;
  const pts = ys.map((y, i) => [pad + i * step, h - pad - (y / max) * (h - pad * 2)]);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const last = rows[rows.length - 1];
  return `<div class="explore-line">
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" class="trend-chart explore-line-svg">
      <path class="trend-line" d="${d}"><title>${escapeHtml(columns[1])}</title></path>
    </svg>
    <div class="trend-axis"><span>${escapeHtml(rows[0][0])}</span><span>peak ${fmtInt(max)}</span><span>${escapeHtml(last[0])}</span></div>
  </div>`;
}

function renderTemplates() {
  $("explore-templates").innerHTML = TEMPLATES.map((t, i) =>
    `<button type="button" class="explore-template" data-i="${i}">${escapeHtml(t.name)}</button>`).join("");
  $("explore-templates").addEventListener("click", (e) => {
    const b = e.target.closest(".explore-template");
    if (!b) return;
    document.querySelectorAll(".explore-template.active").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    setSql(TEMPLATES[Number(b.dataset.i)].sql);
    $("explore-viz").value = "auto";
    runQuery();
  });
}

function renderSchema() {
  $("explore-schema").innerHTML = SCHEMA.map((t) => `
    <details class="explore-tbl" ${t.table === "jobs" ? "open" : ""}>
      <summary><code>${t.table}</code><span>${escapeHtml(t.note)}</span></summary>
      <ul>${t.columns.map(([c, n]) =>
        `<li><code>${c}</code>${n ? `<span>${escapeHtml(n)}</span>` : ""}</li>`).join("")}</ul>
    </details>`).join("");
}

function wireThemeToggle() {
  const btn = $("theme-toggle");
  if (!btn) return;
  const sync = () => {
    btn.textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "Light" : "Dark";
  };
  sync();
  btn.addEventListener("click", () => {
    const root = document.documentElement;
    const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    if (next === "dark") root.setAttribute("data-theme", "dark");
    else root.removeAttribute("data-theme");
    localStorage.setItem("iljobs_theme", next);
    sync();
  });
}

async function boot() {
  wireThemeToggle();
  renderTemplates();
  renderSchema();

  $("explore-run").addEventListener("click", runQuery);
  $("explore-sql").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      runQuery();
    }
  });
  $("explore-viz").addEventListener("change", () => {
    if (currentSql()) runQuery();
  });
  $("explore-share").addEventListener("click", async () => {
    const sql = currentSql();
    if (!sql) return;
    writeSqlToUrl(sql);
    try {
      await navigator.clipboard.writeText(location.href);
      $("explore-share").textContent = "Copied";
      setTimeout(() => { $("explore-share").textContent = "Copy link"; }, 1500);
    } catch {
      // Clipboard denied. The URL bar already has it.
    }
  });

  try {
    await openDatabase();
  } catch (e) {
    $("explore-status").textContent = "The database runtime could not start.";
    const err = $("explore-error");
    err.textContent = `${e.message || e}. This page needs WebAssembly and a modern browser.`;
    err.hidden = false;
    return;
  }

  const fromUrl = readSqlFromUrl();
  if (fromUrl) {
    setSql(fromUrl);
  } else {
    setSql(TEMPLATES[0].sql);
    document.querySelector(".explore-template").classList.add("active");
  }
  runQuery();
}

boot();
