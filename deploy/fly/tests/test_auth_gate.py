"""HARD GATE: the public endpoint must reject every unauthenticated or malformed credential.

  BASE_URL=https://app.fly.dev MEMPALACE_OAUTH_PASSPHRASE=... EXPIRED_TOKEN=<from operator shell> pytest tests/test_auth_gate.py

EXPIRED_TOKEN is minted on the machine with scripts/mint_expired_token.py (there is
deliberately no HTTP route that can create one). If unset, the expiry test instead
uses a token whose access TTL has been exhausted by waiting — only when
MEMPALACE_OAUTH_ACCESS_TTL_S is short on a dev server — or is skipped with a loud message.
"""
from __future__ import annotations

import os
import time

import httpx
import pytest

from oauth_client import BASE_URL, MCP, get_token

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "gate", "version": "1"}}}
HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def http():
    with httpx.Client(timeout=30) as c:
        yield c


def test_tls_only():
    assert BASE_URL.startswith("https://") or BASE_URL.startswith("http://127.0.0.1"), "public URL must be https"


def test_no_token_rejected(http):
    r = http.post(f"{BASE_URL}/mcp", json=INIT, headers=HDR)
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers.get("www-authenticate", "")
    assert "result" not in r.text


def test_wrong_token_rejected(http):
    r = http.post(f"{BASE_URL}/mcp", json=INIT, headers={**HDR, "Authorization": "Bearer definitely-not-a-valid-token-0123456789"})
    assert r.status_code == 401
    assert "invalid_token" in r.headers.get("www-authenticate", "")


def test_garbage_auth_scheme_rejected(http):
    r = http.post(f"{BASE_URL}/mcp", json=INIT, headers={**HDR, "Authorization": "Basic Zm9vOmJhcg=="})
    assert r.status_code == 401


def test_token_in_query_string_rejected(http):
    tok = get_token()["access_token"]
    # even a VALID token is refused when it travels in the URL
    r = http.post(f"{BASE_URL}/mcp?access_token={tok}", json=INIT, headers=HDR)
    assert r.status_code in (400, 401)
    assert "result" not in r.text
    r = http.post(f"{BASE_URL}/mcp?token={tok}", json=INIT, headers=HDR)
    assert r.status_code in (400, 401)


def test_expired_token_rejected(http):
    expired = os.environ.get("EXPIRED_TOKEN")
    if not expired:
        pytest.fail("EXPIRED_TOKEN not provided — mint one on the machine with scripts/mint_expired_token.py")
    r = http.post(f"{BASE_URL}/mcp", json=INIT, headers={**HDR, "Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
    assert "invalid_token" in r.headers.get("www-authenticate", "")


def test_revoked_token_rejected(http):
    tok = get_token(http)
    m = MCP(tok["access_token"], http)
    assert m.initialize()["serverInfo"]["name"] == "mempalace"
    r = http.post(tok["asm"]["revocation_endpoint"], data={"token": tok["access_token"], "client_id": tok["reg"]["client_id"]})
    assert r.status_code == 200
    r = http.post(f"{BASE_URL}/mcp", json=INIT, headers={**HDR, "Authorization": f"Bearer {tok['access_token']}"})
    assert r.status_code == 401


def test_wrong_passphrase_gets_no_code(http):
    from oauth_client import authorize, discover, register
    _, asm = discover(http)
    reg = register(http, asm)
    with pytest.raises(RuntimeError):
        authorize(http, asm, reg, passphrase="not-the-passphrase-xyz")


def test_bad_pkce_verifier_rejected(http):
    from oauth_client import authorize, discover, register
    _, asm = discover(http)
    reg = register(http, asm)
    code, _verifier = authorize(http, asm, reg)
    r = http.post(asm["token_endpoint"], data={"grant_type": "authorization_code", "code": code, "redirect_uri": os.environ.get("TEST_REDIRECT_URI", "http://localhost:3118/callback"),
                                               "client_id": reg["client_id"], "code_verifier": "x" * 50})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_dcr_refuses_foreign_redirect(http):
    r = http.post(f"{BASE_URL}/register", json={"client_name": "evil", "redirect_uris": ["https://evil.example.com/cb"], "token_endpoint_auth_method": "none"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"


def test_health_is_the_only_open_endpoint(http):
    assert http.get(f"{BASE_URL}/health").status_code in (200, 503)
    assert http.get(f"{BASE_URL}/mcp").status_code in (401, 405)
    assert http.delete(f"{BASE_URL}/mcp").status_code == 401


def test_valid_token_works(http):
    tok = get_token(http)
    m = MCP(tok["access_token"], http)
    assert m.initialize()["serverInfo"]["name"] == "mempalace"
    assert any(t["name"] == "mempalace_status" for t in m.tools_list())
