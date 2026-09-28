// cost_basis_harness.mjs — Staff App harness for cost basis (last vs average) +
// name-variant merge (owner 2026-09-28 Osmo "ကြက်ဥ နောက်ဆုံးဝယ်ဈေး 600 မဟုတ်").
//
// Verifies, without a browser:
//   1. The RecipeCost Vue template from app.js COMPILES cleanly (real Vue compiler).
//   2. 🧺 Ingredients tab shows last purchase + date + weighted average, the cost
//      basis toggle (နောက်ဆုံးဝယ်ဈေး / ပျမ်းမျှ), the ⚠️ duplicate-names section and
//      the manual merge popup (so the egg case has a one-tap / manual merge).
//   3. Live API: the egg payload carries last 700 @ 2026-09-27 + avg (574.92 after
//      merge); duplicate candidates list surfaces ကြက်ဉ/ကြက်ဥ; merge mappings exist;
//      staff -> 403 on every new endpoint.
//
// Run:  node /opt/psvibe/api/verify/cost_basis_harness.mjs
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
  sandbox.window = dom.window; sandbox.document = dom.window.document;
  sandbox.navigator = dom.window.navigator; sandbox.console = console; sandbox.globalThis = sandbox;
}
vm.createContext(sandbox);
vm.runInContext(vueSrc + "\n;globalThis.__Vue = Vue;", sandbox);
const Vue = sandbox.__Vue;
check("Vue compiler loaded", Vue && typeof Vue.compile === "function");

// ── 2. Extract + compile the RecipeCost template ─────────────────────────────
const appSrc = readFileSync(APP_JS, "utf8");
const i0 = appSrc.indexOf("const RecipeCost = {");
check("RecipeCost component found", i0 !== -1);
const tplMarker = "template: `";
const t0 = appSrc.indexOf(tplMarker, i0);
check("RecipeCost template literal found", t0 !== -1);
const t1 = appSrc.indexOf("`", t0 + tplMarker.length);
const template = appSrc.slice(t0 + tplMarker.length, t1);

let compileError = null;
if (Vue && Vue.compile) {
  try { Vue.compile(template); } catch (e) { compileError = e; }
  check("Vue.compile(template) succeeds (0 console errors)", !compileError, compileError ? String(compileError).slice(0, 200) : "compiled");
}

// ── 3. Static structural assertions ──────────────────────────────────────────
check("cost basis toggle: Burmese label", template.includes("ကုန်ကျစရိတ် တွက်နည်း"));
check("cost basis: last option", template.includes("နောက်ဆုံးဝယ်ဈေး (last)"));
check("cost basis: average option", template.includes("ပျမ်းမျှ (avg)"));
check("duplicate names section title", template.includes("နာမည်ကွဲနေတဲ့ ပစ္စည်းများ"));
check("one-tap merge button", template.includes("ပေါင်း Merge"));
check("manual merge popup present", template.includes("နာမည်ပေါင်း (Merge names)"));
check("merged-names section present", template.includes("ပေါင်းပြီးသား နာမည်များ"));
check("unit cost shows last + date + avg", appSrc.includes("last_purchase_price") && appSrc.includes("last_purchase_date") && appSrc.includes("weighted_avg_cost"));
check("basis helpers wired", appSrc.includes("setGlobalBasis") && appSrc.includes("doMergeNames") && appSrc.includes("unmergeVariant"));

