import { OrderRepositoryImpl } from '../core/data/orders/OrderRepositoryImpl.ts';
import type { OrdersEntry } from '../features/orders/api/OrdersEntry.ts';
import { createOrdersEntry } from '../features/orders/presentation/createOrdersEntry.tsx';
import { getJson } from '../shared/http.ts';

const Orders: OrdersEntry = createOrdersEntry({
  orderRepository: new OrderRepositoryImpl(getJson),
});

export function AppShell() {
  return <Orders />;
}
