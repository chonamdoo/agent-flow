export function emptyMessage(count: number): string {
  return count === 0 ? "No orders yet" : `${count} orders`;
}
