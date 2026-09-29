"use client";

import { useOrdersView } from "../../src/orders/hook.orders.ts";
import { OrdersScreen } from "../../src/orders/screen.orders.tsx";

async function getJson(path: string): Promise<unknown> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`GET ${path} failed with status ${response.status}`);
  return response.json();
}

export function OrdersClient() {
  const view = useOrdersView(getJson);
  return <OrdersScreen view={view} />;
}
