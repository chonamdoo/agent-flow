import type { Control, UseFormRegister } from "react-hook-form";
import type { OrderDraft } from "../_lib/schema";

export type OrderFieldProps = {
  control: Control<OrderDraft>;
  register: UseFormRegister<OrderDraft>;
};
