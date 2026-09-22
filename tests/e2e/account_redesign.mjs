// The rebuilt account page from the working tree, populated, at desktop
// and phone width in both themes. Stubs every /me/* call so the page is
// in its full state rather than its error state.
// Run from tests/e2e:  node account_redesign.mjs
import { chromium, devices } from "@playwright/test";
import { spawn } from "node:child_process";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8824", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);

const SKILLS = ["Python", "Kubernetes", "Terraform", "PostgreSQL", "Go", "AWS", "Docker", "React"];
const stub = (dark) => `
  try { localStorage.setItem("iljobs_theme", ${dark ? '"dark"' : '"light"'}); } catch {}
  try {
    const payload = btoa(JSON.stringify({ email: "qa@example.com", exp: 4102444800 })).replace(/=+$/, "");
    localStorage.setItem("iljobs_auth_tokens", JSON.stringify({ id_token: "x." + payload + ".y", access_token: "a", refresh_token: "r", expires_at: 4102444800000 }));
  } catch {}
  const ok = (b) => Promise.resolve(new Response(JSON.stringify(b), { status: 200, headers: { "Content-Type": "application/json" } }));
  const real = window.fetch.bind(window);
  window.fetch = (i, init) => {
    const u = String(i && i.url ? i.url : i);
    if (!u.includes("/me/") && !u.includes("/jobs?") && !u.includes("/stats")) return real(i, init);
    if (u.includes("/me/profile")) return ok({ profile: { skills: ${JSON.stringify(SKILLS)}, seniority: "senior", workplace: ["remote"], israel_only: true }, skill_spec: { terms: {} } });
    if (u.includes("/me/alerts")) return ok({ alerts: [
      { alert_id: "1", filter: { search: "devops", country: "IL" }, paused: false, created_at: "2026-09-01T10:00:00Z" },
      { alert_id: "2", filter: { department: "R&D", seniority: "senior" }, paused: true, created_at: "2026-08-14T10:00:00Z" }
    ] });
    if (u.includes("/me/saved")) return ok({ saved: [{ job_id: "a" }, { job_id: "b" }, { job_id: "c" }] });
    if (u.includes("/jobs?")) return ok({ jobs: [
      { id: "a", title: "Senior Platform Engineer", company_name: "ScaleOps", location: "Tel Aviv, Israel", url: "https://example.com/a" },
      { id: "b", title: "Staff Backend Engineer, Data", company_name: "Wiz", location: "Tel Aviv, Israel", url: "https://example.com/b" },
      { id: "c", title: "Infrastructure Engineer", company_name: "Monday.com", location: "Remote", url: "https://example.com/c", closed_at: "2026-09-10T00:00:00Z" }
    ], total: 3, limit: 200, offset: 0, matched_skills: [] });
    if (u.includes("/stats")) return ok({
      top_departments: [{ department: "Software Engineering", n: 93969 }, { department: "Sales", n: 41002 }],
      top_locations: [{ location: "Remote", n: 7700 }, { location: "Tel Aviv, Israel", n: 4120 }],
      top_companies: [{ domain: "wiz.io", n: 210 }, { domain: "scaleops.com", n: 18 }],
      totals: { open_jobs: 599798 }, meta: {} });
    return ok({});
  };`;

const shots = [
  ["desktop-light", { viewport: { width: 1600, height: 1100 } }, false],
  ["desktop-dark", { viewport: { width: 1600, height: 1100 } }, true],
  ["laptop-light", { viewport: { width: 1280, height: 900 } }, false],
  ["tablet-light", { viewport: { width: 900, height: 1000 } }, false],
  ["phone-light", { ...devices["Pixel 7"] }, false],
  ["phone-dark", { ...devices["Pixel 7"] }, true],
  ["phone-small", { viewport: { width: 320, height: 740 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 }, false],
];

for (const [name, opts, dark] of shots) {
  const browser = await chromium.launch();
  const context = await browser.newContext({ ...opts, colorScheme: dark ? "dark" : "light" });
  await context.addInitScript(stub(dark));
  const page = await context.newPage();
  page.on("pageerror", (e) => console.log(name, "PAGEERROR:", e.message));
  await page.goto("http://127.0.0.1:8824/account.html", { waitUntil: "load" });
  await page.waitForTimeout(2000);
  const m = await page.evaluate(() => {
    const r = (s) => { const el = document.querySelector(s); if (!el) return null; const b = el.getBoundingClientRect(); return { x: Math.round(b.left), y: Math.round(b.top), w: Math.round(b.width), h: Math.round(b.height) }; };
    const cs = (s, p) => { const el = document.querySelector(s); return el ? getComputedStyle(el)[p] : null; };
    return {
      doc: Math.round(document.documentElement.scrollWidth),
      viewport: window.innerWidth,
      main: r(".account-main"),
      side: r(".account-side"),
      danger: r(".account-danger"),
      danger: r(".account-danger"),
      counts: [...document.querySelectorAll(".account-nav-count")].map((e) => e.textContent),
      titles: [...document.querySelectorAll(".account-block-title")].map((e) => `${e.textContent} ${cs("#none", "x") || ""}${getComputedStyle(e).fontSize}/${getComputedStyle(e).fontFamily.split(",")[0]}`),
      savedRows: document.querySelectorAll("[data-saved]").length,
      alertRows: document.querySelectorAll("#alerts-list .alert-row").length,
      avatar: r(".account-id-mark .hero-avatar"),
      cvDrop: r("#cv-drop"),
      order: [...document.querySelectorAll(".account-nav, .account-main")]
        .map((e) => [e.className.split(" ")[0], Math.round(e.getBoundingClientRect().top)]),
      nav: r(".account-nav"),
      navLinks: [...document.querySelectorAll(".account-nav-link")]
        .map((a) => `${a.textContent.trim()}${a.classList.contains("active") ? "*" : ""}:${Math.round(a.getBoundingClientRect().height)}`),
      navScrollable: (() => { const e = document.querySelector(".account-nav-scroll"); return e ? e.scrollWidth > e.clientWidth : null; })(),
      wide: [...document.querySelectorAll("#account-body *")]
        .filter((e) => e.getBoundingClientRect().right > window.innerWidth + 1)
        .slice(0, 6).map((e) => `${e.tagName}.${e.className}`),
      tapTooSmall: [...document.querySelectorAll("#account-body button, #account-body a")]
        .filter((e) => { const b = e.getBoundingClientRect(); return b.height > 0 && b.height < 30; })
        .slice(0, 6).map((e) => `${(e.textContent || e.title).trim().slice(0, 18)}:${Math.round(e.getBoundingClientRect().height)}`),
    };
  });
  console.log(name, JSON.stringify(m, null, 1));
  await page.screenshot({ path: `account-${name}.png`, fullPage: true });
  await context.close();
  await browser.close();
}
server.kill();
