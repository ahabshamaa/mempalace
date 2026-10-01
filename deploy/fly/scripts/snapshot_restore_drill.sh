#!/usr/bin/env bash
# Restore drill from a Fly volume snapshot: new volume from the snapshot, temporary machine
# (same image, no public services) mounting it, verify counts + sample hashes inside, then
# destroy the temporary machine and volume. Nothing touches the production machine/volume.
#   scripts/snapshot_restore_drill.sh <snapshot_id> <expected_baseline.json> [sample_ids.json]
set -euo pipefail
SNAP="$1"; EXPECT="$2"; SAMPLES="${3:-}"; APP="${MEMPALACE_FLY_APP:-mempalace-ahab}"; REGION="${MEMPALACE_FLY_REGION:-fra}"
IMAGE=$(fly status -a "$APP" --json | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["Machines"][0]["image_ref"]["registry"]+"/"+d["Machines"][0]["image_ref"]["repository"]+":"+d["Machines"][0]["image_ref"]["tag"])')
echo "[drill] image $IMAGE"
VOL=$(fly volumes create mempalace_drill -a "$APP" -r "$REGION" -s 5 --snapshot-id "$SNAP" --snapshot-retention 1 -y --json | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
echo "[drill] volume $VOL created from $SNAP"
cleanup() { echo "[drill] cleanup"; [ -n "${MID:-}" ] && fly machine destroy "$MID" -a "$APP" --force >/dev/null 2>&1 || true; sleep 3; fly volumes destroy "$VOL" -a "$APP" -y >/dev/null 2>&1 || true; }
trap cleanup EXIT
MID=$(fly machine run "$IMAGE" -a "$APP" -r "$REGION" --name mempalace-drill --volume "$VOL:/data" --vm-memory 2048 --vm-cpu-kind shared --vm-cpus 1 --autostop=off --json 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
echo "[drill] machine $MID started; waiting for chroma"
for _ in $(seq 1 40); do
  if fly ssh console -a "$APP" --machine "$MID" -C "curl -fsS -m 2 http://127.0.0.1:8801/api/v2/heartbeat" >/dev/null 2>&1; then break; fi; sleep 3
done
echo "[drill] verifying inside the drill machine"
VERIFY="/app/scripts/verify_store.py"; fly ssh console -a "$APP" --machine "$MID" -C "test -f $VERIFY" >/dev/null 2>&1 || VERIFY="/data/import/baseline.py"
SAMPLE_ARG=""; [ -n "$SAMPLES" ] && SAMPLE_ARG="/data/import/sample_ids.json"
fly ssh console -a "$APP" --machine "$MID" -C "sh -c 'MEMPALACE_CHROMA_MODE=http CHROMA_PORT=8801 python3 $VERIFY 127.0.0.1 8801 /data/mempalace/knowledge_graph.sqlite3 /data/mempalace /tmp/drill-baseline.json $SAMPLE_ARG >/dev/null 2>&1; cat /tmp/drill-baseline.json'" 2>/dev/null | grep -v Connecting > /tmp/drill-baseline.json
python3 - "$EXPECT" /tmp/drill-baseline.json <<'PY'
import json, sys
a = json.load(open(sys.argv[1])); b = json.load(open(sys.argv[2]))
ca = a["collections"]["mempalace_drawers"]; cb = b["collections"]["mempalace_drawers"]
ok = {"rows": ca["rows"] == cb["rows"], "logical_drawers": ca["logical_drawers"] == cb["logical_drawers"],
      "fingerprint": ca["all_docs_sha256"] == cb["all_docs_sha256"], "kg": a["kg_tables"] == b["kg_tables"],
      "samples": [x["doc_sha256"] == y["doc_sha256"] for x, y in zip(ca["sample"], cb["sample"])]}
print("[drill] RESULT", json.dumps(ok))
sys.exit(0 if all(v is True or (isinstance(v, list) and all(v)) for v in ok.values()) else 1)
PY
