/** Creates a throwaway editor in the dev organization (scripts/e2e_user.py) for this run. */
import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
export const USER_FILE = resolve(here, "../../../.data/e2e-web/user.json");

export default function globalSetup(): void {
  const root = resolve(here, "../../..");
  const out = execFileSync("uv", ["run", "--quiet", "python", "scripts/e2e_user.py"], {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "inherit"],
  });
  const line = out.trim().split("\n").at(-1) ?? "";
  JSON.parse(line); // fail early on anything but the credentials
  mkdirSync(dirname(USER_FILE), { recursive: true });
  writeFileSync(USER_FILE, line, { mode: 0o600 });
}
