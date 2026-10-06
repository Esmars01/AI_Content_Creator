/**
 * End-to-end tests against the running mock stack (`make dev`): the web app on :3000 talking to the
 * API, Temporal, the workers and SeaweedFS. `make test-e2e` runs them.
 *
 * Playback needs a browser that decodes H.264/AAC like users' browsers do: Google Chrome
 * (`channel: "chrome"`), or the binary in `CE_E2E_CHROME`. Playwright's open-source Chromium cannot
 * decode H.264, so the play step fails there instead of passing silently.
 */
import { defineConfig, devices } from "@playwright/test";

const chrome = process.env.CE_E2E_CHROME;

export default defineConfig({
  testDir: "./e2e",
  timeout: 600_000, // the planned video gets camera and realism post on every shot since Phase 7
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  globalSetup: "./e2e/global-setup.ts",
  outputDir: "../../.data/e2e-web",
  use: {
    baseURL: process.env.WEB_BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chrome",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 1000 },
        ...(chrome ? { launchOptions: { executablePath: chrome } } : { channel: "chrome" }),
      },
    },
  ],
});
