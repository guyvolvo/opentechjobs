// The board for /hero's devices, filtered to companies people know, so
// the product shot shows real jobs at Apple, AWS and Nvidia rather than
// whatever happens to be newest. 16:10 for the MacBook, phone-sized for
// the iPhone, light and dark each.
// Run from tests/e2e:  node board_shot.mjs
import { chromium, devices } from "@playwright/test";
const COMPANIES = "apple.com,aws.amazon.com,nvidia.com,microsoft.com,stripe.com";
// Engineering roles at those companies rather than whatever is newest: a
// retail "Operations Expert" at Apple says nothing about a tech board.
const QUERY = `?company=${COMPANIES}&department=${encodeURIComponent("Software Engineering")}`;
const browser = await chromium.launch();
for (const [name, opts] of [
  ["desktop", { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 }],
  ["phone", { ...devices["iPhone 14"], isMobile: false, hasTouch: true }],
]) {
  for (const theme of ["light", "dark"]) {
    const context = await browser.newContext(opts);
    await context.addInitScript((t) => {
      try {
        localStorage.setItem("iljobs_theme", t);
        // The statistics column starts collapsed, so the shot is the board
        // itself rather than half a sidebar. index.html reads this before
        // first paint, so there is no open-then-collapse flash.
        localStorage.setItem("iljobs_stats_collapsed", "1");
      } catch {}
    }, theme);
    const page = await context.newPage();
    await page.goto(`https://opentechjobs.org/${QUERY}`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForSelector("#jobs-body tr", { timeout: 60000 });
    // "Updating" while the pipeline is mid-scrape, "Live" once it settles.
    // That is the board's real state, not a render race, so this waits for
    // a quiet moment and reloads between tries rather than faking the text.
    const atRest = () => page.waitForFunction(
      () => document.getElementById("status-text")?.textContent.trim() === "Live",
      null, { timeout: 20000 }).then(() => true).catch(() => false);
    let settled = await atRest();
    for (let attempt = 0; !settled && attempt < 8; attempt++) {
      await page.reload({ waitUntil: "domcontentloaded", timeout: 60000 });
      await page.waitForSelector("#jobs-body tr", { timeout: 60000 });
      settled = await atRest();
    }
    if (!settled) console.log("   pipeline still updating after retries");
    await page.waitForTimeout(2500);
    const status = await page.evaluate(() => ({
      status: document.getElementById("status-text")?.textContent.trim(),
      collapsed: document.documentElement.classList.contains("stats-collapsed"),
    }));
    console.log("  ", JSON.stringify(status));
    const shown = await page.evaluate(() =>
      [...document.querySelectorAll("#jobs-body tr")].slice(0, 5).map((tr) => [
        tr.querySelector(".job-card-title")?.textContent.trim().slice(0, 44),
        tr.querySelector(".job-meta")?.textContent.trim().split("·")[0].trim(),
      ].join(" @ ")));
    await page.screenshot({ path: `board-${name}-${theme}.png` });
    console.log(name, theme, JSON.stringify(shown));
    await context.close();
  }
}
await browser.close();
