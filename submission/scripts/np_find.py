#!/usr/bin/env python3
"""Find citable sentences (with their ids) in a parsed paper.

Usage:
    python np_find.py OUT "question words" [--top 8]          # all indexed materials
    python np_find.py PARSED "query words" [--top 8] [--page 5] [--section Results] [--regex "3\\.5\\d?%"]
    python np_find.py PARSED --id p5-b89-s2 [--context 1]     # a sentence and its neighbours

PARSED = OUT/parsed/<stem>. Output lines: id | page | section | cues | text.
Use the printed id as evidence in draft.json (or copy the text verbatim as a quote).
"""
import argparse
import json
import math
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import np_common as C  # noqa: E402
import table_query  # noqa: E402


def toks(s):
    return [t[:6] for t in re.findall(r"[a-z0-9]+|[一-鿿]", C.norm(s)) if len(t) > 1 or t.isdigit() or "一" <= t <= "鿿"]


def load(parsed):
    p = os.path.join(parsed, "sentences.jsonl")
    if not os.path.exists(p):
        sys.exit(f"{p} missing: run np_init.py first")
    with open(p, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def fmt(s):
    c = s["cues"]
    tag = []
    if c["numbers"]:
        tag.append("num:" + ",".join(c["numbers"][:5]))
    for k, lab in (("hedges", "hedge"), ("negations", "neg"), ("citations", "cite"), ("comparatives", "cmp"), ("causal", "cause")):
        if c[k]:
            tag.append(lab)
    sec = " > ".join(s.get("section_path") or [])[:40]
    return f"{s['id']} | p{s['page']} | {sec} | {' '.join(tag)} | {s['text']}"


def fragments(sentences, doc):
    for s in sentences:
        yield {**s, "kind": "sentence", "hint": " ".join(s.get("section_path") or [])}
    for table in doc.get("tables", []):
        columns = " / ".join(c["name"] for c in table_query.columns_of(table))
        label = table.get("caption") or ""
        header_rows = (table.get("header") or {}).get("n_header_rows", 1)
        for row in table["rows"][header_rows:] or [[]]:
            yield {"kind": "table", "id": table["id"], "page": table["page"],
                   "text": " | ".join(str(v) for v in row) or label,
                   "hint": f"{label} {columns}"}
    for eq in doc.get("equations", []):
        label = f"Equation ({eq['number']})" if eq.get("number") else "Equation"
        yield {"kind": "equation", "id": eq.get("block_id") or label, "page": eq["page"],
               "text": eq.get("unicode_math") or eq.get("text") or label,
               "hint": label, "crop": eq.get("crop")}


def rank(items, query, limit):
    if not query.strip():
        return items[:limit]
    query_terms = set(toks(query)) - set(toks("the and for from with what which does did how this that about evidence report answer"))
    terms = [(f, set(toks(f["text"])), set(toks(f["hint"]))) for f in items]
    df = Counter(t for _, body, hint in terms for t in body | hint)
    weights = {t: math.log(1 + len(items) / (1 + df[t])) for t in query_terms}
    phrase = C.norm(query)
    ranked = []
    for f, body, hint in terms:
        overlap = query_terms & (body | hint)
        if not overlap:
            continue
        score = sum(weights[t] * (1.0 if t in body else 0.7) for t in sorted(overlap))
        if phrase in C.norm(f["text"] + " " + f["hint"]):
            score += 5
        if f["kind"] == "sentence":
            score += min(C.claim_score(f), 5) * 0.15
        ranked.append((score, f))
    ranked.sort(key=lambda pair: -pair[0])
    locations = Counter()
    hits = []
    for _, f in ranked:
        key = (f.get("stem"), f["kind"], f["id"] if f["kind"] == "table" else f["page"])
        if locations[key] >= max(2, limit // 2):
            continue
        if len(hits) == limit:
            break
        hits.append(f)
        locations[key] += 1
    return hits


def describe(f):
    source = f"{f['stem']} [{f['role']}] | " if f.get("stem") else ""
    if f["kind"] == "sentence":
        return source + fmt(f)
    crop = f" | crop {f['crop']}" if f.get("crop") else ""
    return f"{source}{f['kind']} {f['id']} | p{f['page']} | {f['hint']} | {f['text']}{crop}"


def main():
    C.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("parsed")
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--page", type=int, default=None)
    ap.add_argument("--section", default=None)
    ap.add_argument("--regex", default=None)
    ap.add_argument("--id", default=None)
    ap.add_argument("--context", type=int, default=1)
    a = ap.parse_args()
    root = os.path.join(a.parsed, "parsed") if os.path.isdir(os.path.join(a.parsed, "parsed")) else a.parsed
    is_source = os.path.isfile(os.path.join(root, "sentences.jsonl"))
    sources = [root] if is_source else [
        os.path.join(root, d) for d in sorted(os.listdir(root))
        if os.path.isfile(os.path.join(root, d, "sentences.jsonl"))]
    if not sources:
        sys.exit(f"{root} has no indexed sources: run np_init.py first")
    if a.id:
        if not is_source:
            sys.exit("--id requires a single parsed source directory")
        S = load(root)
        idx = next((i for i, s in enumerate(S) if s["id"] == a.id), None)
        if idx is None:
            sys.exit(f"no sentence {a.id}")
        for s in S[max(0, idx - a.context): idx + a.context + 1]:
            print(("> " if s["id"] == a.id else "  ") + fmt(s))
        return
    items = []
    for p in sources:
        md_path = os.path.join(p, "source_metadata.json")
        md = C.load_json(md_path) if os.path.isfile(md_path) else {}
        doc_path = os.path.join(p, "doc.json")
        doc = C.load_json(doc_path) if os.path.isfile(doc_path) else {}
        items.extend({**f, "stem": os.path.basename(p), "role": (md.get("material_role") or {}).get("value") or "unspecified"}
                     for f in fragments(load(p), doc))
    candidates = [f for f in items if (a.page is None or a.page in f.get("pages", [f["page"]]))
                  and (a.section is None or a.section.lower() in f["hint"].lower())
                  and (a.regex is None or re.search(a.regex, f["text"] + " " + f["hint"], re.I))]
    hits = rank(candidates, a.query, max(a.top, 0))
    for f in hits:
        print(fmt(f) if is_source and f["kind"] == "sentence" else describe(f))
    if not hits:
        print("no match (try fewer words, --regex, or check qa_report.json for unreadable pages)")


if __name__ == "__main__":
    main()
