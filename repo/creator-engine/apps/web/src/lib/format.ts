export function seconds(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${value.toFixed(digits)} s`;
}

export function usd(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(n)) return "—";
  return n < 0.01 && n > 0 ? `< $0.01` : `$${n.toFixed(2)}`;
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

/** "talking_head_explainer" → "Talking head explainer" */
export function humanize(token: string | null | undefined): string {
  if (!token) return "—";
  const text = token.replaceAll("_", " ").replaceAll(".", " · ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function percent(value: number, digits = 0): string {
  return `${(value * 100).toFixed(digits)} %`;
}
