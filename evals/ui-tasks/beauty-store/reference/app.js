"use strict";

// ---------- constants and shared state ----------

const KEYS = { cart: "store.cart", recent: "store.recent", prefs: "store.prefs" };
const SUGGEST_DELAY = 200;
const HERO_INTERVAL = 6000;
const RAIL_PER_VIEW = 5;
const MAX_RECENT = 30;
const SORTS = [
  { id: "popular", label: "Most Popular", compare: (a, b) => b.popularity - a.popularity },
  { id: "price-asc", label: "Price Low to High", compare: (a, b) => a.salePrice - b.salePrice },
  { id: "price-desc", label: "Price High to Low", compare: (a, b) => b.salePrice - a.salePrice },
];

let catalog = null;
let constants = null;
const productsById = new Map();
const categoriesById = new Map(); // id -> { id, name, parentId, childIds }

const main = document.getElementById("main");

// ---------- small helpers ----------

function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// Money is handled in integer cents so sums like 60 - 18 never show float noise.
const cents = (amount) => Math.round(amount * 100);
const money = (centsValue) => `$${(centsValue / 100).toFixed(2)}`;

function el(html) {
  const template = document.createElement("template");
  template.innerHTML = html.trim();
  return template.content.firstElementChild;
}

function load(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function save(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage unavailable: state just won't persist
  }
}

function swatchColor(id) {
  const n = Number(String(id).replace(/\D/g, "")) || 0;
  return `hsl(${(n * 47) % 360} 35% 85%)`;
}

// ---------- catalog ----------

function indexCategories(nodes, parentId = null) {
  for (const node of nodes) {
    const children = node.children || [];
    categoriesById.set(node.id, { id: node.id, name: node.name, parentId, childIds: children.map((c) => c.id) });
    indexCategories(children, node.id);
  }
}

function descendantIds(id) {
  const out = [];
  for (const childId of categoriesById.get(id).childIds) out.push(childId, ...descendantIds(childId));
  return out;
}

const subtreeIds = (id) => [id, ...descendantIds(id)];

function categoryPath(id) {
  const path = [];
  for (let cat = categoriesById.get(id); cat; cat = categoriesById.get(cat.parentId)) path.unshift(cat);
  return path;
}

function productsInCategories(ids) {
  const wanted = new Set(ids);
  return catalog.products.filter((p) => wanted.has(p.categoryId));
}

function maxQty(product, option) {
  return Math.min(constants.maxQty, option ? option.stock : constants.maxQty);
}

// ---------- persisted stores ----------

const getCart = () => load(KEYS.cart, []);

function setCart(lines) {
  save(KEYS.cart, lines);
  updateBadge();
}

function addToCart(productId, optionId, qty) {
  const lines = getCart();
  const product = productsById.get(productId);
  const option = product.options.find((o) => o.id === optionId) || null;
  const line = lines.find((l) => l.productId === productId && l.optionId === optionId);
  if (line) line.qty = Math.min(maxQty(product, option), line.qty + qty);
  else lines.push({ productId, optionId, qty });
  setCart(lines);
}

function updateBadge() {
  const badge = document.getElementById("cart-badge");
  const count = getCart().length;
  badge.textContent = String(count);
  badge.hidden = count === 0;
}

function getPrefs() {
  return load(KEYS.prefs, { location: "GLOBAL", currency: null });
}

const locationById = (id) => catalog.locations.find((l) => l.id === id) || catalog.locations[0];

function recordSearch(query) {
  const recent = load(KEYS.recent, []).filter((term) => term !== query);
  recent.unshift(query);
  save(KEYS.recent, recent.slice(0, MAX_RECENT));
}

// ---------- routing ----------

function navigate(url) {
  history.pushState(null, "", url);
  route();
}

document.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-link]");
  if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
  event.preventDefault();
  navigate(link.getAttribute("href"));
});

window.addEventListener("popstate", route);

