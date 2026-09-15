import type { Product } from '@/entities/product';

export type CartLine = {
  product: Product;
  quantity: number;
};

export function addProduct(lines: readonly CartLine[], product: Product): CartLine[] {
  const existing = lines.find((line) => line.product.id === product.id);
  if (!existing) return [...lines, { product, quantity: 1 }];
  return lines.map((line) =>
    line.product.id === product.id ? { ...line, quantity: line.quantity + 1 } : line,
  );
}

export function removeProduct(lines: readonly CartLine[], productId: string): CartLine[] {
  return lines.filter((line) => line.product.id !== productId);
}
