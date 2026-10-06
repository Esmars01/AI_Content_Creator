/**
 * Phase 5 DoD (§40): in mock mode, create → previz → approve → progress → play, through the UI.
 */
import { readFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

import { USER_FILE } from "./global-setup";

const IDEA = "Create a 30-second TikTok explaining why most people misunderstand AI agents.";
// The build itself (approve → ready) on the mock stack; measured ~110 s on 4 vCPU (FINAL_AUDIT_AND_FIX_REPORT.md).
const BUILD_TIMEOUT_MS = Number(process.env.CE_E2E_BUILD_TIMEOUT_S ?? 420) * 1000;
// Once the backend says ready, the UI must follow quickly: it reconciles with the API, never only SSE.
const UI_LAG_MS = 15_000;

test("create → previz → approve → progress → play", async ({ page }) => {
  const user = JSON.parse(readFileSync(USER_FILE, "utf8")) as { email: string; password: string };
  // Every opened event stream. The shell keeps one per tab; a stream that keeps dying (the Valkey read
  // timeout of audit S1 dropped it every 5 s) shows up as many reconnects.
  let streams = 0;
  page.on("request", (request) => {
    if (request.url().includes("/api/v1/events")) streams += 1;
  });

  // sign in
  await page.goto("/login");
  await page.getByLabel("Email").fill(user.email);
  await page.getByLabel("Password").fill(user.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

  // create: idea → the Plan step
  await page.getByRole("link", { name: "Create" }).first().click();
  await expect(page.getByRole("heading", { name: "Create" })).toBeVisible();
  await page.getByLabel("Idea, outline, notes or exact script").fill(IDEA);
  await page.getByRole("button", { name: /^4\s*Format$/ }).click();
  await expect(page.getByLabel("Mode")).toBeVisible();
  await page.getByRole("button", { name: /^10\s*Plan$/ }).click();
  await page.getByRole("button", { name: "Plan video" }).click();

  // previz: planning, then the review
  await expect(page).toHaveURL(/\/videos\/[^/]+\/versions\/[^/]+\/previz/);
  const approve = page.getByRole("button", { name: "Approve and generate" });
  await expect(approve).toBeEnabled({ timeout: 180_000 });
  await expect(page.getByRole("heading", { name: "Script" })).toBeVisible();
  await expect(page.getByTestId("script")).toContainText("agents");
  await expect(page.getByTestId("storyboard-shot").first()).toBeVisible();
  await expect(page.getByAltText(/^Keyframe for /).first()).toBeVisible();
  await expect(page.getByTestId("performance-lane")).toBeVisible();
  await expect(page.getByTestId("trajectory")).not.toBeEmpty();
  await expect(page.getByText("Measured", { exact: true })).toBeVisible();
  await expect(page.getByTestId("scene-coverage").first()).toContainText("honored");
  await expect(page.getByTestId("scene-coverage").first()).not.toContainText("delivered"); // I9: nothing delivered yet

  // approve → the studio shows progress
  await approve.click();
  await expect(page).toHaveURL(/\/videos\/[^/?]+\?version=/);
  await expect(
    page.locator('[data-state="approved"], [data-state="generating"], [data-state="ready"]').first(),
  ).toBeVisible();
  const progressSeen = await page
    .getByTestId("job-progress")
    .first()
    .waitFor({ timeout: 60_000 })
    .then(
      () => true,
      () => false,
    );

  // ready (backend truth), then the player within UI_LAG_MS: the UI must not lag behind the API
  await expect(page.locator('[data-state="ready"]').first()).toBeVisible({ timeout: BUILD_TIMEOUT_MS });
  const player = page.getByTestId("player");
  await expect(player).toBeVisible({ timeout: UI_LAG_MS });
  await expect(page.getByText(/· final ·/)).toBeVisible(); // the final render, not the proxy

  // the media itself: a range request answers 206 with an MP4 (any browser can check this)
  const media = await player.evaluate(async (element) => {
    const url = (element as HTMLVideoElement).currentSrc || (element as HTMLVideoElement).src;
    const response = await fetch(url, { headers: { Range: "bytes=0-63" } });
    const head = new Uint8Array(await response.arrayBuffer());
    return {
      status: response.status,
      type: response.headers.get("content-type"),
      ftyp: String.fromCharCode(...head.slice(4, 8)),
    };
  });
  expect(media.status).toBe(206);
  expect(media.type).toContain("video/mp4");
  expect(media.ftyp).toBe("ftyp");

  // play: needs H.264, which Playwright's open-source Chromium lacks (playwright.config.ts)
  const playback = await player.evaluate(async (element) => {
    const video = element as HTMLVideoElement;
    const codec = video.canPlayType('video/mp4; codecs="avc1.42E01E, mp4a.40.2"');
    if (!codec) return { codec, error: "this browser cannot decode H.264 (use Google Chrome: CE_E2E_CHROME)" };
    video.muted = true;
    const started = await Promise.race([
      video.play().then(() => true),
      new Promise<boolean>((resolve) => setTimeout(() => resolve(false), 15_000)),
    ]);
    if (!started) return { codec, error: "play() did not start within 15 s" };
    await new Promise((r) => setTimeout(r, 1500));
    const time = video.currentTime;
    video.pause();
    return { codec, time, duration: video.duration, error: video.error?.message ?? null };
  });
  expect(playback.error ?? null).toBeNull();
  expect(playback.time).toBeGreaterThan(0.5);
  expect(playback.duration).toBeGreaterThan(3);
  expect(progressSeen).toBe(true);
  expect(streams, "the event stream reconnected over and over").toBeLessThanOrEqual(4);

  // Phase 11: the QC report with the shot gates and the requested → compiled → observed triad
  await expect(page.getByText("QC report", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Requested, compiled, observed" })).toBeVisible();

  // after observation, delivered counts only confirmed behaviors (I9)
  await expect(page.getByTestId("scene-coverage").first()).toContainText("delivered", { timeout: 30_000 });
});
