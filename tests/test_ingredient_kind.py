"""Unit tests for ingredient-vs-product split + unit display (owner 2026-09-28).

PURE tests only — no database, no writes. They exercise the new pure helpers in
routes/menu_cost.py:
  * normalize_kind         — alias parsing for 'ingredient' / 'product'
  * classify_item_kind     — heuristic (sellable -> product, else ingredient) +
                             explicit override
  * filter_items_by_kind   — ?kind=ingredient|product|all filtering
  * ingredient_display     — unit-cost display (per base unit when a conversion is
                             configured, else per purchase unit — never a fake /g)

Also re-checks the resale/dish costing helpers do not regress (same engine).

Run:  /opt/psvibe/api/venv/bin/python tests/test_ingredient_kind.py
      or  /opt/psvibe/api/venv/bin/python -m pytest tests/test_ingredient_kind.py -q
"""
import sys
from decimal import Decimal

sys.path.insert(0, "/opt/psvibe/api")

from routes.menu_cost import (
    normalize_kind,
    classify_item_kind,
    filter_items_by_kind,
    ingredient_display,
    # regression: resale/dish costing must be unchanged
    resale_unit_cost,
    classify_item_type,
    cost_per_base_unit,
    lookup_conversion,
)

D = Decimal


def main():
    passed = failed = 0

    def check(name, got, expected):
        nonlocal passed, failed
        ok = got == expected
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name} -> got={got!r} expected={expected!r}")

    # ── normalize_kind aliases ───────────────────────────────────────────────
    check("kind: 'ingredient'", normalize_kind("ingredient"), "ingredient")
    check("kind: 'raw'", normalize_kind("raw"), "ingredient")
    check("kind: 'ဟင်းခံ'", normalize_kind("ဟင်းခံ"), "ingredient")
    check("kind: 'INGREDIENT' (case)", normalize_kind("INGREDIENT"), "ingredient")
    check("kind: 'product'", normalize_kind("product"), "product")
    check("kind: 'resale'", normalize_kind("resale"), "product")
    check("kind: 'packaged'", normalize_kind("packaged"), "product")
    check("kind: 'Ready-made'", normalize_kind("Ready-made"), "product")
    check("kind: invalid -> None", normalize_kind("xyz"), None)
    check("kind: empty -> None", normalize_kind(""), None)
    check("kind: None -> None", normalize_kind(None), None)

    # ── classify_item_kind heuristic ────────────────────────────────────────
    check("classify: sellable -> product", classify_item_kind(None, True), "product")
    check("classify: not sellable -> ingredient", classify_item_kind(None, False), "ingredient")
    check("classify: explicit ingredient overrides sellable", classify_item_kind("ingredient", True), "ingredient")
    check("classify: explicit product overrides non-sellable", classify_item_kind("product", False), "product")
    check("classify: empty kind + sellable -> product", classify_item_kind("", True), "product")
    check("classify: empty kind + non-sellable -> ingredient", classify_item_kind("", False), "ingredient")
    check("classify: invalid kind + non-sellable -> ingredient (fallback)", classify_item_kind("banana", False), "ingredient")

    # ── filter_items_by_kind ────────────────────────────────────────────────
    items = [
        {"name": "Amico Cake", "kind": "product"},
        {"name": "ကြက်ဥ", "kind": "ingredient"},
        {"name": "Coca Cola", "kind": "product"},
        {"name": "ဆန်", "kind": "ingredient"},
    ]
    check("filter: ingredient", [i["name"] for i in filter_items_by_kind(items, "ingredient")],
          ["ကြက်ဥ", "ဆန်"])
    check("filter: product", [i["name"] for i in filter_items_by_kind(items, "product")],
          ["Amico Cake", "Coca Cola"])
    check("filter: all", [i["name"] for i in filter_items_by_kind(items, "all")],
          ["Amico Cake", "ကြက်ဥ", "Coca Cola", "ဆန်"])
    check("filter: None (no filter)", [i["name"] for i in filter_items_by_kind(items, None)],
          ["Amico Cake", "ကြက်ဥ", "Coca Cola", "ဆန်"])
    check("filter: unknown -> all", [i["name"] for i in filter_items_by_kind(items, "bogus")],
          ["Amico Cake", "ကြက်ဥ", "Coca Cola", "ဆန်"])
    check("filter: empty -> []", filter_items_by_kind([], "ingredient"), [])

    # ── ingredient_display — unit text ──────────────────────────────────────
    # No conversion configured -> cost per PURCHASE unit, never a fake /g.
    d = ingredient_display(D("515.029"), None, "g", None)
    check("display: no conversion -> has_conversion False", d["has_conversion"], False)
    check("display: no conversion -> basis purchase", d["cost_basis"], "purchase")
    check("display: no conversion -> display_cost = wavg", d["display_cost"], D("515.029"))
    check("display: no conversion -> unit falls back 'pcs' (no fake /g)", d["unit_label"], "pcs")

    # Explicit purchase unit (pack) with no conversion -> per pack.
    d2 = ingredient_display(D("5000"), "ထုပ်", "g", D("1"))
    check("display: pack no conversion -> unit_label ထုပ်", d2["unit_label"], "ထုပ်")
    check("display: pack no conversion -> basis purchase", d2["cost_basis"], "purchase")
    check("display: pack no conversion -> display_cost 5000", d2["display_cost"], D("5000"))

    # Conversion configured (ပိဿာ = 1632 g) -> per BASE unit (g).
    d3 = ingredient_display(D("1632"), "ပိဿာ", "g", D("1632"))
    check("display: ပိဿာ conversion -> has_conversion True", d3["has_conversion"], True)
    check("display: ပိဿာ conversion -> basis base", d3["cost_basis"], "base")
    check("display: ပိဿာ conversion -> unit_label g", d3["unit_label"], "g")
    check("display: ပိဿာ conversion -> display_cost 1/g", d3["display_cost"], D("1.000000"))

    # Liquid conversion (litre = 1000 ml) -> per ml.
    d4 = ingredient_display(D("3000"), "လီတာ", "ml", D("1000"))
    check("display: litre conversion -> unit_label ml", d4["unit_label"], "ml")
    check("display: litre conversion -> display_cost 3/ml", d4["display_cost"], D("3.000000"))

    # No purchase history -> display_cost None (never 0).
    d5 = ingredient_display(None, None, "g", None)
    check("display: no purchase -> display_cost None", d5["display_cost"], None)
    check("display: no purchase -> unit still sensible", d5["unit_label"], "pcs")

    # ── lookup_conversion additions (Burmese units) ──────────────────────────
    check("lookup: လီတာ = 1000", lookup_conversion("လီတာ"), D("1000"))
    check("lookup: ထုပ် = 1", lookup_conversion("ထုပ်"), D("1"))
    check("lookup: ဗူး = 1", lookup_conversion("ဗူး"), D("1"))
    check("lookup: ပိဿာ = 1632 (unchanged)", lookup_conversion("ပိဿာ"), D("1632"))

    # ── regression: resale/dish costing unchanged ───────────────────────────
    check("regress: resale wavg", resale_unit_cost([(10, 1000), (20, 3000)]), D("133.3333"))
    check("regress: resale empty -> None", resale_unit_cost([]), None)
    check("regress: classify dish (has recipe)", classify_item_type("resale", True), "dish")
    check("regress: classify resale (no recipe)", classify_item_type(None, False), "resale")
    check("regress: cost/base ပိဿာ", cost_per_base_unit(D("1632"), D("1632")), D("1.000000"))

    print(f"\nTOTAL: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
