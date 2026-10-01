"""Streamable HTTP front door for the MemPalace MCP server.

Wraps the fork's existing stdio JSON-RPC handler (``mempalace.mcp_server.handle_request``)
in an ASGI app so every tool is exposed unchanged over MCP Streamable HTTP
(spec 2025-03-26 / 2025-06-18 / 2025-11-25):

    POST /mcp     JSON-RPC request(s) in, JSON response out (``application/json``)
    GET  /mcp     405 — this server never opens a server-initiated SSE stream
    DELETE /mcp   ends a session

Auth: every ``/mcp`` call must carry ``Authorization: Bearer <access token>``
issued by :mod:`oauth`. Tokens in the query string are rejected outright.
``/health`` is the only unauthenticated endpoint and leaks nothing but liveness.

Concurrency: the fork's handler was written for a single stdio reader, so tool
calls run on a worker thread under one process-wide lock. Protocol methods
(initialize / ping / tools/list) bypass the lock.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import threading
import time
from typing import Any, Optional

import anyio
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from .oauth import AuthServer

logger = logging.getLogger("mempalace_http")

PUBLIC_URL = os.environ.get("MEMPALACE_PUBLIC_URL", "").rstrip("/")
if not PUBLIC_URL:
    app_name = os.environ.get("FLY_APP_NAME", "")
    PUBLIC_URL = f"https://{app_name}.fly.dev" if app_name else "http://127.0.0.1:8080"

DATA_DIR = os.environ.get("MEMPALACE_DATA_DIR", os.path.expanduser("~/.mempalace"))
OAUTH_DB = os.path.join(DATA_DIR, "oauth.sqlite3")
MAX_BODY_BYTES = 8 * 1024 * 1024
SESSION_IDLE_S = 7 * 24 * 3600
_SESSION_COUNT_CAP = 10_000

# The fork's handler. Importing it redirects fd 1 -> fd 2 (stdio protection);
# harmless here because uvicorn logs to stderr.
from mempalace import mcp_server as _mp  # noqa: E402
from mempalace.palace_client import backend_address, probe_backend  # noqa: E402

_PROTOCOL_METHODS = frozenset({"initialize", "ping", "tools/list"})
_TOOL_LOCK = threading.Lock()
_sessions: dict[str, float] = {}
_sessions_lock = threading.Lock()


def _touch_session(sid: str) -> None:
    with _sessions_lock:
        now = time.monotonic()
        _sessions[sid] = now
        if len(_sessions) > _SESSION_COUNT_CAP:
            for k, t in list(_sessions.items()):
                if now - t > SESSION_IDLE_S:
                    _sessions.pop(k, None)


def _session_known(sid: str) -> bool:
    with _sessions_lock:
        t = _sessions.get(sid)
        if t is None:
            return False
        if time.monotonic() - t > SESSION_IDLE_S:
            _sessions.pop(sid, None)
            return False
        return True


def _dispatch_one(msg: Any) -> Optional[dict]:
    """Run one JSON-RPC message through the fork's handler."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method = msg.get("method") or ""
    if method in _PROTOCOL_METHODS or method.startswith("notifications/"):
        return _mp.handle_request(msg)
    with _TOOL_LOCK:
        return _mp.handle_request(msg)


_ALLOWED_ORIGINS = {
    o.strip().lower()
    for o in os.environ.get(
        "MEMPALACE_ALLOWED_ORIGINS", "https://claude.ai,https://claude.com,https://www.claude.ai"
    ).split(",")
    if o.strip()
}


def _origin_allowed(origin: str) -> bool:
    o = origin.strip().lower().rstrip("/")
    if o in _ALLOWED_ORIGINS or o == PUBLIC_URL.lower():
        return True
    # Loopback origins (MCP Inspector web UI, local dev) on any port.
    return o.startswith(("http://localhost:", "http://127.0.0.1:", "http://localhost", "http://127.0.0.1"))


def _jsonrpc_error(req_id: Any, code: int, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}, status_code=status)


class _State:
    auth: AuthServer


_state = _State()


# ------------------------------------------------------------------ auth layer
class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Protects ``/mcp``. Everything else is either public metadata or the OAuth flow."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path != "/mcp":
            return await call_next(request)
        auth = _state.auth
        # Streamable HTTP: servers MUST validate Origin when present (DNS-rebinding guard).
        origin = request.headers.get("origin")
        if origin and not _origin_allowed(origin):
            logger.warning("rejected: disallowed Origin %r", origin)
            return PlainTextResponse("Forbidden origin", status_code=403)
        qp = request.query_params
        if "access_token" in qp or "token" in qp or "authorization" in qp:
            # No credentials in URLs, ever (RFC 6750 §2.3 is forbidden here).
            logger.warning("rejected: token in query string from %s", AuthServer._client_ip(request))
            return JSONResponse(
                {"error": "invalid_request", "error_description": "credentials must be sent in the Authorization header"},
                status_code=400,
                headers={"WWW-Authenticate": auth.www_authenticate("invalid_request", "token in query string")},
            )
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "unauthorized", "error_description": "missing bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": auth.www_authenticate()},
            )
        token = header.split(" ", 1)[1].strip()
        info = auth.validate_bearer(token)
        if info is None:
            return JSONResponse(
                {"error": "invalid_token", "error_description": "token is invalid, expired or revoked"},
                status_code=401,
                headers={"WWW-Authenticate": auth.www_authenticate("invalid_token", "invalid, expired or revoked")},
            )
        request.state.client_id = info.client_id
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Fly terminates TLS; force_https in fly.toml redirects plain HTTP. Refuse
        # anything that still arrives unencrypted (e.g. a misconfigured proxy).
        if request.headers.get("x-forwarded-proto", "https") != "https" and request.url.hostname not in ("127.0.0.1", "localhost"):
            return PlainTextResponse("TLS required", status_code=426, headers={"Upgrade": "TLS/1.2"})
        resp = await call_next(request)
        resp.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp


