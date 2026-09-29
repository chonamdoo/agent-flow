export type Order = {
  id: string;
  placedAt: Date;
  totalCents: number;
  status: string;
};

export function sortNewestFirst(orders: readonly Order[]): Order[] {
  return [...orders].sort((a, b) => b.placedAt.getTime() - a.placedAt.getTime());
}
