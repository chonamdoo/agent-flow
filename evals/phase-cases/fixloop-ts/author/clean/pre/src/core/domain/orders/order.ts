export type OrderState = "PENDING" | "PAID" | "SHIPPED" | "DELIVERED" | "CANCELLED";

export interface Order {
  readonly id: string;
  readonly state: OrderState;
  readonly placedAt: string;
  readonly totalCents: number;
}
