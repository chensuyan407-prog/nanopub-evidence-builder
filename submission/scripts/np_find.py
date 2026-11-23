#!/usr/bin/env python3
"""Find citable sentences (with their ids) in a parsed paper.

Usage:
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


def toks(s):
    return [t[:6] for t in re.findall(r"[a-z0-9]+|[一-鿿]", C.norm(s)) if len(t) > 1 or "一" <= t <= "鿿"]


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
    S = load(a.parsed)
    if a.id:
        idx = next((i for i, s in enumerate(S) if s["id"] == a.id), None)
        if idx is None:
            sys.exit(f"no sentence {a.id}")
        for s in S[max(0, idx - a.context): idx + a.context + 1]:
            print(("> " if s["id"] == a.id else "  ") + fmt(s))
        return
    cand = [s for s in S if (a.page is None or a.page in s.get("pages", [s["page"]]))
            and (a.section is None or a.section.lower() in " ".join(s.get("section_path") or []).lower())
            and (a.regex is None or re.search(a.regex, s["text"], re.I))]
    q = toks(a.query)
    if q:
        df = Counter(t for s in S for t in set(toks(s["text"])))
        n = len(S)
        scored = []
        for s in cand:
            tf = Counter(toks(s["text"]))
            sc = sum((1 + math.log(tf[t])) * math.log(1 + n / (1 + df[t])) for t in set(q) if tf[t])
            if C.norm(a.query) in C.norm(s["text"]):
                sc += 5
            if sc > 0:
                scored.append((sc, s))
        scored.sort(key=lambda x: -x[0])
        cand = [s for _, s in scored]
    for s in cand[: a.top]:
        print(fmt(s))
    if not cand:
        print("no match (try fewer words, --regex, or check qa_report.json for unreadable pages)")


if __name__ == "__main__":
    main()
