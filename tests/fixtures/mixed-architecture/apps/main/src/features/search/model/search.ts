import type { Product } from '@/entities/product';

export function filterProducts(products: readonly Product[], query: string): readonly Product[] {
  const normalizedQuery = query.trim().toLowerCase();
  if (normalizedQuery === '') return products;
  return products.filter((product) => product.name.toLowerCase().includes(normalizedQuery));
}
