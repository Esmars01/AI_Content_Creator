/**
 * Every main route at phone, tablet, laptop and desktop widths (audit 2026-10): no horizontal page
 * overflow, the navigation is reachable (sidebar on wide screens, the Menu drawer on narrow ones), and
 * each page renders its heading. Screenshots land in .data/e2e-web/responsive for review.
 */
import { readFileSync } from "node:fs";

import { expect, type Page, test } from "@playwright/test";

import { USER_FILE } from "./global-setup";

const VIEWPORTS = [
  { name: "phone", width: 375, height: 812 },
  { name: "tablet", width: 768, height: 1024 },
  { name: "laptop", width: 1280, height: 800 },
  { name: "desktop", width: 1440, height: 1000 },
] as const;

const ROUTES = [
  ["/", "Dashboard"],
  ["/create", "Create"],
  ["/projects", "Projects"],
  ["/creators", "Creators"],
  ["/worlds", "Worlds"],
  ["/templates", "Templates"],
  ["/jobs", "Jobs"],
  ["/ratings", "Ratings"],
  ["/models", "Models"],
  ["/gpu", "GPU"],
  ["/settings", "Settings"],
  ["/developer", "Developer"],
] as const;

async function signIn(page: Page): Promise<void> {
  const user = JSON.parse(readFileSync(USER_FILE, "utf8")) as { email: string; password: string };
  await page.goto("/login");
  await page.getByLabel("Email").fill(user.email);
  await page.getByLabel("Password").fill(user.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
}

async function overflow(page: Page): Promise<number> {
  return page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
}

for (const viewport of VIEWPORTS) {
  test(`main routes fit a ${viewport.name} screen (${viewport.width}px)`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await signIn(page);
    const studio = await page.evaluate(async () => {
      // a video to open in the studio, when the stack has one
      const projects = (await (await fetch("/api/v1/projects?limit=20")).json()) as { items: { id: string }[] };
      for (const project of projects.items ?? []) {
        const videos = (await (await fetch(`/api/v1/projects/${project.id}/videos?limit=1`)).json()) as {
          items: { id: string }[];
        };
        const first = videos.items?.[0];
        if (first) return `/videos/${first.id}`;
      }
      return null;
    });
    const routes: (readonly [string, string | null])[] = [...ROUTES, ...(studio ? [[studio, null] as const] : [])];
    for (const [path, heading] of routes) {
      await page.goto(path);
      if (heading) await expect(page.getByRole("heading", { name: heading, exact: true }).first()).toBeVisible();
      else await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
      await page.waitForLoadState("networkidle").catch(() => undefined);
      await page.screenshot({
        path: `../../.data/e2e-web/responsive/${viewport.name}${path.replaceAll("/", "_") || "_"}.png`,
        fullPage: true,
      });
      expect(await overflow(page), `${path} overflows horizontally at ${viewport.width}px`).toBeLessThanOrEqual(1);
    }
    // the navigation is reachable at every width
    if (viewport.width < 768) {
      await page.getByRole("button", { name: "Open navigation" }).click();
      const drawer = page.getByRole("dialog", { name: "Navigation" });
      await expect(drawer.getByRole("link", { name: "Projects" })).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(drawer).toBeHidden();
    } else {
      await expect(
        page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Projects" }),
      ).toBeVisible();
    }
  });
}