function route() {
  if (!catalog) return; // start() routes once the catalog has loaded
  stopHero();
  closeLayer();
  const { pathname, search } = window.location;
  const params = new URLSearchParams(search);
  let match;
  if (pathname === "/") renderHome();
  else if ((match = pathname.match(/^\/category\/([^/]+)$/)) && categoriesById.has(match[1])) renderListing(match[1]);
  else if ((match = pathname.match(/^\/product\/([^/]+)$/)) && productsById.has(match[1])) renderProduct(match[1]);
  else if (pathname === "/cart") renderCart();
  else if (pathname === "/search") renderSearch(params.get("q") || "");
  else if (pathname === "/checkout") renderCheckout();
  else renderNotFound();
  window.scrollTo(0, 0);
}

// ---------- product cards ----------

function priceHtml(product, option = null) {
  if (!option && product.options.length) {
    const sales = product.options.map((o) => cents(o.salePrice));
    const min = Math.min(...sales);
    const max = Math.max(...sales);
    if (min !== max) return `<span data-testid="price-range" class="price-sale">${money(min)} - ${money(max)}</span>`;
  }
  const item = option || product;
  const sale = `<span data-testid="price-sale" class="price-sale">${money(cents(item.salePrice))}</span>`;
  if (item.originalPrice > item.salePrice) {
    return `<s data-testid="price-original" class="price-original">${money(cents(item.originalPrice))}</s> ${sale}`;
  }
  return sale;
}

function cardHtml(product, rank = null) {
  return `
    <a data-testid="product-card" class="card" href="/product/${esc(product.id)}" data-link>
      <div class="swatch" style="background:${swatchColor(product.id)}"></div>
      ${rank ? `<span data-testid="rank" class="rank">${rank}</span>` : ""}
      <div data-testid="card-brand" class="card-brand">${esc(product.brand)}</div>
      <div data-testid="card-name" class="card-name">${esc(product.name)}</div>
      <div class="prices">${priceHtml(product)}</div>
    </a>`;
}

// ---------- home: hero and rails ----------

const hero = { index: 0, timer: null };

function renderHome() {
  hero.index = 0;
  main.innerHTML = `
    <section data-testid="hero" class="hero">
      <div class="hero-stage"></div>
      <div class="hero-controls">
        <button data-testid="hero-prev" type="button" aria-label="Previous slide">&lsaquo;</button>
        <span data-testid="hero-counter" class="hero-counter"></span>
        <button data-testid="hero-next" type="button" aria-label="Next slide">&rsaquo;</button>
        <button data-testid="hero-toggle" type="button"></button>
      </div>
    </section>
    <div id="rails"></div>`;
  main.querySelector('[data-testid="hero-prev"]').addEventListener("click", () => moveHero(-1));
  main.querySelector('[data-testid="hero-next"]').addEventListener("click", () => moveHero(1));
  main.querySelector('[data-testid="hero-toggle"]').addEventListener("click", () => (hero.timer ? stopHero() : startHero()));
  startHero();
  showHero();
  const rails = document.getElementById("rails");
  for (const rail of catalog.rails) rails.append(railElement(rail, 0));
}

function showHero() {
  const slides = catalog.hero;
  const slide = slides[hero.index];
  main.querySelector(".hero-stage").innerHTML = `
    <div data-testid="hero-slide" class="hero-slide" style="background:${esc(slide.color)}">
      <h2>${esc(slide.title)}</h2>
    </div>`;
  main.querySelector('[data-testid="hero-counter"]').textContent = `${hero.index + 1} / ${slides.length}`;
  const toggle = main.querySelector('[data-testid="hero-toggle"]');
  toggle.textContent = hero.timer ? "Pause" : "Play";
  toggle.setAttribute("aria-pressed", String(!hero.timer));
}

function moveHero(step) {
  const total = catalog.hero.length;
  hero.index = (hero.index + step + total) % total;
  // A manual move restarts the 6 s wait so the slide just chosen gets a full turn.
  if (hero.timer) startHero();
  showHero();
}

