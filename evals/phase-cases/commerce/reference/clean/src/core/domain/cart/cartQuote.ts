export type CartLineRequest = { productId: string; quantity: number };

export type QuotedLine = {
  productId: string;
  name: string;
  quantity: number;
  unitPriceCents: number;
  listPriceCents: number;
  lineTotalCents: number;
  discountRate: number;
};

export type CartQuote = {
  lines: QuotedLine[];
  subtotalCents: number;
  totalDiscountCents: number;
  shippingCents: number;
  freeShippingRemainingCents: number;
  estimatedTotalCents: number;
};

export class CartQuoteUnavailableError extends Error {}
