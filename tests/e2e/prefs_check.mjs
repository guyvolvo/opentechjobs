// The Preferences section on /account, with the working copy of the
// frontend swapped in over the live site and the account API faked, so
// it runs signed in without a real session. Checks that the rows paint
// from the profile, that a row's Save sends the whole profile with the
// new fields, and that a new alert starts from the preferences.
// Screenshots at 1440 and 390, one with the cadence row open.
//
//   node tests/e2e/prefs_check.mjs
import { chromium } from "playwright";

const SUB = "fa0302ac-20c1-70c3-23da-d6734f299216";
const EMAIL = "guyvoloshin@gmail.com";
const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const now = Math.floor(Date.now() / 1000);
const ID_TOKEN = [b64({ alg: "RS256", typ: "JWT" }), b64({ sub: SUB, email: EMAIL, iat: now, exp: now + 3600 }), "sig"].join(".");
const SKILLS = ["Python", "AWS", "Terraform"];
const PROFILE = { skills: SKILLS, seniority: null, workplace: ["remote", "hybrid"], israel_only: true,
                  country: ["IL"], city: [], cadence: "daily", digest_time: "09:00", digest_tz: "Asia/Jerusalem", digest_day: 0 };

const failures = [];
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}: ${name}${ok ? "" : "  -- " + detail}`);
  if (!ok) failures.push(name);
};

const browser = await chromium.launch();
for (const width of [1440, 390]) {
  const page = await browser.newPage({ viewport: { width, height: width < 800 ? 844 : 1000 } });
  const errs = [];
  page.on("pageerror", (e) => errs.push(e.message));
  await page.addInitScript((tok) => {
    localStorage.setItem("iljobs_auth_tokens", JSON.stringify({ id_token: tok, access_token: tok, refresh_token: "r" }));
    localStorage.setItem("iljobs_geo_asked", "1");
  }, ID_TOKEN);
  const json = (r, body) => r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  const LOCAL_JS = ["app", "account", "auth", "signin_dialog", "cv_skills", "mobile_nav"];
  await page.route((u) => LOCAL_JS.some((n) => u.pathname === `/${n}.js`), (r) =>
    r.fulfill({ path: `../../frontend${new URL(r.request().url()).pathname}`, contentType: "application/javascript" }));
  await page.route(/\/style\.css(\?|$)/, (r) => r.fulfill({ path: "../../frontend/style.css", contentType: "text/css" }));
  await page.route(/\/account(\?|$)/, (r) => r.fulfill({ path: "../../frontend/account.html", contentType: "text/html" }));

  let putBody = null;
  await page.route((u) => u.pathname === "/api/me/profile", (r) => {
    if (r.request().method() === "PUT") {
      putBody = JSON.parse(r.request().postData() || "{}");
      return json(r, { profile: putBody });
    }
    return json(r, { profile: PROFILE, options: { skills: SKILLS, seniority: [], workplace: ["remote", "hybrid", "onsite"], cadence: ["instant", "daily", "weekly"] },
                     skill_spec: { labels: SKILLS, terms: SKILLS.map((s) => [s, [s.toLowerCase()]]) } });
  });
  await page.route((u) => u.pathname === "/api/me/alerts", (r) => json(r, { alerts: [] }));
  await page.route((u) => u.pathname === "/api/me/saved", (r) => json(r, { saved: [] }));

  await page.goto("https://opentechjobs.org/account#preferences", { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#preferences.is-active", { timeout: 20000 });
  // The location options come from the live facets; give them a moment.
  await page.waitForTimeout(3500);

  const state = await page.evaluate(() => ({
    title: document.getElementById("acct-title")?.textContent,
    status: document.getElementById("acct-status")?.textContent,
    workplace: document.getElementById("pref-sum-workplace")?.textContent,
    location: document.getElementById("pref-sum-location")?.textContent,
    cadence: document.getElementById("pref-sum-cadence")?.textContent,
    matches: document.getElementById("profile-matches")?.getAttribute("href"),
    openEditors: document.querySelectorAll("#preferences .pref-editor:not([hidden])").length,
  }));
  console.log(`${width}px`, JSON.stringify(state));
  check(`${width}: section title`, state.title === "Preferences", state.title);
  check(`${width}: status line summarises`, /Remote, Hybrid · 1 place · daily at 09:00/.test(state.status || ""), state.status);
  check(`${width}: workplace summary`, state.workplace === "Remote, Hybrid", state.workplace);
  check(`${width}: location summary names Israel`, /Israel/.test(state.location || ""), state.location);
  check(`${width}: cadence summary`, /^Daily at 09:00, Asia\/Jerusalem \(GMT\+0[23]:00\)$/.test(state.cadence || ""), state.cadence);
  check(`${width}: nothing open at first`, state.openEditors === 0, String(state.openEditors));
  check(`${width}: matches link carries country`, /country=IL/.test(state.matches || ""), state.matches);

  await page.screenshot({ path: `prefs-${width}.png`, fullPage: width < 800 });

  // Open the cadence row, pick weekly on Friday at 18:30 New York, save.
  await page.click("#pref-item-cadence .pref-head");
  const opened = await page.evaluate(() => ({
    open: document.getElementById("pref-item-cadence").classList.contains("open"),
    checked: document.querySelector('input[name="pref-cadence"]:checked')?.value,
    dailyTime: document.getElementById("pref-time-daily")?.value,
    dailyLive: !document.getElementById("pref-time-daily")?.disabled,
    weeklyLive: !document.getElementById("pref-time-weekly")?.disabled,
    zones: document.getElementById("pref-tz-daily")?.options.length,
  }));
  check(`${width}: cadence row opens`, opened.open, JSON.stringify(opened));
  check(`${width}: radio painted from the profile`, opened.checked === "daily", opened.checked);
  check(`${width}: daily controls live, weekly greyed`, opened.dailyTime === "09:00" && opened.dailyLive && !opened.weeklyLive, JSON.stringify(opened));
  check(`${width}: zones on offer`, (opened.zones || 0) >= 30, String(opened.zones));
  await page.screenshot({ path: `prefs-${width}-open.png`, fullPage: width < 800 });
  await page.check('input[name="pref-cadence"][value="weekly"]');
  await page.selectOption("#pref-day", "4");
  await page.fill("#pref-time-weekly", "18:30");
  await page.selectOption("#pref-tz-weekly", "America/New_York");
  const swapped = await page.evaluate(() => ({
    weeklyLive: !document.getElementById("pref-time-weekly")?.disabled,
    dailyLive: !document.getElementById("pref-time-daily")?.disabled,
    dailyMirrors: document.getElementById("pref-time-daily")?.value,
  }));
  check(`${width}: weekly controls live once chosen, daily mirrors the time`, swapped.weeklyLive && !swapped.dailyLive && swapped.dailyMirrors === "18:30", JSON.stringify(swapped));
  await page.screenshot({ path: `prefs-${width}-weekly.png`, fullPage: width < 800 });
  await page.click("#pref-item-cadence .pref-save");
  await page.waitForFunction(() => !document.getElementById("pref-item-cadence").classList.contains("open"), null, { timeout: 10000 }).catch(() => {});
  const after = await page.evaluate(() => ({
    cadence: document.getElementById("pref-sum-cadence")?.textContent,
    status: document.getElementById("acct-status")?.textContent,
    open: document.getElementById("pref-item-cadence").classList.contains("open"),
  }));
  check(`${width}: PUT carries cadence, day, time and zone`, putBody?.cadence === "weekly" && putBody?.digest_day === 4 && putBody?.digest_time === "18:30" && putBody?.digest_tz === "America/New_York", JSON.stringify(putBody));
  check(`${width}: PUT keeps skills`, Array.isArray(putBody?.skills) && putBody.skills.length === 3, JSON.stringify(putBody?.skills));
  check(`${width}: PUT carries country and workplace`, putBody?.country?.[0] === "IL" && putBody?.workplace?.length === 2, JSON.stringify(putBody));
  check(`${width}: row closes and summary updates`, !after.open && /^Weekly on Friday at 18:30, America\/New York \(GMT-0[45]:00\)$/.test(after.cadence || ""), JSON.stringify(after));
  check(`${width}: status line follows`, /weekly on Friday/.test(after.status || ""), after.status);
  const help = await page.evaluate(() => document.querySelectorAll("#preferences .pref-help").length);
  check(`${width}: no help lines under the pickers`, help === 0, String(help));

  // Cancel reverts: open workplace, clear it through the draft, cancel.
  await page.click("#pref-item-workplace .pref-head");
  await page.evaluate(() => { draft.workplace = []; });
  await page.click("#pref-item-workplace .pref-cancel");
  const reverted = await page.evaluate(() => ({ w: draft.workplace.slice(), sum: document.getElementById("pref-sum-workplace")?.textContent }));
  check(`${width}: cancel reverts the draft`, reverted.w.length === 2 && reverted.sum === "Remote, Hybrid", JSON.stringify(reverted));

  // A new alert starts from the preferences.
  await page.evaluate(() => { location.hash = "#alerts"; });
  await page.waitForSelector("#alerts.is-active", { timeout: 10000 });
  await page.waitForTimeout(2500);
  const alertState = await page.evaluate(() => ({ ...alertFormState }));
  check(`${width}: new alert pre-filled with workplace`, alertState.workplace?.length === 2, JSON.stringify(alertState.workplace));
  check(`${width}: new alert pre-filled with country`, alertState.country?.[0] === "IL", JSON.stringify(alertState.country));
  check(`${width}: no page errors`, errs.length === 0, errs.join(" | ").slice(0, 200));
  await page.close();
}
await browser.close();
console.log(failures.length ? `${failures.length} FAILED` : "all passed");
process.exit(failures.length ? 1 : 0);
