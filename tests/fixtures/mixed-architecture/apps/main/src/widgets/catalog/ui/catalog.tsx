import type { Product } from '@/entities/product';
import { Cart } from '@/features/cart';
import { filterProducts } from '@/features/search';
import { messages } from '@/i18n/messages';

type CatalogProps = {
  products: readonly Product[];
  query: string;
};

export function Catalog({ products, query }: CatalogProps) {
  const matches = filterProducts(products, query);

  return (
    <section aria-label={messages.catalogTitle}>
      <h1>{messages.catalogTitle}</h1>
      <form method="get">
        <label>
          {messages.searchLabel}
          <input type="search" name="q" defaultValue={query} />
        </label>
        <button type="submit">{messages.searchAction}</button>
      </form>
      {matches.length === 0 ? <p>{messages.noProducts}</p> : null}
      <Cart products={matches} />
    </section>
  );
}
