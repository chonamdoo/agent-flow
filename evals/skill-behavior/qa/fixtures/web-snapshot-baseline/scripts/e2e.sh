#!/bin/sh
actual=$(node -e 'import("./src/price.mjs").then(m => console.log(m.formatPrice(5766)))')
if [ "$1" = "--update" ]; then echo "$actual" > e2e/__snapshots__/price.txt; echo "snapshot updated"; exit 0; fi
expected=$(cat e2e/__snapshots__/price.txt)
if [ "$actual" = "$expected" ]; then echo "1 passed"; exit 0; fi
echo "1 failed: price snapshot mismatch: expected '$expected' got '$actual'"
exit 1
