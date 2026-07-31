# Chroma client/server topology (fork, Block 5)

> Fork-specific document (`ahabshamaa/mempalace`, branch `block5-http-client`).
> Upstream MemPalace runs ChromaDB embedded; this fork migrates it to a
> client/server split. If you are reading this on a machine that is not the
> one described below, adapt paths and the launchd label accordingly.

## Why

Embedded `chromadb.PersistentClient` made every MemPalace process (MCP
server, hook CLI, miner) a writer on the same sqlite file. The second
process blocked on the file lock, and because nothing bounded the wait,
palace calls hung the MCP bridge indefinitely. Block 5 replaces that with
one server process that exclusively owns the palace directory; everything
else talks to it over localhost HTTP with hard timeouts.

## Topology

- **One server**: a launchd agent (`com.ahab.chroma-server`, plist at
  `~/Library/LaunchAgents/com.ahab.chroma-server.plist`, `RunAtLoad` +
  `KeepAlive`) runs `chroma run --path ~/.mempalace/palace --host 127.0.0.1
  --port 8801`. It is the **only** process that opens the files under
  `~/.mempalace/palace`.
- **Thin clients**: every MemPalace process constructs its Chroma client
  through [`mempalace/palace_client.py`](../mempalace/palace_client.py) —
  the single construction point. In HTTP mode nothing outside that module
  may build a Chroma client; that rule is what prevents a second embedded
  writer from re-appearing.
- The client returned by `make_http_client()` is wrapped in a
  `TimeoutProxy` that also wraps every collection obtained from it, so the
  whole call graph is timeout-guarded without call-site changes.

## Config surface

| Variable | Default | Meaning |
| --- | --- | --- |
| `CHROMA_HOST` | `127.0.0.1` | Server address the clients connect to. |
| `CHROMA_PORT` | `8801` | Server port. |
| `MEMPALACE_OP_TIMEOUT` | `15` (seconds) | Hard per-operation ceiling on every wire call. |
| `MEMPALACE_CHROMA_MODE` | `http` | `embedded` is a default-closed escape hatch — see below. |

`MEMPALACE_CHROMA_MODE=embedded` exists for exactly two consumers: the
test suite (temp-dir palaces, forced in `tests/conftest.py`) and offline
repair tooling. It must **never** be used against the live palace while
the server is running — two writers on the same sqlite/HNSW files is the
corruption scenario this whole topology exists to remove. Stop the server
(`launchctl bootout`) first if you must open the palace embedded.

## Error contract

Palace failures are **structured errors, never hangs**. Future sessions
diagnosing "palace is broken": if a call blocks forever, that is a bug in
this contract, not expected behavior. The two shapes:

- `PalaceBackendUnreachableError` — the server did not answer. Raised by
  the construction-time health probe (`GET /api/v2/heartbeat`, 3 s
  timeout, error names `host:port`) **and** by in-flight calls whose
  transport dies mid-request (`httpx.TransportError` / builtin
  `ConnectionError` are translated into this same shape), so consumers see
  one error whether the server died before or during the call.
- `PalaceOperationTimeoutError` — a single operation exceeded the
  `MEMPALACE_OP_TIMEOUT` ceiling. The error names the operation (e.g.
  `collection[memories].query`) and the backend address.

The MCP server catches both, caches nothing, and returns a structured tool
error with a hint (heartbeat curl + launchd respawn note). Errors are not
retried internally — the probe/timeout already bounded the wait; a retry
would just double it.

## Operations runbook

**Heartbeat** (is the server up?):

```bash
curl http://127.0.0.1:8801/api/v2/heartbeat
# → {"nanosecond heartbeat": ...}
```

**Reload / restart the server**:

```bash
launchctl bootout gui/$UID/com.ahab.chroma-server
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.ahab.chroma-server.plist
launchctl print gui/$UID/com.ahab.chroma-server | grep state   # → running
```

**Logs**: `~/Library/Logs/chroma-server.log` (stdout) and
`~/Library/Logs/chroma-server.err` (stderr).

