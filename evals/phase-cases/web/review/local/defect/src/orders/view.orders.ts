import type { TGetJson, TOrder } from "./api.orders.ts";
import { HttpOrdersRepository } from "./data/ordersRepository.ts";
import { sortOrdersForDisplay } from "./sort.orders.ts";

export type TOrdersView =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "empty" }
  | { kind: "loaded"; orders: TOrder[] };

export const initialOrdersView: TOrdersView = { kind: "loading" };

export async function loadOrdersView(getJson: TGetJson): Promise<TOrdersView> {
  let orders: TOrder[];
  try {
    orders = await new HttpOrdersRepository(getJson).list();
  } catch {
    orders = [];
  }
  if (orders.length === 0) return { kind: "empty" };
  return { kind: "loaded", orders: sortOrdersForDisplay(orders) };
}
