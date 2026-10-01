#!/usr/bin/env bash
# Local integration run: scratch Chroma (:8899) + wrapper (:8080) on a COPY of a palace, then pytest.
#   tests/run_local.sh /path/to/backup/mempalace   (dir containing palace/, knowledge_graph.sqlite3, config.json)
# Never touches the live ~/.mempalace: HOME is pointed at a throwaway dir for the server processes.
set -euo pipefail
SRC="$1"; HERE="$(cd "$(dirname "$0")/.." && pwd)"; PY="$HERE/.venv/bin/python"
SCRATCH="${SCRATCH_ROOT:-$HERE/.local-run}/$(date +%s)"; HOMEDIR="$SCRATCH/home"; MP="$HOMEDIR/.mempalace"
mkdir -p "$MP" "$HOMEDIR/.cache"
cp -R "$SRC/palace" "$MP/palace"
[ -f "$SRC/knowledge_graph.sqlite3" ] && cp "$SRC/knowledge_graph.sqlite3" "$MP/"
[ -d "$SRC/wal" ] && cp -R "$SRC/wal" "$MP/wal"
[ -d "$SRC/hook_state" ] && cp -R "$SRC/hook_state" "$MP/hook_state"
$PY - "$SRC/config.json" "$MP/config.json" "$MP/palace" <<'PY'
import json, sys
c = json.load(open(sys.argv[1])); c["palace_path"] = sys.argv[3]; c["embedding_model"] = "minilm"; c["embedding_device"] = "cpu"
json.dump(c, open(sys.argv[2], "w"), indent=1)
PY
# reuse the already-downloaded MiniLM ONNX model instead of fetching it again
REAL_HOME="$HOME"
[ -d "$REAL_HOME/.cache/chroma" ] && cp -R "$REAL_HOME/.cache/chroma" "$HOMEDIR/.cache/chroma"

export HOME="$HOMEDIR" MEMPALACE_DATA_DIR="$MP" MEMPALACE_PALACE_PATH="$MP/palace" MEMPALACE_CHROMA_MODE=http CHROMA_HOST=127.0.0.1 CHROMA_PORT=8899
export MEMPALACE_PUBLIC_URL="${BASE_URL:-http://127.0.0.1:8080}" MEMPALACE_OAUTH_PASSPHRASE="${MEMPALACE_OAUTH_PASSPHRASE:-local-test-passphrase-123}"
export MEMPALACE_MCP_IDLE_HOURS=0 ANONYMIZED_TELEMETRY=False

"$HERE/.venv/bin/chroma" run --path "$MP/palace" --host 127.0.0.1 --port 8899 >"$SCRATCH/chroma.log" 2>&1 &
CH=$!
for _ in $(seq 1 60); do curl -fsS -m 2 http://127.0.0.1:8899/api/v2/heartbeat >/dev/null 2>&1 && break; sleep 1; done
(cd "$HERE" && "$HERE/.venv/bin/uvicorn" server.app:app --host 127.0.0.1 --port 8080 --log-level info >"$SCRATCH/app.log" 2>&1) &
AP=$!
for _ in $(seq 1 90); do curl -fsS -m 2 http://127.0.0.1:8080/health >/dev/null 2>&1 && break; sleep 1; done
echo "chroma pid $CH, app pid $AP, scratch $SCRATCH"
if ! curl -fsS -m 3 http://127.0.0.1:8080/health; then
  echo "wrapper did not come up; tail of app.log:"; tail -20 "$SCRATCH/app.log"
  kill -TERM "$AP" "$CH" 2>/dev/null || true; wait 2>/dev/null || true; exit 1
fi
echo

EXPIRED_TOKEN="$(MEMPALACE_APP_DIR="$HERE" $PY "$HERE/scripts/mint_expired_token.py")"; export EXPIRED_TOKEN
export BASE_URL="${BASE_URL:-http://127.0.0.1:8080}" BASELINE="${BASELINE:-}"
rc=0
# migration-count assertion must run BEFORE anything writes to the store
if [ -n "$BASELINE" ]; then
  (cd "$HERE/tests" && "$HERE/.venv/bin/python" -m pytest -q --tb=short test_roundtrip.py::test_migration_counts_match_baseline) || rc=$?
fi
(cd "$HERE/tests" && "$HERE/.venv/bin/python" -m pytest -q --tb=short --deselect test_roundtrip.py::test_migration_counts_match_baseline "${@:2}" .) || rc=$?

# graceful stop of the two scratch servers started above
kill -TERM "$AP" "$CH" 2>/dev/null || true
wait 2>/dev/null || true
echo "logs: $SCRATCH/app.log $SCRATCH/chroma.log"
exit $rc
