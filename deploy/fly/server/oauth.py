"""Minimal single-user OAuth 2.1 authorization server for the MemPalace connector.

Implements exactly what claude.ai custom connectors, Claude Code and MCP
Inspector need to talk to a remote MCP server:

* RFC 8414 authorization-server metadata (``/.well-known/oauth-authorization-server``)
* RFC 9728 protected-resource metadata (``/.well-known/oauth-protected-resource``)
* RFC 7591 dynamic client registration (``/register``)
* Authorization-code grant with mandatory PKCE S256 (``/authorize`` + ``/token``)
* Rotating refresh tokens, RFC 7009 revocation (``/revoke``)

There is one human (the palace owner). ``/authorize`` shows a passphrase form;
the passphrase lives in the ``MEMPALACE_OAUTH_PASSPHRASE`` secret. Every
client that knows the passphrase gets a token for the single ``mempalace``
scope. Tokens are opaque 256-bit random strings stored *hashed* in SQLite on
the persistent volume, so restarts never log clients out.

Nothing here is specific to the MCP payload; ``app.py`` wires these routes
and the bearer check around ``/mcp``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from html import escape
from typing import Optional
from urllib.parse import urlencode, urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

logger = logging.getLogger("mempalace_oauth")

SCOPE = "mempalace"
AUTH_CODE_TTL_S = 10 * 60
ACCESS_TOKEN_TTL_S = int(os.environ.get("MEMPALACE_OAUTH_ACCESS_TTL_S", "3600"))
REFRESH_TOKEN_TTL_S = int(os.environ.get("MEMPALACE_OAUTH_REFRESH_TTL_S", str(90 * 24 * 3600)))
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_S = 15 * 60

# Redirect URIs a dynamically-registered client may use. Everything else is
# refused at registration time. Loopback is for Claude Code and MCP Inspector;
# the claude.ai / claude.com callbacks are the hosted clients.
_DEFAULT_ALLOWED_REDIRECT_HOSTS = "claude.ai,claude.com,localhost,127.0.0.1"


def _now() -> int:
    return int(time.time())


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _rand_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def _b64url_sha256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OAuthError(Exception):
    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(description or error)
        self.error = error
        self.description = description
        self.status = status

    def response(self) -> JSONResponse:
        body = {"error": self.error}
        if self.description:
            body["error_description"] = self.description
        return JSONResponse(body, status_code=self.status, headers={"Cache-Control": "no-store"})


@dataclass
class TokenInfo:
    client_id: str
    scope: str
    expires_at: int


class AuthServer:
    """All OAuth state + handlers. One instance per process."""

    def __init__(self, issuer: str, db_path: str, passphrase: str, allowed_redirect_hosts: Optional[str] = None):
        if not passphrase or len(passphrase) < 12:
            raise RuntimeError("MEMPALACE_OAUTH_PASSPHRASE must be set and at least 12 characters")
        self.issuer = issuer.rstrip("/")
        self.resource = f"{self.issuer}/mcp"
        self._passphrase = passphrase
        hosts = allowed_redirect_hosts or os.environ.get(
            "MEMPALACE_OAUTH_ALLOWED_REDIRECT_HOSTS", _DEFAULT_ALLOWED_REDIRECT_HOSTS
        )
        self._allowed_hosts = {h.strip().lower() for h in hosts.split(",") if h.strip()}
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._login_failures: dict[str, list[int]] = {}

    # ------------------------------------------------------------------ store
    def _init_schema(self) -> None:
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY,
                    client_secret_hash TEXT,
                    redirect_uris TEXT NOT NULL,
                    client_name TEXT,
                    token_endpoint_auth_method TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_codes (
                    code_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    resource TEXT,
                    expires_at INTEGER NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    token_hash TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,              -- access | refresh
                    client_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    family TEXT NOT NULL,            -- refresh-token family for rotation
                    revoked INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tokens_family ON tokens(family);
                """
            )
            self._db.commit()

    def _q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._db.execute(sql, params)
            rows = cur.fetchall()
            self._db.commit()
            return rows

    def purge_expired(self) -> None:
        now = _now()
        self._q("DELETE FROM auth_codes WHERE expires_at < ?", (now,))
        self._q("DELETE FROM tokens WHERE expires_at < ? OR revoked = 1", (now - 86400,))

    # --------------------------------------------------------------- metadata
    def as_metadata(self) -> dict:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "registration_endpoint": f"{self.issuer}/register",
            "revocation_endpoint": f"{self.issuer}/revoke",
            "scopes_supported": [SCOPE],
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "revocation_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "code_challenge_methods_supported": ["S256"],
            "service_documentation": "https://github.com/ahabshamaa/mempalace/tree/block5-http-client/deploy/fly",
        }

    def resource_metadata(self) -> dict:
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://github.com/ahabshamaa/mempalace/tree/block5-http-client/deploy/fly",
        }

    def www_authenticate(self, error: Optional[str] = None, description: str = "") -> str:
        parts = ['Bearer realm="mempalace"', f'resource_metadata="{self.issuer}/.well-known/oauth-protected-resource"']
        if error:
            parts.append(f'error="{error}"')
        if description:
            parts.append(f'error_description="{description}"')
        return ", ".join(parts)

    # ------------------------------------------------------------- registration
    def _redirect_uri_allowed(self, uri: str) -> bool:
        try:
            p = urlparse(uri)
        except ValueError:
            return False
        if p.scheme not in ("https", "http") or not p.hostname:
            return False
        host = p.hostname.lower()
        if host not in self._allowed_hosts:
            return False
        # Loopback may use http; everything else must be https. No fragments.
        if p.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
            return False
        if p.fragment:
            return False
        return True

    async def register(self, request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return OAuthError("invalid_client_metadata", "body must be JSON").response()
        if not isinstance(body, dict):
            return OAuthError("invalid_client_metadata", "body must be a JSON object").response()
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
            return OAuthError("invalid_redirect_uri", "redirect_uris (non-empty list) is required").response()
        bad = [u for u in uris if not self._redirect_uri_allowed(u)]
        if bad:
            logger.warning("DCR refused: redirect_uris not allowlisted: %s", bad)
            return OAuthError("invalid_redirect_uri", "redirect_uri host not allowed").response()
        grant_types = body.get("grant_types") or ["authorization_code"]
        if not set(grant_types) <= {"authorization_code", "refresh_token"}:
            return OAuthError("invalid_client_metadata", "unsupported grant_types").response()
        method = body.get("token_endpoint_auth_method") or "client_secret_basic"
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            return OAuthError("invalid_client_metadata", "unsupported token_endpoint_auth_method").response()
        client_id = _rand_token(16)
        secret = None if method == "none" else _rand_token(32)
        self._q(
            "INSERT INTO clients(client_id, client_secret_hash, redirect_uris, client_name, token_endpoint_auth_method, created_at) VALUES (?,?,?,?,?,?)",
            (client_id, _sha256(secret) if secret else None, json.dumps(uris), str(body.get("client_name") or "")[:200], method, _now()),
        )
        out = {
            "client_id": client_id,
            "client_id_issued_at": _now(),
            "redirect_uris": uris,
            "grant_types": sorted(set(grant_types) | {"refresh_token"}),
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
            "scope": SCOPE,
            "client_name": body.get("client_name"),
        }
        if secret:
            out["client_secret"] = secret
            out["client_secret_expires_at"] = 0
        logger.info("DCR: registered client %s (%s) uris=%s", client_id, out["client_name"], uris)
        return JSONResponse(out, status_code=201, headers={"Cache-Control": "no-store"})

    def _get_client(self, client_id: str) -> Optional[sqlite3.Row]:
        rows = self._q("SELECT * FROM clients WHERE client_id = ?", (client_id,))
        return rows[0] if rows else None

    # ---------------------------------------------------------------- authorize
    def _login_blocked(self, key: str) -> bool:
        now = _now()
        fails = [t for t in self._login_failures.get(key, []) if now - t < LOGIN_LOCKOUT_S]
        self._login_failures[key] = fails
        return len(fails) >= LOGIN_MAX_FAILURES

    def _record_failure(self, key: str) -> None:
        self._login_failures.setdefault(key, []).append(_now())

    @staticmethod
    def _client_ip(request: Request) -> str:
        xff = request.headers.get("fly-client-ip") or request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "?"

    def _validate_authorize_params(self, p: dict) -> dict:
        client_id = p.get("client_id", "")
        client = self._get_client(client_id)
        if not client:
            raise OAuthError("invalid_client", "unknown client_id", 400)
        redirect_uri = p.get("redirect_uri", "")
        allowed = json.loads(client["redirect_uris"])
        if redirect_uri not in allowed:
            raise OAuthError("invalid_request", "redirect_uri not registered for this client", 400)
        if p.get("response_type") != "code":
            raise OAuthError("unsupported_response_type", "only response_type=code", 400)
        if p.get("code_challenge_method", "S256") != "S256":
            raise OAuthError("invalid_request", "code_challenge_method must be S256", 400)
        cc = p.get("code_challenge", "")
        if not (43 <= len(cc) <= 128):
            raise OAuthError("invalid_request", "PKCE code_challenge is required", 400)
        scope = p.get("scope") or SCOPE
        requested = set(scope.split())
        if not requested <= {SCOPE}:
            raise OAuthError("invalid_scope", f"only scope '{SCOPE}' is available", 400)
        return {
            "client_id": client_id,
            "client_name": client["client_name"] or client_id,
            "redirect_uri": redirect_uri,
            "state": p.get("state", ""),
            "code_challenge": cc,
            "scope": SCOPE,
            "resource": p.get("resource", ""),
        }

    def _login_page(self, params: dict, error: str = "") -> HTMLResponse:
        hidden = "".join(
            f'<input type="hidden" name="{escape(k)}" value="{escape(v)}">'
            for k, v in params.items()
            if k in ("client_id", "redirect_uri", "state", "code_challenge", "scope", "resource")
        )
        hidden += '<input type="hidden" name="response_type" value="code">'
        err = f'<p class="err">{escape(error)}</p>' if error else ""
        html = f"""<!doctype html><html><head><meta charset="utf-8"><title>MemPalace sign in</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:-apple-system,system-ui,sans-serif;background:#111;color:#eee;display:flex;justify-content:center;padding:10vh 16px}}
form{{background:#1b1b1b;border:1px solid #333;border-radius:12px;padding:28px;max-width:380px;width:100%}}
h1{{font-size:20px;margin:0 0 6px}}p{{color:#aaa;font-size:14px;margin:0 0 18px}}
input[type=password]{{width:100%;box-sizing:border-box;padding:12px;border-radius:8px;border:1px solid #444;background:#0d0d0d;color:#fff;font-size:16px}}
button{{margin-top:14px;width:100%;padding:12px;border:0;border-radius:8px;background:#d97757;color:#fff;font-size:16px;font-weight:600}}
.err{{color:#ff7b7b}}</style></head><body>
<form method="post" action="/authorize" autocomplete="off">
<h1>MemPalace</h1>
<p><b>{escape(params.get('client_name', ''))}</b> wants access to your memory palace.</p>
{err}
<input type="password" name="passphrase" placeholder="Passphrase" required autofocus>
{hidden}
<button type="submit">Allow</button></form></body></html>"""
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    async def authorize_get(self, request: Request) -> Response:
        try:
            params = self._validate_authorize_params(dict(request.query_params))
        except OAuthError as e:
            # Unsafe to redirect when client/redirect_uri is bad: render the error.
            return HTMLResponse(f"<h1>Authorization error</h1><p>{escape(e.error)}: {escape(e.description)}</p>", status_code=e.status)
        return self._login_page(params)

    async def authorize_post(self, request: Request) -> Response:
        form = await request.form()
        p = {k: str(v) for k, v in form.items()}
        try:
            params = self._validate_authorize_params(p)
        except OAuthError as e:
            return HTMLResponse(f"<h1>Authorization error</h1><p>{escape(e.error)}: {escape(e.description)}</p>", status_code=e.status)
        ip = self._client_ip(request)
        if self._login_blocked(ip) or self._login_blocked("*"):
            logger.warning("login lockout for %s", ip)
            return self._login_page(params, "Too many attempts. Try again in 15 minutes.")
        supplied = p.get("passphrase", "")
        if not hmac.compare_digest(supplied.encode("utf-8"), self._passphrase.encode("utf-8")):
            self._record_failure(ip)
            self._record_failure("*")
            logger.warning("login failed from %s", ip)
            return self._login_page(params, "Wrong passphrase.")
        code = _rand_token(32)
        self._q(
            "INSERT INTO auth_codes(code_hash, client_id, redirect_uri, code_challenge, scope, resource, expires_at) VALUES (?,?,?,?,?,?,?)",
            (_sha256(code), params["client_id"], params["redirect_uri"], params["code_challenge"], params["scope"], params["resource"], _now() + AUTH_CODE_TTL_S),
        )
        q = {"code": code}
        if params["state"]:
            q["state"] = params["state"]
        sep = "&" if urlparse(params["redirect_uri"]).query else "?"
        logger.info("authorization granted to client %s from %s", params["client_id"], ip)
        return RedirectResponse(params["redirect_uri"] + sep + urlencode(q), status_code=302, headers={"Cache-Control": "no-store"})

    # -------------------------------------------------------------------- token
    def _authenticate_client(self, request: Request, form: dict) -> sqlite3.Row:
        client_id = form.get("client_id", "")
        secret = form.get("client_secret")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                raw = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
                client_id, secret = raw.split(":", 1)
            except Exception:
                raise OAuthError("invalid_client", "malformed Basic credentials", 401)
        client = self._get_client(client_id)
        if not client:
            raise OAuthError("invalid_client", "unknown client", 401)
        if client["token_endpoint_auth_method"] != "none":
            if not secret or not hmac.compare_digest(_sha256(secret), client["client_secret_hash"] or ""):
                raise OAuthError("invalid_client", "bad client credentials", 401)
        return client

    def _issue_tokens(self, client_id: str, scope: str, family: Optional[str] = None) -> dict:
        family = family or _rand_token(16)
        access = _rand_token(32)
        refresh = _rand_token(32)
        now = _now()
        with self._lock:
            self._db.execute(
                "INSERT INTO tokens(token_hash, kind, client_id, scope, expires_at, family, created_at) VALUES (?,?,?,?,?,?,?)",
                (_sha256(access), "access", client_id, scope, now + ACCESS_TOKEN_TTL_S, family, now),
            )
            self._db.execute(
                "INSERT INTO tokens(token_hash, kind, client_id, scope, expires_at, family, created_at) VALUES (?,?,?,?,?,?,?)",
                (_sha256(refresh), "refresh", client_id, scope, now + REFRESH_TOKEN_TTL_S, family, now),
            )
            self._db.commit()
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_S,
            "refresh_token": refresh,
            "scope": scope,
        }

    async def token(self, request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        try:
            client = self._authenticate_client(request, form)
            grant = form.get("grant_type")
            if grant == "authorization_code":
                code = form.get("code", "")
                verifier = form.get("code_verifier", "")
                rows = self._q("SELECT * FROM auth_codes WHERE code_hash = ?", (_sha256(code),))
                if not rows:
                    raise OAuthError("invalid_grant", "unknown code")
                row = rows[0]
                # Single use: consume first, then validate, so a replay can't race.
                self._q("DELETE FROM auth_codes WHERE code_hash = ?", (_sha256(code),))
                if row["used"] or row["expires_at"] < _now():
                    raise OAuthError("invalid_grant", "code expired or used")
                if row["client_id"] != client["client_id"]:
                    raise OAuthError("invalid_grant", "code was issued to another client")
                if form.get("redirect_uri") and form["redirect_uri"] != row["redirect_uri"]:
                    raise OAuthError("invalid_grant", "redirect_uri mismatch")
                if not (43 <= len(verifier) <= 128) or not hmac.compare_digest(_b64url_sha256(verifier), row["code_challenge"]):
                    raise OAuthError("invalid_grant", "PKCE verification failed")
                out = self._issue_tokens(client["client_id"], row["scope"])
                logger.info("tokens issued (code grant) to %s", client["client_id"])
            elif grant == "refresh_token":
                rt = form.get("refresh_token", "")
                rows = self._q("SELECT * FROM tokens WHERE token_hash = ? AND kind = 'refresh'", (_sha256(rt),))
                if not rows:
                    raise OAuthError("invalid_grant", "unknown refresh token")
                row = rows[0]
                if row["client_id"] != client["client_id"]:
                    raise OAuthError("invalid_grant", "refresh token belongs to another client")
                if row["revoked"]:
                    # Reuse of a rotated token: kill the whole family (RFC 6819 §5.2.2.3).
                    self._q("UPDATE tokens SET revoked = 1 WHERE family = ?", (row["family"],))
                    logger.warning("refresh token reuse detected; family %s revoked", row["family"])
                    raise OAuthError("invalid_grant", "refresh token reuse detected")
                if row["expires_at"] < _now():
                    raise OAuthError("invalid_grant", "refresh token expired")
                self._q("UPDATE tokens SET revoked = 1 WHERE token_hash = ?", (row["token_hash"],))
                out = self._issue_tokens(client["client_id"], row["scope"], family=row["family"])
                logger.info("tokens refreshed for %s", client["client_id"])
            else:
                raise OAuthError("unsupported_grant_type", "use authorization_code or refresh_token")
        except OAuthError as e:
            resp = e.response()
            if e.status == 401:
                resp.headers["WWW-Authenticate"] = 'Basic realm="mempalace"'
            return resp
        self.purge_expired()
        return JSONResponse(out, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    async def revoke(self, request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        try:
            client = self._authenticate_client(request, form)
        except OAuthError as e:
            return e.response()
        tok = form.get("token", "")
        rows = self._q("SELECT * FROM tokens WHERE token_hash = ? AND client_id = ?", (_sha256(tok), client["client_id"]))
        if rows:
            self._q("UPDATE tokens SET revoked = 1 WHERE family = ?", (rows[0]["family"],))
            logger.info("token family revoked by client %s", client["client_id"])
        # RFC 7009: 200 even for unknown tokens.
        return Response(status_code=200, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------- bearer check
    def validate_bearer(self, token: str) -> Optional[TokenInfo]:
        if not token:
            return None
        rows = self._q("SELECT * FROM tokens WHERE token_hash = ? AND kind = 'access'", (_sha256(token),))
        if not rows:
            return None
        row = rows[0]
        if row["revoked"] or row["expires_at"] < _now():
            return None
        return TokenInfo(client_id=row["client_id"], scope=row["scope"], expires_at=row["expires_at"])

    # -------------------------------------------------- operator helpers (not routed)
    def issue_expired_access_token_for_test(self) -> str:
        """Insert an already-expired access token. Used only by the auth-gate test
        via an operator shell on the machine; never reachable over HTTP."""
        access = _rand_token(32)
        now = _now()
        self._q(
            "INSERT INTO tokens(token_hash, kind, client_id, scope, expires_at, family, created_at) VALUES (?,?,?,?,?,?,?)",
            (_sha256(access), "access", "test", SCOPE, now - 60, _rand_token(8), now - 3600),
        )
        return access
