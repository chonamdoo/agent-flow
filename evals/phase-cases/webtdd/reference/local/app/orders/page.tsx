import { OrdersClient } from "./OrdersClient.tsx";

export const metadata = { title: "Your orders" };

export default function OrdersPage() {
  return (
    <main>
      <h1>Your orders</h1>
      <OrdersClient />
    </main>
  );
}
