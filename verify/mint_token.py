#!/usr/bin/env python
"""Mint a JWT for the live API (used by verify harnesses). argv[1] = admin|staff."""
import sys, os

sys.path.insert(0, "/opt/psvibe/api")

SEC = {}
with open("/etc/psvibe/secrets.env") as f:
    for ln in f:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        SEC[k.strip()] = v.strip().strip('"').strip("'")
os.environ["JWT_SECRET_KEY"] = SEC.get("JWT_SECRET_KEY", "")

from auth import create_access_token  # noqa: E402

role = sys.argv[1] if len(sys.argv) > 1 else "admin"
if role == "admin":
    print(create_access_token({"sub": "admin"}))
else:
    print(create_access_token({"sub": "demo", "role": "staff", "scope": "staff-webapp"}))
