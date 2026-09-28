// consolidate_ui_harness.mjs — Staff App harness for the Menu Register →
// Recipe & Cost consolidation (owner 2026-09-28 Osmo).
//
// Verifies, without a browser:
//   1. Menu Register is GONE: no route, no component, no menu entries.
//   2. Stock-In "New item" shortcut now points at /recipes (Recipe & Cost).
//   3. Recipe & Cost 🧺 Ingredients tab has the new "Add ingredient" action
//      (name-only → POST /api/menu/register {type:'ingredient'}).
//   4. Recipe & Cost can still create a dish/ready item WITH a selling price,
//      switch type, edit price, and add ingredients to a recipe (static).
//   5. All touched templates COMPILE with the real Vue compiler (0 console
//      errors equivalent).
//   6. Live API: POST /api/menu/register {type:'ingredient'} works (admin),
//      a duplicate sellable name → friendly 409, staff → 403, ingredients
//      excluded from fetch_food_menu.
//
// Run:  node /opt/psvibe/api/verify/consolidate_ui_harness.mjs
import { readFileSync } from "node:fs";
import { execSync } from "node:child_process";
import vm from "node:vm";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);

const WWW = "/opt/kora-projects/psvibe-staff-app/code/www";
const APP_JS = WWW + "/app.js";
const VUE_JS = WWW + "/vendor/vue.global.prod.js";
const TS = Date.now().toString(36);
const DEMO_ING = "ZZZ Consolidate Demo Ingredient " + TS;
const DEMO_DISH = "ZZZ Consolidate Demo Dish " + TS;

let PASS = 0, FAIL = 0;
const failures = [];
function check(name, ok, extra = "") {
  if (ok) PASS++; else { FAIL++; failures.push(name); }
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}` + (extra ? ` | ${extra}` : ""));
}

// ── 1. Load Vue compiler ───────────────────────────────────────────────────
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

const appSrc = readFileSync(APP_JS, "utf8");

// ── 2. Menu Register removal ───────────────────────────────────────────────
check("MenuRegister const removed", !appSrc.includes("const MenuRegister = {"));
check("router.register /menu-register removed", !appSrc.includes('router.register("/menu-register"'));
check("app.component MenuRegister removed", !appSrc.includes('app.component("MenuRegister"'));
check("menu entry path /menu-register removed", !appSrc.includes('path: "/menu-register"'));
check("no 'မီနူး စာရင်း' menu label", !appSrc.includes("မီနူး စာရင်း"));

// ── 3. Stock-In shortcut retargeted to /recipes ────────────────────────────
check("Inventory goRecipes → /recipes", appSrc.includes('router.push("/recipes")'));
check("Stock-In hint says Recipe & Cost", appSrc.includes("Recipe &amp; Cost မှာ ထည့်ပါ") || appSrc.includes("Recipe & Cost မှာ ထည့်ပါ"));

// ── 4. Recipe & Cost: Add ingredient action ────────────────────────────────
const rc = appSrc.indexOf("const RecipeCost = {");
const rcEnd = appSrc.indexOf("const Reports = {", rc);
const rcSrc = appSrc.slice(rc, rcEnd);
check("Ingredients tab has Add ingredient button", rcSrc.includes("openAddIngredient") && rcSrc.includes("ဟင်းခံ အသစ် / Add ingredient"));
check("saveAddIngredient POSTs type:ingredient", /\/api\/menu\/register/.test(rcSrc) && rcSrc.includes('type: "ingredient"'));
check("saveAddIngredient invalidates pick-items", rcSrc.includes('PSV.invalidate("/api/stock/pick-items")'));
check("saveAddIngredient surfaces friendly error", rcSrc.includes('catch (e)') && rcSrc.includes('e.message'));

// Merged section must render LAST in the 🧺 Ingredients tab (owner 2026-09-28 18:31)
const ingTab = rcSrc.slice(rcSrc.indexOf('title="🧺 Ingredients"'));
const iIng = ingTab.indexOf("🍚 ဟင်းခံပစ္စည်းများ (Ingredients)");
const iProd = ingTab.indexOf("🛒 ရောင်းစရာ ပစ္စည်းများ (Products)");
const iMerged = ingTab.indexOf("🔗 ပေါင်းပြီးသား နာမည်များ (merged)");
const iManual = ingTab.indexOf("manual merge");
const iBasis = ingTab.indexOf("ကုန်ကျစရိတ် တွက်နည်း (cost basis)");
const iDup = ingTab.indexOf("နာမည်ကွဲနေတဲ့ ပစ္စည်းများ (duplicate names)");
check("merged section after Ingredients list", iMerged > iIng && iIng !== -1, `Ing@${iIng} → merged@${iMerged}`);
check("merged section after Products list", iMerged > iProd && iProd !== -1, `Prod@${iProd} → merged@${iMerged}`);
check("merged section after cost-basis controls", iMerged > iBasis && iBasis !== -1, `basis@${iBasis} → merged@${iMerged}`);
check("merged section after duplicate-names warning", iMerged > iDup && iDup !== -1, `dup@${iDup} → merged@${iMerged}`);
check("manual-merge button is the last block", iManual > iMerged && iManual > iProd, `merged@${iMerged} → manual@${iManual}`);

// ── 5. Recipe & Cost still supports the owner's core flows ────────────────
check("New item (openNewDish) still present", rcSrc.includes("openNewDish") && rcSrc.includes("အသစ် / New item"));
check("New item has selling price field", rcSrc.includes("Selling price"));
check("type switch still present (switchType)", rcSrc.includes("function switchType"));
check("sell-price edit still present (savePrice)", rcSrc.includes("function savePrice"));
check("add ingredient to recipe still present (pickIngredient)", rcSrc.includes("function pickIngredient"));
check("Ingredient picker offers New ingredient", rcSrc.includes('"__new__"'));

// ── 6. Template compile (0 console errors equivalent) ─────────────────────
function extractTemplate(componentName) {
  const i0 = appSrc.indexOf(`const ${componentName} = {`);
  if (i0 === -1) return null;
  const m = `template: \``;
  const t0 = appSrc.indexOf(m, i0);
  if (t0 === -1) return null;
  const t1 = appSrc.indexOf("`", t0 + m.length);
  return appSrc.slice(t0 + m.length, t1);
}
for (const comp of ["RecipeCost", "Inventory", "MoreMenu", "More"]) {
  const tpl = extractTemplate(comp);
  check(`${comp} template found`, tpl !== null);
  if (tpl) {
    let err = null;
    try { Vue.compile(tpl); } catch (e) { err = e; }
    check(`Vue.compile(${comp}) succeeds (0 console errors)`, !err, err ? String(err).slice(0, 180) : "compiled");
  }
}