function startHero() {
  clearInterval(hero.timer);
  hero.timer = setInterval(() => {
    hero.index = (hero.index + 1) % catalog.hero.length;
    showHero();
  }, HERO_INTERVAL);
  if (main.querySelector(".hero-stage")) showHero();
}

function stopHero() {
  clearInterval(hero.timer);
  hero.timer = null;
  if (main.querySelector(".hero-stage")) showHero();
}

function railElement(rail, page) {
  const products = rail.productIds.map((id) => productsById.get(id));
  const pages = Math.ceil(products.length / RAIL_PER_VIEW);
  const start = page * RAIL_PER_VIEW;
  const cards = products
    .map((p, i) => ({ p, rank: rail.ranked ? i + 1 : null }))
    .slice(start, start + RAIL_PER_VIEW)
    .map(({ p, rank }) => cardHtml(p, rank))
    .join("");
  const node = el(`
    <section data-testid="rail" class="rail">
      <div class="rail-head">
        <h2>${esc(rail.title)}</h2>
        <div>
          <button data-testid="rail-prev" type="button" aria-label="Previous" ${page === 0 ? "disabled" : ""}>&lsaquo;</button>
          <button data-testid="rail-next" type="button" aria-label="Next" ${page >= pages - 1 ? "disabled" : ""}>&rsaquo;</button>
        </div>
      </div>
      <div class="grid rail-grid">${cards}</div>
    </section>`);
  node.querySelector('[data-testid="rail-prev"]').addEventListener("click", () => node.replaceWith(railElement(rail, page - 1)));
  node.querySelector('[data-testid="rail-next"]').addEventListener("click", () => node.replaceWith(railElement(rail, page + 1)));
  return node;
}

// ---------- category listing ----------

let listing = null;

function renderListing(categoryId) {
  const category = categoriesById.get(categoryId);
  listing = { categoryId, sort: SORTS[0].id, pageSize: constants.pageSizes[0], page: 1, filters: new Set() };
  const filterRows = descendantIds(categoryId)
    .map((id) => {
      const depth = categoryPath(id).length - categoryPath(categoryId).length;
      return `<label class="filter" style="padding-left:${(depth - 1) * 16}px">
        <input data-testid="filter-${esc(id)}" type="checkbox" value="${esc(id)}"> ${esc(categoriesById.get(id).name)}
      </label>`;
    })
    .join("");
  main.innerHTML = `
    <h1 data-testid="listing-title">${esc(category.name)}</h1>
    <div class="listing">
      ${filterRows ? `<aside class="filters"><h3>Category</h3>${filterRows}</aside>` : ""}
      <section class="results">
        <div class="toolbar">
          <span data-testid="result-count" id="result-count"></span>
          <span class="toolbar-controls">
            <select data-testid="sort-select" aria-label="Sort">
              ${SORTS.map((s) => `<option value="${s.id}">${esc(s.label)}</option>`).join("")}
            </select>
            <select data-testid="page-size" aria-label="Items per page">
              ${constants.pageSizes.map((n) => `<option value="${n}">${n}</option>`).join("")}
            </select>
          </span>
        </div>
        <div id="chips" class="chips"></div>
        <div id="listing-body"></div>
      </section>
    </div>`;
  main.querySelector('[data-testid="sort-select"]').addEventListener("change", (e) => {
    listing.sort = e.target.value;
    showListingResults(true);
  });
  main.querySelector('[data-testid="page-size"]').addEventListener("change", (e) => {
    listing.pageSize = Number(e.target.value);
    showListingResults(true);
  });
  for (const box of main.querySelectorAll(".filters input")) {
    box.addEventListener("change", () => {
      if (box.checked) listing.filters.add(box.value);
      else listing.filters.delete(box.value);
      showListingResults(true);
    });
  }
  showListingResults(true);
}

