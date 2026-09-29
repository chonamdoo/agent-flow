import { useState } from 'react';
import type { CreateOrdersEntry } from '../api/OrdersEntry.ts';
import { OrdersScreen } from './OrdersScreen.tsx';
import { createOrdersScreenStore } from './ordersScreenStore.ts';

export const createOrdersEntry: CreateOrdersEntry = ({ orderRepository }) =>
  function OrdersEntry() {
    const [store] = useState(() => createOrdersScreenStore(orderRepository));
    return <OrdersScreen store={store} />;
  };
