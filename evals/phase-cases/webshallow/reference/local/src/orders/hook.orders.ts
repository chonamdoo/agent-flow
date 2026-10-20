import { useEffect, useState } from "react";
import type { TGetJson } from "./api.orders.ts";
import { initialOrdersView, loadOrdersView, type TOrdersView } from "./view.orders.ts";

export function useOrdersView(getJson: TGetJson): TOrdersView {
  const [view, setView] = useState<TOrdersView>(initialOrdersView);

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
