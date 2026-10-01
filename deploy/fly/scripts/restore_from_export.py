#!/usr/bin/env python3
"""Restore (or verify) a mempalace-export-*.tar.gz into a Chroma server.

  restore_from_export.py EXPORT.tar.gz --host 127.0.0.1 --port 8899 [--data-dir DIR] [--verify-only]

Re-adds every row with its stored embedding (no re-embedding, so document hashes
are byte-identical) and restores knowledge_graph.sqlite3 / wal / hook_state /
config into --data-dir when given. Prints a comparison against manifest.json and
exits non-zero on any mismatch. Used by the restore drill and the rollback path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile

import chromadb

ap = argparse.ArgumentParser()
ap.add_argument("export")
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--data-dir")
ap.add_argument("--verify-only", action="store_true")
ap.add_argument("--batch", type=int, default=500)
a = ap.parse_args()

work = tempfile.mkdtemp(prefix="mprestore-")
with tarfile.open(a.export) as tar:
    tar.extractall(work, filter="data")
manifest = json.load(open(os.path.join(work, "manifest.json")))
client = chromadb.HttpClient(host=a.host, port=a.port)

if not a.verify_only:
    by_col = {}
    for line in open(os.path.join(work, "drawers.jsonl")):
        r = json.loads(line)
        by_col.setdefault(r["collection"], []).append(r)
    for cmeta in manifest["collections"]:
        name = cmeta["name"]
        # Reproduce the source index configuration exactly (space, ef_*, max_neighbors ...).
        configuration = None
        hnsw = (cmeta.get("configuration") or {}).get("hnsw") or {}
        if hnsw:
            from chromadb.api.collection_configuration import CreateCollectionConfiguration, CreateHNSWConfiguration
            allowed = {"space", "ef_construction", "max_neighbors", "ef_search", "num_threads", "batch_size", "sync_threshold", "resize_factor"}
            configuration = CreateCollectionConfiguration(hnsw=CreateHNSWConfiguration(**{k: v for k, v in hnsw.items() if k in allowed and v is not None}))
        existing = {x.name if hasattr(x, "name") else x for x in client.list_collections()}
        if name in existing:
            col = client.get_collection(name)
            if col.count() and not a.verify_only:
                print(f"ERROR: target collection {name} already has {col.count()} rows; refusing to merge into a non-empty store", file=sys.stderr)
                sys.exit(3)
        else:
            col = client.create_collection(name, metadata=cmeta.get("metadata") or None, configuration=configuration)
        rows = by_col.get(name, [])
        with_emb = [r for r in rows if r["embedding"] is not None]
        without = [r for r in rows if r["embedding"] is None]
        for i in range(0, len(with_emb), a.batch):
            chunk = with_emb[i:i + a.batch]
            col.upsert(ids=[r["id"] for r in chunk], documents=[r["document"] for r in chunk],
                       metadatas=[r["metadata"] or None for r in chunk], embeddings=[r["embedding"] for r in chunk])
        if without:
            # rows exported without a vector: re-embed with the palace's embedding function
            from mempalace.embedding import get_embedding_function
            ef = get_embedding_function(device="cpu", model="minilm")
            col2 = client.get_collection(name, embedding_function=ef)
            for i in range(0, len(without), 100):
                chunk = without[i:i + 100]
                col2.upsert(ids=[r["id"] for r in chunk], documents=[r["document"] for r in chunk],
                            metadatas=[r["metadata"] or None for r in chunk])
            print(f"re-embedded {len(without)} rows that had no stored vector")
        print(f"restored {len(rows)} rows into {name}")
    if a.data_dir:
        os.makedirs(a.data_dir, exist_ok=True)
        for item in ("knowledge_graph.sqlite3", "oauth.sqlite3", "config.json", "mempalace.yaml", "wal", "hook_state"):
            src = os.path.join(work, item)
            dst = os.path.join(a.data_dir, item)
            if os.path.isdir(src):
                if os.path.exists(dst): shutil.rmtree(dst)
                shutil.copytree(src, dst)
            elif os.path.isfile(src):
                shutil.copy2(src, dst)
        print(f"restored aux files into {a.data_dir}")

# verify
ok = True
fp = hashlib.sha256()
for cmeta in manifest["collections"]:
    col = client.get_collection(cmeta["name"])
    n = col.count()
    if n != cmeta["rows"]:
        print(f"MISMATCH {cmeta['name']}: {n} rows vs manifest {cmeta['rows']}"); ok = False
    g = col.get(include=["documents"], limit=n)
    for i, d in sorted(zip(g["ids"], g["documents"])):
        fp.update(i.encode()); fp.update(b"\0"); fp.update((d or "").encode()); fp.update(b"\n")
if fp.hexdigest() != manifest["all_docs_sha256"]:
    print("MISMATCH content fingerprint"); ok = False
print(json.dumps({"verified": ok, "total_rows": manifest["total_rows"], "all_docs_sha256": manifest["all_docs_sha256"]}))
shutil.rmtree(work, ignore_errors=True)
sys.exit(0 if ok else 1)
