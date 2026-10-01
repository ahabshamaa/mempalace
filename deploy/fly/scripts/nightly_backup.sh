#!/usr/bin/env bash
# Nightly pull of a full MemPalace export from the Fly.io machine to this Mac.
#   - runs the on-machine logical exporter over `fly ssh console`
#   - downloads the tarball with `fly ssh sftp get`
#   - verifies the tarball's sha256 manifest, keeps the last $KEEP, logs, notifies on failure
# Installed by launchd (com.ahab.mempalace-nightly-backup) at 03:30 local; RunAtLoad + a
# "last success > 20h ago" guard covers nights the Mac was powered off (runs at next login).
set -u
APP="${MEMPALACE_FLY_APP:-mempalace-ahab}"
DEST="$HOME/Backups/mempalace-nightly"
LOG="$HOME/Library/Logs/mempalace-backup.log"
KEEP="${KEEP:-14}"
FLY="${FLY_BIN:-/opt/homebrew/bin/fly}"
STAMP_FILE="$DEST/.last_success"
mkdir -p "$DEST"
ts() { date +%F_%T; }
log() { echo "$(ts) $*" >>"$LOG"; }
notify() { /usr/bin/osascript -e "display notification \"$2\" with title \"MemPalace backup\" subtitle \"$1\"" 2>/dev/null || true; }
fail() { log "FAIL: $*"; notify "FAILED" "$*"; exit 1; }

# Skip if a successful run happened in the last 20h (guards RunAtLoad double-runs).
if [ "${FORCE:-0}" != "1" ] && [ -f "$STAMP_FILE" ]; then
  last=$(cat "$STAMP_FILE" 2>/dev/null || echo 0); now=$(date +%s)
  if [ $((now - last)) -lt 72000 ]; then log "skip: last success $(( (now - last) / 3600 ))h ago"; exit 0; fi
fi

# single-instance lock (launchd RunAtLoad and a manual run must never overlap)
LOCK="$DEST/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -mmin +180 2>/dev/null)" ]; then rmdir "$LOCK" 2>/dev/null && mkdir "$LOCK"; else log "skip: another run holds $LOCK"; exit 0; fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT
[ -x "$FLY" ] || fail "flyctl not found at $FLY"
log "start (app=$APP)"
remote_json=$("$FLY" ssh console -a "$APP" -C "python3 /app/scripts/export_store.py --out /data/export --keep 2" 2>>"$LOG") || fail "remote export failed (see log)"
remote_path=$(printf '%s' "$remote_json" | /usr/bin/python3 -c 'import sys,json; print(json.loads(sys.stdin.read().strip().splitlines()[-1])["export"])' 2>/dev/null) || fail "could not parse remote export result: ${remote_json:0:200}"
name=$(basename "$remote_path")
tmp="$DEST/.$name.part"
"$FLY" ssh sftp get "$remote_path" "$tmp" -a "$APP" >>"$LOG" 2>&1 || fail "sftp download failed for $remote_path"
# verify: tar readable + manifest present + member hashes match
/usr/bin/python3 - "$tmp" <<'PY' >>"$LOG" 2>&1 || fail "verification failed for $name"
import sys, tarfile, json, hashlib, io
p = sys.argv[1]
with tarfile.open(p) as t:
    names = t.getnames()
    man = json.load(t.extractfile("./manifest.json"))
    for member, want in man["members_sha256"].items():
        f = t.extractfile("./" + member)
        h = hashlib.sha256()
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
        if h.hexdigest() != want:
            print("HASH MISMATCH", member); sys.exit(1)
print(f"verified {p}: rows={man['total_rows']} kg={man['kg_tables']} members={len(man['members_sha256'])}")
PY
mv -f "$tmp" "$DEST/$name"
date +%s >"$STAMP_FILE"
# retention: keep newest $KEEP
ls -1t "$DEST"/mempalace-export-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do rm -f "$old"; log "pruned $(basename "$old")"; done
size=$(du -h "$DEST/$name" | cut -f1)
log "OK: $name ($size); $(ls -1 "$DEST"/mempalace-export-*.tar.gz | wc -l | tr -d ' ') kept"
exit 0
