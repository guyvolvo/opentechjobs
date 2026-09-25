// How long a listing takes to open, as a person sees it: from the click
// on a row to the description replacing its skeleton. Against the live
// site, with /api/* served either as deployed (CloudFront to the box) or
// rerouted straight at the tunnel hostname, to price the CloudFront hop.
//
//   node tests/e2e/desc_latency_probe.mjs site
//   node tests/e2e/desc_latency_probe.mjs tunnel
import { chromium } from "playwright";

const mode = process.argv[2] || "site";
const rowsToOpen = Number(process.argv[3] || 8);

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

if (mode === "tunnel") {
  await page.route(
    (u) => u.hostname === "opentechjobs.org" && u.pathname.startsWith("/api/"),
    async (route) => {
      const u = new URL(route.request().url());
      u.hostname = "box.opentechjobs.org";
      const r = await route.fetch({ url: u.toString() });
      await route.fulfill({ response: r });
    },
  );
}

await page.goto("https://opentechjobs.org/board?country=IL", { waitUntil: "networkidle" });
await page.waitForSelector("tr[data-id]");
const rows = await page.$$("tr[data-id]");

const times = [];
for (let i = 0; i < rowsToOpen && i * 3 < rows.length; i++) {
  const row = rows[i * 3];
  const t0 = Date.now();
  await row.click();
  await page.waitForSelector(".job-detail-description:not(.skeleton-desc)", { timeout: 20000 });
  times.push(Date.now() - t0);
  await page.waitForTimeout(250);
}
const sorted = [...times].sort((a, b) => a - b);
const median = sorted[Math.floor(sorted.length / 2)];
console.log(`${mode}: ${times.join(" ")} ms; median ${median} ms, max ${sorted[sorted.length - 1]} ms`);
await browser.close();
