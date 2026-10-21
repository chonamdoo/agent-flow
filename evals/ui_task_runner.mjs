// Runs a UI task's acceptance checks against a base URL with Playwright and prints one JSON result.
//
//   node evals/ui_task_runner.mjs <acceptance.json> <base-url> [--only <check-id>]
//
// Each check gets a fresh browser context (empty localStorage) and an installed fake clock, so timer-driven behaviour
// (debounce, carousel) is checked with `pause` and `tick` instead of real waits. Elements are found only by
// data-testid, so any framework that follows the task's test-id contract can be scored. Started by evals/ui_task.py,
// which also serves static builds; it can be run directly against an app that is already running.
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(import.meta.url);

function globalNodeModules() {
  try {
    return execFileSync("npm", ["root", "-g"], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  } catch {
    return null;
  }
}

function loadPlaywright() {
  // createRequire only searches node_modules above this file, so a global `npm i -g playwright` needs its own path.
  const globalRoot = globalNodeModules();
  const names = ["playwright", "/opt/node-tools/node_modules/playwright", "playwright-core"];
  if (globalRoot) names.push(path.join(globalRoot, "playwright"), path.join(globalRoot, "playwright-core"));
  for (const name of names) {
    try {
      return require(name);
    } catch {
      // try the next location
    }
  }
  throw new Error("playwright is not installed (npm i -g playwright)");
}

const [, , acceptancePath, baseUrl, ...rest] = process.argv;
const only = rest[0] === "--only" ? rest[1] : null;
const spec = JSON.parse(readFileSync(acceptancePath, "utf8"));
const STEP_TIMEOUT = spec.stepTimeoutMs ?? 4000;
const PAUSE_LEAD_MS = 50;

function locator(page, target) {
  const t = typeof target === "string" ? { testid: target } : target;
  let loc = (t.within ? locator(page, t.within) : page).getByTestId(t.testid);
  if (t.hasText !== undefined) loc = loc.filter({ hasText: t.hasText });
  if (t.nth !== undefined) loc = loc.nth(t.nth);
  else if (t.last) loc = loc.last();
  return loc;
}

const describe = (target) => (typeof target === "string" ? target : JSON.stringify(target));
const money = (text) => Number(String(text).replace(/[^0-9.\-]/g, ""));

const firstAmount = (text) => Number((String(text).match(/-?[0-9][0-9,]*(?:\.[0-9]+)?/) ?? ["NaN"])[0].replace(/,/g, ""));

async function sortKeys(items, by) {
  const keys = [];
  for (const item of await items.all()) {
    let key = null;
    for (const testid of by) {
      const child = item.getByTestId(testid);
      if (await child.count()) {
        key = firstAmount(await child.first().innerText());
        break;
      }
    }
    if (key === null || Number.isNaN(key)) throw new Error(`item ${keys.length} has no amount in ${by.join(", ")}`);
    keys.push(key);
  }
  return keys;
}

// Inputs report their value, so a quantity shown in an <input> and one shown as text are read the same way.
async function textOf(loc) {
  const text = await loc.evaluate((el) => (["INPUT", "TEXTAREA"].includes(el.tagName) ? el.value : el.innerText),
    undefined, { timeout: STEP_TIMEOUT });
  return text.replace(/\s+/g, " ").trim();
}

async function poll(fn, message) {
  const deadline = Date.now() + STEP_TIMEOUT;
  let last;
  for (;;) {
    try {
      const outcome = await fn();
      if (outcome === true) return;
      last = outcome;
    } catch (error) {
      last = error.message.split("\n")[0];
    }
    if (Date.now() > deadline) throw new Error(`${message}${last && last !== false ? ` (${last})` : ""}`);
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}

function compareText(actual, step) {
  if (step.equals !== undefined) return actual === step.equals || `got "${actual}"`;
  if (step.contains !== undefined) return actual.includes(step.contains) || `got "${actual}"`;
  if (step.matches !== undefined) return new RegExp(step.matches).test(actual) || `got "${actual}"`;
  if (step.notContains !== undefined) return !actual.includes(step.notContains) || `got "${actual}"`;
  return true;
}

async function runStep(page, step, base) {
  const [kind] = Object.keys(step).filter((key) => !["nth", "value", "key", "label", "equals", "contains", "matches",
    "notContains", "eq", "min", "max", "name", "order", "delay", "hasText", "last"].includes(key));
  const target = step[kind];
  switch (kind) {
    case "goto":
      // Wait for the network to go quiet so a server-rendered page has hydrated before the first interaction;
      // otherwise a select or keystroke can land on markup React has not taken over yet and be lost.
      await page.goto(new URL(target, base).toString(), { waitUntil: "networkidle" });
      return;
    case "pause":
      // Pausing at "now" races the still-running clock ("Cannot fast-forward to the past"); a small step ahead
      // cannot reach any timer a check depends on, since checks start their timed actions after pausing.
      await page.clock.pauseAt((await page.evaluate(() => Date.now())) + PAUSE_LEAD_MS);
      return;
    case "tick":
      await page.clock.runFor(target);
      return;
    case "click":
      await locator(page, target).click({ timeout: STEP_TIMEOUT });
      return;
    case "focus":
      await locator(page, target).focus({ timeout: STEP_TIMEOUT });
      return;
    case "fill":
      await locator(page, target).fill(step.value, { timeout: STEP_TIMEOUT });
      return;
    case "type":
      await locator(page, target).pressSequentially(step.value, { delay: step.delay ?? 0, timeout: STEP_TIMEOUT });
      return;
    case "press":
      await locator(page, target).press(step.key, { timeout: STEP_TIMEOUT });
      return;
    case "select":
      await locator(page, target).selectOption({ label: step.label }, { timeout: STEP_TIMEOUT });
      return;
    case "check":
      await locator(page, target).check({ timeout: STEP_TIMEOUT });
      return;
    case "uncheck":
      await locator(page, target).uncheck({ timeout: STEP_TIMEOUT });
      return;
    case "reload":
      await page.reload({ waitUntil: "networkidle" });
      return;
    case "expectVisible":
      await poll(async () => (await locator(page, target).isVisible()) || "not visible", `${describe(target)} visible`);
      return;
    case "expectHidden":
      await poll(async () => !(await locator(page, target).isVisible()) || "still visible", `${describe(target)} hidden`);
      return;
    case "expectCount":
      await poll(async () => {
        const n = await page.getByTestId(target).filter({ visible: true }).count();
        if (step.eq !== undefined && n !== step.eq) return `count ${n}`;
        if (step.min !== undefined && n < step.min) return `count ${n}`;
        if (step.max !== undefined && n > step.max) return `count ${n}`;
        return true;
      }, `${describe(target)} count`);
      return;
    case "expectText":
      await poll(async () => compareText(await textOf(locator(page, target)), step), `${describe(target)} text`);
      return;
    case "expectMoney":
      await poll(async () => {
        const value = money(await textOf(locator(page, target)));
        return Math.abs(value - step.equals) < 0.005 || `got ${value}`;
      }, `${describe(target)} amount ${step.equals}`);
      return;
    case "expectDisabled":
      await poll(async () => (await locator(page, target).isDisabled()) || "enabled", `${describe(target)} disabled`);
      return;
    case "expectEnabled":
      await poll(async () => (await locator(page, target).isEnabled()) || "disabled", `${describe(target)} enabled`);
      return;
    case "expectAttr":
      await poll(async () => compareText(String(await locator(page, target).getAttribute(step.name)), step),
        `${describe(target)} [${step.name}]`);
      return;
    case "expectSelectedLabel":
      await poll(async () => compareText(await locator(page, target).evaluate((el) => el.options[el.selectedIndex]?.text
        .trim() ?? ""), step), `${describe(target)} selected label`);
      return;
    case "expectValue":
      await poll(async () => compareText(await locator(page, target).inputValue(), step), `${describe(target)} value`);
      return;
    case "expectUrl":
      await poll(async () => compareText(decodeURIComponent(page.url().replace(base.replace(/\/$/, ""), "")), step),
        "url");
      return;
    case "expectSorted": {
      // With `by`, each visible target is keyed by the first amount in its first `by` child that is present, so every
      // card is compared (a range "$min - $max" sorts by its min) instead of only the cards that show one price.
      const values = step.by ? await sortKeys(page.getByTestId(target).filter({ visible: true }), step.by)
        : (await page.getByTestId(target).filter({ visible: true }).allInnerTexts()).map(money);
      for (let i = 1; i < values.length; i += 1) {
        if (step.order === "desc" ? values[i] > values[i - 1] : values[i] < values[i - 1]) {
          throw new Error(`${target} not ${step.order ?? "asc"} at ${i}: ${values.slice(Math.max(0, i - 2), i + 1)}`);
        }
      }
      if (!values.length) throw new Error(`${target} has no values`);
      return;
    }
    default:
      throw new Error(`unknown step ${JSON.stringify(step)}`);
  }
}

const { chromium } = loadPlaywright();
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.UI_TASK_CHROMIUM || undefined,
  args: ["--no-sandbox"],
});
const results = [];
for (const check of spec.checks) {
  if (only && check.id !== only) continue;
  const context = await browser.newContext({ viewport: spec.viewport ?? { width: 1280, height: 900 }, locale: "en-US" });
  const page = await context.newPage();
  page.on("dialog", (dialog) => dialog.dismiss());
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error.message || error).slice(0, 200)));
  await page.clock.install();
  const started = Date.now();
  let failure = null;
  for (const [index, step] of check.steps.entries()) {
    try {
      await runStep(page, step, baseUrl);
    } catch (error) {
      failure = { step: index, action: step, message: String(error.message || error).split("\n")[0].slice(0, 300) };
      break;
    }
  }
  results.push({ id: check.id, spec: check.spec, title: check.title, passed: !failure, failure, page_errors: errors,
    ms: Date.now() - started });
  await context.close();
}
await browser.close();
process.stdout.write(`${JSON.stringify({ task: spec.task, version: spec.version, base_url: baseUrl, results })}\n`);
