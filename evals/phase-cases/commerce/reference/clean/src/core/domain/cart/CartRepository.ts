import type { CartLineRequest, CartQuote } from "./cartQuote.ts";

export interface CartRepository {
  quote(lines: CartLineRequest[]): Promise<CartQuote>;
}
