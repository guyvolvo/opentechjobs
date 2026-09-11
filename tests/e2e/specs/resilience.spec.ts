import { expect, test } from "@playwright/test";
import { BoardPage } from "../pages/BoardPage";
import { FiltersPanel } from "../pages/FiltersPanel";
import { MALFORMED_PARAMS } from "../fixtures/filter-cases";

test.describe("malformed input", () => {
  for (const { name, query, ignored } of MALFORMED_PARAMS) {
    test(`${name} degrades instead of breaking the board`, async ({ page }) => {
      const board = new BoardPage(page);
      await board.goto(query);

      await expect(board.errorState, "no error state").toBeHidden();
      const total = await board.total();
      expect(total, "the board still reports a result set").not.toBeNull();

      // A value the app cannot honour must be dropped, not clamped to
      // something plausible: silently turning max_age_days=-5 into 30
      // would show results the URL never asked for.
      const params = await board.urlParams();
      for (const key of ignored) {
        expect(params.has(key), `${key} was kept despite being invalid`).toBe(false);
      }
    });
  }

  test("a malformed parameter does not poison later visits", async ({ page }) => {
    const board = new BoardPage(page);
    // Found live 2026-09-11: max_age_days is persisted across sessions,
    // so one bad link was saved and every later visit to the plain board
    // reloaded it and failed again, with no way out but clearing site
    // data.
    await board.goto("?max_age_days=abc");
    await expect(board.errorState).toBeHidden();

    for (const attempt of [1, 2]) {
      await page.goto("/");
      await board.settled();
      await expect(board.errorState, `still broken on visit ${attempt}`).toBeHidden();
      expect(await board.total()).toBeGreaterThan(0);
    }

    const stored = await board.storedFilters();
    expect(stored?.max_age_days ?? "", "the junk was not saved").not.toBe("abc");
  });

  test("a browser already holding a poisoned value heals itself", async ({ page }) => {
    const board = new BoardPage(page);
    await board.goto();
    // Simulate storage written by a version that did not validate.
    await page.evaluate(() => {
      const raw = JSON.parse(localStorage.getItem("iljobs_filters") ?? "{}");
      raw.max_age_days = "abc";
      raw.sort = "banana";
      localStorage.setItem("iljobs_filters", JSON.stringify(raw));
    });

    await page.goto("/");
    await board.settled();
    await expect(board.errorState, "a poisoned browser must recover unaided").toBeHidden();
    expect(await board.total()).toBeGreaterThan(0);
  });
});

test.describe("network failure", () => {
  test("a failed jobs request shows an error, not a blank board", async ({ page }) => {
    const board = new BoardPage(page);
    await board.goto();
    await page.route("**/api/jobs**", (route) => route.abort("failed"));
    await board.searchFor("engineer").catch(() => { /* the request is aborted by design */ });

    // Either state is acceptable; silence is not. A reader must be told
    // something went wrong rather than shown an empty table.
    await expect
      .poll(async () => (await board.errorState.isVisible()) || (await board.rows.count()) > 0)
      .toBe(true);
  });

  test("the board recovers once the network comes back", async ({ page }) => {
    const board = new BoardPage(page);
    await board.goto();

    await page.route("**/api/jobs**", (route) => route.abort("failed"));
    await board.search.fill("sre");
    await page.waitForTimeout(2500);

    await page.unroute("**/api/jobs**");
    await board.searchFor("sre");
    await expect(board.errorState, "the error clears on the next good response").toBeHidden();
    expect(await board.total()).toBeGreaterThan(0);
  });

  test("a 500 from the API is reported rather than rendered as empty", async ({ page }) => {
    const board = new BoardPage(page);
    await board.goto();
    await page.route("**/api/jobs**", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: '{"error":"boom"}' }));
    await board.search.fill("engineer");

    await expect.poll(async () => board.errorState.isVisible()).toBe(true);
    // An empty-state message would be a lie: nothing is known about
    // whether listings match.
    await expect(board.emptyState).toBeHidden();
  });

  test("a slow response does not leave the board looking finished but empty", async ({ page }) => {
    const board = new BoardPage(page);
    await board.goto();
    await page.route("**/api/jobs**", async (route) => {
      await new Promise((r) => setTimeout(r, 4000));
      await route.continue();
    });
    await board.search.fill("data");
    // Mid-flight the board must show it is working, not zero results.
    await page.waitForTimeout(1200);
    const claimsEmpty = await board.emptyState.isVisible();
    expect(claimsEmpty, "an in-flight request must not render the empty state").toBe(false);
  });
});

