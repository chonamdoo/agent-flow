"use client";

import { useOrdersView } from "../../src/features/order-list/model/useOrdersView.ts";
import { OrdersList } from "../../src/features/order-list/ui/OrdersList.tsx";
import { getJson } from "../../src/shared/api/getJson.ts";

export function OrdersClient() {
  const view = useOrdersView(getJson);
  return <OrdersList view={view} />;
}
