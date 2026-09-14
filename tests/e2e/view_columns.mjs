// Show all and Best matches must put a row's columns in the same place.
// Serves frontend/ locally with /jobs stubbed, loads both views at desktop
// width, and compares where the logo, title, salary and age land.
// Run from tests/e2e:  node view_columns.mjs
import { chromium } from "@playwright/test";
import { spawn } from "node:child_process";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8830", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);

const MINE = ["Python", "AWS", "Linux", "Terraform", "Azure", "Docker", "Ansible", "Grafana", "Kubernetes", "Git", "Bash", "DevOps", "CI/CD"];
const now = new Date().toISOString();
const jobs = [
  { id: "a", title: "Senior DevEx/DevOps Engineer", company_domain: "axonius.com", company_name: "Axonius", department: "R&D",
    seniority: "senior", location: "Tel Aviv, Israel", posted_at: now, url: "https://example.com",
    skills: [...MINE, "GCP", "Kubernetes"].join(","), salary_text: "₪35K–45K", salary_source: "table" },
  { id: "b", title: "System Linux Administrator", company_domain: "ceragon.com", company_name: "Ceragon", department: "IT",
    location: "Rosh Ha'ayin (Hybrid)", posted_at: now, url: "https://example.com", skills: "Linux,DevOps,SQL,Oracle,Puppet" },
];

const init = (skills) => `
  try { localStorage.setItem("iljobs_theme", "dark"); } catch {}
  const ok = (b) => Promise.resolve(new Response(JSON.stringify(b), { status: 200, headers: { "Content-Type": "application/json" } }));
  const real = window.fetch.bind(window);
  window.fetch = (i, init) => {
    const u = String(i && i.url ? i.url : i);
    if (/\\.(woff2|css|js|svg|png)(\\?|$)/.test(u) && !u.includes("/api/")) return real(i, init);
    if (u.includes("/jobs?")) return ok({ jobs: ${JSON.stringify(jobs)}, total: 2, limit: 50, offset: 0, matched_skills: ${JSON.stringify(skills)} });
    return ok({});
  };`;

const positions = {};
const browser = await chromium.launch();
for (const [view, query, skills] of [["show-all", "", []], ["best-matches", `?skills=${MINE.join(",")}`, MINE]]) {
  const context = await browser.newContext({ viewport: { width: 1465, height: 900 } });
  await context.addInitScript(init(skills));
  const page = await context.newPage();
  await page.goto(`http://127.0.0.1:8830/index.html${query}`, { waitUntil: "networkidle" });
  await page.waitForSelector("#jobs-body tr");
  await page.waitForTimeout(800);
  positions[view] = await page.evaluate(() => [...document.querySelectorAll("#jobs-body tr")].map((tr) => {
    const x = (sel) => { const el = tr.querySelector(sel); return el ? Math.round(el.getBoundingClientRect().left) : null; };
    return { id: tr.dataset.id, star: x(".star-btn"), logo: x(".company-logo"), title: x(".job-card-title"),
      salary: x(".job-salary-col"), age: x("td.age-cell") };
  }));
  await page.screenshot({ path: `columns-${view}.png`, clip: { x: 0, y: 120, width: 1465, height: 420 } });
  await context.close();
}
await browser.close();
server.kill();

const same = positions["show-all"].every((row, i) => {
  const other = positions["best-matches"][i];
  return ["star", "logo", "title", "salary", "age"].every((k) => row[k] === other[k]);
});
console.log(JSON.stringify(positions, null, 1));
console.log(same ? "PASS: every column lands in the same place in both views" : "FAIL: columns differ between the views");
process.exit(same ? 0 : 1);
