// The three-column board, shot at the widths it changes shape at.
//
//   node board_layout_shot.mjs
//
// Runs this checkout's board.html/app.js/style.css against the live API,
// the same swap board_refresh_check.mjs makes. The one thing it fakes is
// the three facet keys the rail needs that the deployed API does not
// answer with yet (seniority, workplace and salary; see
// api/aggregates.py's compute_facets). Those numbers are not invented:
// they were measured on the box's own snapshot for this exact view on
// 2026-09-23, so the rail draws what it will draw once the API ships.
import { chromium } from "playwright";

// Measured: SELECT ... GROUP BY on the open tech rows, and
// loader/salary_range.py run over the shekel strings among them.
const SENIORITY = [
  { value: "senior", n: 47342 }, { value: "manager", n: 26399 },
  { value: "lead", n: 12258 }, { value: "staff", n: 9134 },
  { value: "principal", n: 8918 }, { value: "director", n: 6897 },
  { value: "intern", n: 6471 }, { value: "junior", n: 3845 },
  { value: "exec", n: 2949 }, { value: "mid", n: 590 },
];
const WORKPLACE = [
  { value: "remote", n: 25034 }, { value: "onsite", n: 16023 }, { value: "hybrid", n: 14104 },
];
const SALARY = { min: 12000, max: 100000, known: 2805, median: 38500 };

const SHOTS = [
  { name: "board-3col-light", w: 1440, h: 980, dark: false },
  { name: "board-3col-dark", w: 1440, h: 980, dark: true },
  { name: "board-3col-wide", w: 1800, h: 1000, dark: false },
  { name: "board-2col-1024", w: 1024, h: 900, dark: false },
  { name: "board-1col-760", w: 760, h: 900, dark: false },
  { name: "board-phone", w: 390, h: 844, dark: false },
];

const browser = await chromium.launch();
const errs = [];

for (const shot of SHOTS) {
  const page = await browser.newPage({ viewport: { width: shot.w, height: shot.h } });
  page.on("pageerror", (e) => errs.push(`${shot.name}: ${e.message}`));
  await page.addInitScript((dark) => {
    localStorage.setItem("iljobs_geo_asked", "1");
    if (dark) localStorage.setItem("iljobs_theme", "dark");
  }, shot.dark);

  await page.route(/\/app\.js(\?|$)/, (r) => r.fulfill({ path: "../../frontend/app.js", contentType: "application/javascript" }));
  await page.route(/\/style\.css(\?|$)/, (r) => r.fulfill({ path: "../../frontend/style.css", contentType: "text/css" }));
  await page.route(/\/board(\?|$)/, (r) => r.fulfill({ path: "../../frontend/board.html", contentType: "text/html" }));
  // The precomputed artifact answers the unfiltered case, so both paths
  // to a facet payload need the same three keys added.
  const addFacets = async (r) => {
    const res = await r.fetch();
    let body;
    try { body = await res.json(); } catch { return r.fulfill({ response: res }); }
    const fill = (o) => Object.assign(o, { seniority: SENIORITY, workplace: WORKPLACE, salary: SALARY });
    if (Array.isArray(body.categories)) fill(body);
    else for (const v of Object.values(body)) if (v && Array.isArray(v.categories)) fill(v);
    return r.fulfill({ response: res, body: JSON.stringify(body) });
  };
  await page.route(/\/api\/facets/, addFacets);
  await page.route(/\/facets\.json/, addFacets);

  await page.goto("https://opentechjobs.org/board", { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#jobs-body tr[data-id]", { timeout: 30000 }).catch(() => {});
  await page.waitForSelector(".rail-group", { timeout: 15000 }).catch(() => {});
  // The rail redraws when the facets land, and the ticker animates
  // forever, so settle rather than wait for network idle.
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `${shot.name}.png` });

  // The same view with a listing open, which is the state the pane was
  // built for.
  if (shot.name === "board-3col-light" || shot.name === "board-phone") {
    await page.click("#jobs-body tr[data-id]").catch(() => {});
    await page.waitForTimeout(1800);
    await page.screenshot({ path: `${shot.name}-open.png` });
  }
  // Routes still in flight when a page closes reject inside the route
  // callback and take the run down with them, which is a bug in this
  // script rather than in the board.
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.close();
}

await browser.close();
console.log(errs.length ? `page errors:\n  ${errs.join("\n  ")}` : "no page errors");
