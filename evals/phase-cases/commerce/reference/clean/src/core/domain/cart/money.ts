export function formatUsd(cents: number): string {
  return `US$${Math.trunc(cents / 100)}.${String(cents % 100).padStart(2, "0")}`;
}
