#!/usr/bin/env bash
# MemPalace on Fly.io — single-machine entrypoint.
#   1. point ~/.mempalace at the persistent volume (/data)
#   2. start the standalone Chroma server on loopback :8801 (owns the palace files)
#   3. start the Streamable HTTP + OAuth front door on :8080
# If either process dies the container exits non-zero and Fly restarts the machine.
set -euo pipefail

DATA=/data/mempalace
mkdir -p "$DATA/palace" "$DATA/wal" "$DATA/hook_state" "$DATA/locks"
if [ ! -L "$HOME/.mempalace" ]; then
  rm -rf "$HOME/.mempalace"
  ln -s "$DATA" "$HOME/.mempalace"
fi
if [ ! -f "$DATA/config.json" ]; then
  cat > "$DATA/config.json" <<JSON
{"palace_path": "$DATA/palace", "collection_name": "mempalace_drawers", "embedding_model": "minilm", "embedding_device": "cpu"}
JSON
fi

export MEMPALACE_PALACE_PATH="$DATA/palace"
export MEMPALACE_DATA_DIR="$DATA"
export MEMPALACE_CHROMA_MODE=http
export CHROMA_HOST=127.0.0.1
export CHROMA_PORT=8801
export MEMPALACE_MCP_IDLE_HOURS=0        # never self-exit
export MEMPALACE_EMBEDDING_DEVICE=cpu
export MEMPALACE_LOG_FILE="$DATA/mcp_server.log"
export ANONYMIZED_TELEMETRY=False
export CHROMA_SERVER_NOFILE=65535

echo "[entrypoint] starting chroma (palace=$MEMPALACE_PALACE_PATH)"
chroma run --path "$MEMPALACE_PALACE_PATH" --host 127.0.0.1 --port 8801 &
CHROMA_PID=$!

for i in $(seq 1 60); do
  if curl -fsS -m 2 http://127.0.0.1:8801/api/v2/heartbeat >/dev/null 2>&1; then
    echo "[entrypoint] chroma up after ${i}s"; break
  fi
  if ! kill -0 "$CHROMA_PID" 2>/dev/null; then echo "[entrypoint] chroma died during startup"; exit 1; fi
  sleep 1
done

echo "[entrypoint] starting MCP front door on :8080"
uvicorn server.app:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips='*' \
  --log-level info --timeout-keep-alive 75 --no-server-header &
APP_PID=$!

trap 'kill -TERM $APP_PID $CHROMA_PID 2>/dev/null; wait' TERM INT
wait -n $CHROMA_PID $APP_PID
rc=$?
echo "[entrypoint] a child exited (rc=$rc); shutting down"
kill -TERM $APP_PID $CHROMA_PID 2>/dev/null || true
wait || true
exit 1