// ── 4. Pure JS mirror of the unit-cost label ────────────────────────────────
function fKs(n) { return (Number(n) || 0).toLocaleString("en-US"); }
function shortDate(s) {
  const m = String(s || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return "";
  const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  return MON[parseInt(m[2], 10) - 1] + " " + parseInt(m[3], 10);
}
function unitCostText(it) {
  const u = it.unit_label || "pcs";
  if (it.display_cost == null) return "No purchase yet · " + u;
  const basis = it.effective_cost_basis || "last";
  let s = fKs(it.display_cost) + " Ks / " + u;
  if (basis === "last" && it.last_purchase_price != null) {
    s += " · last " + fKs(it.last_purchase_price) + (it.last_purchase_date ? " (" + shortDate(it.last_purchase_date) + ")" : "");
  }
  if (it.weighted_avg_cost != null) s += " · avg " + fKs(it.weighted_avg_cost);
  return s;
}
const eggLabel = unitCostText({ unit_label: "pcs", display_cost: 700, effective_cost_basis: "last", last_purchase_price: 700, last_purchase_date: "2026-09-27 04:52:30", weighted_avg_cost: 574.9175 });
check("egg unit-cost text shows last 700 + date + avg", eggLabel.includes("last 700 (Sep 27)") && eggLabel.includes("avg 574"), eggLabel);

// ── 5. Live API ──────────────────────────────────────────────────────────────
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
  const rIng = await api("/api/menu/ingredients?kind=all", ADMIN);
  check("live /ingredients 200", rIng.status === 200, String(rIng.status));
  const items = (rIng.json && rIng.json.data && rIng.json.data.items) || [];
  const egg = items.find((i) => i.name === "ကြက်ဥ");
  check("egg ingredient present", !!egg);
  check("egg last purchase 700", egg && egg.last_purchase_price === 700, String(egg && egg.last_purchase_price));
  check("egg last date 27 Sep", egg && /2026-09-27/.test(String(egg.last_purchase_date || "")), String(egg && egg.last_purchase_date));
  check("egg avg 574.92 (merged variants)", egg && Math.abs((egg.weighted_avg_cost || 0) - 574.9175) < 0.001, String(egg && egg.weighted_avg_cost));
  check("egg unit_cost = last = 700", egg && egg.unit_cost === 700, String(egg && egg.unit_cost));
  check("egg variants = Egg + ကြက်ဉ", egg && (egg.variants || []).includes("Egg") && (egg.variants || []).includes("ကြက်ဉ"), String(egg && egg.variants));
  check("Egg no longer a separate item", !items.some((i) => i.name === "Egg"));
  check("ကြက်ဉ no longer a separate item", !items.some((i) => i.name === "ကြက်ဉ"));

  const cand = await api("/api/menu/ingredient-variants/candidates", ADMIN);
  const groups = (cand.json && cand.json.data && cand.json.data.groups) || [];
  check("candidates endpoint 200", cand.status === 200, String(cand.status));
  check("candidates empty after merge (egg already merged)", groups.length === 0, String(groups.length));

  const maps = await api("/api/menu/ingredient-variants", ADMIN);
  const mappings = (maps.json && maps.json.data && maps.json.data.mappings) || {};
  check("merge mapping for ကြက်ဥ exists", !!mappings["ကြက်ဥ"], Object.keys(mappings).join(", "));
  const eggVariants = (mappings["ကြက်ဥ"] || []).map((v) => v.variant_name);
  check("mapping holds Egg + ကြက်ဉ", eggVariants.includes("Egg") && eggVariants.includes("ကြက်ဉ"), eggVariants.join(", "));

  const cb = await api("/api/menu/cost-basis", ADMIN);
  check("cost-basis default = last", cb.json && cb.json.data && cb.json.data.global === "last", String(cb.json && cb.json.data && cb.json.data.global));

  check("staff 403 on /cost-basis", (await api("/api/menu/cost-basis", STAFF)).status === 403);
  check("staff 403 on /ingredient-variants", (await api("/api/menu/ingredient-variants", STAFF)).status === 403);
  check("staff 403 on /candidates", (await api("/api/menu/ingredient-variants/candidates", STAFF)).status === 403);
} catch (e) {
  check("live API reachable", false, String(e).slice(0, 160));
}

console.log(`\n==== HARNESS: ${PASS} passed, ${FAIL} failed ====`);
if (failures.length) { console.log("FAILED:", failures.join(", ")); process.exit(1); }
process.exit(0);
