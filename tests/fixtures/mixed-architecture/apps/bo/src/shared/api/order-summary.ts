import { parseResponse } from "@fixture/http";
import { z } from "zod";

const orderSummarySchema = z.object({ readyCount: z.number().int().nonnegative() });

export function parseOrderSummary(body: string) {
  return parseResponse(body, (value) => orderSummarySchema.parse(value));
}
