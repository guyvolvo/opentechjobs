// iPhone (WebKit, Safari's engine) against Android (Chromium) on the live
// site: the same pages and interactions on both, with screenshots, page
// errors and the measurements that differ between the two engines.
//
// WebKit on a desktop is not iOS: it does not reproduce Safari's zoom on
// focusing a small input or its address-bar viewport height. Those are
// checked in code instead. What this does catch is everything the engine
// itself renders or runs differently.
//
// Run from tests/e2e:  node ios-vs-android.mjs [baseUrl] [outDir]
import { chromium, devices, webkit } from "@playwright/test";
import { mkdirSync } from "node:fs";

const BASE = process.argv[2] || "https://opentechjobs.org";
const OUT = process.argv[3] || "ios-vs-android";
mkdirSync(OUT, { recursive: true });

const RIGS = [
  { name: "iphone", browser: webkit, device: devices["iPhone 14"] },
  { name: "android", browser: chromium, device: devices["Pixel 7"] },
];

const report = {};

async function measure(page) {
  return page.evaluate(() => {
    const vw = innerWidth;
    const wide = [...document.querySelectorAll("body *")]
      .filter((el) => el.offsetParent !== null && el.getBoundingClientRect().right > vw + 1)
      .slice(0, 5)
      .map((el) => `${el.tagName.toLowerCase()}.${String(el.className || "").split(" ")[0]} right=${Math.round(el.getBoundingClientRect().right)}`);
    const inputs = [...document.querySelectorAll("input, select, textarea")]
      .filter((el) => el.offsetParent !== null && !["checkbox", "radio", "hidden"].includes(el.type))
      .map((el) => `${el.id || el.name || el.className || el.tagName}: ${getComputedStyle(el).fontSize}`);
    return { vw, scrollWidth: document.documentElement.scrollWidth, overflowing: wide, inputs };
  });
}

for (const rig of RIGS) {
  const browser = await rig.browser.launch();
  const context = await browser.newContext({ ...rig.device, colorScheme: "dark" });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`pageerror: ${String(e.message).slice(0, 160)}`));
  page.on("console", (m) => { if (m.type() === "error") errors.push(`console: ${m.text().slice(0, 160)}`); });
  const r = (report[rig.name] = { steps: {} });
  const shot = (name, opts = {}) => page.screenshot({ path: `${OUT}/${rig.name}-${name}.png`, ...opts });

  // Board, top of page.
  await page.goto(`${BASE}/?country=IL`, { waitUntil: "networkidle", timeout: 60000 });
  await page.waitForSelector("#jobs-body tr", { timeout: 30000 });
  await shot("01-board-top");
  r.steps.board = await measure(page);

  // First rows.
  await page.locator("#jobs-body tr").first().scrollIntoViewIfNeeded();
  await page.evaluate(() => scrollBy(0, -80));
  await page.waitForTimeout(400);
  await shot("02-rows");

  // Filters panel.
  const toggle = page.locator(".filters-toggle");
  if (await toggle.isVisible()) {
    await page.evaluate(() => scrollTo(0, 0));
    await toggle.click();
    await page.waitForTimeout(400);
    await shot("03-filters-open");
    r.steps.filters = await measure(page);
    const company = page.locator("#ms-company .ms-toggle");
    if (await company.isVisible()) {
      await company.click();
      await page.locator("#ms-company .ms-search").fill("micr");
      await page.waitForTimeout(1500);
      await shot("04-company-search");
      r.steps.companySearch = await page.locator("#ms-company .ms-option").allTextContents();
      await company.click();
    }
    await toggle.click().catch(() => {});
  }

  // Job sheet: open the first row, measure whether its top and bottom
  // controls are inside the viewport.
  await page.locator("#jobs-body tr").first().click();
  await page.waitForTimeout(1200);
  await shot("05-sheet");
  r.steps.sheet = await page.evaluate(() => {
    const sheet = document.querySelector(".job-detail");
    const box = sheet && sheet.getBoundingClientRect();
    const apply = document.querySelector(".job-detail-apply")?.getBoundingClientRect();
    const close = document.querySelector(".job-detail-close, [data-close-detail], .job-detail button[aria-label*='Close' i]")?.getBoundingClientRect();
    return box && { innerHeight, sheetTop: Math.round(box.top), sheetHeight: Math.round(box.height),
      applyVisible: !!apply && apply.top >= 0 && apply.bottom <= innerHeight,
      closeVisible: !!close && close.top >= 0 && close.bottom <= innerHeight };
  });
  await page.keyboard.press("Escape").catch(() => {});

  // Other pages.
  for (const [name, path] of [["06-account", "/account"], ["07-explore", "/stats"], ["08-404", "/no-such-page-qa"]]) {
    await page.goto(`${BASE}${path}`, { waitUntil: "networkidle", timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(1500);
    await shot(name);
    r.steps[name] = await measure(page);
  }

  r.errors = errors;
  await browser.close();
}

console.log(JSON.stringify(report, null, 1));
