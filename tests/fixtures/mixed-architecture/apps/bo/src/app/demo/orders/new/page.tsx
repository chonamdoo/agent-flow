import { SectionTitle } from "../_components/section-title";
import { OrderForm } from "./_components/order-form";

export default function NewOrderPage() {
  return (
    <main>
      <SectionTitle title="Prepare a synthetic order" />
      <p>Submit to inspect the prepared payload locally. Nothing is sent or saved.</p>
      <OrderForm />
    </main>
  );
}
