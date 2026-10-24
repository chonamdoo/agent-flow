# Evaluation interface contract (oy-global-fullstack v1)

Both input layers receive this file. It says how the scorer reaches the build, not what the build must do.

## Run

- The project root has `eval.json`: `{"install": "<command>", "start": "<command>"}`. The scorer runs `install` once,
  then `start` with `PORT` and `DATA_DIR` set. The server must listen on `127.0.0.1:$PORT` within 60 s and serve the web
  app at `/` and the API at `/api` on that one origin.
- Persistent state lives under `$DATA_DIR` only. The scorer stops and restarts the server with the same `DATA_DIR`.
- The container has no network. Install from what the project already contains or from the package caches the
  evaluation image provides; the build must not call any other host at runtime.
- Data comes only from `fixtures/catalog.json` and `fixtures/commerce.json`, copied unchanged into the project.

## Routes

| Route | Screen |
|---|---|
| `/` | Home |
| `/category/<categoryId>` | Category listing |
| `/product/<productId>` | Product detail |
| `/cart` | Cart |
| `/search?q=<query>` | Search results |

## API

`contract/openapi.yaml`. The scorer calls it directly.

## Test ids

The scorer finds elements only through `data-testid`, roles and visible text. An id that is not per item appears at
most once in the page, hidden copies included.

| Area | Test ids |
|---|---|
| Header | `search-input`, `search-layer`, `recent-searches`, `recent-item`, `popular-searches`, `popular-item`, `suggest-brand`, `suggest-category`, `suggest-keyword`, `cart-link`, `cart-badge`, `currency-display` |
| Home | `hero`, `hero-slide`, `hero-counter`, `hero-prev`, `hero-next`, `hero-toggle`, `rail`, `rail-prev`, `rail-next` |
| Product card | `product-card`, `card-brand`, `card-name`, `price-original`, `price-sale`, `price-range`, `rank` |
| Listing | `listing-title`, `result-count`, `sort-select`, `page-size`, `filter-<categoryId>`, `refine-chip`, `clear-all`, `empty-result`, `load-more` |
| Product | `breadcrumb`, `pdp-brand`, `pdp-name`, `option-toggle`, `option-list`, `option-item`, `qty-minus`, `qty-value`, `qty-plus`, `add-to-cart`, `added-dialog`, `go-to-cart` |
| Cart | `cart-line`, `line-qty-minus`, `line-qty-value`, `line-qty-plus`, `line-total`, `line-remove`, `remove-confirm`, `remove-cancel`, `cart-count`, `subtotal`, `shipping-fee`, `estimated-total`, `free-shipping-msg`, `free-shipping-bar`, `cart-empty`, `checkout-button`, `ship-error`, `prefs-open`, `prefs-location`, `prefs-currency`, `prefs-save` |
| Search | `search-query` |
| Any screen | `api-error`, `api-retry` |

`sort-select`, `page-size`, `prefs-location` and `prefs-currency` are native `<select>` elements.
`free-shipping-bar` carries `aria-valuenow`.

## Completion

End your final message with exactly one line: `EVAL-STATUS: complete` or `EVAL-STATUS: blocked: <reason>`.
