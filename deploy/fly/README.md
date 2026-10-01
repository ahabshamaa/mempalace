# MemPalace on Fly.io — remote MCP connector

One hosted, always-on, OAuth-protected MemPalace that every Claude surface on Ahab's
account shares: claude.ai web, the iPhone app, Claude Desktop, and Claude Code. The Mac
is a client like the others; its old local Chroma stays installed but disabled as the
rollback path.

```
claude.ai / iPhone / Desktop / Claude Code
        │  HTTPS, Streamable HTTP MCP, Bearer token (OAuth 2.1)
        ▼
https://mempalace-ahab.fly.dev/mcp            Fly.io app  mempalace-ahab  (region fra)
┌──────────────────────────────────────────────────────────────────────────────┐
│ machine: shared-cpu-1x, 2 GB, auto_stop = off (never cold-starts)             │
│  server/app.py   Starlette + uvicorn :8080  — /mcp, /health, OAuth routes     │
│      └─ mempalace.mcp_server.handle_request()  (the fork's 30 tools, unchanged)│
│  chroma run :8801 (loopback only)  — owns the palace files                    │
│  /data (5 GB volume "mempalace_data", encrypted, daily snapshots kept 60 days) │
│      ├─ mempalace/palace/            Chroma (24,244 rows / 23,613 drawers)     │
│      ├─ mempalace/knowledge_graph.sqlite3, wal/, hook_state/, config.json      │
│      ├─ mempalace/oauth.sqlite3      OAuth clients + hashed tokens             │
│      └─ export/                      last 2 nightly logical exports            │
└──────────────────────────────────────────────────────────────────────────────┘
        │  nightly 03:30 (launchd on the Mac)  fly ssh console + sftp get
        ▼
~/Backups/mempalace-nightly/  (last 14 exports, verified by manifest hashes)
```

## Files

| Path | Purpose |
|---|---|
| `server/app.py` | Streamable HTTP transport (`POST /mcp`), bearer check, Origin check, `/health`, metadata routes |
| `server/oauth.py` | Single-user OAuth 2.1 authorization server: RFC 8414/9728 metadata, DCR (RFC 7591), PKCE S256, rotating refresh tokens, revocation |
| `Dockerfile`, `entrypoint.sh`, `fly.toml` | Image (python:3.12-slim, chromadb==1.5.9, MiniLM model baked in), process supervisor, Fly config |
| `scripts/export_store.py` | Full logical export (rows + embeddings + KG + aux) → `mempalace-export-<UTC>.tar.gz` with manifest hashes |
| `scripts/restore_from_export.py` | Restore/verify an export into a fresh Chroma, reproducing the HNSW configuration |
| `scripts/migrate_to_fly.sh` | Upload an export to the machine and restore it into the hosted store |
| `scripts/secrets_scan.py`, `scripts/apply_redactions.py` | gitleaks + regex scan of a drawer export; apply `[REDACTED:<type>]` to a migration copy |
| `scripts/nightly_backup.sh`, `launchd/com.ahab.mempalace-nightly-backup.plist` | Nightly pull of a full export to the Mac |
| `scripts/mint_expired_token.py` | Operator-only helper for the expired-token auth test (never routed over HTTP) |
| `tests/` | Auth gate, every tool round-trip, concurrent writes, migration-count assertion; `run_local.sh` runs them against a scratch copy |

## Fly.io app and machine

- App `mempalace-ahab`, org `personal`, region `fra` (Frankfurt). Fly has no Middle East region; `bom` was retired in 2026-09.
- One machine, `shared-cpu-1x`, 2 GB RAM. Sized from the measured store: 24k × 384-dim vectors (~40 MB) plus HNSW graph and Chroma's Rust frontend (~400–600 MB resident), plus the MiniLM ONNX embedder in the wrapper process (~300 MB). 1 GB would be marginal; 2 GB leaves headroom for growth.
- `auto_stop_machines = "off"`, `auto_start_machines = false`: the machine never sleeps, so no cold starts.
- Volume `mempalace_data`, 5 GB, encrypted, `snapshot_retention = 60` (the maximum Fly offers), scheduled daily snapshots on.
- Health check: `GET /health` every 30 s (verifies the loopback Chroma heartbeat).
- Deploy from the repository root:
  `fly deploy . -c deploy/fly/fly.toml --dockerfile deploy/fly/Dockerfile -a mempalace-ahab --ha=false --remote-only`

### Monthly cost (Fly list prices, fra region, 2026-10)

| Item | USD / month |
|---|---|
| shared-cpu-1x 2 GB machine, always on | ≈ 12.34 |
| 5 GB volume @ $0.15/GB | 0.75 |
| Daily snapshots (incremental, first 10 GB free) | ≈ 0 |
| Egress (a few hundred MB) | < 0.10 |
| **Total** | **≈ $13.20** |

## Auth

