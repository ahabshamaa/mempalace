# Handoff: Fly.io Deployment — 2026-10-01

**Status**: Scaffolding complete. Infrastructure layer ready. Paused at Fly account onboarding.

## 1. Session Summary

Initial handoff for Fly.io deployment of MemPalace remote HTTP connector. This session created Brain project scaffolding (MOC, project context, journal entry) and this handoff document. No code changes in this session — ceremonial project initialization.

Prior work (commits 913a870, 7ccd939, etc.) completed:
- FastAPI app with OAuth 2.1 auth gate
- Streamable HTTP transport for MCP server
- Instance guard (PID lockfile registry) for concurrent access
- Export/restore operations
- Secrets scanning and nightly backup automation
- Chunked drawer reassembly fixes (get/delete/update)
- Run-local.sh teardown control (exec uvicorn)

## 2. What Shipped

- **Main feature**: Fly.io remote MCP connector (feat commit 913a870)
  - FastAPI server with OAuth 2.1 front door
  - Streamable HTTP responses for long-running MCP operations
  - Export/restore workflows
  - Nightly backup via launchd
  - Secrets scanning and validation
  
- **Reliability fixes**:
  - Chunked drawer operation fixes (7ccd939, 48b868d, 787f279)
  - Instance guard PID locking for safe concurrent access
  - Teardown control improvements (96e057f)
  - Transport layer hardening (Block 5 Chroma HTTP migration)

## 3. Architecture Decisions

1. **FastAPI + Uvicorn**: Chosen for async/streaming support and MCP integration simplicity
2. **OAuth 2.1 gate**: Anthropic identity as auth provider (BYOK-style, user configures)
3. **Nightly backup via launchd**: macOS-native scheduling for resilience (not external cron)
4. **Instance guard (PID lockfiles)**: Per-palace locking to prevent concurrent writes to the same palace
5. **Export/restore as primary backup**: Preserve palace data across container restarts or migration

## 4. Files of Note

### Core Application
- `server/app.py` — FastAPI main app, MCP tool dispatch, streaming response wrapping
- `server/oauth.py` — OAuth 2.1 client/token validation, identity middleware
- `fly.toml` — Fly.io app configuration (regions, env vars, health probe)
- `Dockerfile` — Multi-stage build, uvicorn entrypoint

### Deployment & Automation
- `entrypoint.sh` — Container startup (env validation, server start)
- `scripts/nightly_backup.sh` — Export palace to timestamped tar.gz
- `scripts/secrets_scan.py` — Validate no plaintext secrets in palace data
- `scripts/export_store.py` — Export palace data (all drawers + metadata)
- `scripts/restore_from_export.py` — Restore from tar.gz backup
- `scripts/mint_expired_token.py` — OAuth token refresh utility
- `launchd/com.ahab.mempalace-nightly-backup.plist` — Scheduled backup daemon config

### Testing
- `tests/test_auth_gate.py` — OAuth flow validation
- `tests/test_roundtrip.py` — Export→restore integrity check
- `tests/test_concurrent.py` — Instance guard locking under load
- `tests/run_local.sh` — Local dev harness (starts Chroma, uvicorn, teardown)

### Configuration
- `.gitignore` — secrets, venv, pytest cache, .venv
- `fly.toml` — Fly.io deployment config

## 5. What's Next

### Phase 1: Account & Deployment (Next)
1. Set up Fly.io account (billing, org, credentials)
2. Deploy staging app (fly deploy)
3. Configure OAuth redirect URI on Anthropic identity
4. Validate health probe responses

### Phase 2: E2E Validation
1. Run full test suite against staging deployment
2. Verify export/restore workflows
3. Test concurrent access safety
4. Nightly backup automation (launchd on macOS or equivalent cron)

### Phase 3: Production Hardening
1. Database failover (backup Chroma instance or multi-replica ChromaDB)
2. TLS certificate management (Fly.io auto-renews via Let's Encrypt)
3. Secrets rotation policy (OAuth tokens, palace encryption keys)
4. Monitoring & alerting setup (error rates, latency, export success/failure)

### Phase 4: Client Integration
1. Test Claude Code MCP server connection to Fly.io endpoint
2. MemPalace drawer sync workflows (pull latest from deployed server)
3. CLI client support for remote palace operations

### Known Blockers
- Fly account setup incomplete
- OAuth redirect URI not yet registered with Anthropic identity
- No staging deployment yet

### Test Coverage
- `test_auth_gate.py` — OAuth flow (token validation, expiry handling)
- `test_roundtrip.py` — Data integrity (export → restore → verify content)
- `test_concurrent.py` — Concurrent drawer ops (instance guard locking)
- Run-local tests pass ✓

---

**Next phase number**: Phase 1 (Account & Deployment)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
