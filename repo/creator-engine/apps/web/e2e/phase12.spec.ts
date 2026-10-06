/**
 * Phase 12 on the running stack (mock engines, fixture LLM): brand kits in Settings, a spec template
 * written as values and found in the list, and a research note ingested on a project page.
 */
import { readFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

import { USER_FILE } from "./global-setup";

test("brand kits, templates and research sources", async ({ page }) => {
  const user = JSON.parse(readFileSync(USER_FILE, "utf8")) as { email: string; password: string };
  const suffix = Date.now().toString(36);
  await page.goto("/login");
  await page.getByLabel("Email").fill(user.email);
  await page.getByLabel("Password").fill(user.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

  await page.getByRole("link", { name: "Settings" }).first().click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await page.getByLabel("Name", { exact: true }).fill(`Kit ${suffix}`);
  await page.getByRole("button", { name: "Create brand kit" }).click();
  await expect(page.getByTestId("brand-kit-row").filter({ hasText: `Kit ${suffix}` })).toBeVisible();

  await page.getByRole("link", { name: "Templates" }).first().click();
  await expect(page.getByRole("heading", { name: "Templates" })).toBeVisible();
  await page.getByLabel("Name", { exact: true }).fill(`Captions ${suffix}`);
  await page.getByLabel("Write the values").check();
  await page.getByRole("button", { name: "Save template" }).click();
  const row = page.getByTestId("template-row").filter({ hasText: `Captions ${suffix}` });
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: `Captions ${suffix}` }).click();
  await expect(page.getByText("/captions/style_id").first()).toBeVisible();

  await page.getByRole("link", { name: "Projects" }).first().click();
  const project = page.locator("table a").first();
  await project.click();
  await expect(page.getByRole("heading", { name: "Research sources" })).toBeVisible();
  await page.getByLabel("Source type").selectOption("note");
  await page.getByLabel("Title").fill(`Notes ${suffix}`);
  await page.getByLabel("Note text").fill("Our 2026 survey: most viewers watch the first three seconds.");
  await page.getByRole("button", { name: "Add source" }).click();
  const source = page.getByTestId("source-row").filter({ hasText: `Notes ${suffix}` });
  await expect(source).toBeVisible();
  await expect(source.getByText("Ingested")).toBeVisible({ timeout: 120_000 });
});
