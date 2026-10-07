"use client";
import { useQueryClient } from "@tanstack/react-query";
import {
  Boxes,
  Briefcase,
  Code2,
  Cpu,
  FolderOpen,
  Globe2,
  LayoutDashboard,
  LayoutTemplate,
  ListChecks,
  LogOut,
  Menu,
  Settings,
  Sparkles,
  Star,
  UserRound,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { OfflineNotice } from "@/components/offline-notice";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/misc";
import { api, ApiError } from "@/lib/api";
import { useLiveEvents } from "@/lib/events";
import { useMe } from "@/lib/queries";
import { usePrefs } from "@/lib/store";
import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  label: string;
  icon: typeof LayoutDashboard;
  later?: string;
  group: NavGroup;
}

type NavGroup = "Make" | "Library" | "Quality" | "Operations";
const GROUPS: NavGroup[] = ["Make", "Library", "Quality", "Operations"];

/**
 * §31 navigation, grouped so the creative flow comes first and the operator surfaces (models, GPU
 * fleet, jobs, developer viewers) sit apart. Sections built in later phases are listed, disabled.
 */
export const NAV: NavItem[] = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard, group: "Make" },
  { href: "/create", label: "Create", icon: Sparkles, group: "Make" },
  { href: "/projects", label: "Projects", icon: FolderOpen, group: "Make" },
  { href: "/creators", label: "Creators", icon: UserRound, group: "Library" },
  { href: "/worlds", label: "Worlds", icon: Globe2, group: "Library" },
  { href: "/templates", label: "Templates", icon: LayoutTemplate, group: "Library" },
  { href: "/assets", label: "Assets", icon: Boxes, later: "a later phase", group: "Library" },
  { href: "/ratings", label: "Ratings", icon: Star, group: "Quality" },
  { href: "/models", label: "Models", icon: Briefcase, group: "Quality" },
  { href: "/jobs", label: "Jobs", icon: ListChecks, group: "Operations" },
  { href: "/gpu", label: "GPU", icon: Cpu, group: "Operations" },
  { href: "/settings", label: "Settings", icon: Settings, group: "Operations" },
  { href: "/developer", label: "Developer", icon: Code2, group: "Operations" },
];

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

export function AppShell({ children }: { children: ReactNode }) {
  const me = useMe();
  const router = useRouter();
  const pathname = usePathname();
  const client = useQueryClient();
  const unauthenticated = me.error instanceof ApiError && me.error.status === 401;
  useLiveEvents(Boolean(me.data));
  const userId = me.data?.user.id ?? "";
  const advanced = usePrefs((s) => s.advancedByUser[userId] ?? false);
  const setAdvanced = usePrefs((s) => s.setAdvanced);
  const [menuOpen, setMenuOpen] = useState(false);
  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && setMenuOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  useEffect(() => {
    if (unauthenticated) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [unauthenticated, pathname, router]);

  async function logout() {
    await api.POST("/v1/auth/logout");
    client.clear();
    router.replace("/login");
  }

  if (!me.data) {
    return (
      <div className="flex min-h-screen items-center justify-center" aria-busy="true">
        {me.error && !unauthenticated ? (
          <p className="text-sm text-red-800">The API is not reachable: {(me.error as Error).message}</p>
        ) : (
          <Skeleton className="h-6 w-48" />
        )}
      </div>
    );
  }

  const nav = (
    <nav aria-label="Main" className="flex flex-col gap-4">
      {GROUPS.map((group) => (
        <div key={group} className="flex flex-col gap-0.5">
          <p className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">{group}</p>
          {NAV.filter((item) => item.group === group).map((item) => {
            const Icon = item.icon;
            if (item.later) {
              return (
                <span
                  key={item.href}
                  aria-disabled="true"
                  title={`Arrives in ${item.later} (ROADMAP.md)`}
                  className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-slate-500"
                >
                  <Icon className="h-4 w-4" aria-hidden />
                  {item.label}
                  <span className="ml-auto text-[10px] uppercase tracking-wide">later</span>
                </span>
              );
            }
            const active = isActive(pathname, item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                onClick={() => setMenuOpen(false)}
                className={cn(
                  "flex min-h-9 items-center gap-2 rounded-md px-2 py-1.5 text-sm text-slate-800 hover:bg-slate-200",
                  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700",
                  active && "bg-slate-200 font-medium",
                )}
              >
                <Icon className="h-4 w-4" aria-hidden />
                {item.label}
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );

  return (
    <div className="flex min-h-screen">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-white px-3 py-2 text-sm shadow focus:not-sr-only focus:fixed focus:left-3 focus:top-3"
      >
        Skip to content
      </a>
      <aside className="hidden w-56 shrink-0 flex-col gap-3 border-r border-[var(--color-border)] bg-slate-50 p-3 md:flex">
        <Link href="/" className="mb-1 px-2 text-lg font-semibold">
          Creator Engine
        </Link>
        {nav}
      </aside>
      {menuOpen ? (
        <div className="fixed inset-0 z-40 md:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
          <button
            type="button"
            className="absolute inset-0 bg-slate-900/40"
            aria-label="Close navigation"
            onClick={() => setMenuOpen(false)}
          />
          <div className="relative flex h-full w-64 max-w-[85vw] flex-col gap-3 overflow-y-auto bg-slate-50 p-3 shadow-xl">
            <div className="flex items-center justify-between">
              <span className="px-2 text-lg font-semibold">Creator Engine</span>
              <Button variant="ghost" size="sm" onClick={() => setMenuOpen(false)} aria-label="Close navigation">
                <X className="h-4 w-4" aria-hidden />
              </Button>
            </div>
            {nav}
          </div>
        </div>
      ) : null}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center justify-end gap-x-3 gap-y-1 border-b border-[var(--color-border)] px-4 py-2 text-sm md:px-6">
          <Button
            variant="ghost"
            size="sm"
            className="mr-auto md:hidden"
            aria-label="Open navigation"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen(true)}
          >
            <Menu className="h-4 w-4" aria-hidden /> Menu
          </Button>
          <label className="flex items-center gap-2" title="Advanced views show the structured fields (§31)">
            <input type="checkbox" checked={advanced} onChange={(e) => setAdvanced(userId, e.target.checked)} />
            Advanced
          </label>
          <span className="max-w-full truncate text-slate-700" title={`${me.data.user.email} · ${me.data.org.name}`}>
            {me.data.user.email} · {me.data.org.name} · {me.data.role}
          </span>
          <Button variant="ghost" size="sm" onClick={logout}>
            <LogOut className="h-4 w-4" aria-hidden /> Sign out
          </Button>
        </header>
        <main id="main" tabIndex={-1} className="min-w-0 flex-1 p-4 outline-none md:p-6">
          <OfflineNotice />
          {children}
        </main>
      </div>
    </div>
  );
}

export function useAdvanced(): boolean {
  const me = useMe();
  return usePrefs((s) => s.advancedByUser[me.data?.user.id ?? ""] ?? false);
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold">{title}</h1>
        {description ? <p className="mt-1 text-sm text-slate-700">{description}</p> : null}
      </div>
      {actions}
    </div>
  );
}
