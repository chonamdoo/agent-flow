import { z } from "zod";

const noteSchema = z.string().trim().min(1, "Enter a note.");

export const orderSchema = z.object({
  reference: z.string().trim().min(1, "Enter an order reference."),
  lines: z.array(z.discriminatedUnion("kind", [
    z.object({ kind: z.literal("note"), note: noteSchema }),
    z.object({
      kind: z.literal("quantity"),
      quantity: z.string().regex(/^[1-9]\d*$/, "Enter a positive whole quantity.")
        .pipe(z.coerce.number<string>().int().positive()),
    }),
  ])).min(1),
});

export type OrderDraft = z.input<typeof orderSchema>;
export type ValidatedOrder = z.output<typeof orderSchema>;

export function validateReference(value: string): string | undefined {
  return orderSchema.shape.reference.safeParse(value).error?.issues[0]?.message;
}

export function validateNote(value: string): string | undefined {
  return noteSchema.safeParse(value).error?.issues[0]?.message;
}
