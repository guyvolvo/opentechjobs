// The three vertical gaps around the ticker band: bullets -> numbers,
// numbers -> logos, logos -> the product card.
import { chromium, devices } from "@playwright/test";
import { spawn } from "node:child_process";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8844", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);
const STATS = {
  totals: { open_jobs: 177969, companies_hiring: 4203 },
  workplace: [{ workplace: "remote", n: 26032 }],
  top_companies_logos: ["#ff9900", "#4285f4", "#111", "#e4002b", "#00a4ef", "#232f3e"].map((fill, i) => ({
    domain: `c${i}.com`, name: `C${i}`, n: 100 - i,
    logo_url: "data:image/svg+xml," + encodeURIComponent(`<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'><circle cx='5' cy='5' r='5' fill='${fill}'/></svg>`),
  })),
};
const browser = await chromium.launch();
for (const [label, opts] of [["desktop", { viewport: { width: 1440, height: 900 } }], ["phone", devices["Pixel 7"]]]) {
  const context = await browser.newContext({ ...opts });
  await context.addInitScript((stats) => {
    const real = window.fetch.bind(window);
    window.fetch = (i, init) => String(i && i.url ? i.url : i).includes("stats.json")
      ? Promise.resolve(new Response(JSON.stringify(stats), { status: 200, headers: { "Content-Type": "application/json" } }))
      : real(i, init);
  }, STATS);
  const page = await context.newPage();
  await page.goto("http://127.0.0.1:8844/hero.html", { waitUntil: "networkidle" });
  await page.waitForTimeout(700);
  const m = await page.evaluate(() => {
    const r = (sel) => document.querySelector(sel).getBoundingClientRect();
    const proof = document.querySelector(".hero-proof li:last-child").getBoundingClientRect();
    const nums = r(".hero-ticker.to-right");
    const logos = r(".hero-logos");
    const card = r(".hero-showcase");
    const block = r(".hero-block");
    const cs = getComputedStyle(document.querySelector(".hero-block"));
    return {
      above: Math.round(nums.top - proof.bottom),
      between: Math.round(logos.top - nums.bottom),
      below: Math.round(card.top - logos.bottom),
      padTop: cs.paddingTop, padBottom: cs.paddingBottom, rowGap: cs.rowGap,
      blockTop: Math.round(block.top - proof.bottom),
      cardGap: Math.round(card.top - block.bottom),
    };
  });
  console.log(label, JSON.stringify(m));
  await context.close();
}
await browser.close();
server.kill();
