/**
 * Phase 11 (§26, §24): the Models registry with benchmarks and the human rating queue render on the
 * running stack (mock engines).
 */
import { readFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

import { USER_FILE } from "./global-setup";

test("models registry and rating queue", async ({ page }) => {
  const user = JSON.parse(readFileSync(USER_FILE, "utf8")) as { email: string; password: string };
  await page.goto("/login");
  await page.getByLabel("Email").fill(user.email);
  await page.getByLabel("Password").fill(user.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

  await page.getByRole("link", { name: "Models" }).first().click();
  await expect(page.getByRole("heading", { name: "Models" })).toBeVisible();
  await expect(page.getByText("mock_avatar_segment").first()).toBeVisible();

  await page.getByRole("link", { name: "Ratings" }).first().click();
  await expect(page.getByRole("heading", { name: "Ratings" })).toBeVisible();
});
