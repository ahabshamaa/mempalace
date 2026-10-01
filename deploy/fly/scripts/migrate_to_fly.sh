#!/usr/bin/env bash
# One-shot migration: upload a mempalace-export-*.tar.gz to the Fly machine and restore it
# into the hosted store, then verify counts + fingerprint against the manifest.
#   scripts/migrate_to_fly.sh ~/Backups/mempalace-2026-10-01/migration/mempalace-export-XXXX.tar.gz
# Refuses to run if the hosted collection already holds rows (restore_from_export exits 3).
set -euo pipefail
EXPORT="$1"; APP="${MEMPALACE_FLY_APP:-mempalace-ahab}"
[ -f "$EXPORT" ] || { echo "no such file: $EXPORT"; exit 1; }
name=$(basename "$EXPORT")
local_sha=$(shasum -a 256 "$EXPORT" | cut -d' ' -f1)
echo "[migrate] uploading $name ($(du -h "$EXPORT" | cut -f1)) to $APP:/data/import/"
fly ssh console -a "$APP" -C "mkdir -p /data/import" >/dev/null
printf 'put %s /data/import/%s\n' "$EXPORT" "$name" | fly ssh sftp shell -a "$APP"
remote_sha=$(fly ssh console -a "$APP" -C "sha256sum /data/import/$name" | awk '{print $1}' | tr -d '\r')
[ "$local_sha" = "$remote_sha" ] || { echo "[migrate] upload corrupted: $local_sha != $remote_sha"; exit 1; }
echo "[migrate] upload verified (sha256 $local_sha)"
echo "[migrate] restoring into hosted store (fresh collection, aux files into /data/mempalace)"
fly ssh console -a "$APP" -C "env MEMPALACE_CHROMA_MODE=http CHROMA_HOST=127.0.0.1 CHROMA_PORT=8801 MEMPALACE_PALACE_PATH=/data/mempalace/palace MEMPALACE_DATA_DIR=/data/mempalace python3 /app/scripts/restore_from_export.py /data/import/$name --port 8801 --data-dir /data/mempalace"
echo "[migrate] removing uploaded tarball from the machine"
fly ssh console -a "$APP" -C "rm -f /data/import/$name"
echo "[migrate] done"
