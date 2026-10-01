"""Headless OAuth 2.1 client for the MemPalace connector test-suite.

Does what claude.ai / Claude Code do: discover metadata, dynamically register,
authorize (posting the passphrase to the login form), exchange the code with
PKCE, refresh. Reads BASE_URL and MEMPALACE_OAUTH_PASSPHRASE from the environment.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from urllib.parse import parse_qs, urlparse

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080").rstrip("/")
PASSPHRASE = os.environ.get("MEMPALACE_OAUTH_PASSPHRASE", "")
REDIRECT = os.environ.get("TEST_REDIRECT_URI", "http://localhost:3118/callback")


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def discover(client: httpx.Client) -> tuple[dict, dict]:
    r = client.get(f"{BASE_URL}/.well-known/oauth-protected-resource"); r.raise_for_status()
    prm = r.json()
    r = client.get(f"{prm['authorization_servers'][0]}/.well-known/oauth-authorization-server"); r.raise_for_status()
    return prm, r.json()


def register(client: httpx.Client, asm: dict, auth_method="none") -> dict:
    r = client.post(asm["registration_endpoint"], json={
        "client_name": "mempalace-tests", "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
    })
    assert r.status_code == 201, r.text
    return r.json()


def authorize(client: httpx.Client, asm: dict, reg: dict, passphrase: str = PASSPHRASE) -> tuple[str, str]:
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(8)
    params = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
              "code_challenge": challenge, "code_challenge_method": "S256", "scope": "mempalace",
              "state": state, "resource": f"{BASE_URL}/mcp"}
    r = client.get(asm["authorization_endpoint"], params=params); assert r.status_code == 200, r.text
    r = client.post(asm["authorization_endpoint"], data={**params, "passphrase": passphrase}, follow_redirects=False)
    if r.status_code != 302:
        raise RuntimeError(f"authorize did not redirect: {r.status_code}")
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"][0] == state
    return q["code"][0], verifier


def exchange(client: httpx.Client, asm: dict, reg: dict, code: str, verifier: str) -> dict:
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": reg["client_id"], "code_verifier": verifier, "resource": f"{BASE_URL}/mcp"}
    if reg.get("client_secret"):
        data["client_secret"] = reg["client_secret"]
    r = client.post(asm["token_endpoint"], data=data); assert r.status_code == 200, r.text
    return r.json()


def refresh(client: httpx.Client, asm: dict, reg: dict, refresh_token: str) -> httpx.Response:
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": reg["client_id"]}
    if reg.get("client_secret"):
        data["client_secret"] = reg["client_secret"]
    return client.post(asm["token_endpoint"], data=data)


def get_token(client: httpx.Client | None = None) -> dict:
    """Full flow → token response dict (+ 'reg', 'asm' for follow-ups)."""
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        _, asm = discover(client)
        reg = register(client, asm)
        code, verifier = authorize(client, asm, reg)
        tok = exchange(client, asm, reg, code, verifier)
        tok["reg"] = reg; tok["asm"] = asm
        return tok
    finally:
        if own:
            client.close()


class MCP:
    """Tiny Streamable HTTP MCP client."""

    def __init__(self, token: str, client: httpx.Client | None = None):
        self.c = client or httpx.Client(timeout=120)
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                  "Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"}
        self.sid = None
        self._id = 0

    def call(self, method: str, params: dict | None = None, notify: bool = False) -> httpx.Response:
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self._id += 1; body["id"] = self._id
        h = dict(self.h)
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        r = self.c.post(f"{BASE_URL}/mcp", json=body, headers=h)
        if "mcp-session-id" in r.headers:
            self.sid = r.headers["mcp-session-id"]
        return r

    def initialize(self) -> dict:
        r = self.call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "tests", "version": "1"}})
        r.raise_for_status(); res = r.json()["result"]
        self.call("notifications/initialized", {}, notify=True)
        return res

    def tools_list(self) -> list[dict]:
        r = self.call("tools/list", {}); r.raise_for_status(); return r.json()["result"]["tools"]

    def tool(self, name: str, args: dict | None = None) -> dict:
        r = self.call("tools/call", {"name": name, "arguments": args or {}}); r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RuntimeError(j["error"])
        return j["result"]

    def tool_text(self, name: str, args: dict | None = None) -> str:
        res = self.tool(name, args)
        return "\n".join(c.get("text", "") for c in res.get("content", []))
