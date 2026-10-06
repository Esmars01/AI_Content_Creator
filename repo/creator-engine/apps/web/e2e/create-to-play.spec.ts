/**
 * Phase 5 DoD (§40): in mock mode, create → previz → approve → progress → play, through the UI.
 */
import { readFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

import { USER_FILE } from "./global-setup";

const IDEA = "Create a 30-second TikTok explaining why most people misunderstand AI agents.";

test("create → previz → approve → progress → play", async ({ page }) => {
  const user = JSON.parse(readFileSync(USER_FILE, "utf8")) as { email: string; password: string };

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

  // play
  const player = page.getByTestId("player");
  await expect(player).toBeVisible({ timeout: 420_000 });
  await expect(page.locator('[data-state="ready"]').first()).toBeVisible({ timeout: 60_000 });
  const playback = await player.evaluate(async (element) => {
    const video = element as HTMLVideoElement;
    const codec = video.canPlayType('video/mp4; codecs="avc1.42E01E, mp4a.40.2"');
    if (!codec) return { codec, error: "this browser cannot decode H.264 (use Google Chrome: CE_E2E_CHROME)" };
    video.muted = true;
    await video.play();
    await new Promise((r) => setTimeout(r, 1500));
    const time = video.currentTime;
    video.pause();
    return { codec, time, duration: video.duration, error: video.error?.message ?? null };
  });
  expect(playback.error ?? null).toBeNull();
  expect(playback.time).toBeGreaterThan(0.5);
  expect(playback.duration).toBeGreaterThan(3);
  expect(progressSeen).toBe(true);

  // Phase 11: the QC report with the shot gates and the requested → compiled → observed triad
  await expect(page.getByText("QC report", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Requested, compiled, observed" })).toBeVisible();

  // after observation, delivered counts only confirmed behaviors (I9)
  await expect(page.getByTestId("scene-coverage").first()).toContainText("delivered", { timeout: 30_000 });
});
