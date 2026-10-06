import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { cn } from "@/lib/utils";

const badgeVariants = cva("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium", {
  variants: {
    variant: {
      default: "border-transparent bg-slate-800 text-white",
      outline: "border-[var(--color-input)] text-slate-800",
      success: "border-green-700 bg-green-50 text-green-900",
      warning: "border-amber-700 bg-amber-50 text-amber-900",
      danger: "border-red-700 bg-red-50 text-red-900",
      info: "border-blue-700 bg-blue-50 text-blue-900",
      muted: "border-slate-300 bg-slate-100 text-slate-700",
    },
  },
  defaultVariants: { variant: "default" },
});

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
