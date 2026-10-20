import type { CartRepository } from "../../domain/cart/CartRepository.ts";
import { CartQuoteUnavailableError, type CartLineRequest, type CartQuote } from "../../domain/cart/cartQuote.ts";

type PostJson = (path: string, body: unknown) => Promise<unknown>;

function cents(value: unknown): number {
  if (typeof value !== "string" || !/^\d+\.\d{2}$/.test(value)) throw new CartQuoteUnavailableError("bad money");
  const [whole, fraction] = value.split(".");
  return Number(whole) * 100 + Number(fraction);
}

export class HttpCartRepository implements CartRepository {
  private readonly postJson: PostJson;

  constructor(postJson: PostJson) {
    this.postJson = postJson;
  }

  async quote(lines: CartLineRequest[]): Promise<CartQuote> {
    let body: any;
    try {
      body = await this.postJson("/api/cart/quote", {
        lines: lines.map((line) => ({ product_id: line.productId, quantity: line.quantity })),
      });
      return {
        lines: body.lines.map((line: any) => ({
          productId: line.product_id,
          name: line.name,
          quantity: line.quantity,
          unitPriceCents: cents(line.unit_price),
          listPriceCents: cents(line.list_price),
          lineTotalCents: cents(line.line_total),
          discountRate: line.discount_rate,
        })),
        subtotalCents: cents(body.subtotal),
        totalDiscountCents: cents(body.total_discount),
        shippingCents: cents(body.shipping),
        freeShippingRemainingCents: cents(body.free_shipping_remaining),
        estimatedTotalCents: cents(body.estimated_total),
      };
    } catch (error) {
      if (error instanceof CartQuoteUnavailableError) throw error;
      throw new CartQuoteUnavailableError(String(error));
    }
  }
}