function listingProducts() {
  const roots = listing.filters.size ? [...listing.filters] : [listing.categoryId];
  const products = productsInCategories(roots.flatMap(subtreeIds));
  const sort = SORTS.find((s) => s.id === listing.sort);
  return products.sort(sort.compare);
}

function setFilter(id, checked) {
  const box = main.querySelector(`[data-testid="filter-${id}"]`);
  if (box) box.checked = checked;
  if (checked) listing.filters.add(id);
  else listing.filters.delete(id);
}

function showListingResults(resetPage) {
  if (resetPage) listing.page = 1;
  const products = listingProducts();
  const pages = Math.ceil(products.length / listing.pageSize);
  document.getElementById("result-count").textContent = `${products.length} items`;
  showChips();
  const body = document.getElementById("listing-body");
  if (!products.length) {
    body.innerHTML = `<p data-testid="empty-result" class="empty">Sorry, there is no item matching the applied filter.</p>`;
    return;
  }
  const visible = products.slice(0, listing.page * listing.pageSize);
  body.innerHTML = `
    <div class="grid">${visible.map((p) => cardHtml(p)).join("")}</div>
    ${listing.page < pages
      ? `<button data-testid="load-more" type="button" class="load-more">MORE (${listing.page}/${pages})</button>`
      : ""}`;
  body.querySelector('[data-testid="load-more"]')?.addEventListener("click", () => {
    listing.page += 1;
    showListingResults(false);
  });
}

function showChips() {
  const chips = document.getElementById("chips");
  if (!listing.filters.size) {
    chips.innerHTML = "";
    return;
  }
  chips.innerHTML = `
    ${[...listing.filters].map((id) => `
      <button data-testid="refine-chip" type="button" class="chip" data-id="${esc(id)}">
        ${esc(categoriesById.get(id).name)} <span aria-hidden="true">&times;</span>
      </button>`).join("")}
    <button data-testid="clear-all" type="button" class="link-button">Clear all</button>`;
  for (const chip of chips.querySelectorAll('[data-testid="refine-chip"]')) {
    chip.addEventListener("click", () => {
      setFilter(chip.dataset.id, false);
      showListingResults(true);
    });
  }
  chips.querySelector('[data-testid="clear-all"]').addEventListener("click", () => {
    for (const id of [...listing.filters]) setFilter(id, false);
    showListingResults(true);
  });
}

// ---------- product detail ----------

let pdp = null;

function renderProduct(productId) {
  const product = productsById.get(productId);
  pdp = { product, option: null, qty: 1, listOpen: false };
  const crumbs = [{ name: "Home", href: "/" }, ...categoryPath(product.categoryId).map((c) => ({
    name: c.name, href: `/category/${c.id}`,
  }))];
  main.innerHTML = `
    <nav data-testid="breadcrumb" class="breadcrumb">
      ${crumbs.map((c) => `<a href="${esc(c.href)}" data-link>${esc(c.name)}</a>`).join('<span class="sep"> &gt; </span>')}
    </nav>
    <div class="pdp">
      <div class="swatch swatch-large" style="background:${swatchColor(product.id)}"></div>
      <div id="buy-box" class="buy-box"></div>
    </div>`;
  showBuyBox();
}

