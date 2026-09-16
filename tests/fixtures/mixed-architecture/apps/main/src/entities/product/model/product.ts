import { z } from 'zod';

const productSchema = z.object({
  id: z.string().min(1),
  name: z.string().min(1),
});

const productsSchema = z.array(productSchema).refine(
  (products) => new Set(products.map((product) => product.id)).size === products.length,
  { message: 'Product identifiers must be unique within a catalog.' },
);

export type Product = z.infer<typeof productSchema>;

export function decodeProducts(value: unknown): Product[] {
  return productsSchema.parse(value);
}
