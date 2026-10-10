#!/usr/bin/env python3
"""Standalone SAFE smoke check — no DB connection, no app.main import.

Checks: syntax of every module + an AST inventory of HTTP route decorators.
Usage: python3 smoke.py [project_dir]
Exit code 0 = OK, 1 = syntax errors found.
"""
import ast
import pathlib
import py_compile
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
SKIP = ("/.venv/", "/venv/", "/node_modules/", "/site-packages/", "/_archive/",
        "/.git/", "/dist/", "/build/", "/.dart_tool/")

bad, routes, nfiles = [], 0, 0
for p in sorted(root.rglob("*.py")):
    s = str(p)
    if any(x in s for x in SKIP):
        continue
    nfiles += 1
    try:
        py_compile.compile(str(p), doraise=True)
    except py_compile.PyCompileError as e:
        bad.append(f"{p}: {e}")
        continue
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                try:
                    src = ast.unparse(d)
                except Exception:
                    continue
                if src.startswith(("router.", "app.")) and any(
                    m in src for m in ("get", "post", "put", "patch", "delete")
                ):
                    routes += 1

print(f"smoke: files={nfiles} routes={routes} syntax_errors={len(bad)}")
for b in bad[:10]:
    print("  ERR", b)
sys.exit(1 if bad else 0)
