import { expect, test } from "@playwright/test";
import { BoardPage } from "../pages/BoardPage";
import { FiltersPanel } from "../pages/FiltersPanel";
import { AGE_OPTIONS, FACETS, SEARCH_TERMS } from "../fixtures/filter-cases";

test.describe("filters", () => {
  let board: BoardPage;
  let filters: FiltersPanel;
  let baseline: number | null;

  test.beforeEach(async ({ page }) => {
    board = new BoardPage(page);
    filters = new FiltersPanel(page);
    await board.goto();
    baseline = await board.total();
    expect(baseline, "the board has listings to filter").toBeGreaterThan(0);
  });

  test("search narrows the set, reaches the URL, and survives a reload", async ({ page }) => {
    const response = await board.searchFor("engineer");
    expect(response.status(), "the API answered the search").toBe(200);

    const body = await response.json();
    const narrowed = await board.total();
    expect(narrowed).toBeLessThan(baseline!);
    // The UI count and the API total are the same number, so a stale
    // render cannot pass this by showing an older result set.
    expect(narrowed, "the rendered count matches the API total").toBe(body.total);
    expect((await board.urlParams()).get("q")).toBe("engineer");

    const shared = page.url();
    await page.goto(shared);
    await board.settled();
    await expect(board.search).toHaveValue("engineer");
    expect(await board.total(), "a shared URL reproduces the same set").toBe(narrowed);
  });

  for (const { name, term, expect: outcome } of SEARCH_TERMS) {
    test(`search handles ${name}`, async ({ page }) => {
      await board.searchFor(term);

      await expect(board.errorState, "no error state").toBeHidden();
      await expect(board.search, "the box keeps exactly what was typed").toHaveValue(term);

      const total = await board.total();
      if (outcome === "narrows") expect(total).toBeLessThanOrEqual(baseline!);
      if (outcome === "empty") {
        expect(total).toBe(0);
        await expect(board.emptyState).toBeVisible();
      }

      // Whatever was typed has to round-trip through the address bar.
      await page.goto(page.url());
      await board.settled();
      await expect(board.search, "and through a share-and-reload").toHaveValue(term);
    });
  }

  test("a script tag is rendered as text, never as markup", async ({ page }) => {
    const term = "<script>alert(1)</script>";
    await board.searchFor(term);
    // The value is echoed in the empty state and the address bar; what
    // matters is that no element was created from it.
    const injected = await page.locator("body script:not([src])").evaluateAll(
      (nodes) => nodes.some((n) => n.textContent?.includes("alert(1)")),
    );
    expect(injected, "no script element was created from the query").toBe(false);
  });

  for (const { name, param } of FACETS) {
    test(`the ${name} facet filters and clears`, async ({ page }) => {
      await filters.open();
      const ms = filters.multiSelect(name);
      await ms.open();

      const count = await ms.options.count();
      test.skip(count === 0, `${name} offered no options`);

      const picked = await ms.selectNth(0);
      await board.settled();

      expect((await board.urlParams()).get(param), "the choice reaches the URL").toBeTruthy();
      expect(await board.total(), "and narrows the set").toBeLessThanOrEqual(baseline!);
      expect(await ms.label(), `the closed control names the choice (${picked})`)
        .not.toBe(name);

      await ms.clearAll();
      await board.settled();
      expect(await board.total(), "clearing restores the full set").toBe(baseline);
      expect((await board.urlParams()).has(param), "and clears the URL").toBe(false);
    });
  }

  test("a second value in one facet widens rather than narrows", async () => {
    await filters.open();
    const ms = filters.multiSelect("seniority");
    await ms.open();
    test.skip((await ms.options.count()) < 2, "needs two options");

    await ms.selectNth(0);
    await board.settled();
    const one = await board.total();

    await ms.selectNth(1);
    await board.settled();
    const two = await board.total();

    // Values within one facet are OR'd, so adding one can only widen.
    expect(two!).toBeGreaterThanOrEqual(one!);
  });

  test("date posted narrows monotonically as the window grows", async () => {
    await filters.open();
    const totals: number[] = [];
    for (const days of AGE_OPTIONS) {
      const response = await board.applyFilter(
        () => filters.datePosted.selectOption(days),
        (params) => params.get("max_age_days") === days,
      );
      const body = await response.json();
      // Read the total from the response for this window rather than
      // from the screen, which can still be showing the previous one.
      totals.push(body.total);
      expect(await board.total(), `the screen shows the ${days}d set`).toBe(body.total);
    }
    for (let i = 1; i < totals.length; i++) {
      expect(totals[i], `${AGE_OPTIONS[i]}d is not smaller than ${AGE_OPTIONS[i - 1]}d`)
        .toBeGreaterThanOrEqual(totals[i - 1]);
    }
    expect(totals.at(-1)).toBeLessThanOrEqual(baseline!);
  });

  test("two filters compose instead of replacing each other", async ({ page }) => {
    await page.goto("/?israel_only=1");
    await board.settled();
    const israelOnly = await board.total();

    await page.goto("/?israel_only=1&seniority=senior");
    await board.settled();
    const both = await board.total();

    expect(both!).toBeLessThanOrEqual(israelOnly!);
    const params = await board.urlParams();
    expect(params.get("israel_only")).toBe("1");
    expect(params.get("seniority")).toBe("senior");
  });

  test("reset clears the screen, the URL and what was remembered", async ({ page }) => {
    await board.searchFor("devops");
    await filters.open();
    await filters.datePosted.selectOption("7");
    await board.settled();

    await board.reset();
    await expect(board.search).toHaveValue("");
    expect(await board.total()).toBe(baseline);

    const params = await board.urlParams();
    expect([...params.keys()].filter((k) => k !== "qa")).toHaveLength(0);

    // The real regression risk: reset clears the screen but not storage,
    // so the old filter returns on the next visit.
    await page.goto("/");
    await board.settled();
    await expect(board.search, "and does not come back on the next visit").toHaveValue("");
  });

  test("filtering after paging returns to the first page", async ({ page }) => {
    const pages = board.pagination.getByRole("button");
    test.skip((await pages.count()) === 0, "no pagination on this result set");

    await pages.filter({ hasText: /^3$/ }).first().click();
    await board.settled();
    expect((await board.urlParams()).get("offset"), "paging moves the offset").toBeTruthy();

    await board.searchFor("quantum");
    const offset = (await board.urlParams()).get("offset");
    expect(offset === null || offset === "0", `offset left at ${offset}`).toBe(true);
    // An offset carried into a smaller set shows an empty page with a
    // non-zero count, which reads as a broken board.
    const shown = await board.rows.count();
    const total = await board.total();
    expect(total === 0 || shown > 0, "a filtered page is not blank").toBe(true);
  });

  test("fast retyping settles on the last query, not a slower earlier one", async ({ page }) => {
    await board.search.fill("a");
    await page.waitForTimeout(120);
    await board.search.fill("kubernetes");
    await board.settled();
    // Give a slow first response time to arrive and overwrite.
    await page.waitForTimeout(3000);
    await board.settled();

    await expect(board.search).toHaveValue("kubernetes");
    expect((await board.urlParams()).get("q")).toBe("kubernetes");
    expect(await board.total()).toBeLessThan(baseline!);
  });

  test("changing a filter does not litter the back history", async ({ page }) => {
    const before = await page.evaluate(() => history.length);
    await board.searchFor("platform");
    const after = await page.evaluate(() => history.length);
    // app.js uses replaceState for filters on purpose: a keystroke is
    // not a page a reader expects Back to undo one at a time.
    expect(after).toBe(before);
  });

  test("the Filters button counts what is active", async ({ page }) => {
    await page.goto("/?israel_only=1&seniority=senior&max_age_days=7");
    await board.settled();
    expect(await filters.activeCount()).toBeGreaterThanOrEqual(2);
  });

  test("starred-only with nothing starred is empty, not an error", async () => {
    await filters.open();
    await filters.starred.check();
    await board.settled();
    await expect(board.errorState).toBeHidden();
    expect((await board.rows.count()) === 0 || (await board.emptyState.isVisible())).toBe(true);
  });
});
