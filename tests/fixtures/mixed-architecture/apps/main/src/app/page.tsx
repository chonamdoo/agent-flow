// Webpack rewrites new URL(...) into an asset wrapper that node:fs cannot read.
import { URL as FileURL } from 'node:url';
import { loadProducts } from '@/entities/product/index.server';
import { Catalog } from '@/widgets/catalog';

type PageProps = {
  searchParams: Promise<{ q?: string | string[] }>;
};

export default async function Page({ searchParams }: PageProps) {
  const [products, params] = await Promise.all([
    loadProducts(new FileURL('./data/catalog.json', import.meta.url)),
    searchParams,
  ]);
  const query = Array.isArray(params.q) ? (params.q[0] ?? '') : (params.q ?? '');

  return (
    <main>
      <Catalog products={products} query={query} />
    </main>
  );
}
