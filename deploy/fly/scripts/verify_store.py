"""Compute MemPalace baseline against a running Chroma server + KG sqlite.
Usage: python baseline.py <host> <port> <kg_sqlite_path> <data_dir> <out.json> [sample_ids_json]
"""
import sys, json, hashlib, random, sqlite3, os, subprocess
import chromadb
host, port, kg_path, data_dir, out = sys.argv[1:6]
fixed_ids = json.load(open(sys.argv[6])) if len(sys.argv) > 6 else None
c = chromadb.HttpClient(host=host, port=int(port))
cols = {}
total_rows = 0
for col in c.list_collections():
    name = col.name if hasattr(col, "name") else col
    coll = c.get_collection(name)
    n = coll.count()
    cols[name] = {"rows": n}
    total_rows += n
    if name == "mempalace_drawers":
        # logical drawer count: rows w/o parent_drawer_id + distinct parents
        got = coll.get(include=["metadatas"], limit=n)
        ids = got["ids"]; metas = got["metadatas"]
        parents = set(); plain = 0
        wings = {}
        for i, m in zip(ids, metas):
            m = m or {}
            p = m.get("parent_drawer_id")
            if p: parents.add(p)
            else: plain += 1
            w = m.get("wing", "?"); wings[w] = wings.get(w, 0) + 1
        cols[name]["logical_drawers"] = plain + len(parents)
        cols[name]["chunk_rows"] = n - plain
        cols[name]["rows_per_wing"] = dict(sorted(wings.items()))
        all_ids = sorted(ids)
        if fixed_ids:
            sample_ids = fixed_ids
        else:
            random.seed(20261001); sample_ids = random.sample(all_ids, 5)
        sample = []
        s = coll.get(ids=sample_ids, include=["documents", "metadatas"])
        order = {i: k for k, i in enumerate(s["ids"])}
        for sid in sample_ids:
            k = order[sid]
            doc = s["documents"][k] or ""
            meta = s["metadatas"][k] or {}
            sample.append({"id": sid,
                           "doc_sha256": hashlib.sha256(doc.encode("utf-8")).hexdigest(),
                           "meta_sha256": hashlib.sha256(json.dumps(meta, sort_keys=True).encode()).hexdigest(),
                           "doc_len": len(doc)})
        cols[name]["sample"] = sample
        # whole-collection content fingerprint (ids+docs), order-independent
        h = hashlib.sha256()
        allg = coll.get(include=["documents"], limit=n)
        for i, d in sorted(zip(allg["ids"], allg["documents"])):
            h.update(i.encode()); h.update(b"\0"); h.update((d or "").encode()); h.update(b"\n")
        cols[name]["all_docs_sha256"] = h.hexdigest()
kg = {}
if os.path.exists(kg_path):
    db = sqlite3.connect(kg_path)
    for (t,) in db.execute("select name from sqlite_master where type='table'"):
        kg[t] = db.execute(f'select count(*) from "{t}"').fetchone()[0]
def du(p):
    return int(subprocess.check_output(["du", "-sk", p]).split()[0]) * 1024 if os.path.exists(p) else None
res = {"computed_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
       "chroma": f"{host}:{port}", "collections": cols, "total_rows": total_rows,
       "kg_tables": kg, "kg_entries_total": sum(kg.values()),
       "disk_bytes": {"palace": du(os.path.join(data_dir, "palace")), "kg_sqlite": du(kg_path),
                      "wal": du(os.path.join(data_dir, "wal")), "mempalace_dir_total": du(data_dir)}}
json.dump(res, open(out, "w"), indent=2)
print(json.dumps({k: v for k, v in res.items() if k != "collections"}, indent=1))
for n, v in cols.items():
    print(n, {k: v[k] for k in v if k not in ("sample", "rows_per_wing")})
    if "rows_per_wing" in v: print(" wings:", v["rows_per_wing"])
    if "sample" in v: print(" sample:", [(s["id"], s["doc_sha256"][:12]) for s in v["sample"]])
