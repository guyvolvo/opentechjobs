import { expect, test } from "@playwright/test";

// The board moved from / to /board, and the landing page took /. Every
// link shared before the move still points at /, so the edge function in
// infra/cloudfront.tf forwards any / that carries a board parameter.
// These read the raw response rather than the rendered page, because a
// 200 on the wrong page is exactly the failure being guarded against.
test.describe("routing after the board moved to /board", () => {
  const redirects: [string, string][] = [
    ["/?country=IL", "/board?country=IL"],
    ["/?job=abc&search=devops", "/board?job=abc&search=devops"],
    ["/?view=matches", "/board?view=matches"],
    ["/hero", "/"],
    ["/board.html", "/board"],
  ];
  for (const [from, to] of redirects) {
    test(`${from} redirects to ${to}`, async ({ request }) => {
      const res = await request.get(from, { maxRedirects: 0 });
      expect(res.status()).toBe(301);
      expect(res.headers()["location"]).toBe(to);
    });
  }

  test("a bare / is the landing page", async ({ request }) => {
    const res = await request.get("/", { maxRedirects: 0 });
    expect(res.status()).toBe(200);
    expect(await res.text()).toContain('class="hero-page"');
  });

  test("tracking parameters alone keep the landing page", async ({ request }) => {
    const res = await request.get("/?utm_source=newsletter", { maxRedirects: 0 });
    expect(res.status()).toBe(200);
    expect(await res.text()).toContain('class="hero-page"');
  });

  test("/board is the board", async ({ request }) => {
    const res = await request.get("/board", { maxRedirects: 0 });
    expect(res.status()).toBe(200);
    expect(await res.text()).toContain('id="board"');
  });

  test("the landing page's calls to action go to the board", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator(".hero-primary")).toHaveAttribute("href", "/board");
    await expect(page.locator(".hero-cta-link")).toHaveAttribute("href", "/board");
  });
});
