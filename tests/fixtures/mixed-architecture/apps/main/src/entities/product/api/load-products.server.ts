import 'server-only';

import { readFile } from 'node:fs/promises';
import { parseResponse } from '@fixture/http';
import { decodeProducts, type Product } from '../model/product';

export async function loadProducts(source: URL): Promise<Product[]> {
  const body = await readFile(source, 'utf8');
  return parseResponse(body, decodeProducts);
}
