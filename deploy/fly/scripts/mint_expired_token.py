#!/usr/bin/env python3
"""Operator-only: mint an already-expired access token for the auth-gate test.
Run ON THE MACHINE (fly ssh console), never exposed over HTTP:
    python /app/scripts/mint_expired_token.py
Prints the token once; it is useless for anything except proving rejection.
"""
import os, sys
sys.path.insert(0, os.environ.get("MEMPALACE_APP_DIR", "/app"))
os.environ.setdefault("MEMPALACE_PUBLIC_URL", "https://placeholder.invalid")
from server.oauth import AuthServer  # noqa: E402
db = os.path.join(os.environ.get("MEMPALACE_DATA_DIR", "/data/mempalace"), "oauth.sqlite3")
a = AuthServer(issuer=os.environ["MEMPALACE_PUBLIC_URL"], db_path=db, passphrase=os.environ.get("MEMPALACE_OAUTH_PASSPHRASE") or "x" * 12)
print(a.issue_expired_access_token_for_test())
