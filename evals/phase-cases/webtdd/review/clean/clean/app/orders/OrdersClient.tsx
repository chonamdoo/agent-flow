"use client";

import { HttpOrderRepository } from "../../src/core/data/orders/HttpOrderRepository.ts";
import { getJson } from "../../src/core/network/getJson.ts";
import { OrdersScreen } from "../../src/features/orders/presentation/OrdersScreen.tsx";
import { useOrdersViewState } from "../../src/features/orders/presentation/useOrdersViewState.ts";

// Composition root for the orders route: the only place that constructs the data adapter.
const orderRepository = new HttpOrderRepository(getJson);

export function OrdersClient() {
  const state = useOrdersViewState(orderRepository);
  return <OrdersScreen state={state} />;
}
