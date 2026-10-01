#!/usr/bin/env python3
"""Apply a redaction map (from secrets_scan.py) to a Chroma collection in place.

  apply_redactions.py redaction-map.jsonl --port 8899 [--collection mempalace_drawers]

For each {"id","document","metadata"} row: update the document (re-embedded by the
configured embedding function, since the text changed) and metadata. Verifies
afterwards that no redacted drawer still contains the original text and that
every updated document contains at least one [REDACTED:...] marker.
Run this ONLY against the migration copy — never the live local store or the cold backup.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import chromadb

ap = argparse.ArgumentParser()
ap.add_argument("map")
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--collection", default="mempalace_drawers")
a = ap.parse_args()

os.environ.setdefault("MEMPALACE_CHROMA_MODE", "http")
from mempalace.embedding import get_embedding_function  # noqa: E402

client = chromadb.HttpClient(host=a.host, port=a.port)
col = client.get_collection(a.collection, embedding_function=get_embedding_function(device="cpu", model="minilm"))
rows = [json.loads(l) for l in open(a.map)]
before = col.get(ids=[r["id"] for r in rows], include=["documents"])
orig = dict(zip(before["ids"], before["documents"]))
col.update(ids=[r["id"] for r in rows], documents=[r["document"] for r in rows], metadatas=[r["metadata"] or None for r in rows])
after = col.get(ids=[r["id"] for r in rows], include=["documents"])
new = dict(zip(after["ids"], after["documents"]))
bad = [i for i in orig if new.get(i) == orig[i] or "[REDACTED:" not in (new.get(i) or "")]
print(json.dumps({"updated": len(rows), "failed": bad}))
sys.exit(1 if bad else 0)
