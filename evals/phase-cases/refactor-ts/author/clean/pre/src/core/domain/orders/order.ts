export type OrderState = "PENDING" | "PAID" | "SHIPPED" | "DELIVERED" | "CANCELLED" | "REFUNDED";

export interface Order {
  readonly id: string;
  readonly state: OrderState;
  readonly placedAt: string;
  /** Negative for a refunded order: the amount returned to the customer. */
  readonly totalCents: number;
}
