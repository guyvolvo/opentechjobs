// The board shot at the phone mockup's own screen aspect (804x1648 =
// 0.4879), so it fills the glass instead of leaving paper at the foot.
import { chromium } from "@playwright/test";
const COMPANIES = "apple.com,aws.amazon.com,nvidia.com,microsoft.com,stripe.com";
const QUERY = `?company=${COMPANIES}&department=${encodeURIComponent("Software Engineering")}`;
const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 402, height: 824 },
  deviceScaleFactor: 3,
  hasTouch: true,
});
await context.addInitScript(() => {
  try {
    localStorage.setItem("iljobs_theme", "light");
    localStorage.setItem("iljobs_stats_collapsed", "1");
  } catch {}
});
const page = await context.newPage();
await page.goto(`https://opentechjobs.org/${QUERY}`, { waitUntil: "domcontentloaded", timeout: 60000 });
await page.waitForSelector("#jobs-body tr", { timeout: 60000 });
const atRest = () => page.waitForFunction(
  () => document.getElementById("status-text")?.textContent.trim() === "Live",
  null, { timeout: 20000 }).then(() => true).catch(() => false);
let settled = await atRest();
for (let i = 0; !settled && i < 8; i++) {
  await page.reload({ waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForSelector("#jobs-body tr", { timeout: 60000 });
  settled = await atRest();
}
await page.waitForTimeout(2500);
await page.screenshot({ path: "board-phone-tall.png" });
const m = await page.evaluate(() => ({
  status: document.getElementById("status-text")?.textContent.trim(),
  collapsed: document.documentElement.classList.contains("stats-collapsed"),
}));
console.log(JSON.stringify(m), settled ? "" : "(pipeline still updating)");
await browser.close();