- Every call to `/mcp` needs `Authorization: Bearer <access token>`. Tokens in the URL are rejected with 400; missing/invalid/expired/revoked tokens get 401 with `WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"`, which is how claude.ai discovers the authorization server.
- Clients register themselves (DCR). Allowed redirect hosts: `claude.ai`, `claude.com`, `localhost`, `127.0.0.1` (Claude Code and MCP Inspector use loopback callbacks). Anything else is refused at registration.
- `/authorize` shows a passphrase form. The passphrase is the Fly secret `MEMPALACE_OAUTH_PASSPHRASE`; a copy is in the Mac Keychain as item **mempalace-oauth** (`security find-generic-password -s mempalace-oauth -w`). 5 wrong attempts lock the form for 15 minutes.
- Access tokens live 1 hour, refresh tokens 90 days with rotation; reuse of a rotated refresh token revokes the whole family. Tokens are stored only as SHA-256 hashes in `/data/mempalace/oauth.sqlite3`.
- Only `/health` and the OAuth discovery/flow endpoints are reachable without a token. TLS is terminated by Fly (`force_https = true`); the app refuses plain-HTTP forwarded requests.
- Rotate the passphrase: `fly secrets set MEMPALACE_OAUTH_PASSPHRASE=<new> -a mempalace-ahab` (restarts the machine; existing tokens stay valid until they expire). Revoke everything: stop the machine, delete `/data/mempalace/oauth.sqlite3`, start it.

## Snapshots (Fly volume)

- Automatic daily snapshots, retained 60 days. On demand: `fly volumes snapshots create vol_42kj0g3q8zyldo84 -a mempalace-ahab`.
- List: `fly volumes snapshots list vol_42kj0g3q8zyldo84 -a mempalace-ahab`.

## Nightly Mac backup

- `launchd` job `com.ahab.mempalace-nightly-backup` runs `scripts/nightly_backup.sh` at 03:30 local. It runs `export_store.py` on the machine (`fly ssh console`), downloads the tarball (`fly ssh sftp get`), verifies every member against the manifest's SHA-256, keeps the last 14 in `~/Backups/mempalace-nightly/`, logs to `~/Library/Logs/mempalace-backup.log`, and raises a macOS notification on failure.
- Missed nights: launchd fires a missed calendar job at the next wake; `RunAtLoad` plus a "last success > 20 h ago" guard covers nights the Mac was fully off (it then runs at next login).
- Install: `cp launchd/com.ahab.mempalace-nightly-backup.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ahab.mempalace-nightly-backup.plist`. Run now: `FORCE=1 bash scripts/nightly_backup.sh`.

## Restore procedure

**From a Mac nightly export (or the migration export):**
1. Fresh target: either a new Fly volume + machine, or a local scratch Chroma (`chroma run --path <empty dir> --port 8898`).
2. `python scripts/restore_from_export.py <export.tar.gz> --port 8898 --data-dir <dir>`; it re-adds every row with its stored vector (re-embedding only rows that had none), restores the KG/wal/hook_state/config, then verifies row counts and the content fingerprint against the manifest and exits non-zero on mismatch.
3. On Fly: `scripts/migrate_to_fly.sh <export.tar.gz>` does the upload + restore in one go (it refuses to merge into a non-empty collection).

**From a Fly volume snapshot:**
1. `fly volumes snapshots list vol_42kj0g3q8zyldo84 -a mempalace-ahab` → pick a snapshot id.
2. `fly volumes create mempalace_data -a mempalace-ahab -r fra -s 5 --snapshot-id <snap_id> --snapshot-retention 60`.
3. `fly machine stop <machine>`; `fly machine update <machine> --volume <new_vol_id>` (or destroy the machine and `fly deploy` so the new volume is mounted); `fly volumes destroy <old_vol_id>` once verified.
4. Verify: `curl https://mempalace-ahab.fly.dev/health` and run `tests/test_roundtrip.py` with `BASELINE=` pointing at the latest baseline.

## Rollback to local (the Mac becomes the host again)

The local Chroma is still installed, just disabled:
1. `launchctl enable gui/$(id -u)/com.ahab.chroma-server && launchctl kickstart gui/$(id -u)/com.ahab.chroma-server` (plist: `~/Library/LaunchAgents/com.ahab.chroma-server.plist`, data: `~/.mempalace/palace`).
2. Bring the local store up to date from the newest nightly export: stop the launchd job again, move `~/.mempalace/palace` aside, start Chroma on an empty dir, `restore_from_export.py <latest export> --port 8801 --data-dir ~/.mempalace`.
3. Repoint clients: Claude Desktop → restore the `mempalace` stdio entry in `~/Library/Application Support/Claude/claude_desktop_config.json` (backup copy in `~/Backups/mempalace-2026-10-01/`); Claude Code → `claude mcp remove mempalace -s user` so the plugin's stdio server is the only one again; claude.ai → remove the custom connector.

## Adding a new device

Nothing to install. On the new device sign in to the same Claude account; a connector added once in claude.ai (Settings → Connectors) appears on web, Desktop, mobile and Claude Code automatically. Connecting asks for the passphrase (Keychain item **mempalace-oauth**). For a stand-alone Claude Code install: `claude mcp add --transport http --scope user mempalace https://mempalace-ahab.fly.dev/mcp`, then `/mcp` → sign in.

## Operations cheat-sheet

```
fly status -a mempalace-ahab                 # machine state
fly logs -a mempalace-ahab                   # live logs (uvicorn + chroma)
fly ssh console -a mempalace-ahab            # shell on the machine (/app, /data)
fly secrets list -a mempalace-ahab           # digests only
curl -s https://mempalace-ahab.fly.dev/health
```