function showBuyBox() {
  const { product, option, qty, listOpen } = pdp;
  const needsOption = product.options.length > 0 && !option;
  const max = maxQty(product, option);
  const label = needsOption ? "Add to Cart" : `Add to Cart - ${money(cents((option || product).salePrice) * qty)}`;
  const box = document.getElementById("buy-box");
  box.innerHTML = `
    <div data-testid="pdp-brand" class="card-brand">${esc(product.brand)}</div>
    <h1 data-testid="pdp-name">${esc(product.name)}</h1>
    <div class="prices">${priceHtml(product, option)}</div>
    ${product.options.length ? optionsHtml(product, option, listOpen) : ""}
    <div class="qty">
      <button data-testid="qty-minus" type="button" aria-label="Decrease quantity"
        ${needsOption || qty <= 1 ? "disabled" : ""}>&minus;</button>
      <span data-testid="qty-value" class="qty-value">${qty}</span>
      <button data-testid="qty-plus" type="button" aria-label="Increase quantity"
        ${needsOption || qty >= max ? "disabled" : ""}>+</button>
    </div>
    <button data-testid="add-to-cart" type="button" class="primary">${label}</button>
    <div id="added-slot"></div>`;
  box.querySelector('[data-testid="option-toggle"]')?.addEventListener("click", () => {
    pdp.listOpen = !pdp.listOpen;
    showBuyBox();
  });
  for (const item of box.querySelectorAll('[data-testid="option-item"]')) {
    item.addEventListener("click", () => chooseOption(item.dataset.id));
  }
  box.querySelector('[data-testid="qty-minus"]').addEventListener("click", () => changeQty(-1));
  box.querySelector('[data-testid="qty-plus"]').addEventListener("click", () => changeQty(1));
  box.querySelector('[data-testid="add-to-cart"]').addEventListener("click", onAddToCart);
}

function optionsHtml(product, option, listOpen) {
  const items = product.options.map((o) => `
    <li><button data-testid="option-item" type="button" class="option-item" data-id="${esc(o.id)}"
      ${o.stock > 0 ? "" : "disabled"}>
      <span>${esc(o.name)}</span> <span>${money(cents(o.salePrice))}</span>
      ${o.stock > 0 ? "" : '<span class="sold-out">Sold out</span>'}
    </button></li>`).join("");
  return `
    <div class="options">
      <button data-testid="option-toggle" type="button" class="option-toggle" aria-expanded="${listOpen}">
        ${option ? esc(option.name) : "Select an option"}
      </button>
      <ul data-testid="option-list" class="option-list" ${listOpen ? "" : "hidden"}>${items}</ul>
    </div>`;
}

function chooseOption(optionId) {
  pdp.option = pdp.product.options.find((o) => o.id === optionId);
  pdp.qty = 1;
  pdp.listOpen = false;
  showBuyBox();
}

function changeQty(step) {
  const max = maxQty(pdp.product, pdp.option);
  pdp.qty = Math.min(max, Math.max(1, pdp.qty + step));
  showBuyBox();
}

function onAddToCart() {
  const { product, option, qty } = pdp;
  if (product.options.length && !option) {
    pdp.listOpen = true;
    showBuyBox();
    return;
  }
  addToCart(product.id, option ? option.id : null, qty);
  showBuyBox();
  const dialog = el(`
    <div data-testid="added-dialog" class="added-dialog" role="dialog" aria-label="Added to cart">
      <p>Added to your cart.</p>
      <a data-testid="go-to-cart" class="primary" href="/cart" data-link>Go to Cart</a>
      <button type="button" class="link-button">Continue Shopping</button>
    </div>`);
  dialog.querySelector("button").addEventListener("click", () => dialog.remove());
  document.getElementById("added-slot").append(dialog);
}

// ---------- cart ----------

let cartUi = { pendingRemove: null, prefsOpen: false };

function cartLines() {
  return getCart()
    .map((line) => {
      const product = productsById.get(line.productId);
      if (!product) return null;
      const option = product.options.find((o) => o.id === line.optionId) || null;
      return { ...line, product, option, unit: cents((option || product).salePrice) };
    })
    .filter(Boolean);
}

function renderCart() {
  cartUi = { pendingRemove: null, prefsOpen: false };
  showCart();
}

function showCart() {
  const lines = cartLines();
  main.innerHTML = `<h1>Cart <span data-testid="cart-count" class="muted">(${lines.length})</span></h1>`;
  if (!lines.length) {
    main.append(el(`<div class="cart-empty-box">
      <p data-testid="cart-empty">Your cart is empty.</p><a href="/" data-link>Continue shopping</a></div>`));
    return;
  }
  const layout = el(`<div class="cart"><div class="cart-lines"></div><aside class="summary"></aside></div>`);
  lines.forEach((line, index) => layout.firstElementChild.append(cartLineElement(line, index)));
  if (cartUi.pendingRemove !== null) layout.firstElementChild.append(removeConfirmElement());
  layout.lastElementChild.append(summaryElement(lines));
  main.append(layout);
}

