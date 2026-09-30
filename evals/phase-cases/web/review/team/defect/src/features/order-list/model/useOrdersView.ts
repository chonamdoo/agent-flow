import { useEffect, useState } from "react";
import type { GetJson } from "../api/fetchOrders.ts";
import { initialOrdersView, loadOrdersView, type OrdersView } from "./loadOrdersView.ts";

export function useOrdersView(getJson: GetJson): OrdersView {
  const [view, setView] = useState<OrdersView>(initialOrdersView);

  useEffect(() => {
    let active = true;
    void loadOrdersView(getJson).then((next) => {
      if (active) setView(next);
    });
    return () => {
      active = false;
    };
  }, [getJson]);

  return view;
}
