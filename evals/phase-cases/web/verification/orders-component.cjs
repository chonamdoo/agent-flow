const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");

async function main() {
  const project = path.resolve(process.argv[2]);
  const fromProject = createRequire(path.join(project, "package.json"));
  const React = fromProject("react");
  const { renderToString } = fromProject("react-dom/server");
  const { JSDOM } = fromProject("jsdom");
  const esbuild = fromProject("esbuild");
  const state = path.join(project, ".phase-eval-environment");
  fs.mkdirSync(state, { recursive: true });
  const output = path.join(state, "orders-component.cjs");
  await esbuild.build({
    entryPoints: [path.join(project, "app/orders/OrdersClient.tsx")],
    outfile: output, bundle: true, platform: "node", format: "cjs", jsx: "automatic",
    packages: "external", logLevel: "silent",
  });
  const { OrdersClient } = fromProject(output);
  const server = renderToString(React.createElement(OrdersClient));
  const dom = new JSDOM(`<div id="root">${server}</div>`, { url: "http://fixture.test/orders" });
  global.window = dom.window;
  global.document = dom.window.document;
  global.IS_REACT_ACT_ENVIRONMENT = true;
  const { hydrateRoot, createRoot } = fromProject("react-dom/client");
  const container = document.getElementById("root");
  assert.match(container.querySelector('[role="status"]').textContent, /Loading orders/);
  const recoverable = [];
  const requests = [];
  let release;
  global.fetch = async (url, options) => {
    requests.push([url, options]);
    return new Promise((resolve) => { release = resolve; });
  };
  let root;
  await React.act(async () => {
    root = hydrateRoot(container, React.createElement(OrdersClient), {
      onRecoverableError: (error) => recoverable.push(error.message),
    });
  });
  assert.equal(requests.length, 1);
  assert.equal(requests[0][0], "/api/orders");
  assert.equal(requests[0][1].headers.Accept, "application/json");
  await React.act(async () => {
    release({ ok: true, json: async () => ({ orders: [
      { order_id: "older", state: "PENDING", placed_at: "2025-12-31T00:00:00Z", total_cents: 200 },
      { order_id: "newer", state: "PENDING", placed_at: "2026-01-02T00:00:00Z", total_cents: 100 },
    ] }) });
  });
  const rows = [...container.querySelectorAll("li")].map((row) => row.textContent);
  assert.equal(rows.length, 2);
  assert.match(rows[0], /Order newer/);
  assert.match(rows[1], /Order older/);
  assert.equal(container.querySelector('[role="status"]').textContent, "2 orders");
  assert.equal(container.querySelector('[role="status"]').getAttribute("aria-live"), "polite");
  assert.deepEqual(recoverable, []);
  console.log("PASS OrdersClient server-rendered loading -> hydration -> loaded; GET /api/orders; cross-year display order; live status; zero recoverable hydration errors");
  await React.act(async () => root.unmount());
  for (const response of [
    { name: "empty", result: { ok: true, json: async () => ({ orders: [] }) }, role: "status", text: /no orders/ },
    { name: "error", result: { ok: false, status: 503 }, role: "alert", text: /couldn't load/ },
  ]) {
    global.fetch = async () => response.result;
    await React.act(async () => {
      root = createRoot(container);
      root.render(React.createElement(OrdersClient));
    });
    assert.match(container.querySelector(`[role="${response.role}"]`).textContent, response.text);
    assert.equal(container.querySelectorAll("li").length, 0);
    console.log(`PASS OrdersClient ${response.name} state and accessible message`);
    await React.act(async () => root.unmount());
  }
  dom.window.close();
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
