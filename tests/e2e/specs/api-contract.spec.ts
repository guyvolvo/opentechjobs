import { expect, test } from "@playwright/test";
import { BoardPage } from "../pages/BoardPage";
import { MALFORMED_PARAMS } from "../fixtures/filter-cases";

/**
 * The API on its own. The board is one client of it, and a shared link
 * or a saved alert is another, so these assertions are about the
 * contract rather than about what any screen renders.
 */
test.describe("/api/jobs", () => {
  test("answers the unfiltered board", async ({ request }) => {
    const res = await request.get("/api/jobs?limit=5");
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.total).toBeGreaterThan(0);
    expect(body.jobs).toHaveLength(5);
    for (const job of body.jobs) {
      expect(job.id, "every listing is addressable").toBeTruthy();
      expect(job.title).toBeTruthy();
      expect(job.company_domain, "and attributable to a company").toBeTruthy();
      expect(job.url, "and applies somewhere").toMatch(/^https?:\/\//);
    }
  });

  test("limit and offset page without overlapping", async ({ request }) => {
    const first = await (await request.get("/api/jobs?limit=5&offset=0")).json();
    const second = await (await request.get("/api/jobs?limit=5&offset=5")).json();
    const ids = new Set(first.jobs.map((j: { id: string }) => j.id));
    const overlap = second.jobs.filter((j: { id: string }) => ids.has(j.id));
    expect(overlap, "page two repeats page one").toHaveLength(0);
    expect(second.total).toBe(first.total);
  });

  test("israel_only is a real subset", async ({ request }) => {
    const all = await (await request.get("/api/jobs?limit=1")).json();
    const israel = await (await request.get("/api/jobs?israel_only=1&limit=1")).json();
    expect(israel.total).toBeGreaterThan(0);
    expect(israel.total).toBeLessThan(all.total);
  });

  test("max_age_days grows monotonically with the window", async ({ request }) => {
    const totals: number[] = [];
    for (const days of [1, 3, 7, 14, 30]) {
      const body = await (await request.get(`/api/jobs?max_age_days=${days}&limit=1`)).json();
      totals.push(body.total);
    }
    for (let i = 1; i < totals.length; i++) {
      expect(totals[i]).toBeGreaterThanOrEqual(totals[i - 1]);
    }
  });

  test("a search term actually appears in what comes back", async ({ request }) => {
    const body = await (await request.get("/api/jobs?q=kubernetes&limit=10")).json();
    expect(body.total).toBeGreaterThan(0);
    const hits = body.jobs.filter((j: Record<string, string>) =>
      `${j.title} ${j.company_domain} ${j.company_name ?? ""} ${j.location ?? ""}`
        .toLowerCase().includes("kubernetes"));
    // Full-text search also matches descriptions, which are not returned
    // here, so this is a sanity floor rather than an exact rule.
    expect(hits.length, "no visible field mentions the term").toBeGreaterThan(0);
  });

  test("injection-shaped input returns nothing rather than everything", async ({ request }) => {
    const body = await (await request.get(`/api/jobs?q=${encodeURIComponent("' OR 1=1 --")}&limit=5`)).json();
    const all = await (await request.get("/api/jobs?limit=1")).json();
    expect(body.total, "the quote was treated as text, not as SQL").toBeLessThan(all.total);
  });

  for (const { name, query } of MALFORMED_PARAMS) {
    test(`${name} never returns a 5xx`, async ({ request }) => {
      const res = await request.get(`/api/jobs${query}${query.includes("?") ? "&" : "?"}limit=1`);
      // 400 is a fair answer for a malformed request; 500 is not.
      expect(res.status(), `${query} -> ${res.status()}`).toBeLessThan(500);
    });
  }

  test("an unknown sort is rejected or ignored, never a 500", async ({ request }) => {
    const res = await request.get("/api/jobs?sort=banana&limit=1");
    expect(res.status()).toBeLessThan(500);
  });
});

test.describe("/api/health and /api/stats", () => {
  test("health reports a real freshness clock", async ({ request }) => {
    const res = await request.get("/api/health");
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.ok).toBe(true);
    expect(body.db_reachable).toBe(true);
    expect(body.jobs_open).toBeGreaterThan(0);
    expect(typeof body.minutes_since_check).toBe("number");
    expect(body.minutes_since_check, "the snapshot is not from the future").toBeGreaterThanOrEqual(0);
  });

  test("stats agrees with the jobs route on the open total", async ({ request }) => {
    const stats = await (await request.get("/api/stats")).json();
    const jobs = await (await request.get("/api/jobs?limit=1")).json();
    const open = stats.totals.open_jobs;
    // Both read the same snapshot, so a gap means one of them is stale.
    const drift = Math.abs(open - jobs.total) / Math.max(open, jobs.total);
    expect(drift, `stats says ${open}, jobs says ${jobs.total}`).toBeLessThan(0.02);
  });

  test("the ATS breakdown sums to roughly the open total", async ({ request }) => {
    const stats = await (await request.get("/api/stats")).json();
    const summed = stats.open_jobs_by_ats.reduce((a: number, r: { n: number }) => a + r.n, 0);
    const drift = Math.abs(summed - stats.totals.open_jobs) / stats.totals.open_jobs;
    expect(drift, `by_ats sums to ${summed}, totals says ${stats.totals.open_jobs}`).toBeLessThan(0.05);
  });
});

test.describe("the board and the API agree", () => {
  test("the rendered count is the API's total", async ({ page }) => {
    const board = new BoardPage(page);
    await board.goto();
    const response = await board.searchFor("security");
    const body = await response.json();
    expect(await board.total()).toBe(body.total);
    expect(await board.rows.count()).toBe(body.jobs.length);
  });
});
