// The board as a real iPhone draws it, for /hero's phone product shot.
//
// The old capture was 402x824 and went into a screen hole of 804x1748
// (0.4600). Compositing fitted the wider picture by cropping, so the search
// box, Filters and the "Showing 1-50" line ran under the right bezel and
// the shot read as a screenshot taken elsewhere and trimmed.
//
// The glass is exactly twice an iPhone 16 Pro's screen, 402x874 CSS px. Of
// that screen Safari gives a page everything below the 59pt status bar, so
// the page is shot at 402x815 and compose_phone.py paints the status strip
// in the page's own paper. That is why the topbar's API link and GitHub
// mark are no longer buried under the Dynamic Island: on a real phone the
// island never sits on page content. Three device pixels to the CSS pixel
// throughout, so the board lands in the glass at native size.
//
// WebKit, not Chromium: this is the engine an iPhone actually runs, and
// with the iPhone 16 Pro descriptor it brings the mobile user agent, touch
// and the mobile viewport rules with it.
// Run from tests/e2e:  node phone_device_shot.mjs
import { webkit, devices } from "@playwright/test";

// The same five companies the other device shots use, so the phone and the
// laptop in the card show the same board rather than two different ones.
const COMPANIES = "apple.com,aws.amazon.com,nvidia.com,microsoft.com,stripe.com";
const QUERY = `?company=${COMPANIES}&department=${encodeURIComponent("Software Engineering")}`;
const OUT = "board-phone-device.png";
const SCREEN = { width: 402, height: 874 };
const STATUS_BAR = 59;

const iphone = devices["iPhone 16 Pro"];
const browser = await webkit.launch();
const context = await browser.newContext({
  ...iphone,
  // The descriptor's 402x681 is what is left after Safari's address bar and
  // toolbar. There is no browser in the product shot, so the page gets the
  // screen less the status bar, which is also what makes it fit the glass.
  viewport: { width: SCREEN.width, height: SCREEN.height - STATUS_BAR },
});
await context.addInitScript(() => {
  try {
    // Always the light board, whatever the reader's theme: this is a
    // picture of the product, not a mirror of their settings.
    localStorage.setItem("iljobs_theme", "light");
    // The statistics column starts collapsed, so the shot is the board
    // itself rather than half a sidebar. index.html reads this before
    // first paint, so there is no open-then-collapse flash.
    localStorage.setItem("iljobs_stats_collapsed", "1");
  } catch {}
});
const page = await context.newPage();
await page.goto(`https://opentechjobs.org/${QUERY}`, { waitUntil: "domcontentloaded", timeout: 60000 });
// A row exists long before it says anything: the board puts skeleton rows
// in first, and waiting on "#jobs-body tr" caught those, which is how a
// shot of grey bars got composited into the frame. This waits for real
// titles with no skeleton left beside them.
const filled = () => page.waitForFunction(() => {
  const body = document.getElementById("jobs-body");
  return body && body.querySelectorAll(".job-card-title").length >= 5
    && body.querySelectorAll(".skeleton").length === 0;
}, null, { timeout: 60000 });
await filled();

// "Updating" while the pipeline is mid-scrape, "Live" once it settles.
// That is the board's real state, not a render race, so this waits for a
// quiet moment and reloads between tries rather than faking the text.
const atRest = () => page.waitForFunction(
  () => document.getElementById("status-text")?.textContent.trim() === "Live",
  null, { timeout: 20000 }).then(() => true).catch(() => false);
let settled = await atRest();
for (let attempt = 0; !settled && attempt < 8; attempt++) {
  await page.reload({ waitUntil: "domcontentloaded", timeout: 60000 });
  await filled();
  settled = await atRest();
}
if (!settled) console.log("   pipeline still updating after retries");
await page.waitForTimeout(2500);

await page.screenshot({ path: OUT });
const m = await page.evaluate(() => ({
  status: document.getElementById("status-text")?.textContent.trim(),
  collapsed: document.documentElement.classList.contains("stats-collapsed"),
  theme: document.documentElement.getAttribute("data-theme") || "light",
  dpr: devicePixelRatio,
  vw: innerWidth,
  vh: innerHeight,
  // Nothing may stick out sideways, or the frame would clip it again.
  overflow: document.documentElement.scrollWidth - innerWidth,
  rows: document.querySelectorAll("#jobs-body tr").length,
  titles: document.querySelectorAll("#jobs-body .job-card-title").length,
}));
console.log(OUT, JSON.stringify(m));
await browser.close();
