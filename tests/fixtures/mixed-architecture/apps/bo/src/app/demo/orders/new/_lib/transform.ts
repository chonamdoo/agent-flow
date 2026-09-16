import type { ValidatedOrder } from "./schema";

export function toPayload(order: ValidatedOrder) {
  return {
    reference: order.reference,
    lines: order.lines.map((line) => line.kind === "note"
      ? { kind: line.kind, text: line.note }
      : { kind: line.kind, units: line.quantity }),
  };
}

export type OrderPayload = ReturnType<typeof toPayload>;
