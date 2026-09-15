// The board's count line names the view it is counting: every role, the
// roles a filter leaves, or the ones a CV matches. Serves frontend/ with
// /jobs stubbed and reads the line in each case.
// Run from tests/e2e:  node result_count.mjs
import { chromium } from "@playwright/test";
import { spawn } from "node:child_process";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8834", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);

const now = new Date().toISOString();
const jobs = [
  { id: "a", title: "DevOps Engineer", company_domain: "axonius.com", company_name: "Axonius", location: "Tel Aviv, Israel",
    posted_at: now, url: "https://example.com", skills: "Python,AWS" },
  { id: "b", title: "Backend Engineer", company_domain: "wix.com", company_name: "Wix", location: "Tel Aviv, Israel",
    posted_at: now, url: "https://example.com", skills: "Python" },
];

const init = (total, skills) => `
  try { localStorage.clear(); } catch {}
  const ok = (b) => Promise.resolve(new Response(JSON.stringify(b), { status: 200, headers: { "Content-Type": "application/json" } }));
  const real = window.fetch.bind(window);
  window.fetch = (i, init) => {
    const u = String(i && i.url ? i.url : i);
    if (/\\.(woff2|css|js|svg|png)(\\?|$)/.test(u) && !u.includes("/api/")) return real(i, init);
    if (u.includes("/jobs?")) return ok({ jobs: ${JSON.stringify(jobs)}, total: ${total}, limit: 50, offset: 0, matched_skills: ${JSON.stringify(skills)} });
    return ok({});
  };`;

const failures = [];
const check = (name, ok, detail = "") => { console.log(`${ok ? "PASS" : "FAIL"}: ${name}${ok ? "" : "  -- " + detail}`); if (!ok) failures.push(name); };

const browser = await chromium.launch();
for (const [name, query, total, skills, expected] of [
  ["nothing applied", "", 177539, [], "Showing 1–2 of 177,539 roles"],
  ["a search", "?search=devops", 3003, [], "Showing 1–2 of 3,003 matching roles"],
  ["a country filter", "?country=IL", 3003, [], "Showing 1–2 of 3,003 matching roles"],
  ["Best matches", "?skills=Python,AWS", 812, ["Python", "AWS"], "Showing 1–2 of 812 roles matching your CV"],
]) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addInitScript(init(total, skills));
  const page = await context.newPage();
  await page.goto(`http://127.0.0.1:8834/index.html${query}`, { waitUntil: "networkidle" });
  await page.waitForFunction(() => document.getElementById("result-count").textContent.trim().length > 0);
  const m = await page.evaluate(() => {
    const el = document.getElementById("result-count");
    return { text: el.textContent.replace(/\s+/g, " ").trim(), bold: [...el.querySelectorAll("b")].map((b) => b.textContent) };
  });
  check(`${name}: "${expected}"`, m.text === expected, m.text);
  check(`${name}: the range and total are the bold parts`, m.bold.length === 2 && m.bold[0] === "1–2", JSON.stringify(m.bold));
  await context.close();
}
await browser.close();
server.kill();
console.log(failures.length ? `\n${failures.length} failed` : "\nall passed");
process.exit(failures.length ? 1 : 0);
