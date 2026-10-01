#!/usr/bin/env python3
"""Scan an exported MemPalace drawer corpus for secrets and build a redaction map.

Input : drawers-export.jsonl  (one {"id","wing","room","document","metadata"} per line)
Output: <outdir>/secrets-report.json      — counts per wing, drawer ids, secret TYPES only
        <outdir>/redaction-map.jsonl      — {"id", "document", "metadata"} with values replaced
                                            by [REDACTED:<type>] (chmod 600; never commit)
        <outdir>/rotation-list.json       — distinct live-looking secrets by type + fingerprint
Never prints a secret value. Combines gitleaks (if installed) with the regexes below.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict

SRC, OUTDIR = sys.argv[1], sys.argv[2]
os.makedirs(OUTDIR, exist_ok=True)

PLACEHOLDER = re.compile(r"(REDACTED|xxx|XXX|\.\.\.|\*\*\*|<[^>]*>|your[_-]|example|placeholder|changeme|dummy|\$\{?[A-Z_]+|sk-ant-api03-\.{3})", re.I)


def looks_placeholder(v: str) -> bool:
    if PLACEHOLDER.search(v):
        return True
    if len(set(v)) <= 3:
        return True
    return False


# (type, compiled regex, group index holding the secret value)
RULES = [
    ("anthropic-api-key", re.compile(r"sk-ant-(?:api\d+-|admin\d+-|sid\d+-)?[A-Za-z0-9_-]{24,}"), 0),
    ("openai-api-key", re.compile(r"(?<![A-Za-z0-9])sk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}"), 0),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"), 0),
    ("github-fine-grained-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b"), 0),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0),
    ("google-oauth-access-token", re.compile(r"\bya29\.[0-9A-Za-z_-]{30,}"), 0),
    ("google-oauth-refresh-token", re.compile(r"\b1//0[A-Za-z0-9_-]{30,}"), 0),
    ("google-oauth-client-secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b"), 0),
    ("fly-api-token", re.compile(r"\b(?:FlyV1\s+)?fm[12]_[A-Za-z0-9+/=_,-]{40,}"), 0),
    ("fly-org-token", re.compile(r"\bfo1_[A-Za-z0-9_-]{30,}\b"), 0),
    ("cloudflare-api-token", re.compile(r"(?i)(?:cloudflare|cf)[_-]?(?:api[_-]?)?(?:token|key)\W{0,20}([A-Za-z0-9_-]{40})\b"), 1),
    ("cloudflare-global-key", re.compile(r"(?i)x-auth-key\W{0,10}([a-f0-9]{37})\b"), 1),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    ("aws-secret-access-key", re.compile(r"(?i)aws[_-]?secret[_-]?access[_-]?key\W{0,20}([A-Za-z0-9/+=]{40})\b"), 1),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"), 0),
    ("stripe-key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b"), 0),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"), 0),
    ("twilio-key", re.compile(r"\bSK[a-f0-9]{32}\b"), 0),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), 0),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"), 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), 0),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{24,})"), 1),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), 0),
    ("private-key-header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), 0),
    ("password-in-url", re.compile(r"\b[a-z][a-z0-9+.-]{1,15}://[^/\s:@'\"]{1,64}:([^@\s/'\"]{4,})@[A-Za-z0-9.-]+"), 1),
    # UPPER_CASE env-style assignment anywhere in the text (transcripts flatten newlines):
    # FOO_TOKEN=<value>, export API_KEY="<value>", APP_TOKEN = <value>. Value must be >= 12
    # chars and mix letters+digits (checked below) so `KEY=value` prose never matches.
    # Transcripts often carry literal "\t" / "\n" escapes right before the variable name.
    ("env-secret-assignment", re.compile(r"(?:(?<![A-Za-z0-9_])|(?<=\\[tnr]))(?:export[ \t]+)?[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)[ \t]*=[ \t]*[\"']?([A-Za-z0-9_\-./+=]{12,})"), 1),
    # Lower/mixed-case `api_key: <long opaque>` / `token=<long opaque>` with a 32+ char value.
    ("generic-secret-assignment", re.compile(r"(?i)(?<![A-Za-z0-9])[A-Za-z_]*(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|passw(?:or)?d)[A-Za-z_]*[\"']?[ \t]*[:=][ \t]*[\"']?([A-Za-z0-9_\-]{32,})"), 1),
    ("password-assignment", re.compile(r"(?i)\b(?:password|passwd|pwd)\b[ \t]*[:=][ \t]*[\"']?([^\s\"',;]{6,})"), 1),
]

# Rules whose match is only meaningful if the value doesn't look like a placeholder/code.
SOFT = {"bearer-token", "env-secret-assignment", "generic-secret-assignment", "password-assignment", "cloudflare-api-token", "password-in-url", "jwt"}
NEEDS_MIXED = {"env-secret-assignment", "generic-secret-assignment", "bearer-token"}


def mixed_alnum(v: str) -> bool:
    return any(c.isdigit() for c in v) and any(c.isalpha() for c in v)


def fingerprint(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()[:12]


def scan_text(text: str):
    """Return list of (type, start, end) spans of secret VALUES in text, non-overlapping."""
    spans = []
    for typ, rx, grp in RULES:
        for m in rx.finditer(text):
            s, e = m.span(grp)
            if s == e:
                continue
            val = text[s:e]
            if typ in SOFT and (looks_placeholder(val) or val.lower() in ("password", "secret", "token", "none", "null", "true", "false")):
                continue
            if typ in NEEDS_MIXED and not mixed_alnum(val):
                continue
            if typ == "env-secret-assignment" and (val.startswith(("$", "`", "(", "{", "[", "/", "~", ".")) or val in ("required", "optional")):
                continue
            if typ == "password-assignment" and (val.startswith(("$", "{", "[", "(", "<", "`")) or "/" in val[:1]):
                continue
            spans.append((typ, s, e))
    # resolve overlaps: longer/earlier wins
    spans.sort(key=lambda t: (t[1], -(t[2] - t[1])))
    out = []
    last_end = -1
    for typ, s, e in spans:
        if s < last_end:
            continue
        out.append((typ, s, e))
        last_end = e
    return out


def redact(text: str, spans):
    buf = []
    pos = 0
    for typ, s, e in spans:
        buf.append(text[pos:s]); buf.append(f"[REDACTED:{typ}]"); pos = e
    buf.append(text[pos:])
    return "".join(buf)


# ---------------------------------------------------------------- gitleaks pass
gitleaks_hits = defaultdict(list)  # drawer id -> [(rule, secret)]
if shutil.which("gitleaks"):
    tmp = tempfile.mkdtemp(prefix="gl-")
    os.chmod(tmp, 0o700)
    idx = []  # line_no -> drawer id (1-based lines)
    with open(os.path.join(tmp, "corpus.txt"), "w") as f, open(SRC) as src:
        line_no = 1
        for raw in src:
            d = json.loads(raw)
            header = f"===== DRAWER {d['id']} =====\n"
            body = (d.get("document") or "") + "\n" + json.dumps(d.get("metadata") or {}, ensure_ascii=False) + "\n"
            f.write(header + body)
            n = header.count("\n") + body.count("\n")
            idx.append((line_no, line_no + n - 1, d["id"]))
            line_no += n
    rep = os.path.join(tmp, "report.json")
    subprocess.run(["gitleaks", "detect", "--no-git", "--source", tmp, "--report-format", "json", "--report-path", rep, "--exit-code", "0", "--no-banner", "--redact=100"],
                   check=False, capture_output=True)
    # --redact=100 masks the Secret field in the report; we use StartLine + Match context length only.
    # Re-run without redaction into memory is unnecessary: we locate the value via our own regexes
    # plus gitleaks' RuleID as a cross-check. Fall back to its (masked) match for counting.
    try:
        findings = json.load(open(rep))
    except Exception:
        findings = []
    import bisect
    starts = [a for a, _, _ in idx]
    for fnd in findings:
        ln = fnd.get("StartLine", 0)
        k = bisect.bisect_right(starts, ln) - 1
        if 0 <= k < len(idx) and idx[k][0] <= ln <= idx[k][1]:
            gitleaks_hits[idx[k][2]].append(fnd.get("RuleID", "gitleaks"))
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"gitleaks: {len(findings)} findings across {len(gitleaks_hits)} drawers", file=sys.stderr)
else:
    print("gitleaks not installed; regex-only scan", file=sys.stderr)

# ---------------------------------------------------------------- regex pass + redaction
per_wing = Counter()
per_type = Counter()
drawers = {}
rotation = defaultdict(dict)  # type -> fingerprint -> {count, drawers, wings}
n_redactions = 0
gitleaks_only = []

map_path = os.path.join(OUTDIR, "redaction-map.jsonl")
with open(SRC) as src, open(map_path, "w") as out:
    os.chmod(map_path, 0o600)
    for raw in src:
        d = json.loads(raw)
        doc = d.get("document") or ""
        meta = d.get("metadata") or {}
        meta_s = json.dumps(meta, ensure_ascii=False, sort_keys=True)
        spans_doc = scan_text(doc)
        spans_meta = scan_text(meta_s)
        types = [t for t, _, _ in spans_doc + spans_meta]
        gl = gitleaks_hits.get(d["id"], [])
        if not types and gl:
            # gitleaks found something our regexes did not: record for manual review.
            gitleaks_only.append({"id": d["id"], "wing": d.get("wing"), "gitleaks_rules": sorted(set(gl))})
        if not types:
            continue
        wing = d.get("wing") or "?"
        per_wing[wing] += len(types)
        per_type.update(types)
        drawers[d["id"]] = {"wing": wing, "room": d.get("room"), "types": sorted(set(types)), "hits": len(types),
                            "gitleaks_rules": sorted(set(gl)), "created": meta.get("created_at") or meta.get("date") or meta.get("timestamp")}
        for t, s, e in spans_doc:
            v = doc[s:e]
            r = rotation[t].setdefault(fingerprint(v), {"occurrences": 0, "drawers": set(), "wings": set(), "value_length": len(v), "prefix": v[:4] if t in ("anthropic-api-key", "openai-api-key", "github-token", "github-fine-grained-pat", "google-api-key", "fly-api-token", "slack-token", "stripe-key", "huggingface-token", "npm-token") else ""})
            r["occurrences"] += 1; r["drawers"].add(d["id"]); r["wings"].add(wing)
        for t, s, e in spans_meta:
            v = meta_s[s:e]
            r = rotation[t].setdefault(fingerprint(v), {"occurrences": 0, "drawers": set(), "wings": set(), "value_length": len(v), "prefix": ""})
            r["occurrences"] += 1; r["drawers"].add(d["id"]); r["wings"].add(wing)
        n_redactions += len(types)
        new_doc = redact(doc, spans_doc)
        new_meta = json.loads(redact(meta_s, spans_meta)) if spans_meta else meta
        out.write(json.dumps({"id": d["id"], "document": new_doc, "metadata": new_meta}, ensure_ascii=False) + "\n")

for t in rotation:
    for fp, r in rotation[t].items():
        r["drawers"] = sorted(r["drawers"]); r["wings"] = sorted(r["wings"])

report = {
    "source": os.path.basename(SRC),
    "drawers_with_hits": len(drawers),
    "total_redactions": n_redactions,
    "hits_per_wing": dict(per_wing.most_common()),
    "hits_per_type": dict(per_type.most_common()),
    "drawers": drawers,
    "gitleaks_only_for_manual_review": gitleaks_only,
}
json.dump(report, open(os.path.join(OUTDIR, "secrets-report.json"), "w"), indent=2)
json.dump({t: v for t, v in rotation.items()}, open(os.path.join(OUTDIR, "rotation-list.json"), "w"), indent=2)
print(json.dumps({k: report[k] for k in ("drawers_with_hits", "total_redactions", "hits_per_wing", "hits_per_type")}, indent=1))
print("gitleaks-only drawers for manual review:", len(gitleaks_only))
print("distinct secrets by type:", {t: len(v) for t, v in rotation.items()})
