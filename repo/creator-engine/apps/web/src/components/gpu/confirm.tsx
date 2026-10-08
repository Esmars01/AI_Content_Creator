"use client";
/** A destructive action behind a typed confirmation (terminate, delete): the button opens a small
 * panel saying what will happen; the action runs only after the operator types the phrase. */
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";

export function ConfirmAction({
  label,
  phrase,
  warning,
  onConfirm,
  disabled,
}: {
  label: string;
  phrase: string;
  warning: string;
  onConfirm: () => void;
  disabled?: boolean;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  if (!open)
    return (
      <Button size="sm" variant="outline" disabled={disabled} onClick={() => setOpen(true)}>
        {label}
      </Button>
    );
  return (
    <div
      className="flex flex-col gap-2 rounded border border-red-300 bg-red-50 p-2 text-sm"
      role="group"
      aria-label={label}
    >
      <p className="text-red-900">{warning}</p>
      <Label htmlFor={id}>
        Type <code className="font-mono">{phrase}</code> to confirm
      </Label>
      <Input id={id} value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
      <div className="flex gap-2">
        <Button
          size="sm"
          disabled={disabled || typed.trim() !== phrase}
          onClick={() => {
            setOpen(false);
            setTyped("");
            onConfirm();
          }}
        >
          {label}
        </Button>
        <Button size="sm" variant="outline" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
