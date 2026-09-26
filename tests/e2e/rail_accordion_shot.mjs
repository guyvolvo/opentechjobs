// The filter rail as an accordion, working copy over the live site.
// node tests/e2e/rail_accordion_shot.mjs
import { chromium } from "playwright";
const browser = await chromium.launch();
const errs = [];
async function shot(name, url, { theme = "light", width = 1440, setup } = {}) {
  const page = await browser.newPage({ viewport: { width, height: width < 800 ? 844 : 1000 } });
  page.on("pageerror", (e) => errs.push(`${name}: ${e.message}`));
  await page.addInitScript((t) => { localStorage.setItem("iljobs_geo_asked", "1"); localStorage.setItem("iljobs_theme", t); localStorage.removeItem("iljobs_rail_open"); }, theme);
  await page.route(/\/app\.js(\?|$)/, (r) => r.fulfill({ path: "../../frontend/app.js", contentType: "application/javascript" }));
  await page.route(/\/style\.css(\?|$)/, (r) => r.fulfill({ path: "../../frontend/style.css", contentType: "text/css" }));
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForSelector(".rail-acc");
  if (setup) await setup(page);
  await page.waitForTimeout(600);
  const state = await page.$$eval(".rail-acc", (els) => els.map((e) => `${e.dataset.acc}:${e.classList.contains("open") ? "open" : "closed"}:${e.querySelector(".rail-acc-sum")?.textContent || ""}`));
  console.log(name, state.join(" | "));
  const rail = await page.$(width < 800 ? "#filter-rail" : ".rail-groups");
  await rail.screenshot({ path: `rail-${name}.png` });
  await page.close();
}
await shot("light", "https://opentechjobs.org/board?country=IL");
await shot("dark-filtered", "https://opentechjobs.org/board?country=IL&department=Infrastructure&seniority=senior&salary_min=32000&salary_known=1", { theme: "dark",
  setup: async (p) => { await p.evaluate(() => document.documentElement.setAttribute("data-theme", "dark")); await p.click('.rail-acc[data-acc="pay"] .rail-acc-head'); } });
await shot("phone", "https://opentechjobs.org/board?country=IL&workplace=hybrid", { width: 390,
  setup: async (p) => { await p.click("#rail-toggle"); await p.waitForTimeout(500); } });
console.log(errs.length ? errs.join("\n") : "no page errors");
await browser.close();
