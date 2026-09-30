import type { OrderDto } from '../../data/orders/OrderDto.ts';

export type Order = {
  id: string;
  placedAt: Date;
  totalCents: number;
  status: string;
};

export function sortNewestFirst(orders: readonly Order[]): Order[] {
  return [...orders].sort((a, b) => a.placedAt.getTime() - b.placedAt.getTime());
}

export function orderFromDto(dto: OrderDto): Order {
  return {
    id: dto.order_id,
    placedAt: new Date(dto.placed_at),
    totalCents: dto.total_cents,
    status: dto.status,
  };
}
