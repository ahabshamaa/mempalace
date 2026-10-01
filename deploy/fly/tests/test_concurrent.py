"""Two (or more) clients writing at once must not lose or corrupt writes."""
from __future__ import annotations

import json
import re
import threading
import uuid

import httpx

from oauth_client import MCP, get_token

WING = "connector_tests"


def _writer(token: str, tag: str, n: int, out: list, errors: list):
    with httpx.Client(timeout=240) as c:
        m = MCP(token, c)
        m.initialize()
        for i in range(n):
            try:
                t = m.tool_text("mempalace_add_drawer", {"wing": WING, "room": "concurrent", "content": f"concurrent write {tag} #{i} {uuid.uuid4().hex}"})
                try:
                    did = json.loads(t).get("drawer_id")
                except Exception:
                    did = None
                if not did:
                    errors.append(t[:200]); continue
                out.append(did)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))


def test_two_clients_write_concurrently():
    tok_a = get_token()["access_token"]
    tok_b = get_token()["access_token"]
    ids_a, ids_b, errs = [], [], []
    n = 8
    ta = threading.Thread(target=_writer, args=(tok_a, "A", n, ids_a, errs))
    tb = threading.Thread(target=_writer, args=(tok_b, "B", n, ids_b, errs))
    ta.start(); tb.start(); ta.join(); tb.join()
    assert not errs, errs
    assert len(ids_a) == n and len(ids_b) == n
    assert len(set(ids_a + ids_b)) == 2 * n, "duplicate drawer ids across clients"
    with httpx.Client(timeout=240) as c:
        m = MCP(get_token(c)["access_token"], c); m.initialize()
        for did in ids_a + ids_b:
            assert "concurrent write" in m.tool_text("mempalace_get_drawer", {"drawer_id": did}), did
        for did in ids_a + ids_b:
            m.tool("mempalace_delete_drawer", {"drawer_id": did})
