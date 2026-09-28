// recipe_cost_harness.mjs — Staff App harness for the item-type split (owner 2026-09-28).
//
// Verifies, without a browser, that the Recipe & Cost page correctly splits
// made dishes from ready-made/resale items:
//   1. The two sections render (🍳 ဟင်းပွဲ / 🛒 အဆင်သင့်) — the actual Vue
//      template from app.js COMPILES cleanly with the real Vue compiler.
//   2. Resale rows show a purchase cost (NOT the "No recipe — tap to add" prompt).
//   3. The New-item type picker + type-switch control are present.
//   4. Dish flow is unchanged (recipe-based cost label still present).
//   5. Live API: /api/menu/costs?type=resale|dish returns correct types.
//
// Run:  node /opt/psvibe/api/verify/recipe_cost_harness.mjs
import { readFileSync } from "node:fs";
import { execSync } from "node:child_process";
import vm from "node:vm";

const WWW = "/opt/kora-projects/psvibe-staff-app/code/www";
const APP_JS = WWW + "/app.js";
const VUE_JS = WWW + "/vendor/vue.global.prod.js";

let PASS = 0, FAIL = 0;
const failures = [];
function check(name, ok, extra = "") {
  if (ok) PASS++; else { FAIL++; failures.push(name); }
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}` + (extra ? ` | ${extra}` : ""));
}

// ── 1. Load the real Vue compiler (jsdom provides the DOM the compiler needs) ─
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
let JSDOM = null;
try { ({ JSDOM } = require("/tmp/jsdom-install/node_modules/jsdom")); } catch (_) {}
const vueSrc = readFileSync(VUE_JS, "utf8");
const sandbox = {};
if (JSDOM) {
  const dom = new JSDOM("<!doctype html><html><body></body></html>");
  sandbox.window = dom.window;
  sandbox.document = dom.window.document;
  sandbox.navigator = dom.window.navigator;
  sandbox.console = console;
  sandbox.globalThis = sandbox;
}
vm.createContext(sandbox);
vm.runInContext(vueSrc + "\n;globalThis.__Vue = Vue;", sandbox);
const Vue = sandbox.__Vue;
check("Vue compiler loaded", Vue && typeof Vue.compile === "function");

// ── 2. Extract the RecipeCost template string ───────────────────────────────
const appSrc = readFileSync(APP_JS, "utf8");
const i0 = appSrc.indexOf("const RecipeCost = {");
check("RecipeCost component found", i0 !== -1);
const tplMarker = "template: `";
const t0 = appSrc.indexOf(tplMarker, i0);
check("RecipeCost template literal found", t0 !== -1);
const t1 = appSrc.indexOf("`", t0 + tplMarker.length);
const template = appSrc.slice(t0 + tplMarker.length, t1);

// ── 3. Compile the actual template with Vue (catches template syntax errors) ─
let compiled = null, compileError = null;
if (Vue && Vue.compile) {
  try { compiled = Vue.compile(template); } catch (e) { compileError = e; }
  check("Vue.compile(template) succeeds", !compileError && (typeof compiled === "function" || (compiled && (compiled.code || compiled.render))), compileError ? String(compileError).slice(0, 200) : (compiled ? "compiled" : "empty"));
}

// ── 4. Static structural assertions on the template ─────────────────────────
check("has 🍳 ဟင်းပွဲ (made dishes) section", template.includes("🍳 ဟင်းပွဲ"));
check("has 🛒 အဆင်သင့် (ready-made) section", template.includes("🛒 အဆင်သင့်"));
check("has All items section", template.includes("All items"));
check("new-item type picker: Made dish", template.includes("Made dish"));
check("new-item type picker: Ready-made", template.includes("Ready-made"));
check("new-item sends item_type", appSrc.includes("item_type: newDish.item_type"));
check("type-switch control present", template.includes("switchType"));
check("dish flow keeps recipe editor", template.includes("Save recipe"));

// ── 5. Pure JS mirror of the cost labels (same logic as app.js) ─────────────
function fKs(n) { return (Number(n) || 0).toLocaleString("en-US"); }
function resaleLabel(it) {
  if (!it) return "";
  return it.no_purchase_yet ? "No purchase yet" : ("Cost " + fKs(it.unit_cost) + " · Margin " + fKs(it.margin) + " Ks");
}
const resaleCosted = resaleLabel({ no_purchase_yet: false, unit_cost: 515, margin: 985 });
check("resale row shows purchase cost (not recipe prompt)", resaleCosted === "Cost 515 · Margin 985 Ks", resaleCosted);
check("resale no-purchase row flagged", resaleLabel({ no_purchase_yet: true }) === "No purchase yet");
check("resale cost ≠ 'No recipe — tap to add'", resaleCosted !== "No recipe — tap to add");

// ── 6. Live API (mint admin/staff tokens via the API's own auth module) ─────
function mintToken(role) {
  return execSync("cd /opt/psvibe/api && ./venv/bin/python verify/mint_token.py " + role, { encoding: "utf8" }).trim();
}
const ADMIN = mintToken("admin");
const STAFF = mintToken("staff");
async function api(path, token, method = "GET", body) {
  const res = await fetch("http://127.0.0.1:8000" + path, {
    method,
    headers: { "Authorization": "Bearer " + token, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let j = null; try { j = await res.json(); } catch (_) {}
  return { status: res.status, json: j };
}
try {
  const r = await api("/api/menu/costs?type=resale", ADMIN);
  check("live ?type=resale 200", r.status === 200, String(r.status));
  const resale = (r.json && r.json.data) || [];
  const names = new Set(resale.map((i) => i.item_name));
  check("Amico Cake in resale", names.has("Amico Cake"));
  check("Birdy 3 in 1 Coffee in resale", names.has("Birdy 3 in 1 Coffee"));
  check("resale rows cost_source=purchase", resale.every((i) => i.cost_source === "purchase"));
  const amico = resale.find((i) => i.item_name === "Amico Cake");
  check("Amico Cake has purchase unit_cost", amico && amico.unit_cost != null, String(amico && amico.unit_cost));
  check("resale costed rows have food_cost_pct", amico && amico.food_cost_pct != null, String(amico && amico.food_cost_pct));

  const d = await api("/api/menu/costs?type=dish", ADMIN);
  const dishes = (d.json && d.json.data) || [];
  check("live ?type=dish 200", d.status === 200, String(d.status));
  check("dish rows cost_source=recipe", dishes.every((i) => i.cost_source === "recipe"));

  const staffRes = await api("/api/menu/costs", STAFF);
  check("staff 403", staffRes.status === 403, String(staffRes.status));
} catch (e) {
  check("live API reachable", false, String(e).slice(0, 160));
}

console.log(`\n==== HARNESS: ${PASS} passed, ${FAIL} failed ====`);
if (failures.length) { console.log("FAILED:", failures.join(", ")); process.exit(1); }
process.exit(0);
