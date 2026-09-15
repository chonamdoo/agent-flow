import type { Metadata } from "next";
import { parseOrderSummary } from "@/shared/api/order-summary";
import { formatLabel } from "@/shared/lib/format";
import { SectionTitle } from "./_components/section-title";
import { routeLabel } from "./_lib/route-label";

export const metadata: Metadata = { title: "Synthetic order workspace" };

export default function OrdersPage() {
  const summary = parseOrderSummary('{"readyCount":0}');
  return (
    <main>
      <SectionTitle title={formatLabel("Orders")} />
      <p>Synthetic response: {routeLabel(summary.readyCount)}</p>
      <nav aria-label="Order routes">
        <a href="/demo/orders/new">Prepare an order</a>{" "}
        <a href="/demo/orders/history">View synthetic history</a>
      </nav>
    </main>
  );
}
