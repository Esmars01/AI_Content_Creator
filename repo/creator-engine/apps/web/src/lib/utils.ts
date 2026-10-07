import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/**
 * Starts a browser download of a (presigned) URL. The API signs downloads with an attachment
 * disposition, so following the link saves the file without leaving the page.
 */
export function startDownload(url: string, filename?: string): void {
  const link = document.createElement("a");
  link.href = url;
  if (filename) link.download = filename;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
}
