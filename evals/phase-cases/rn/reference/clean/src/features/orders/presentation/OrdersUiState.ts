import type { Order } from '../../../core/domain/orders/Order.ts';
import { formatCents } from '../../../shared/money.ts';

export type OrderRowUiModel = {
  id: string;
  totalLabel: string;
  statusLabel: string;
};

export type OrdersUiState =
  | { status: 'loading' }
  | { status: 'content'; rows: OrderRowUiModel[] }
  | { status: 'empty'; message: string }
  | { status: 'error'; message: string };

export function toOrderRow(order: Order): OrderRowUiModel {
  return {
    id: order.id,
    totalLabel: formatCents(order.totalCents),
    statusLabel: order.status,
  };
}
