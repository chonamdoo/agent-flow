import { useEffect, useState } from "react";
import type { HttpOrderRepository } from "../../../core/data/orders/HttpOrderRepository.ts";
import { initialOrdersViewState, loadOrdersViewState, type OrdersViewState } from "./ordersViewState.ts";

export function useOrdersViewState(repository: HttpOrderRepository): OrdersViewState {
  const [state, setState] = useState<OrdersViewState>(initialOrdersViewState);

  useEffect(() => {
    let active = true;
    loadOrdersViewState(repository).then(
      (next) => {
        if (active) setState(next);
      },
      (error: unknown) => {
        // Anything other than OrdersUnavailableError is a defect; rethrow during render so the
        // route's error boundary handles it instead of an unhandled rejection.
        if (active) {
          setState(() => {
            throw error;
          });
        }
      },
    );
    return () => {
      active = false;
    };
  }, [repository]);

  return state;
}
