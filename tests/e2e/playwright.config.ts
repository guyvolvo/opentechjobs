import { defineConfig, devices } from "@playwright/test";

/**
 * Scoped to this directory on purpose. The frontend it tests ships no
 * build step and no node_modules, and that stays true: nothing here is
 * imported by the app, and `npm test` in the repo root does not need it.
 *
 * BASE_URL points at production by default because that is what the
 * hand-rolled CDP harnesses this replaces were pointed at, and because
 * several of these assertions are about CDN and cache behaviour that a
 * file:// or local static server does not reproduce.
 */
const BASE_URL = process.env.BASE_URL ?? "https://opentechjobs.org";

export default defineConfig({
  testDir: "./specs",
  // The board talks to a Lambda behind CloudFront; a cold one is slow
  // enough that the default 30s produces flakes that are not bugs.
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  // Never retry locally: a test that only passes on the second run is a
  // bug report, not a pass. CI gets one retry to absorb genuine network
  // failure, and the report says which tests needed it.
  retries: process.env.CI ? 1 : 0,
  // Serial by default. These share one live backend and several specs
  // assert on total counts, which a parallel run would race.
  workers: 1,
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    // An ad blocker in a developer's own profile injects console errors
    // that look like page errors. The console assertions below would be
    // useless without this; it cost me a false failure once already.
    launchOptions: { args: ["--disable-extensions"] },
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } } },
    { name: "mobile", use: { ...devices["Pixel 7"] } },
  ],
});
