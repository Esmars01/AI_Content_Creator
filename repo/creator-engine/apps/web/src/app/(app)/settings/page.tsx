"use client";
/** Settings (Phase 12): brand kits. Other settings (operator profile, feature flags) are read on the
 * Developer page and changed in configuration. */
import { PageHeader } from "@/components/app-shell";
import { BrandKitsManager } from "@/components/brand-kits";

export default function SettingsPage() {
  return (
    <div className="flex flex-col gap-4">
      <PageHeader title="Settings" description="Brand kits of this organization." />
      <BrandKitsManager />
    </div>
  );
}
