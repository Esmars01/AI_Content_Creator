"use client";
import { useState } from "react";

import { Button } from "@/components/ui/button";

export function JsonViewer({ value, label }: { value: unknown; label: string }) {
  const [copied, setCopied] = useState(false);
  const text = JSON.stringify(value, null, 2);
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">{label}</span>
        <Button
          size="sm"
          variant="outline"
          onClick={async () => {
            await navigator.clipboard?.writeText(text);
            setCopied(true);
          }}
        >
          {copied ? "Copied" : "Copy JSON"}
        </Button>
      </div>
      <pre
        aria-label={label}
        className="max-h-[70vh] overflow-auto rounded-md bg-slate-950 p-3 font-mono text-xs leading-relaxed text-slate-100"
      >
        {text}
      </pre>
    </div>
  );
}
