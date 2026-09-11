import { expect, type Locator, type Page, type Response } from "@playwright/test";

/**
 * The job board: search, results, paging, and the job drawer.
 *
 * Locator policy, in order of preference:
 *   1. role and accessible name, which survive a restyle and assert
 *      something about accessibility at the same time;
 *   2. the element's id, where the app already treats it as a contract
 *      (#f-q and friends are read by name throughout app.js);
 *   3. data-testid, added only where the markup offers neither.
 *
 * No CSS descendant chains and no XPath: this app's class names are
 * presentational and change with the design system.
 */
export class BoardPage {
  /** Mirrors PAGE_SIZE in app.js; the ticker uses 10 for the same routes. */
  static readonly PAGE_SIZE = 50;

  readonly page: Page;
  readonly search: Locator;
  readonly resultCount: Locator;
  readonly rows: Locator;
  readonly emptyState: Locator;
  readonly errorState: Locator;
  readonly companyChip: Locator;
  readonly pagination: Locator;
  readonly jobDrawer: Locator;
  readonly dataHealth: Locator;

  constructor(page: Page) {
    this.page = page;
    this.search = page.locator("#f-q");
    this.resultCount = page.locator("#result-count");
    this.rows = page.locator("#jobs-body tr");
    this.emptyState = page.locator("#jobs-empty");
    this.errorState = page.locator("#jobs-error");
    this.companyChip = page.locator("#company-chip");
    this.pagination = page.locator("#pagination");
    this.jobDrawer = page.locator("#job-detail");
    this.dataHealth = page.locator("#metric-api-status");
  }

  async goto(query = "", { clearStorage = true } = {}) {
    if (clearStorage) {
      // The board remembers filters across sessions on purpose, so a
      // spec that does not clear them inherits the previous one's. Three
      // false failures came from exactly this before it was handled.
      await this.page.goto("/");
      await this.page.evaluate(() => {
        try { localStorage.clear(); } catch { /* private mode */ }
      });
    }
    await this.page.goto(`/${query}`);
    await this.settled();
  }

  /** Resolves when the board has finished rendering a result set. */
  async settled() {
    await expect(this.page.locator("#jobs-body .skeleton")).toHaveCount(0);
    await expect(async () => {
      const done =
        (await this.resultCount.textContent())?.trim() ||
        (await this.emptyState.isVisible()) ||
        (await this.errorState.isVisible());
      expect(done).toBeTruthy();
    }).toPass();
  }

  /** The "of N" in "1–50 of 118,974 open listings", or null when empty. */
  async total(): Promise<number | null> {
    if (await this.emptyState.isVisible()) return 0;
    const text = (await this.resultCount.textContent()) ?? "";
    const match = text.replace(/,/g, "").match(/of\s*(\d+)/i);
    return match ? Number(match[1]) : null;
  }

  /**
   * Type into the search box and wait for the request it triggers, so
   * the assertion that follows is about a settled board rather than a
   * race. Returns the API response for contract assertions.
   */
  async searchFor(term: string): Promise<Response> {
    // Two things make a looser predicate wrong here. The board has an
    // in-flight unfiltered request from page load, so matching the route
    // alone resolves with that one's body while the screen shows the
    // filtered set. And the ticker asks for the same filters at
    // limit=10, so matching the term alone returns ten jobs for a table
    // showing fifty. Pin the term and the board's own page size.
    const responded = this.page.waitForResponse(
      (r) => {
        if (!r.url().includes("/api/jobs") || r.request().method() !== "GET") return false;
        const params = new URL(r.url()).searchParams;
        return params.get("q") === term && params.get("limit") === String(BoardPage.PAGE_SIZE);
      },
      { timeout: 60_000 },
    );
    await this.search.fill(term);
    const response = await responded;
    await this.settled();
    return response;
  }

  /**
   * Run an action that changes a filter and wait for the board's own
   * request for the new set, not merely for the page to look idle.
   *
   * settled() alone is not enough: between the click and the new
   * request, the previous count is still on screen and no skeleton has
   * appeared yet, so it returns immediately and the caller reads a
   * stale number. That passed on desktop and failed under mobile
   * emulation, which is slow enough to lose the race, and produced the
   * impossible claim that a 3-day window held fewer listings than a
   * 1-day one.
   */
  async applyFilter(
    action: () => Promise<void>,
    matches: (params: URLSearchParams) => boolean,
  ): Promise<Response> {
    const responded = this.page.waitForResponse(
      (r) => {
        if (!r.url().includes("/api/jobs") || r.request().method() !== "GET") return false;
        const params = new URL(r.url()).searchParams;
        // The ticker mirrors the board's filters at limit=10, so pin the
        // page size or the wait resolves on the wrong response.
        return params.get("limit") === String(BoardPage.PAGE_SIZE) && matches(params);
      },
      { timeout: 60_000 },
    );
    await action();
    const response = await responded;
    await this.settled();
    return response;
  }

  async reset() {
    await this.page.locator("#f-reset").click();
    await this.settled();
  }

  async openFirstJob() {
    await this.rows.first().getByRole("link").first().click();
    await expect(this.jobDrawer).toBeVisible();
  }

  /** Every filter currently encoded in the address bar. */
  async urlParams(): Promise<URLSearchParams> {
    return new URLSearchParams(new URL(this.page.url()).search);
  }

  async storedFilters(): Promise<Record<string, unknown> | null> {
    return this.page.evaluate(() => {
      try { return JSON.parse(localStorage.getItem("iljobs_filters") ?? "null"); }
      catch { return null; }
    });
  }
}
