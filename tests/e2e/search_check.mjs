// The search box: ranking, highlighting, and what a search that finds
// nothing says. Runs against the live site with this checkout's app.js,
// style.css and board.html swapped in.
//
//   node search_check.mjs
import { chromium } from "playwright";
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1400, height: 950 } });
const errs = []; p.on("pageerror", (e) => errs.push(e.message));
await p.addInitScript(() => localStorage.setItem("iljobs_geo_asked", "1"));
for (const [re, path, type] of [[/\/app\.js(\?|$)/, "../../frontend/app.js", "application/javascript"],
                                [/\/style\.css(\?|$)/, "../../frontend/style.css", "text/css"],
                                [/\/board(\?|$)/, "../../frontend/board.html", "text/html"]]) {
  await p.route(re, (r) => r.fulfill({ path, contentType: type }));
}
const seen = [];
await p.route(/\/api\/jobs\?/, async (r) => { seen.push(r.request().url()); return r.fulfill({ response: await r.fetch() }); });
const settle = () => p.waitForFunction(() => !document.querySelector("#jobs-body .skeleton"), null, { timeout: 30000 }).then(() => p.waitForTimeout(2500));

await p.goto("https://opentechjobs.org/board?search=devops");
await settle();
console.log("highlight in title:", await p.evaluate(() => document.querySelector("#jobs-body mark")?.outerHTML), "| marks:", await p.$$eval("#jobs-body mark", (m) => m.length));
console.log("sort options:", await p.$$eval("#f-sort option", (o) => o.map((x) => x.value + (x.hidden ? "(hidden)" : ""))));

// Relevance sort
if (await p.locator("#filters-toggle").isVisible()) await p.click("#filters-toggle");
await p.selectOption("#f-sort", "relevance:asc"); await settle();
console.log("relevance url:", p.url().includes("sort=relevance"), "| request:", seen.at(-1)?.includes("sort=relevance"));
const titles = await p.$$eval("#jobs-body .job-card-title a", (a) => a.slice(0, 5).map((x) => x.textContent.trim()));
console.log("top rows:", titles.map((t) => t.slice(0, 40)));

// Zero results: explanation + broadening
await p.fill("#f-search", "devops zebra"); await settle();
const empty = await p.evaluate(() => document.getElementById("jobs-empty").textContent.replace(/\s+/g, " ").trim());
console.log("empty state:", empty);
await p.click("[data-search-any]"); await settle();
console.log("after 'any':", p.url().includes("search_mode=any"), "| count:", await p.textContent("#result-count"));
console.log("notice:", (await p.textContent("#search-notice")).replace(/\s+/g, " ").trim());
await p.click("[data-search-all]"); await settle();
console.log("after 'require all':", !p.url().includes("search_mode=any"), "| empty shown:", await p.isVisible("#jobs-empty"));
await p.click("[data-drop-term]"); await settle();
console.log("after dropping a term:", await p.inputValue("#f-search"), "| count:", await p.textContent("#result-count"));

// Ten-term cap
await p.fill("#f-search", "a b c d e f g h i j k l"); await settle();
console.log("cap notice:", (await p.textContent("#search-notice")).replace(/\s+/g, " ").trim().slice(0, 120), "| marks for 1-letter words:", await p.$$eval("#jobs-body mark", (m) => m.length));
await p.unrouteAll({ behavior: "ignoreErrors" });
await p.screenshot({ path: "C:/Users/admin/AppData/Local/Temp/claude/C--Users-admin-Desktop-Test-scripts/27e70a8e-ff66-4fcc-b7c3-a924176cd57a/scratchpad/search-empty.png", clip: { x: 0, y: 60, width: 1000, height: 420 } });
console.log("errors:", errs);
await b.close();
