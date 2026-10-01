# MemPalace Roadmap

## v3.1.1 — Stability Patch

Bug fixes and hardening merged to `develop`, releasing soon.

**Merged:**
- Security hardening: input validation, KG threading locks, WAL permission fixes (#647)
- MCP tools: drawer CRUD, paginated export, hook settings (#667)
- Backend storage seam: ChromaDB abstraction layer enabling swappable backends (#413)
- MCP ping health check for AnythingLLM compatibility (#600)
- Windows reparse point crash fix (#558)
- `mempalace compress` KeyError crash fix (#569)
- Token count estimate fix (#609)
- Mtime float precision fix preventing unnecessary re-mines (#610)

**In review (merging this week):**
- Auto-repair BLOB seq_ids from chromadb 0.6→1.5 migration (#664)
- Graph cache with write-invalidation (#661)
- L1 importance pre-filter for large palaces (#660)
- Windows Chinese/Unicode encoding fix (#631)
- HNSW index bloat prevention — 441GB→433KB on large palaces (#346, pending rebase)
- ~25 additional small bug fixes and platform compatibility patches

## v4.0.0-alpha — Next Generation (this week)

The v4 alpha introduces three major capabilities: pluggable storage backends, local NLP processing, and improved retrieval quality.

### Swappable Storage

ChromaDB remains the default, but v4 introduces a backend abstraction (shipped in #413) that enables drop-in replacements:

- **PostgreSQL backend** with pg_sorted_heap support (#665) — for production deployments needing ACID guarantees, concurrent access, and standard backup/restore
- **LanceDB backend** (#574) — for local-first deployments wanting multi-device sync without a database server
- **PalaceStore** (#643) — bespoke storage layer purpose-built for MemPalace's access patterns (draft, evaluating)

Users choose their backend at init time. Existing ChromaDB palaces continue to work unchanged.

### Local NLP

On-device natural language processing via local models (#507):

- Entity extraction, relationship detection, and topic classification without external API calls
- Feature-flagged and optional — falls back to existing heuristic extractors
- Runs on consumer hardware (no GPU required, GPU-accelerated when available)

### Improved Retrieval

- **Hybrid search**: keyword text-match fallback when vector similarity misses exact terms (#662)
- **Stale index detection**: automatic reconnection when the HNSW index changes on disk (#663)
- **Time-decay scoring**: recent memories surface before older ones (#337)
- **Query sanitization**: system prompt contamination mitigation already shipped in v3.1 (#385)

### What's Not in v4 Alpha

These are under consideration for v4 stable or later:

- Synapse advanced retrieval — MMR, pinned memory, query expansion (#596)
- Multi-device sync (#575) — depends on LanceDB backend
- Multilingual embedding support (#488, #442)
- Qdrant vector search backend (#381)

## Block 5 — Fly.io HTTP Connector (In Progress)

Parallel deployment infrastructure track enabling remote MCP server deployment on Fly.io. Complements v3.1.1 and v4.0 core development.

**Shipped (2026-10-01):**
- Deployment verification record (auth gate, migration drills, backup automation)
- Snapshot restore drill script (automated validation baked into image)
- Nightly backup single-instance locking (prevents concurrent export corruption)
- Full Fly.io connector README (migration script, restore procedures, runbook)
- Infrastructure setup (app name, region, build context)

**In Progress:**
- Operationalization & validation (Phase 2)
- Staging deployment & E2E testing
- Production hardening (failover, secrets rotation, monitoring)

**Known Blockers:**
- Fly.io account setup (paused at billing/org registration)
- OAuth 2.1 credentials not yet obtained

**Next Phase:** Complete account onboarding, deploy staging, run full E2E verification cycle.

## Branch Model

```
main                    ← tagged production releases
develop                 ← active development (PRs merge here)
block5-http-client      ← Fly.io HTTP connector fork (parallel to main)
release/3.1             ← hotfixes for current stable (v3.1.x)
release/3.0             ← hotfixes for prior stable
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines. PRs should target `develop`. We review all contributions for correctness, security, and compatibility before merging.
