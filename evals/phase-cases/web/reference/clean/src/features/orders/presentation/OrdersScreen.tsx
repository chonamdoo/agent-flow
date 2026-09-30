import type { OrdersViewState } from "./ordersViewState.ts";

function statusMessage(state: OrdersViewState): string {
  switch (state.kind) {
    case "loading":
      return "Loading orders…";
    case "empty":
      return "You have no orders yet.";
    case "loaded":
      return `${state.orders.length} ${state.orders.length === 1 ? "order" : "orders"}`;
    case "error":
      return "";
  }
}

export function OrdersScreen({ state }: { state: OrdersViewState }) {
  return (
    <section aria-labelledby="orders-heading">
      <h2 id="orders-heading">Orders</h2>
      {/* Always mounted so screen readers announce every change of its text. */}
      <p role="status" aria-live="polite">
        {statusMessage(state)}
      </p>
      {state.kind === "error" && <p role="alert">{state.message}</p>}
      {state.kind === "loaded" && (
        <ul aria-labelledby="orders-heading">
          {state.orders.map((order) => (
            <li key={order.id}>
              Order {order.id}, {order.status}, placed{" "}
              <time dateTime={order.placedAt}>{order.placedAt.slice(0, 10)}</time>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
