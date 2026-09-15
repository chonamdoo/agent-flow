'use client';

import { useState } from 'react';
import type { Product } from '@/entities/product';
import { messages } from '@/i18n/messages';
import { addProduct, removeProduct, type CartLine } from '../model/cart';

type CartProps = {
  products: readonly Product[];
};

export function Cart({ products }: CartProps) {
  const [lines, setLines] = useState<CartLine[]>([]);
  const quantity = lines.reduce((total, line) => total + line.quantity, 0);

  return (
    <div>
      <ul aria-label="Matching products">
        {products.map((product) => (
          <li key={product.id}>
            <span>{product.name}</span>{' '}
            <button
              type="button"
              onClick={() => setLines((current) => addProduct(current, product))}
            >
              Add {product.name}
            </button>
          </li>
        ))}
      </ul>
      <section aria-label={messages.cartTitle}>
        <h2>{messages.cartTitle}</h2>
        <p aria-live="polite">Items in cart: {quantity}</p>
        {lines.length === 0 ? (
          <p>{messages.emptyCart}</p>
        ) : (
          <ul>
            {lines.map((line) => (
              <li key={line.product.id}>
                {line.product.name}: {line.quantity}{' '}
                <button
                  type="button"
                  onClick={() =>
                    setLines((current) => removeProduct(current, line.product.id))
                  }
                >
                  Remove {line.product.name}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
