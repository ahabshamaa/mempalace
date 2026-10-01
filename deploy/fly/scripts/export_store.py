#!/usr/bin/env python3
"""Full logical export of a MemPalace store (runs where Chroma is reachable).

Produces <outdir>/mempalace-export-<UTC timestamp>.tar.gz containing:
  drawers.jsonl             one {"id","document","metadata","embedding"} per row (all collections)
  collections.json          collection names, metadata, row counts
  knowledge_graph.sqlite3   consistent copy via sqlite3 backup API
  oauth.sqlite3             (if present) consistent copy — keeps clients logged in after a restore
  wal/, hook_state/, config.json, mempalace.yaml   copied verbatim
  manifest.json             counts + sha256 of every member + content fingerprint
Usage: export_store.py --host 127.0.0.1 --port 8801 --data-dir ~/.mempalace --out /data/export
Exit code 0 only if the export verifies (row counts re-read match).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile

import chromadb

ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.environ.get("CHROMA_HOST", "127.0.0.1"))
ap.add_argument("--port", type=int, default=int(os.environ.get("CHROMA_PORT", "8801")))
ap.add_argument("--data-dir", default=os.environ.get("MEMPALACE_DATA_DIR", os.path.expanduser("~/.mempalace")))
ap.add_argument("--out", required=True)
ap.add_argument("--keep", type=int, default=3, help="keep this many exports in --out")
a = ap.parse_args()

os.makedirs(a.out, exist_ok=True)
stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
work = tempfile.mkdtemp(prefix="mpexp-")
client = chromadb.HttpClient(host=a.host, port=a.port)

collections = []
fingerprint = hashlib.sha256()
total = 0
with open(os.path.join(work, "drawers.jsonl"), "w") as f:
    for col in client.list_collections():
        name = col.name if hasattr(col, "name") else col
        c = client.get_collection(name)
        n = c.count()
        cfg = getattr(c, "configuration_json", None) or getattr(getattr(c, "_model", None), "configuration_json", None)
        collections.append({"name": name, "metadata": c.metadata, "rows": n, "configuration": cfg})
        got = 0
        offset = 0
        page = 2000
        rows = []
        missing_emb = 0
        while offset < n:
            try:
                g = c.get(include=["documents", "metadatas", "embeddings"], limit=page, offset=offset)
                embs = g["embeddings"]
            except Exception:
                # Chroma raises "Error finding id" when a row has no vector in the HNSW
                # segment. Fall back to per-row fetch; such rows are exported with
                # embedding=None and re-embedded on restore.
                g = c.get(include=["documents", "metadatas"], limit=page, offset=offset)
                embs = []
                for rid in g["ids"]:
                    try:
                        embs.append(c.get(ids=[rid], include=["embeddings"])["embeddings"][0])
                    except Exception:
                        embs.append(None); missing_emb += 1
            for i, d, m, e in zip(g["ids"], g["documents"], g["metadatas"], embs):
                rows.append((i, d, m, [float(x) for x in e] if e is not None else None))
            got += len(g["ids"])
            offset += page
        collections[-1]["embedding_missing"] = missing_emb
        if got != n:
            print(f"ERROR: {name}: read {got} rows, expected {n}", file=sys.stderr)
            sys.exit(2)
        rows.sort(key=lambda r: r[0])
        for i, d, m, e in rows:
            f.write(json.dumps({"collection": name, "id": i, "document": d, "metadata": m, "embedding": e}, ensure_ascii=False) + "\n")
            fingerprint.update(i.encode()); fingerprint.update(b"\0"); fingerprint.update((d or "").encode()); fingerprint.update(b"\n")
        total += n
json.dump(collections, open(os.path.join(work, "collections.json"), "w"), indent=1)

for db in ("knowledge_graph.sqlite3", "oauth.sqlite3"):
    src = os.path.join(a.data_dir, db)
    if os.path.exists(src):
        s = sqlite3.connect(src); d = sqlite3.connect(os.path.join(work, db))
        s.backup(d); d.close(); s.close()
for item in ("wal", "hook_state", "config.json", "mempalace.yaml"):
    src = os.path.join(a.data_dir, item)
    if os.path.isdir(src):
        shutil.copytree(src, os.path.join(work, item))
    elif os.path.isfile(src):
        shutil.copy2(src, os.path.join(work, item))

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

members = {}
for root, _, files in os.walk(work):
    for fn in files:
        p = os.path.join(root, fn)
        members[os.path.relpath(p, work)] = sha(p)
kg = {}
kgp = os.path.join(work, "knowledge_graph.sqlite3")
if os.path.exists(kgp):
    db = sqlite3.connect(kgp)
    for (t,) in db.execute("select name from sqlite_master where type='table'"):
        kg[t] = db.execute(f'select count(*) from "{t}"').fetchone()[0]
manifest = {"exported_at": stamp, "total_rows": total, "collections": collections, "kg_tables": kg,
            "all_docs_sha256": fingerprint.hexdigest(), "members_sha256": members}
json.dump(manifest, open(os.path.join(work, "manifest.json"), "w"), indent=1)

out_path = os.path.join(a.out, f"mempalace-export-{stamp}.tar.gz")
tmp_path = out_path + ".part"
with tarfile.open(tmp_path, "w:gz", compresslevel=1) as tar:
    tar.add(work, arcname=".")
os.replace(tmp_path, out_path)
shutil.rmtree(work, ignore_errors=True)

# retention inside --out
exports = sorted(p for p in os.listdir(a.out) if p.startswith("mempalace-export-") and p.endswith(".tar.gz"))
for old in exports[:-a.keep]:
    os.remove(os.path.join(a.out, old))

print(json.dumps({"export": out_path, "size_bytes": os.path.getsize(out_path), "total_rows": total,
                  "kg_tables": kg, "all_docs_sha256": manifest["all_docs_sha256"]}))
