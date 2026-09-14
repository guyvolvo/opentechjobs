// /hero: desktop and phone, light and dark. Checks the tickers carry real
// numbers and move in opposite directions, the wordmark fits, nothing
// scrolls sideways, and the feature rows reveal on scroll. Screenshots.
// Run from tests/e2e:  node hero_check.mjs
import { chromium, devices } from "@playwright/test";
import { spawn } from "node:child_process";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8832", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);

const STATS = {
  totals: { open_jobs: 176465, companies_hiring: 4134 },
  throughput: { new_jobs_24h: 38776, closed_jobs_24h: 6727 },
  age: { median_open_days: 21.3 },
  location: { israel: 2984 },
  top_skills: [{ skill: "python", n: 19336 }, { skill: "llm", n: 16476 }, { skill: "aws", n: 14835 }, { skill: "machine learning", n: 12883 }],
};

const failures = [];
const check = (name, ok, detail = "") => { console.log(`${ok ? "PASS" : "FAIL"}: ${name}${ok ? "" : "  -- " + detail}`); if (!ok) failures.push(name); };

const browser = await chromium.launch();
for (const [label, device] of [["desktop", { viewport: { width: 1440, height: 900 } }], ["phone", devices["Pixel 7"]]]) {
  for (const theme of ["light", "dark"]) {
    const context = await browser.newContext({ ...device });
    await context.addInitScript(({ theme, stats }) => {
      try { localStorage.setItem("iljobs_theme", theme); } catch {}
      const real = window.fetch.bind(window);
      window.fetch = (i, init) => String(i && i.url ? i.url : i).includes("stats.json")
        ? Promise.resolve(new Response(JSON.stringify(stats), { status: 200, headers: { "Content-Type": "application/json" } }))
        : real(i, init);
    }, { theme, stats: STATS });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto("http://127.0.0.1:8832/hero.html", { waitUntil: "networkidle" });
    await page.waitForTimeout(700);
    const tag = `${label}/${theme}`;

    const m = await page.evaluate(() => {
      const x = (id) => new DOMMatrixReadOnly(getComputedStyle(document.getElementById(id)).transform).m41;
      const word = document.querySelector(".hero-word").getBoundingClientRect();
      return { overflow: document.documentElement.scrollWidth > innerWidth, wordRight: word.right, vw: innerWidth,
        top0: x("ticker-top"), bottom0: x("ticker-bottom"),
        topText: document.getElementById("ticker-top").textContent.slice(0, 60),
        bottomText: document.getElementById("ticker-bottom").textContent.slice(0, 80),
        cta: document.getElementById("cta-count").textContent, theme: document.documentElement.getAttribute("data-theme") };
    });
    await page.screenshot({ path: `hero-${label}-${theme}-top.png` });
    await page.waitForTimeout(1500);
    const later = await page.evaluate(() => {
      const x = (id) => new DOMMatrixReadOnly(getComputedStyle(document.getElementById(id)).transform).m41;
      return { top1: x("ticker-top"), bottom1: x("ticker-bottom") };
    });
    check(`${tag}: no sideways scroll`, !m.overflow);
    check(`${tag}: the wordmark fits the page`, m.wordRight <= m.vw, `${m.wordRight} > ${m.vw}`);
    check(`${tag}: tickers carry live numbers`, m.topText.includes("176,465") && m.bottomText.includes("6,727") && /LLM|Python/.test(m.bottomText), `${m.topText} | ${m.bottomText}`);
    check(`${tag}: top ticker moves right, bottom moves left`, later.top1 > m.top0 && later.bottom1 < m.bottom0, JSON.stringify({ ...m, ...later }));
    check(`${tag}: the search link names the count`, m.cta === "176,465 open jobs", m.cta);
    if (theme === "dark") check(`${tag}: dark theme applied`, m.theme === "dark", String(m.theme));

    const feature = page.locator(".hero-feature").nth(2);
    const before = await feature.evaluate((el) => getComputedStyle(el.querySelector("h2")).opacity);
    await feature.scrollIntoViewIfNeeded();
    await page.waitForTimeout(1100);
    const after = await feature.evaluate((el) => ({ in: el.classList.contains("in"), opacity: getComputedStyle(el.querySelector("h2")).opacity }));
    check(`${tag}: a feature row reveals when scrolled to`, before === "0" && after.in && after.opacity === "1", JSON.stringify({ before, after }));
    await page.evaluate(() => window.scrollTo(0, document.querySelector(".hero-features").offsetTop - 20));
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `hero-${label}-${theme}-features.png` });
    check(`${tag}: no page errors`, errors.length === 0, errors.join(" | "));
    await context.close();
  }
}
await browser.close();
server.kill();
console.log(failures.length ? `\n${failures.length} failed` : "\nall passed");
process.exit(failures.length ? 1 : 0);
