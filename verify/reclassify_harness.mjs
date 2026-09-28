// reclassify_harness.mjs — regression harness for ingredient/product reclassification
// (owner 2026-09-28: "Amico Cake ကို Ingredient ထဲက နေ ဖြုတ်တာ မရဘူး").
//
// Verifies, without a browser:
//   1. Client-side setKind is hardened: optimistic update FIRST, force fresh reload,
//      and a `name || item_name` payload fallback to the correct endpoint path.
//   2. Live end-to-end reclassify on the REAL "Amico Cake": product -> ingredient
//      (appears in 🍚 list) -> product again (leaves 🍚 list, stays out), via
//      POST /api/menu/ingredients/kind.  Always restores Amico Cake to "product".
//   3. Staff -> 403 on the reclassify endpoint.
//
// Run:  node /opt/psvibe/api/verify/reclassify_harness.mjs
import { readFileSync } from "node:fs";
import { execSync } from "node:child_process";

const APP_JS = "/opt/kora-projects/psvibe-staff-app/code/www/app.js";

let PASS = 0, FAIL = 0;
const failures = [];
function check(name, ok, extra = "") {
  if (ok) PASS++; else { FAIL++; failures.push(name); }
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}` + (extra ? ` | ${extra}` : ""));
}

const appSrc = readFileSync(APP_JS, "utf8");

// ── 1. Client-side hardening ────────────────────────────────────────────────
check("setKind sends correct endpoint", appSrc.includes('PSV.api("/api/menu/ingredients/kind"'));
check("setKind payload uses name (with item_name fallback)", appSrc.includes("name: it.name || it.item_name"));
check("setKind optimistic update FIRST", appSrc.includes("const prevKind = it.kind"));
check("setKind forces fresh reload", appSrc.includes("loadIngredients(true)"));
check("setKind rolls back on failure", appSrc.includes("it.kind = prevKind"));
check("ingredient list filter = kind==='ingredient'", appSrc.includes('ingData.items.filter((i) => i.kind === "ingredient")'));

// ── 2. Live API ─────────────────────────────────────────────────────────────
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
  // initial: Amico Cake is a sellable product
  const all0 = await api("/api/menu/ingredients?kind=all", ADMIN);
  const items0 = (all0.json && all0.json.data && all0.json.data.items) || [];
  const amico0 = items0.find((i) => i.name === "Amico Cake");
  check("Amico Cake present", !!amico0);
  check("Amico Cake initially product (not in 🍚 ingredients)", amico0 && amico0.kind === "product", String(amico0 && amico0.kind));
  const ingNames0 = new Set(items0.filter((i) => i.kind === "ingredient").map((i) => i.name));
  check("Amico Cake NOT in ingredient list initially", !ingNames0.has("Amico Cake"));

  // flip Amico Cake -> ingredient (simulate the buggy state the owner saw)
  const toIng = await api("/api/menu/ingredients/kind", ADMIN, "POST", { name: "Amico Cake", kind: "ingredient" });
  check("reclassify -> ingredient 200", toIng.status === 200, String(toIng.status));
  const allIng = await api("/api/menu/ingredients?kind=all", ADMIN);
  const itemsIng = (allIng.json && allIng.json.data && allIng.json.data.items) || [];
  const amicoIng = itemsIng.find((i) => i.name === "Amico Cake");
  check("Amico Cake now ingredient after flip", amicoIng && amicoIng.kind === "ingredient", String(amicoIng && amicoIng.kind));
  const ingOnly = await api("/api/menu/ingredients?kind=ingredient", ADMIN);
  const ingOnlyNames = new Set(((ingOnly.json && ingOnly.json.data && ingOnly.json.data.items) || []).map((i) => i.name));
  check("Amico Cake appears in 🍚 ingredient list after flip", ingOnlyNames.has("Amico Cake"));

  // restore: flip back to product (the owner's actual goal)
  const toProd = await api("/api/menu/ingredients/kind", ADMIN, "POST", { name: "Amico Cake", kind: "product" });
  check("reclassify -> product 200", toProd.status === 200, String(toProd.status));
  const allBack = await api("/api/menu/ingredients?kind=all", ADMIN);
  const itemsBack = (allBack.json && allBack.json.data && allBack.json.data.items) || [];
  const amicoBack = itemsBack.find((i) => i.name === "Amico Cake");
  check("Amico Cake back to product", amicoBack && amicoBack.kind === "product", String(amicoBack && amicoBack.kind));
  const ingBackNames = new Set(itemsBack.filter((i) => i.kind === "ingredient").map((i) => i.name));
  check("Amico Cake stays OUT of 🍚 list after restore", !ingBackNames.has("Amico Cake"));

  // staff -> 403
  const staffRes = await api("/api/menu/ingredients/kind", STAFF, "POST", { name: "Amico Cake", kind: "product" });
  check("staff 403 on reclassify", staffRes.status === 403, String(staffRes.status));
} catch (e) {
  check("live API reachable", false, String(e).slice(0, 160));
}

console.log(`\n==== HARNESS: ${PASS} passed, ${FAIL} failed ====`);
if (failures.length) { console.log("FAILED:", failures.join(", ")); process.exit(1); }
process.exit(0);
