// /hero on a phone with real touch input (Chromium, CDP touch events): a
// horizontal swipe throws the logo row, a vertical swipe still scrolls.
// Run from tests/e2e:  node hero_touch.mjs
import { chromium, devices } from "@playwright/test";
import { spawn } from "node:child_process";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8833", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);

const logos = ["#ff9900", "#4285f4", "#111111", "#e4002b", "#00a4ef", "#232f3e"].map((fill, i) => ({
  domain: `c${i}.com`, n: 100 - i,
  logo_url: "data:image/svg+xml," + encodeURIComponent(`<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'><circle cx='5' cy='5' r='5' fill='${fill}'/></svg>`),
}));
const STATS = { totals: { open_jobs: 176465 }, workplace: [{ workplace: "remote", n: 25841 }], top_companies_logos: logos };

const failures = [];
const check = (name, ok, detail = "") => { console.log(`${ok ? "PASS" : "FAIL"}: ${name}  ${detail}`); if (!ok) failures.push(name); };

const browser = await chromium.launch();
const context = await browser.newContext({ ...devices["Pixel 7"] });
await context.addInitScript((stats) => {
  const real = window.fetch.bind(window);
  window.fetch = (i, init) => String(i && i.url ? i.url : i).includes("stats.json")
    ? Promise.resolve(new Response(JSON.stringify(stats), { status: 200, headers: { "Content-Type": "application/json" } }))
    : real(i, init);
}, STATS);
const page = await context.newPage();
await page.goto("http://127.0.0.1:8833/hero.html", { waitUntil: "networkidle" });
await page.waitForTimeout(800);
const cdp = await context.newCDPSession(page);
const touch = async (type, x, y) => cdp.send("Input.dispatchTouchEvent", { type, touchPoints: type === "touchEnd" ? [] : [{ x, y }] });

const speed = async () => {
  const read = () => page.evaluate(() => {
    const tr = document.getElementById("ticker-logos");
    return { x: new DOMMatrixReadOnly(getComputedStyle(tr).transform).m41, half: tr.scrollWidth / 2, t: performance.now() };
  });
  const a = await read(); await page.waitForTimeout(150); const b = await read();
  let d = b.x - a.x;
  if (d > a.half / 2) d -= a.half;
  if (d < -a.half / 2) d += a.half;
  return Math.round(d / ((b.t - a.t) / 1000));
};

const box = await page.locator(".hero-logos").boundingBox();
const y = box.y + box.height / 2;
const drift = await speed();
await touch("touchStart", 60, y);
for (let i = 1; i <= 10; i++) { await touch("touchMove", 60 + i * 28, y); await page.waitForTimeout(8); }
await touch("touchEnd");
const thrown = await speed();
await page.waitForTimeout(3000);
const settled = await speed();
check("swipe right throws the logos right", drift < 0 && thrown > Math.abs(drift) * 4, JSON.stringify({ drift, thrown }));
check("then they ease back into the drift", settled < 0 && Math.abs(settled - drift) <= Math.abs(drift) * 0.3, JSON.stringify({ drift, settled }));

const scroll0 = await page.evaluate(() => scrollY);
await touch("touchStart", 200, y);
for (let i = 1; i <= 10; i++) { await touch("touchMove", 200, y - i * 30); await page.waitForTimeout(16); }
await touch("touchEnd");
await page.waitForTimeout(600);
const scroll1 = await page.evaluate(() => scrollY);
check("a vertical swipe on the band still scrolls the page", scroll1 > scroll0 + 100, JSON.stringify({ scroll0, scroll1 }));

await browser.close();
server.kill();
console.log(failures.length ? `\n${failures.length} failed` : "\nall passed");
process.exit(failures.length ? 1 : 0);