function cartLineElement(line, index) {
  const max = maxQty(line.product, line.option);
  const node = el(`
    <div data-testid="cart-line" class="cart-line">
      <div class="swatch swatch-small" style="background:${swatchColor(line.product.id)}"></div>
      <div class="cart-line-info">
        <div class="card-brand">${esc(line.product.brand)}</div>
        <a href="/product/${esc(line.product.id)}" data-link>${esc(line.product.name)}</a>
        ${line.option ? `<div class="muted">${esc(line.option.name)}</div>` : ""}
        <div class="muted">${money(line.unit)} each</div>
      </div>
      <div class="qty">
        <button data-testid="line-qty-minus" type="button" aria-label="Decrease quantity"
          ${line.qty <= 1 ? "disabled" : ""}>&minus;</button>
        <span data-testid="line-qty-value" class="qty-value">${line.qty}</span>
        <button data-testid="line-qty-plus" type="button" aria-label="Increase quantity"
          ${line.qty >= max ? "disabled" : ""}>+</button>
      </div>
      <div data-testid="line-total" class="line-total">${money(line.unit * line.qty)}</div>
      <button data-testid="line-remove" type="button" class="link-button">Remove</button>
    </div>`);
  node.querySelector('[data-testid="line-qty-minus"]').addEventListener("click", () => setLineQty(index, line.qty - 1, max));
  node.querySelector('[data-testid="line-qty-plus"]').addEventListener("click", () => setLineQty(index, line.qty + 1, max));
  node.querySelector('[data-testid="line-remove"]').addEventListener("click", () => {
    cartUi.pendingRemove = index;
    showCart();
  });
  return node;
}

function setLineQty(index, qty, max) {
  const lines = getCart();
  lines[index].qty = Math.min(max, Math.max(1, qty));
  setCart(lines);
  showCart();
}

function removeConfirmElement() {
  const node = el(`
    <div class="confirm" role="alertdialog" aria-label="Remove item">
      <p>Remove this item from your cart?</p>
      <button data-testid="remove-confirm" type="button" class="primary">Remove</button>
      <button data-testid="remove-cancel" type="button">Cancel</button>
    </div>`);
  node.querySelector('[data-testid="remove-confirm"]').addEventListener("click", () => {
    const lines = getCart();
    lines.splice(cartUi.pendingRemove, 1);
    cartUi.pendingRemove = null;
    setCart(lines);
    showCart();
  });
  node.querySelector('[data-testid="remove-cancel"]').addEventListener("click", () => {
    cartUi.pendingRemove = null;
    showCart();
  });
  return node;
}

function summaryElement(lines) {
  const threshold = cents(constants.freeShippingThreshold);
  const subtotal = lines.reduce((sum, l) => sum + l.unit * l.qty, 0);
  const free = subtotal >= threshold;
  const shipping = free ? 0 : cents(constants.flatShipping);
  const progress = free ? 100 : Math.round((100 * subtotal) / threshold);
  const prefs = getPrefs();
  const location = locationById(prefs.location);
  const node = el(`
    <div>
      <p data-testid="free-shipping-msg" class="free-msg">
        ${free ? "You've got Free Shipping!" : `Only ${money(threshold - subtotal)} left for Free Shipping!`}</p>
      <div data-testid="free-shipping-bar" class="bar" role="progressbar" aria-label="Free shipping progress"
        aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><div style="width:${progress}%"></div></div>
      <dl class="totals">
        <dt>Subtotal</dt><dd data-testid="subtotal">${money(subtotal)}</dd>
        <dt>Shipping</dt><dd data-testid="shipping-fee">${free ? "Free" : money(shipping)}</dd>
        <dt>Estimated total</dt><dd data-testid="estimated-total">${money(subtotal + shipping)}</dd>
      </dl>
      <div class="ship-to">
        <span>Ship to: ${esc(location.name)}${prefs.currency ? ` (${esc(prefs.currency)})` : ""}</span>
        <button data-testid="prefs-open" type="button" class="link-button">Change</button>
      </div>
      ${location.shippable ? "" : `<p data-testid="ship-error" class="error">Unable to ship to ${esc(location.name)}</p>`}
      <div id="prefs-slot"></div>
      <button data-testid="checkout-button" type="button" class="primary wide" ${location.shippable ? "" : "disabled"}>
        Checkout</button>
    </div>`);
  node.querySelector('[data-testid="prefs-open"]').addEventListener("click", () => {
    cartUi.prefsOpen = true;
    showCart();
  });
  node.querySelector('[data-testid="checkout-button"]').addEventListener("click", () => navigate("/checkout"));
  if (cartUi.prefsOpen) node.querySelector("#prefs-slot").append(prefsElement(prefs));
  return node;
}

