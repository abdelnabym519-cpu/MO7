import { defineConfig } from "@playwright/test";

/**
 * Phase 32 production browser run.
 *
 * The suites in web/tests are pointed at the deployed production frontend
 * through the *validation* ingress (a local TLS terminator used only by the
 * harnesses; production TLS terminates at the platform edge and is verified
 * from outside the deployment host). The session state comes from
 * `prod_session.py` acting as a real provisioned production account.
 *
 * The browser binary is the Chromium build supplied by @sparticuz/chromium
 * (the Playwright CDN is unreachable from this host); LD_LIBRARY_PATH must
 * carry the bundled AL2023 libraries, which the wrapper script sets.
 */
const BASE_URL = process.env.WEB_BASE_URL || "https://127.0.0.1:8443";
const STORAGE_STATE = process.env.MO7_STORAGE_STATE;

export default defineConfig({
  testDir: "/home/user/MO7/web/tests",
  timeout: 60_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    // The full Chromium build from @sparticuz/chromium, not the bundled
    // headless shell, so the browser under test is the real browser engine.
    channel: "chromium",
    baseURL: BASE_URL,
    ignoreHTTPSErrors: true,
    storageState: STORAGE_STATE,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "ui-audit", testMatch: /.*\.audit\.ts/, testIgnore: /epub-reader\.audit\.ts/ },
    { name: "epub-reader-chromium", testMatch: /epub-reader\.audit\.ts/ },
  ],
});
