// menuregister_ui_harness.mjs — Staff App harness for the Menu Register +
// Stock-In cleanup (owner 2026-09-28 Osmo).
//
// Verifies, without a browser:
//   1. The MenuRegister and Inventory Vue templates from app.js COMPILE cleanly
//      with the real Vue compiler (catches template syntax errors).
//   2. Static structure: Menu Register (မီနူး စာရင်း) with type tabs + selling
//      price + Burmese hints; Stock-In picker has NO "new item" path.
//   3. Live API: /api/menu/register is admin-only (staff -> 403).
//
// Run:  node /opt/psvibe/api/verify/menuregister_ui_harness.mjs
import { readFileSync } from "node:fs";
import { execSync } from "node:child_process";
import vm from "node:vm";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);

const WWW = "/opt/kora-projects/psvibe-staff-app/code/www";
const APP_JS = WWW + "/app.js";
const VUE_JS = WWW + "/vendor/vue.global.prod.js";

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

// ── 2. Extract a component's template literal (no backticks inside) ───────
const appSrc = readFileSync(APP_JS, "utf8");
function extractTemplate(componentName) {
  const i0 = appSrc.indexOf(`const ${componentName} = {`);
  if (i0 === -1) return null;
  const m = `template: \``;
  const t0 = appSrc.indexOf(m, i0);
  if (t0 === -1) return null;
  const t1 = appSrc.indexOf("`", t0 + m.length);
  return appSrc.slice(t0 + m.length, t1);
}

// ── 3. MenuRegister template ───────────────────────────────────────────────
const mrTpl = extractTemplate("MenuRegister");
check("MenuRegister component found", mrTpl !== null);
if (mrTpl) {
  let compiled = null, compileError = null;
  try { compiled = Vue.compile(mrTpl); } catch (e) { compileError = e; }
  check("Vue.compile(MenuRegister) succeeds", !compileError && !!compiled, compileError ? String(compileError).slice(0, 200) : "compiled");
  check("MenuRegister title မီနူး စာရင်း", mrTpl.includes("မီနူး စာရင်း"));
  check("MenuRegister type tabs (dish/ready/ingredient)", mrTpl.includes("ဟင်းပွဲ") && mrTpl.includes("အဆင်သင့်") && mrTpl.includes("ဟင်းခံ"));
  check("MenuRegister selling price field (ရောင်းဈေး)", mrTpl.includes("ရောင်းဈေး"));
  check("MenuRegister ingredient no-price note", mrTpl.includes("ရောင်းဈေး မလို"));
  check("MenuRegister type switch setType", appSrc.slice(appSrc.indexOf("const MenuRegister = {"), appSrc.indexOf("const RecipeCost = {", appSrc.indexOf("const MenuRegister = {") + 1)).includes("function setType"));
  check("MenuRegister warns missing price (warnText)", appSrc.includes("function warnText"));
}

// ── 4. Inventory (Stock-In) template — no "new item" ──────────────────────
const invTpl = extractTemplate("Inventory");
check("Inventory component found", invTpl !== null);
if (invTpl) {
  let compiled = null, compileError = null;
  try { compiled = Vue.compile(invTpl); } catch (e) { compileError = e; }
  check("Vue.compile(Inventory) succeeds", !compileError && !!compiled, compileError ? String(compileError).slice(0, 200) : "compiled");
  check("Stock-In: no 'New Item Name' field", !invTpl.includes("New Item Name"));
  check("Stock-In: no custom_name", !invTpl.includes("custom_name"));
  check("Stock-In: no isNew field", !invTpl.includes("row.isNew"));
}
// The itemColumns '__new__' entry must be gone from the Inventory setup region.
const invSetup = appSrc.slice(appSrc.indexOf("const Inventory = {"), appSrc.indexOf("const MenuRegister = {"));
check("Stock-In picker has no '__new__' entry", !invSetup.includes('value: "__new__"'));
check("Stock-In picker loads /api/stock/pick-items", invSetup.includes("/api/stock/pick-items"));
check("Stock-In has goRegister shortcut to /menu-register", invSetup.includes('router.push("/menu-register")'));

// ── 5. Menu wiring: route + component + menu entries ──────────────────────
check("router.register /menu-register", appSrc.includes('router.register("/menu-register", MenuRegister)'));
check("app.component MenuRegister", appSrc.includes('app.component("MenuRegister", MenuRegister)'));
check("menu entry in Kitchen group", appSrc.includes('path: "/menu-register", adminOnly: true'));

// ── 6. Live API: admin-only register ───────────────────────────────────────
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
  const rAdmin = await api("/api/menu/register", ADMIN);
  check("live GET /api/menu/register 200 (admin)", rAdmin.status === 200, String(rAdmin.status));
  check("register list is an array", Array.isArray(rAdmin.json && rAdmin.json.data), String(rAdmin.json && rAdmin.json.success));
  const rStaff = await api("/api/menu/register", STAFF);
  check("live staff 403 on GET /api/menu/register", rStaff.status === 403, String(rStaff.status));
  const rStaffPost = await api("/api/menu/register", STAFF, "POST", { item_name: "staff-x", type: "resale", unit_price: 1 });
  check("live staff 403 on POST /api/menu/register", rStaffPost.status === 403, String(rStaffPost.status));
  const rPick = await api("/api/stock/pick-items", STAFF);
  check("live staff 200 on /api/stock/pick-items", rPick.status === 200, String(rPick.status));
} catch (e) {
  check("live API reachable", false, String(e).slice(0, 160));
}

console.log(`\n==== HARNESS: ${PASS} passed, ${FAIL} failed ====`);
if (failures.length) { console.log("FAILED:", failures.join(", ")); process.exit(1); }
process.exit(0);
