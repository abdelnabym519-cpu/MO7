import { defineConfig } from "@playwright/test";

/**
 * Phase 31 staging run: the suites in web/tests are pointed at the deployed
 * staging origin (TLS ingress) instead of a local dev server, with the session
 * of a real staging account supplied as storage state.
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