function prefsElement(prefs) {
  const draft = { ...prefs };
  const node = el(`
    <div class="prefs" role="dialog" aria-label="Shipping preferences">
      <label>Location
        <select data-testid="prefs-location">
          ${catalog.locations.map((l) => `<option value="${esc(l.id)}">${esc(l.name)}</option>`).join("")}
        </select>
      </label>
      <label>Currency
        <select data-testid="prefs-currency">
          <option value="">Select currency</option>
          ${catalog.currencies.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("")}
        </select>
      </label>
      <button data-testid="prefs-save" type="button" class="primary">Save</button>
      <button type="button" class="prefs-cancel">Cancel</button>
    </div>`);
  const locationSelect = node.querySelector('[data-testid="prefs-location"]');
  const currencySelect = node.querySelector('[data-testid="prefs-currency"]');
  const sync = () => {
    locationSelect.value = draft.location;
    currencySelect.value = draft.currency || "";
    currencySelect.disabled = !locationById(draft.location).currency;
  };
  locationSelect.addEventListener("change", () => {
    draft.location = locationSelect.value;
    draft.currency = locationById(draft.location).currency;
    sync();
  });
  currencySelect.addEventListener("change", () => {
    draft.currency = currencySelect.value || null;
  });
  node.querySelector('[data-testid="prefs-save"]').addEventListener("click", () => {
    save(KEYS.prefs, draft);
    cartUi.prefsOpen = false;
    showCart();
  });
  node.querySelector(".prefs-cancel").addEventListener("click", () => {
    cartUi.prefsOpen = false;
    showCart();
  });
  sync();
  return node;
}

// ---------- search page, checkout, not found ----------

function renderSearch(query) {
  document.getElementById("search-input").value = query;
  main.innerHTML = `<h1>Search results for "<span data-testid="search-query">${esc(query)}</span>"</h1>
    <p class="muted">Search results are a placeholder in this store.</p>`;
}

function renderCheckout() {
  main.innerHTML = `<h1>Checkout</h1><p class="muted">Checkout is a placeholder in this store.</p>
    <a href="/cart" data-link>Back to cart</a>`;
}

function renderNotFound() {
  main.innerHTML = `<h1>Page not found</h1><a href="/" data-link>Go home</a>`;
}

// ---------- header search ----------

const searchBox = document.getElementById("search-box");
const searchInput = document.getElementById("search-input");
const searchLayer = document.getElementById("search-layer");
let suggestTimer = null;

function openLayer() {
  searchLayer.hidden = false;
  showLayerContent();
}

function closeLayer() {
  clearTimeout(suggestTimer);
  searchLayer.hidden = true;
}