test.describe("session and storage", () => {
  test("blocked storage does not stop the board rendering", async ({ browser }) => {
    // Private-mode and locked-down browsers throw on any access.
    const context = await browser.newContext();
    await context.addInitScript(() => {
      const boom = () => { throw new Error("storage disabled"); };
      Object.defineProperty(window, "localStorage", {
        get: boom, configurable: true,
      });
    });
    const page = await context.newPage();
    const board = new BoardPage(page);
    await page.goto("/");
    await board.settled();
    await expect(board.errorState).toBeHidden();
    expect(await board.total()).toBeGreaterThan(0);
    await context.close();
  });

  test("corrupt saved filters are ignored rather than fatal", async ({ page }) => {
    const board = new BoardPage(page);
    await board.goto();
    await page.evaluate(() => localStorage.setItem("iljobs_filters", "{not json"));
    await page.goto("/");
    await board.settled();
    await expect(board.errorState).toBeHidden();
    expect(await board.total()).toBeGreaterThan(0);
  });

  test("an expired token does not break the public board", async ({ page }) => {
    const board = new BoardPage(page);
    await board.goto();
    // A JWT whose exp is in the past, shaped like the real thing.
    await page.evaluate(() => {
      const payload = btoa(JSON.stringify({ exp: 1, email: "expired@example.com" }));
      localStorage.setItem("iljobs_tokens", JSON.stringify({
        id_token: `header.${payload}.sig`, access_token: "stale", refresh_token: "stale",
      }));
    });
    await page.goto("/");
    await board.settled();
    await expect(board.errorState, "listings are public and must still load").toBeHidden();
    expect(await board.total()).toBeGreaterThan(0);
  });

  test("the alerts area never 401s the board out from under a reader", async ({ page }) => {
    const board = new BoardPage(page);
    await page.route("**/me/alerts**", (route) =>
      route.fulfill({ status: 401, contentType: "application/json", body: '{"error":"expired"}' }));
    await board.goto();
    await expect(board.errorState).toBeHidden();
    expect(await board.total()).toBeGreaterThan(0);
  });
});

test.describe("console health", () => {
  test("no uncaught errors across a normal filtering session", async ({ page }) => {
    const errors: string[] = [];
    const failedAssets: string[] = [];
    page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));
    page.on("console", (m) => {
      const text = m.text();
      if (m.type() !== "error" || text.includes("chrome-extension://")) return;
      // A logo the browser could not fetch is noise here, not a fault.
      // Rows whose company has no resolved logo_url still run the old
      // guess cascade, which asks a company domain for an icon it may
      // not have. Counted separately so the number stays visible without
      // failing the run: it is the case for resolving logos server-side.
      if (text.startsWith("Failed to load resource")) failedAssets.push(text.slice(0, 120));
      else errors.push(text.slice(0, 200));
    });

    const board = new BoardPage(page);
    const filters = new FiltersPanel(page);
    await board.goto();
    await board.searchFor("engineer");
    await filters.open();
    await filters.datePosted.selectOption("7");
    await board.settled();
    await filters.sort.selectOption("age:desc");
    await board.settled();
    await board.reset();
    await page.goto("/?sort=banana&dir=sideways&max_age_days=abc");
    await board.settled();

    expect(errors, errors.join("\n")).toHaveLength(0);
    if (failedAssets.length) {
      console.log(`  note: ${failedAssets.length} asset requests failed, company logos`);
    }
  });
});