**Respawn behavior**: `KeepAlive=true` — launchd restarts the server
within seconds if it crashes or is killed. Clients need no action: the MCP
server health-probes on construction and returns structured errors until
the backend is back, then reconnects on the next call.

**Stray / second mcp_server instance** (incident 2026-07-30: a stray
foreground `mempalace-mcp` contending with Claude Desktop's instance took
chat-side MemPalace down until the stray exited):

```bash
ps aux | grep -E "mempalace|mcp_server" | grep -v grep
```

Expected steady state: one `chroma run` (the only file owner), plus one
thin-client `mcp_server` per host — Claude Desktop's for the app
lifetime and one per live Claude Code session. Anything launched by hand
in a terminal is a stray; kill it. Since the instance guard
([`mempalace/instance_guard.py`](../mempalace/instance_guard.py)), every
server registers a PID lockfile under
`~/.mempalace/locks/mcp_instances_<key>/` at startup: in embedded mode a
second instance refuses to start with an error naming the holding PID;
in HTTP mode peers are supported and only logged (check the MCP server
log for "Another mempalace-mcp instance is serving palace"). Lockfiles
of dead holders are reaped automatically via process-liveness check, so
a crashed server never blocks the next one.

**Backup / restore**: back up with the server stopped (or accept a
crash-consistent copy at your own risk):

```bash
launchctl bootout gui/$UID/com.ahab.chroma-server
tar -czf ~/mempalace-backup-$(date +%Y%m%d-%H%M).tar.gz -C ~/.mempalace palace
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.ahab.chroma-server.plist
```

Restore is the reverse: bootout, untar over `~/.mempalace/palace`,
bootstrap. Current known-good backup: `~/mempalace-backup-20260713-1701.tar.gz`
(taken 2026-07-13, pre-migration).

**Vacuum** (chromadb 1.5.9): stop all writers first, take a backup, then:

```bash
launchctl bootout gui/$UID/com.ahab.chroma-server
chroma vacuum --path ~/.mempalace/palace
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.ahab.chroma-server.plist
```

## Upgrade procedure

The installed CLI/MCP server comes from **this fork via pipx**, not from
PyPI. To pick up upstream changes:

```bash
cd ~/dev/mempalace
git fetch upstream
git rebase upstream/main block5-http-client   # or merge, resolve conflicts
pipx install --force ~/dev/mempalace          # reinstall from the fork
```

Rules:

- **Never** `pipx upgrade mempalace` — that pulls the upstream PyPI
  package and silently drops every Block 5 change.
- **Never** hand-patch files in the pipx venv's `site-packages`. All
  changes go through the fork branch and a reinstall, so the installed
  code is always attributable to a commit.

## Embedded-only paths gated off in HTTP mode

These five embedded-era mechanisms are explicitly skipped when
`MEMPALACE_CHROMA_MODE` is `http` (the default). Each is correct for a
process that owns the palace files and wrong for a thin HTTP client:

1. **Pre-open repair pass** (`ChromaBackend.make_client`) — quarantining
   HNSW segments client-side against files a live server owns is a
   corruption risk; the server does its own segment management.
2. **Inode-keyed client cache** (`ChromaBackend._client`) — cache is keyed
   on the server address instead; file identity is meaningless when this
   process never opens the files.
3. **mtime-based reconnect** (`mcp_server._get_client`) — the server bumps
   `chroma.sqlite3`'s mtime on every write, so the embedded freshness check
   would force a client rebuild on every call; the client is cached for the
   process lifetime instead.
4. **HNSW capacity probe** (`mcp_server._refresh_vector_disabled_flag`) —
   guards an embedded-only segfault (#1222) that cannot happen in a process
   that never loads HNSW segments; left on it would wrongly route search to
   the BM25 fallback.
5. **`_write_lock` flock** (`ChromaCollection._write_lock`) — the
   `mine_palace_lock` flock guarded embedded ChromaDB's multi-threaded HNSW
   corruption (#974/#965); over HTTP the server serializes writes, and the
   non-blocking flock turned legitimate concurrent writers into hard
   `MineAlreadyRunning` errors.
