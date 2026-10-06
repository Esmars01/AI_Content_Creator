/**
 * Phase 6 DoD (§40): "make him more skeptical" on a generated video through the Studio's NL edit
 * panel — a proposal card with operations, impact (the voice lock keeps the audio) and the coverage
 * delta, then Apply — and a compare of the two versions with side-by-side players and the spec and
 * CBS differences of the edited scene.
 */
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

import { USER_FILE } from "./global-setup";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");

/** A generated two-scene video (scripts/e2e_video.py builds it on the running stack). */
function generatedVideo(): { video_id: string; version_id: string; state: string } {
  const out = execFileSync("uv", ["run", "--quiet", "python", "scripts/e2e_video.py"], {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "inherit"],
    timeout: 600_000,
  });
  return JSON.parse(out.trim().split("\n").at(-1) ?? "{}") as { video_id: string; version_id: string; state: string };
}

test("make him more skeptical → proposal → apply → compare", async ({ page }) => {
  test.setTimeout(900_000);
  const video = generatedVideo();
  expect(video.state).toBe("ready");
  const user = JSON.parse(readFileSync(USER_FILE, "utf8")) as { email: string; password: string };

  await page.goto("/login");
  await page.getByLabel("Email").fill(user.email);
  await page.getByLabel("Password").fill(user.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

  // the studio of the generated version
  await page.goto(`/videos/${video.video_id}`);
  await expect(page.getByText("version 1", { exact: false })).toBeVisible();
  await expect(page.getByTestId("player")).toBeVisible({ timeout: 60_000 });

  // the NL edit panel: instruction + selection → a proposal card; nothing changes yet
  await page.getByLabel("Instruction").fill("make him more skeptical");
  await page.getByLabel("Scene scn_reveal").check();
  await page.getByTestId("propose-edit").click();
  const card = page.getByTestId("proposal-card");
  await expect(card.getByTestId("proposal-status")).toHaveText("proposed", { timeout: 120_000 });
  await expect(card.getByTestId("proposal-ops")).toContainText("Acting in scn_reveal");
  await expect(card.getByTestId("proposal-ops")).toContainText("Eyebrow raise");
  await expect(card.getByLabel("Changes")).toContainText("scn_reveal");
  await expect(card.getByTestId("impact-summary")).toContainText("regenerate");
  await expect(card.getByLabel("Held by locks")).toContainText("Voice"); // the voice lock keeps the audio
  await expect(card.getByLabel("Coverage delta")).toBeVisible();
  await expect(page.getByText("version 1", { exact: false })).toBeVisible();

  // apply → a new version, generated (its parent passed approval)
  await card.getByTestId("apply-edit").click();
  await expect(page).not.toHaveURL(new RegExp(`version=${video.version_id}`));
  await expect(page.getByText("version 2", { exact: false })).toBeVisible({ timeout: 120_000 });
  await expect(page.getByTestId("player")).toBeVisible({ timeout: 300_000 });
  await expect(page.getByRole("list", { name: "Versions" })).toContainText("v2 · Edit");

  // compare v1 and v2
  await page.getByLabel("Compare v1").check();
  await page.getByLabel("Compare v2").check();
  await page.getByTestId("compare-versions").click();
  await expect(page).toHaveURL(/\/compare\?a=.+&b=.+/);
  await expect(page.getByRole("heading", { name: "Compare versions" })).toBeVisible();
  await expect(page.getByTestId("player-a")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("player-b")).toBeVisible({ timeout: 60_000 });
  const spec = page.getByRole("table", { name: "Spec differences" });
  await expect(spec).toContainText("Acting");
  await expect(spec).toContainText("scn_reveal");
  await expect(spec).not.toContainText("scn_hook");
  await expect(page.getByRole("table", { name: "CBS differences in scn_reveal" })).toBeVisible();
  await expect(page.getByRole("table", { name: "CBS differences in scn_hook" })).toHaveCount(0);
});