// ── 7. Live API probes ─────────────────────────────────────────────────────
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

let ingCreated = false;
let dishCreated = false;
try {
  // admin creates an ingredient (name only)
  const c = await api("/api/menu/register", ADMIN, "POST", { item_name: DEMO_ING, type: "ingredient" });
  check("live POST ingredient (admin) 200", c.status === 200, String(c.status));
  ingCreated = c.status === 200;
  check("live POST ingredient returns type ingredient", c.json && c.json.data && c.json.data.type === "ingredient", JSON.stringify(c.json && c.json.data));

  // duplicate ingredient name → idempotent (no duplicate row, no ugly error)
  const dup = await api("/api/menu/register", ADMIN, "POST", { item_name: DEMO_ING, type: "ingredient" });
  check("live duplicate ingredient → idempotent 200", dup.status === 200, String(dup.status));

  // friendly duplicate: a name that is already a SELLABLE item → 409
  const dish = await api("/api/menu/register", ADMIN, "POST", { item_name: DEMO_DISH, type: "dish", unit_price: 1500 });
  dishCreated = dish.status === 200;
  check("live create dish with price (admin) 200", dish.status === 200, String(dish.status));
  const clash = await api("/api/menu/register", ADMIN, "POST", { item_name: DEMO_DISH, type: "ingredient" });
  const raw = (clash.json && (clash.json.detail || clash.json.error)) || "";
  const clashMsg = (raw && typeof raw === "object") ? (raw.error || JSON.stringify(raw)) : raw;
  check("live sellable-name collision → friendly 409", clash.status === 409 && /exists|menu|type/i.test(clashMsg), String(clash.status) + " " + String(clashMsg));

  // admin GET register list contains the demo ingredient
  const list = await api("/api/menu/register", ADMIN);
  const found = list.status === 200 && Array.isArray(list.json && list.json.data) && list.json.data.some((i) => i.item_name === DEMO_ING && i.type === "ingredient");
  check("live register list contains demo ingredient", found, String(list.status));

  // staff → 403 on GET and POST
  const sGet = await api("/api/menu/register", STAFF);
  check("live staff 403 on GET /api/menu/register", sGet.status === 403, String(sGet.status));
  const sPost = await api("/api/menu/register", STAFF, "POST", { item_name: DEMO_ING, type: "ingredient" });
  check("live staff 403 on POST /api/menu/register", sPost.status === 403, String(sPost.status));

  // ingredients excluded from the POS food menu (fetch_food_menu)
  const fm = await api("/api/fetch_food_menu?all=1", ADMIN);
  const fmJson = fm.json;
  let excluded = true;
  const walk = (v) => {
    if (typeof v === "string") { if (v === DEMO_ING) excluded = false; return; }
    if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === "object") Object.values(v).forEach(walk);
  };
  if (fm.status === 200) walk(fmJson);
  check("live fetch_food_menu excludes demo ingredient", fm.status === 200 && excluded, String(fm.status));
} catch (e) {
  check("live API reachable", false, String(e).slice(0, 160));
}

// ── 8. Cleanup demo rows (direct SQL, demo rows only) ──────────────────────
function sql(q) {
  return execSync(`docker exec psvibe-mysql mysql -upsvibe_user -p'PsVibe@User2024!' psvibe_api -e "${q}"`, { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
}
let cleaned = 0;
if (ingCreated) { try { sql(`DELETE FROM ingredients WHERE name='${DEMO_ING.replace(/'/g, "''")}'`); cleaned++; } catch (_) {} }
if (dishCreated) {
  try { sql(`DELETE FROM ingredients WHERE name='${DEMO_DISH.replace(/'/g, "''")}'`); } catch (_) {}
  try { sql(`DELETE FROM inventory WHERE item_name='${DEMO_DISH.replace(/'/g, "''")}'`); cleaned++; } catch (_) {}
}
check("cleanup: demo rows deleted (ing " + (ingCreated ? "yes" : "n/a") + ", dish " + (dishCreated ? "yes" : "n/a") + ")", (ingCreated ? cleaned >= 1 : true));

console.log(`\n==== HARNESS: ${PASS} passed, ${FAIL} failed ====`);
if (failures.length) { console.log("FAILED:", failures.join(", ")); process.exit(1); }
process.exit(0);
