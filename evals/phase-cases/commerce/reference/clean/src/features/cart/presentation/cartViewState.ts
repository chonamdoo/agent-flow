import type { CartRepository } from "../../../core/domain/cart/CartRepository.ts";
import { CartQuoteUnavailableError, type CartLineRequest } from "../../../core/domain/cart/cartQuote.ts";
import { formatUsd } from "../../../core/domain/cart/money.ts";

export type CartLineView = {
  productId: string;
  name: string;
  quantity: number;
  unitPrice: string;
  lineTotal: string;
  discountLabel: string | null;
};

export type CartViewState =
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "empty" }
  | {
      kind: "loaded";
      lines: CartLineView[];
      itemCountLabel: string;
      subtotal: string;
      discountLabel: string | null;
      shipping: string;
      shippingMessage: string;
      estimatedTotal: string;
    };

export const initialCartViewState: CartViewState = { kind: "loading" };

export async function loadCartViewState(repository: CartRepository, lines: CartLineRequest[]): Promise<CartViewState> {
  if (lines.length === 0) return { kind: "empty" };
  let quote;
  try {
    quote = await repository.quote(lines);
  } catch (error) {
    if (error instanceof CartQuoteUnavailableError) return { kind: "error" };
    throw error;
  }
  const units = quote.lines.reduce((total, line) => total + line.quantity, 0);
  return {
    kind: "loaded",
    lines: quote.lines.map((line) => ({
      productId: line.productId,
      name: line.name,
      quantity: line.quantity,
      unitPrice: formatUsd(line.unitPriceCents),
      lineTotal: formatUsd(line.lineTotalCents),
      discountLabel: line.discountRate > 0 ? `${line.discountRate}%` : null,
    })),
    itemCountLabel: units === 1 ? "(1 item)" : `(${units} items)`,
    subtotal: formatUsd(quote.subtotalCents),
    discountLabel: quote.totalDiscountCents > 0 ? `-${formatUsd(quote.totalDiscountCents)}` : null,
    shipping: quote.shippingCents === 0 ? "Free" : formatUsd(quote.shippingCents),
    shippingMessage: quote.freeShippingRemainingCents > 0
      ? `Only ${formatUsd(quote.freeShippingRemainingCents)} left for free shipping`
      : "Free shipping",
    estimatedTotal: formatUsd(quote.estimatedTotalCents),
  };
}
