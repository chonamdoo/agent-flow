import type { TOrdersView } from "./view.orders.ts";

function statusMessage(view: TOrdersView): string {
  switch (view.kind) {
    case "loading":
      return "Loading orders…";
    case "empty":
      return "You have no orders yet.";
    case "loaded":
      return `${view.orders.length} ${view.orders.length === 1 ? "order" : "orders"}`;
    case "error":
      return "";
  }
}

export function OrdersScreen({ view }: { view: TOrdersView }) {
  return (
    <section aria-labelledby="orders-heading">
      <h2 id="orders-heading">Orders</h2>
      {/* Always mounted so screen readers announce every change of its text. */}
      <p role="status" aria-live="polite">
        {statusMessage(view)}
      </p>
      {view.kind === "error" && <p role="alert">{view.message}</p>}
      {view.kind === "loaded" && (
        <ul aria-labelledby="orders-heading">
          {view.orders.map((order) => (
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
