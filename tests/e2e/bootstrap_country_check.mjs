// The precomputed country page: used when it matches, ignored when it
// doesn't, absent without harm.
//
//   node bootstrap_country_check.mjs
//
// The API is held for five seconds on purpose. Rows that appear before
// it answers can only have come from the static file, which is the whole
// claim being tested. Without the file, country=IL waits on a Lambda
// that may be cold, which is the 14.3s this exists to remove.
import { chromium } from "playwright";

const IL_PARAMS = "country=IL&confidence=all&sort=age&dir=asc&limit=50&offset=0";
const API_DELAY_MS = 5000;

const failures = [];
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}: ${name}${ok ? "" : "  -- " + detail}`);
  if (!ok) failures.push(name);
};

// Real rows, so the render path is exercised on the shape it will see.
const live = await fetch(`https://opentechjobs.org/api/jobs?${IL_PARAMS}`).then((r) => r.json());
console.log(`fixture: ${live.jobs.length} of ${live.total} Israeli listings from the live API\n`);

const b = await chromium.launch();

async function visit({ payload, url = `https://opentechjobs.org/board?country=IL` }) {
  const ctx = await b.newContext({ viewport: { width: 1400, height: 900 }, serviceWorkers: "block" });
  const p = await ctx.newPage();
  p.on("pageerror", (e) => failures.push("pageerror: " + e.message.slice(0, 100)));
  await p.route(/\/app\.js(\?|$)/, (r) => r.fulfill({ path: "../../frontend/app.js", contentType: "application/javascript" }));
  await p.route(/\/board(\?|$)/, (r) => r.fulfill({ path: "../../frontend/board.html", contentType: "text/html" }));
  await p.route(/\/bootstrap-il\.json/, (r) =>
    payload === null
      ? r.fulfill({ status: 404, body: "" })
      : r.fulfill({ body: JSON.stringify(payload), contentType: "application/json" }));
  // Held, not blocked: the request still happens and still answers, so
  // the confirming fetch behind the render is exercised too.
  await p.route(/\/api\/jobs\?.*country=IL.*limit=50/, async (r) => {
    await new Promise((res) => setTimeout(res, API_DELAY_MS));
    await r.continue();
  });
  const t0 = Date.now();
  await p.goto(url, { waitUntil: "commit" });
  await p.waitForSelector("#jobs-body tr[data-id]", { timeout: 30_000 });
  const ms = Date.now() - t0;
  const rows = await p.locator("#jobs-body tr[data-id]").count();
  const firstId = await p.locator("#jobs-body tr[data-id]").first().getAttribute("data-id");
  await ctx.close();
  return { ms, rows, firstId };
}

const good = { params: IL_PARAMS, generated_at: new Date().toISOString(),
               jobs: { jobs: live.jobs, total: live.total, limit: 50, offset: 0 } };

{
  const r = await visit({ payload: good });
  check("the country page renders before the API answers", r.ms < API_DELAY_MS, `${r.ms}ms`);
  check("with its rows", r.rows === live.jobs.length && r.firstId === live.jobs[0].id,
    `rows=${r.rows} first=${r.firstId}`);
}

{
  // The guard that makes this safe: a file describing a different query
  // must not be rendered, however plausible its contents look.
  const wrong = { ...good, params: "confidence=all&sort=age&dir=asc&limit=50&offset=0" };
  const r = await visit({ payload: wrong });
  check("a payload naming another view is ignored", r.ms >= API_DELAY_MS, `${r.ms}ms`);
}

{
  // What production looks like until the next merge publishes the file.
  const r = await visit({ payload: null });
  check("a missing file costs nothing but the speed-up", r.ms >= API_DELAY_MS && r.rows > 0,
    `${r.ms}ms rows=${r.rows}`);
}

await b.close();
console.log();
if (failures.length) {
  console.log(`${failures.length} failed:`);
  for (const f of failures) console.log("  - " + f);
  process.exit(1);
}
console.log("all passed");
