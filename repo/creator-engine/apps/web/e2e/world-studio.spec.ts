/**
 * Phase 10 (§40): World Studio in the browser on the running stack (mock engines) — a draft of the
 * seeded world, plate candidates from BuildWorldPlatesWorkflow, a canonical plate chosen per
 * permitted position (each choice fingerprints the set), then approval.
 */
import { readFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

import { USER_FILE } from "./global-setup";

test("world → plates → choose → approve", async ({ page }) => {
  test.setTimeout(600_000);
  const user = JSON.parse(readFileSync(USER_FILE, "utf8")) as { email: string; password: string };
  await page.goto("/login");
  await page.getByLabel("Email").fill(user.email);
  await page.getByLabel("Password").fill(user.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

  await page.goto("/worlds");
  await page.getByRole("link", { name: "Alex's home office" }).first().click();
  await expect(page.getByRole("tablist", { name: "World Studio" })).toBeVisible();
  await expect(page.getByRole("img", { name: "Floor plan" })).toBeVisible();
  const start = page.getByRole("button", { name: "Start a draft" });
  if (await start.isVisible()) {
    await start.click();
    await expect(page.getByText("Editing the draft.")).toBeVisible({ timeout: 30_000 });
  }

  await page.getByRole("tab", { name: "Plates" }).click();
  await page.getByRole("button", { name: "Generate candidates" }).click();
  await expect(page.getByText("Job Succeeded")).toBeVisible({ timeout: 300_000 });
  const sections = page.locator("section").filter({ has: page.getByRole("button", { name: "Choose this plate" }) });
  const cells = await sections.count();
  expect(cells).toBeGreaterThan(0);
  for (let i = 0; i < cells; i += 1) {
    const cell = page.locator("section").nth(i);
    const choose = cell.getByRole("button", { name: "Choose this plate" }).first();
    if (await choose.isVisible()) {
      await choose.click();
      await expect(cell.getByRole("button", { name: "Chosen plate" })).toBeVisible({ timeout: 30_000 });
    }
  }
  await expect(page.getByText("Fingerprints: computed")).toBeVisible({ timeout: 300_000 });
  await page.getByRole("button", { name: "Approve world version" }).click();
  await expect(page.getByText("Approved: plates are frozen.")).toBeVisible({ timeout: 60_000 });

  await page.getByRole("tab", { name: "Versions" }).click();
  await expect(page.getByRole("button", { name: /approved/ }).last()).toBeVisible();
  await page.getByRole("tab", { name: "Continuity" }).click();
  await expect(page.getByRole("heading", { name: "Continuity" })).toBeVisible();
});