# ------------------------------------------------------------------ /mcp
async def mcp_post(request: Request) -> Response:
    accept = request.headers.get("accept", "")
    if accept and "application/json" not in accept and "*/*" not in accept:
        return _jsonrpc_error(None, -32600, "Accept must include application/json", 406)
    ctype = request.headers.get("content-type", "")
    if "application/json" not in ctype:
        return _jsonrpc_error(None, -32600, "Content-Type must be application/json", 415)
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return _jsonrpc_error(None, -32600, "request too large", 413)
    try:
        payload = json.loads(body)
    except Exception:
        return _jsonrpc_error(None, -32700, "Parse error", 400)

    sid = request.headers.get("mcp-session-id")
    is_batch = isinstance(payload, list)
    msgs = payload if is_batch else [payload]
    has_initialize = any(isinstance(m, dict) and m.get("method") == "initialize" for m in msgs)
    if sid is not None and not _session_known(sid) and not has_initialize:
        return _jsonrpc_error(None, -32600, "unknown session; re-initialize", 404)

    results = await anyio.to_thread.run_sync(lambda: [_dispatch_one(m) for m in msgs])
    responses = [r for r in results if r is not None]

    headers = {}
    if has_initialize:
        sid = secrets.token_urlsafe(24)
        _touch_session(sid)
        logger.info("session %s initialized by client %s", sid[:8], getattr(request.state, "client_id", "?"))
    if sid:
        _touch_session(sid)
        headers["Mcp-Session-Id"] = sid

    if not responses:
        # Only notifications/responses: 202 Accepted, no body.
        return Response(status_code=202, headers=headers)
    out = responses if is_batch else responses[0]
    return Response(json.dumps(out, ensure_ascii=False), media_type="application/json", headers=headers)


async def mcp_get(request: Request) -> Response:
    # No server-initiated messages; tell the client not to try to open a stream.
    return PlainTextResponse("Method Not Allowed", status_code=405, headers={"Allow": "POST, DELETE"})


async def mcp_delete(request: Request) -> Response:
    sid = request.headers.get("mcp-session-id")
    if sid:
        with _sessions_lock:
            _sessions.pop(sid, None)
    return Response(status_code=200)


# ------------------------------------------------------------------ misc
async def health(request: Request) -> Response:
    try:
        probe_backend(timeout_s=2.0)
        chroma = "ok"
    except Exception as exc:  # noqa: BLE001
        chroma = f"down ({exc.__class__.__name__})"
    status = 200 if chroma == "ok" else 503
    return JSONResponse({"status": "ok" if status == 200 else "degraded", "chroma": chroma}, status_code=status)


async def as_metadata(request: Request) -> Response:
    return JSONResponse(_state.auth.as_metadata(), headers={"Cache-Control": "public, max-age=3600"})


async def pr_metadata(request: Request) -> Response:
    return JSONResponse(_state.auth.resource_metadata(), headers={"Cache-Control": "public, max-age=3600"})


async def root(request: Request) -> Response:
    return PlainTextResponse("MemPalace remote MCP connector. Endpoint: /mcp (OAuth 2.1 required).\n")


def build_app() -> Starlette:
    passphrase = os.environ.get("MEMPALACE_OAUTH_PASSPHRASE", "")
    os.makedirs(DATA_DIR, exist_ok=True)
    _state.auth = AuthServer(issuer=PUBLIC_URL, db_path=OAUTH_DB, passphrase=passphrase)
    a = _state.auth
    routes = [
        Route("/", root, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/mcp", mcp_post, methods=["POST"]),
        Route("/mcp", mcp_get, methods=["GET"]),
        Route("/mcp", mcp_delete, methods=["DELETE"]),
        Route("/.well-known/oauth-authorization-server", as_metadata, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server/mcp", as_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", pr_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp", pr_metadata, methods=["GET"]),
        Route("/.well-known/openid-configuration", as_metadata, methods=["GET"]),
        Route("/register", a.register, methods=["POST"]),
        Route("/authorize", a.authorize_get, methods=["GET"]),
        Route("/authorize", a.authorize_post, methods=["POST"]),
        Route("/token", a.token, methods=["POST"]),
        Route("/revoke", a.revoke, methods=["POST"]),
    ]
    middleware = [Middleware(SecurityHeadersMiddleware), Middleware(BearerAuthMiddleware)]

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Starlette):
        try:
            probe_backend()
            logger.info("Chroma backend reachable at %s", backend_address())
        except Exception as exc:  # noqa: BLE001
            logger.error("Chroma backend NOT reachable at %s: %s", backend_address(), exc)
        logger.info("MemPalace remote MCP ready at %s/mcp (%d tools)", PUBLIC_URL, len(_mp.TOOLS))
        yield

    return Starlette(routes=routes, middleware=middleware, lifespan=_lifespan)


app = build_app()
