export function routeLabel(count: number): string {
  return `${count} ${count === 1 ? "order" : "orders"} ready for review`;
}
