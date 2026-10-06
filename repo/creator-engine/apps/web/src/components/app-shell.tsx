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
  Settings,
  Sparkles,
  Star,
  UserRound,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

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
}

/** §31 navigation. Sections built in later phases are listed, disabled, with their phase. */
export const NAV: NavItem[] = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/create", label: "Create", icon: Sparkles },
  { href: "/projects", label: "Projects", icon: FolderOpen },
  { href: "/creators", label: "Creators", icon: UserRound },
  { href: "/worlds", label: "Worlds", icon: Globe2 },
  { href: "/assets", label: "Assets", icon: Boxes, later: "a later phase" },
  { href: "/templates", label: "Templates", icon: LayoutTemplate },
  { href: "/models", label: "Models", icon: Briefcase },
  { href: "/ratings", label: "Ratings", icon: Star },
  { href: "/gpu", label: "GPU", icon: Cpu },
  { href: "/jobs", label: "Jobs", icon: ListChecks },
  { href: "/settings", label: "Settings", icon: Settings },
  { href: "/developer", label: "Developer", icon: Code2 },
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

  return (
    <div className="flex min-h-screen">
      <nav
        aria-label="Main"
        className="flex w-56 shrink-0 flex-col gap-1 border-r border-[var(--color-border)] bg-slate-50 p-3"
      >
        <Link href="/" className="mb-3 px-2 text-lg font-semibold">
          Creator Engine
        </Link>
        {NAV.map((item) => {
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
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={isActive(pathname, item.href) ? "page" : undefined}
              className={cn(
                "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-slate-800 hover:bg-slate-200",
                isActive(pathname, item.href) && "bg-slate-200 font-medium",
              )}
            >
              <Icon className="h-4 w-4" aria-hidden />
              {item.label}
            </Link>
          );
        })}
      </nav>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-end gap-3 border-b border-[var(--color-border)] px-6 py-2 text-sm">
          <label className="flex items-center gap-2" title="Advanced views show the structured fields (§31)">
            <input type="checkbox" checked={advanced} onChange={(e) => setAdvanced(userId, e.target.checked)} />
            Advanced
          </label>
          <span className="text-slate-700">
            {me.data.user.email} · {me.data.org.name} · {me.data.role}
          </span>
          <Button variant="ghost" size="sm" onClick={logout}>
            <LogOut className="h-4 w-4" aria-hidden /> Sign out
          </Button>
        </header>
        <main className="min-w-0 flex-1 p-6">{children}</main>
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
    <div className="mb-5 flex items-start justify-between gap-4">
      <div>
        <h1 className="text-2xl font-semibold">{title}</h1>
        {description ? <p className="mt-1 text-sm text-slate-700">{description}</p> : null}
      </div>
      {actions}
    </div>
  );
}
