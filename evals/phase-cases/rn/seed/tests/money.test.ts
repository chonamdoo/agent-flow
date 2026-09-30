import test from 'node:test';
import assert from 'node:assert/strict';
import { formatCents } from '../src/shared/money.ts';

test('formatCents renders dollars with two decimals', () => {
  assert.equal(formatCents(1250), '$12.50');
  assert.equal(formatCents(-5), '-$0.05');
});