// Empty query: recent + popular lists. Non-empty: suggestions, filled in only after the debounce.
function showLayerContent() {
  if (!catalog) return;
  const empty = searchInput.value.trim() === "";
  const recent = load(KEYS.recent, []);
  document.getElementById("recent-searches").hidden = !empty || recent.length === 0;
  document.getElementById("popular-searches").hidden = !empty;
  document.getElementById("recent-list").innerHTML = recent
    .map((term) => `<li><button data-testid="recent-item" type="button" class="term" data-term="${esc(term)}">${esc(term)}</button></li>`)
    .join("");
  document.getElementById("popular-list").innerHTML = catalog.popularSearches
    .map((term) => `<li><button data-testid="popular-item" type="button" class="term" data-term="${esc(term)}">${esc(term)}</button></li>`)
    .join("");
  document.getElementById("suggestions").innerHTML = "";
}

function suggestionsFor(query) {
  const q = query.toLowerCase();
  const has = (text) => text.toLowerCase().includes(q);
  const brands = [...new Set(catalog.products.map((p) => p.brand))].sort((a, b) => a.localeCompare(b, "en", { numeric: true }));
  const categories = [...categoriesById.values()];
  const keywords = [...new Set([...catalog.popularSearches.filter(has), ...catalog.products.map((p) => p.name).filter(has)])];
  return {
    brands: brands.filter(has).slice(0, 2),
    categories: categories.filter((c) => has(c.name)).slice(0, 2),
    keywords: keywords.slice(0, 10),
  };
}

function showSuggestions() {
  const query = searchInput.value.trim();
  if (!query || !catalog) return;
  const { brands, categories, keywords } = suggestionsFor(query);
  const group = (title, html) => (html ? `<section><h3>${title}</h3><ul class="term-list">${html}</ul></section>` : "");
  const html =
    group("Brands", brands.map((b) => `<li><button data-testid="suggest-brand" type="button" class="term" data-term="${esc(b)}">${esc(b)}</button></li>`).join("")) +
    group("Categories", categories.map((c) => `<li><a data-testid="suggest-category" class="term" href="/category/${esc(c.id)}" data-link>${esc(c.name)}</a></li>`).join("")) +
    group("Suggestions", keywords.map((k) => `<li><button data-testid="suggest-keyword" type="button" class="term" data-term="${esc(k)}">${esc(k)}</button></li>`).join(""));
  document.getElementById("suggestions").innerHTML = html || '<p class="muted">No suggestions</p>';
}

function submitSearch(raw) {
  const query = raw.trim();
  if (!query) return;
  recordSearch(query);
  searchInput.value = query;
  navigate(`/search?q=${encodeURIComponent(query)}`);
}

function initSearch() {
  searchInput.addEventListener("focus", openLayer);
  searchInput.addEventListener("input", () => {
    openLayer();
    clearTimeout(suggestTimer);
    if (searchInput.value.trim()) suggestTimer = setTimeout(showSuggestions, SUGGEST_DELAY);
  });
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeLayer();
    if (event.key === "Enter") {
      event.preventDefault();
      submitSearch(searchInput.value);
    }
  });
  searchLayer.addEventListener("click", (event) => {
    const term = event.target.closest("button[data-term]");
    if (term) submitSearch(term.dataset.term);
  });
  document.addEventListener("mousedown", (event) => {
    if (!searchBox.contains(event.target)) closeLayer();
  });
}

// ---------- start ----------

function renderCategoryNav() {
  document.getElementById("category-nav").innerHTML = catalog.categories
    .map((c) => `<a href="/category/${esc(c.id)}" data-link>${esc(c.name)}</a>`)
    .join("");
}

async function start() {
  try {
    const response = await fetch("/fixtures/catalog.json");
    catalog = await response.json();
  } catch {
    main.innerHTML = '<p class="error">The catalog could not be loaded.</p>';
    return;
  }
  constants = catalog.constants;
  for (const product of catalog.products) productsById.set(product.id, product);
  indexCategories(catalog.categories);
  renderCategoryNav();
  updateBadge();
  if (!searchLayer.hidden) showLayerContent();
  route();
}

// Search listeners go on before the catalog arrives so an early focus or Enter is not lost.
initSearch();
start();
