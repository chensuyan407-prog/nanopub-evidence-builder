#!/usr/bin/env python3
"""Step 2 of nanopub-evidence-builder: turn the agent's draft into checked,
exportable nanopublications and fill result.json.

Usage:
    python np_build.py --draft WORK/draft.json --out OUT [--parsed OUT/parsed/<stem>]
                       [--creator "agent name"] [--created 2026-01-01T00:00:00Z] [--no-previews]
                       [--merge] [--allow-shrink]

Safety (v0.3): a draft that is not valid JSON, has no assertion list, or names no usable source never touches
OUT/result.json (the error goes to OUT/build_error.json, exit code 2). One malformed assertion does not stop the
build: it is listed in result.json.build_errors while valid records proceed. A build that would replace a
result holding N built records with fewer (e.g. the empty template) is refused (exit 3) unless --allow-shrink or
--merge. Every build first copies the previous result.json to OUT/result.prev.json.
--merge: the draft is merged onto the last successfully built draft (OUT/draft.built.json) by assertion id
(same id replaces, new ids are appended, "delete": ["a3"] removes), fields / overrides are updated and
unreported / conflicts / uncertainty unioned -- so a small draft with only the new or fixed assertions is enough.

For every draft assertion it
  1. resolves each evidence item to the PDF: sentence id -> exact sentence; {page, quote} ->
     verbatim search (cited page first, then all pages; fuzzy only as a flagged fallback);
     table cell -> parsed table cell; image region -> crop provenance. Line-level rectangles
     of the quote are taken from the PDF words, so the location can be drawn on the page;
  2. resolves citation markers ([12], (Smith et al., 2020)) to the reference list;
  3. attaches source provenance (title, authors, year, DOI/arXiv id + version, PDF SHA-256,
     each with where it was read) and publication info;
  4. runs np_check.py rules -> quality verdict + extraction confidence per record;
  5. exports OUT/nanopubs.json, nanopubs.trig, nanopubs.nq, nanopubs.jsonld (round-trip checked),
     OUT/review.md and OUT/evidence_previews/*.png (quotes boxed on the page);
  6. fills the output_contract fields of OUT/result.json as mapped in draft.fields.
Re-running with the same draft gives the same ids (content-derived).
"""
import argparse
import copy
import difflib
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import np_common as C  # noqa: E402
import np_check  # noqa: E402
import np_contract as K  # noqa: E402

NP = "http://www.nanopub.org/nschema#"
PROV = "http://www.w3.org/ns/prov#"
DCT = "http://purl.org/dc/terms/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
XSD = "http://www.w3.org/2001/XMLSchema#"
TEMP = "http://purl.org/nanopub/temp/"
PREFIXES = {"np": NP, "prov": PROV, "dct": DCT, "rdf": RDF, "rdfs": RDFS, "xsd": XSD,
            "npeb": TEMP + "npeb-terms#", "npent": TEMP + "entity/"}


def eco_code(evidence_type, scope, relation_type):
    """One ECO code per record (hint for downstream mapping)."""
    if scope == "cited_work" or evidence_type == "cited":
        return "ECO:0000304", "author statement supported by traceable reference used in manual assertion"
    if evidence_type == "inferred":
        return "ECO:0000305", "curator inference used in manual assertion"
    if relation_type in ("quantitative", "comparative", "causal", "associative", "existence", "negative"):
        return "ECO:0000006", "experimental evidence"
    return "ECO:0000303", "author statement without traceable support used in manual assertion"


# ------------------------------------------------------------------ loading
class Source:
    def __init__(self, parsed_dir, overrides=None):
        self.dir = parsed_dir
        self.doc = C.load_json(os.path.join(parsed_dir, "doc.json"))
        self.sents = []
        sp = os.path.join(parsed_dir, "sentences.jsonl")
        if os.path.exists(sp):
            with open(sp, encoding="utf-8") as fh:
                self.sents = [json.loads(l) for l in fh if l.strip()]
        else:
            self.sents = C.build_sentence_index(self.doc)
        self.by_id = {s["id"]: s for s in self.sents}
        self.blocks = {b["id"]: b for b in self.doc.get("blocks", [])}
        mp = os.path.join(parsed_dir, "source_metadata.json")
        self.file = (self.doc.get("meta") or {}).get("source_file")
        self.format = (self.doc.get("meta") or {}).get("format") or "pdf"
        self.pdf = self.file if (self.file and os.path.exists(self.file) and self.file.lower().endswith(".pdf")) else None
        if os.path.exists(mp):
            self.md = C.load_json(mp)
        elif self.pdf:
            self.md = C.extract_metadata(self.pdf, self.doc)
        elif self.file and os.path.exists(self.file):
            import text_source
            self.md = text_source.extract_metadata(self.file, self.doc)
        else:
            self.md = {}
        self.overrides_log = []
        for k, v in (overrides or {}).items() if isinstance(overrides, dict) else []:
            if k in ("pdf_sha256", "file_sha256"):
                self.overrides_log.append({"field": k, "from": None, "to": None, "refused": "the file hash is computed, not overridable"})
                continue
            val = v.get("value") if isinstance(v, dict) else v
            old = (self.md.get(k) or {}).get("value")
            self.md[k] = {"value": val, "source": {"kind": "agent_override", "reason": (v.get("reason") if isinstance(v, dict) else None),
                                                    "evidence": (v.get("evidence") if isinstance(v, dict) else None)},
                          "confidence": "medium", "previous_value": old}
            self.overrides_log.append({"field": k, "from": old, "to": val})
        self.name = os.path.basename(parsed_dir)
        self.refs = {str(r.get("label")): r for r in self.doc.get("references", []) if r.get("label") is not None}
        self.ref_list = self.doc.get("references", [])
        self._fitz = None
        self._page_text = {}

    def fitz_doc(self):
        if self._fitz is None and self.pdf:
            try:
                self._fitz = C.fitz.open(self.pdf)
            except Exception:
                self._fitz = False
        return self._fitz or None

    def page_text(self, pg):
        if pg not in self._page_text:
            d = self.fitz_doc()
            self._page_text[pg] = d[pg - 1].get_text() if d and 1 <= pg <= d.page_count else ""
        return self._page_text[pg]


def find_parsed_dirs(out):
    base = os.path.join(out, "parsed")
    if not os.path.isdir(base):
        return []
    return [os.path.join(base, d) for d in sorted(os.listdir(base)) if os.path.exists(os.path.join(base, d, "doc.json"))]


# ------------------------------------------------------------------ geometry
def quote_rects(src, page, quote, block_ids=None):
    """Line rectangles covering `quote` on `page`, from the PDF words (None if not locatable)."""
    d = src.fitz_doc()
    if not d or not (1 <= page <= d.page_count):
        return None
    words = d[page - 1].get_text("words")
    if block_ids:
        boxes = [src.blocks[b]["bbox"] for b in block_ids if b in src.blocks and src.blocks[b]["page"] == page]
        if boxes:
            def inside(w):
                cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
                return any(b[0] - 3 <= cx <= b[2] + 3 and b[1] - 3 <= cy <= b[3] + 3 for b in boxes)
            sub = [w for w in words if inside(w)]
            if sub:
                words = sub
    words = sorted(words, key=lambda w: (w[5], w[6], w[7]))
    target = C.norm_loose(quote)
    if not target:
        return None
    pieces, spans, pos = [], [], 0
    for w in words:
        t = C.norm_loose(w[4])
        pieces.append(t)
        spans.append((pos, pos + len(t)))
        pos += len(t)
    hay = "".join(pieces)
    i = hay.find(target)
    partial = False
    if i < 0:  # sentence continues on the next page/column outside these words: match the longest head
        for cut in (int(len(target) * 0.6), int(len(target) * 0.35)):
            if cut >= 12:
                i = hay.find(target[:cut])
                if i >= 0:
                    partial = True
                    target = target[:cut]
                    break
        if i < 0:
            for cut in (int(len(target) * 0.6), int(len(target) * 0.35)):
                if cut >= 12 and hay.find(target[-cut:]) >= 0:
                    i = hay.find(target[-cut:])
                    target = target[-cut:]
                    partial = True
                    break
    if i < 0:
        return None
    j = i + len(target)
    sel = [w for w, (a, b) in zip(words, spans) if b > i and a < j]
    lines = {}
    for w in sel:
        k = (w[5], w[6])
        r = lines.get(k)
        lines[k] = [min(r[0], w[0]), min(r[1], w[1]), max(r[2], w[2]), max(r[3], w[3])] if r else [w[0], w[1], w[2], w[3]]
    rects = [[round(x, 2) for x in r] for r in lines.values()]
    return {"rects": rects, "bbox": union(rects), "partial": partial}


def union(rects):
    if not rects:
        return None
    return [round(min(r[0] for r in rects), 2), round(min(r[1] for r in rects), 2),
            round(max(r[2] for r in rects), 2), round(max(r[3] for r in rects), 2)]


# ------------------------------------------------------------------ evidence resolution
def resolve_citations(src, text):
    out = []
    for c in C.CITE_RE.findall(text):
        nums = []
        for part in re.findall(r"\d+\s*[-–]\s*\d+|\d+", c) if c.startswith("[") else []:
            if re.search(r"[-–]", part):
                a, b = [int(x) for x in re.split(r"\s*[-–]\s*", part)]
                nums += list(range(a, min(b, a + 30) + 1))
            else:
                nums.append(int(part))
        refs = []
        for n in nums:
            r = src.refs.get(str(n))
            refs.append({"label": str(n), "text": (r or {}).get("text"), "page": (r or {}).get("page"), "block_id": (r or {}).get("block_id")} if r
                        else {"label": str(n), "text": None, "note": "not found in the parsed reference list"})
        if not c.startswith("["):  # author-year
            m = re.search(r"([A-Z][A-Za-z\-]+).*?(\d{4})", c)
            if m:
                hit = next((r for r in src.ref_list if m.group(1) in (r.get("text") or "") and m.group(2) in (r.get("text") or "")), None)
                refs.append({"label": c, "text": (hit or {}).get("text"), "page": (hit or {}).get("page"), "block_id": (hit or {}).get("block_id")} if hit
                            else {"label": c, "text": None, "note": "author-year citation not matched in the reference list"})
        out.append({"marker": c, "references": refs})
    return out


def sentence_hits(src, quote, pages=None):
    nq = C.norm(quote)
    hits = []
    for s in src.sents:
        if pages and s["page"] not in pages:
            continue
        if nq and nq in C.norm(s["text"]):
            hits.append(s)
    return hits


def resolve_text(src, item, field_role):
    sid = item.get("sentence") or item.get("sentence_id") or (item.get("id") if isinstance(item.get("id"), str) and re.match(r"p\d+-[bgm]\d+-s\d+$", item.get("id", "")) else None)
    quote = item.get("quote")
    cited_page = item.get("page")
    rec = {"kind": "text", "role": field_role, "cited_page": cited_page}
    s = None
    if sid:
        s = src.by_id.get(sid)
        if s is None:
            rec.update({"match": "not_found", "quote": quote or "", "sentence_id": sid, "error": f"sentence id {sid} not in sentences.jsonl"})
            return rec
        if quote and C.norm(quote) not in C.norm(s["text"]):
            # quote given but not inside that sentence: search elsewhere
            s2 = sentence_hits(src, quote)
            if s2:
                s = s2[0]
                rec["sentence_id_corrected_from"] = sid
            else:
                s = None
        if s is not None:
            q = quote or s["text"]
            rec.update({"match": "exact_sentence", "quote": norm_keep(q, s["text"]), "sentence_id": s["id"]})
    if s is None and quote:
        hits = sentence_hits(src, quote, [cited_page] if cited_page else None) or sentence_hits(src, quote)
        if not hits and len(C.norm(quote)) > 40:  # quote spanning sentence boundaries: search paragraphs
            for p in src.doc.get("paragraphs", []):
                if C.norm(quote) in C.norm(p.get("text", "")):
                    first = (p.get("sources") or [{}])[0]
                    s = {"id": None, "page": first.get("page"), "block_id": first.get("block_id"),
                         "blocks": [x.get("block_id") for x in p.get("sources", [])], "section_path": p.get("section_path"),
                         "text": quote, "pages": sorted({x.get("page") for x in p.get("sources", [])})}
                    rec.update({"match": "exact_paragraph", "quote": quote})
                    break
        elif hits:
            s = hits[0]
            rec.update({"match": "exact_sentence" if C.norm(quote) == C.norm(s["text"]) else "exact_substring",
                        "quote": norm_keep(quote, s["text"]), "sentence_id": s["id"]})
            if len(hits) > 1:
                rec["also_found_at"] = [h["id"] for h in hits[1:5]]
        if s is None:  # raw page text (captions, tables, removed margins)
            d = src.fitz_doc()
            pages = ([cited_page] if cited_page else []) + (list(range(1, d.page_count + 1)) if d else [])
            for pg in pages:
                if C.norm(quote) and C.norm(quote) in C.norm(src.page_text(pg)):
                    s = {"id": None, "page": pg, "block_id": None, "blocks": None, "section_path": None, "text": quote, "pages": [pg]}
                    rec.update({"match": "exact_page_text", "quote": quote,
                                "note": "found in the raw page text, not in a body sentence (caption/table/margin?)"})
                    break
        if s is None:  # fuzzy fallback, flagged
            best, bs = 0.0, None
            nq = C.norm(quote)
            for cand in src.sents:
                if cited_page and abs(cand["page"] - cited_page) > 1:
                    continue
                r = difflib.SequenceMatcher(None, nq, C.norm(cand["text"])).ratio()
                if r > best:
                    best, bs = r, cand
            if bs is not None and best >= 0.85:
                s = bs
                rec.update({"match": "fuzzy", "similarity": round(best, 3), "quote": quote, "closest_text": bs["text"], "sentence_id": bs["id"]})
            else:
                rec.update({"match": "not_found", "quote": quote, "closest_text": bs["text"] if bs else None,
                            "similarity": round(best, 3)})
                return rec
    if s is None:
        rec.update({"match": "not_found", "quote": quote or "", "error": "evidence item has neither a valid sentence id nor a quote"})
        return rec
    rec["page"] = s["page"]
    if cited_page and cited_page != s["page"] and cited_page not in (s.get("pages") or []):
        rec["page_corrected_from"] = cited_page
    rec["pages"] = s.get("pages") or [s["page"]]
    rec["block_id"] = s.get("block_id")
    rec["blocks"] = s.get("blocks")
    rec["section_path"] = s.get("section_path")
    rec["sentence_text"] = s["text"] if rec.get("quote") != s["text"] else None
    loc = None
    for pg in rec["pages"]:
        r = quote_rects(src, pg, rec["quote"], s.get("blocks"))
        if r:
            loc = (pg, r)
            break
    if loc:
        rec["bbox"], rec["rects"], rec["bbox_page"] = loc[1]["bbox"], loc[1]["rects"], loc[0]
        rec["bbox_precision"] = "quote_partial" if loc[1]["partial"] else "quote"
    else:
        b = src.blocks.get(s.get("block_id") or "")
        rec["bbox"], rec["bbox_page"] = (b or {}).get("bbox"), s["page"]
        rec["bbox_precision"] = "block" if b else "page"
    rec["citations"] = resolve_citations(src, rec["quote"])
    rec["text_source"] = next((p.get("source") for p in src.doc.get("pages", []) if p.get("page") == s["page"]), "text_layer")
    return rec


def norm_keep(q, sentence):
    """Return the quote as printed in the sentence (keeps the PDF's own spelling)."""
    nq, ns = C.norm(q), C.norm(sentence)
    if nq == ns:
        return C.norm(sentence, lower=False)
    i = ns.find(nq)
    orig = C.norm(sentence, lower=False)
    if i >= 0 and len(orig) == len(ns):
        return orig[i:i + len(nq)]
    return q


def resolve_table(src, item, role):
    import table_query
    row = item.get("row") or {}
    conds = [f"{k}={v}" for k, v in row.items()] if isinstance(row, dict) else list(row)
    r = table_query.query(src.doc, item.get("table"), conds, item.get("column") or "")
    rec = {"kind": "table_cell", "role": role, "table": item.get("table"), "row": row, "column": item.get("column"),
           "cited_page": item.get("page")}
    if r.get("status") != "ok":
        rec.update({"match": "not_found", "quote": item.get("value") or "", "error": r.get("status"),
                    "candidates": r.get("candidates") or r.get("available") or r.get("columns")})
        return rec
    val = r.get("value")
    given = item.get("value")
    rec.update({"match": "table_cell" if (not given or C.norm(str(given)) == C.norm(val)) else "not_found",
                "quote": val, "value_pretty": r.get("value_pretty"), "page": r.get("page"), "pages": [r.get("page")],
                "bbox": r.get("cell_bbox"), "bbox_page": r.get("page"), "bbox_precision": "table_cell",
                "table_id": r.get("table_id"), "column_path": r.get("column_path"), "row_key": r.get("row_key"),
                "table_caption": next((t.get("caption") for t in src.doc.get("tables", []) if t.get("id") == r.get("table_id")), None),
                "table_context": " ".join(str(x) for x in (r.get("row_values") or [])) + " " + " ".join(str(x) for x in (r.get("column_path") or []))
                                 + " " + (next((t.get("caption") or "" for t in src.doc.get("tables", []) if t.get("id") == r.get("table_id")), "")),
                "citations": []})
    if rec["match"] == "not_found":
        rec["error"] = f"given value {given!r} != parsed cell {val!r}"
    if item.get("page") is not None and item["page"] != rec["page"]:
        rec.update(match="not_found", error=f"cited page {item['page']} != cell page {rec['page']}")
    if r.get("footnote_marker"):
        rec["footnote_marker"] = r["footnote_marker"]
    return rec


def resolve_image(src, item, role):
    rec = {"kind": "image_region", "role": role, "page": item.get("page"), "pages": [item.get("page")], "bbox": item.get("bbox"),
           "bbox_page": item.get("page"), "bbox_precision": "image_region", "crop": item.get("crop"),
           "transcription": item.get("transcription"), "quote": item.get("transcription") or "", "match": "image_read",
           "read_by": item.get("read_by", "agent_vision"), "citations": []}
    prov = item.get("provenance")
    if prov and src.doc.get("meta", {}).get("sha256") and prov.get("source_pdf_sha256") not in (None, src.doc["meta"]["sha256"]):
        rec["match"] = "not_found"
        rec["error"] = "crop provenance points to another PDF"
    rec["provenance"] = prov
    return rec


def resolve_item(src, item, default_role="support"):
    if isinstance(item, (int, float)) and not isinstance(item, bool):
        item = {"quote": str(item)}
    if isinstance(item, str):
        item = {"sentence": item} if re.match(r"p\d+-[bgm]\d+-s\d+$", item.strip()) else {"quote": item}
    if not isinstance(item, dict):
        raise ValueError(f"evidence item must be a sentence id, a quote or an object, got {type(item).__name__}")
    role = item.get("role", default_role)
    t = item.get("type")
    if t == "table_cell" or "table" in item and "row" in item:
        r = resolve_table(src, item, role)
    elif t == "image_region":
        r = resolve_image(src, item, role)
    else:
        r = resolve_text(src, item, role)
    r["evidence_id"] = "ev-" + C.short_hash(C.src_hash(src.md), r.get("page"), C.norm(r.get("quote") or ""), r.get("kind"), n=10)
    if item.get("note"):
        r["note"] = item["note"]
    return r


# ------------------------------------------------------------------ records
def slot(x, kind=None):
    if isinstance(x, dict):
        d = {"label": x.get("label") or x.get("name") or "", "type": x.get("type") or kind}
        if x.get("iri"):
            d["iri"] = x["iri"]
        return d
    return {"label": x or "", "type": kind}


_PRED_ALIAS = {"has": "has_property", "is": "is_a", "isa": "is_a", "value": "has_value", "has_measured_value": "has_value",
               "reports": "has_value", "equals": "has_value", "outperform": "outperforms", "beats": "outperforms",
               "better_than": "outperforms", "worse_than": "underperforms", "greater_than": "higher_than", "more_than": "higher_than",
               "less_than": "lower_than", "smaller_than": "lower_than", "similar_to": "comparable_to", "lead_to": "causes",
               "leads_to": "causes", "results_in": "causes", "correlated_with": "correlates_with", "associates_with": "associated_with",
               "contains": "has_part", "consists_of": "has_part", "includes": "has_part", "define": "defined_as", "is_defined_as": "defined_as",
               "evaluated_using": "evaluated_on", "tested_on": "evaluated_on", "observed": "observed_in", "detected": "detected_in",
               "not_improves": "does_not_improve", "not_associated": "not_associated_with"}
_ENUM_ALIAS = {
    "modality": ({"asserted", "hedged", "negated", "hypothesis"},
                 {"assert": "asserted", "fact": "asserted", "hedge": "hedged", "uncertain": "hedged", "negative": "negated",
                  "negation": "negated", "hypothesised": "hypothesis", "hypothesized": "hypothesis"}, "asserted"),
    "scope": ({"this_work", "cited_work", "background"},
              {"this": "this_work", "own": "this_work", "this_paper": "this_work", "cited": "cited_work", "prior_work": "cited_work",
               "related_work": "cited_work", "general": "background"}, "this_work"),
    "evidence_type": ({"direct", "inferred", "cited"},
                      {"explicit": "direct", "quote": "direct", "inference": "inferred", "indirect": "inferred", "derived": "inferred",
                       "computed": "inferred", "citation": "cited"}, "direct"),
}


def normalize_assertion(a, idx, vocab):
    """Auto-fix spelling of ids / controlled terms instead of failing on them; returns the list of changes."""
    notes = []
    if not a.get("id"):
        a["id"] = f"a{idx}"
        notes.append(f"id missing -> {a['id']}")
    preds = vocab["predicates"]
    p = a.get("predicate")
    if isinstance(p, str) and p not in preds:
        k = re.sub(r"[\s\-]+", "_", p.strip().lower())
        cand = [k, _PRED_ALIAS.get(k), k + "s", k[:-1] if k.endswith("s") else None, k + "_to", k + "_in", k + "_on"]
        hit = next((c for c in cand if c and c in preds), None)
        if hit:
            a["predicate"] = hit
            notes.append(f"predicate {p!r} -> {hit!r}")
    import difflib
    for key, (allowed, alias, default) in _ENUM_ALIAS.items():
        v = a.get(key)
        if v in (None, "") or v in allowed:
            continue
        k = re.sub(r"[\s\-]+", "_", str(v).strip().lower())
        if k in allowed:
            new = k
        elif k in alias:
            new = alias[k]
        else:
            close = difflib.get_close_matches(k, sorted(allowed), n=1, cutoff=0.75)
            new = close[0] if close else default
        a[key] = new
        notes.append(f"{key} {v!r} -> {new!r}")
    if a.get("scope") == "cited_work" and not a.get("evidence_type"):
        a["evidence_type"] = "cited"
    if isinstance(a.get("evidence"), (str, dict)):
        a["evidence"] = [a["evidence"]]
        notes.append("evidence wrapped in a list")
    return notes


def build_record(src, a, idx, vocab, creator, created, draft_unreported=()):
    pred = a.get("predicate")
    pinfo = vocab["predicates"].get(pred, {})
    raw_q = a.get("qualifiers")
    quals = {str(k): v for k, v in raw_q.items() if v not in (None, "", [], {})} if isinstance(raw_q, dict) else {}
    for k, v in list(quals.items()):
        if isinstance(v, dict):
            quals[k] = json.dumps(v, ensure_ascii=False, sort_keys=True)
    ev_raw = a.get("evidence") or []
    ev_items = ev_raw if isinstance(ev_raw, list) else [ev_raw]
    ctx_raw = a.get("context") or []
    ctx_items = ctx_raw if isinstance(ctx_raw, list) else [ctx_raw]
    ev = [resolve_item(src, e) for e in ev_items] + [resolve_item(src, e, "context") for e in ctx_items]
    md = src.md
    sha = C.src_hash(md)
    s, o = slot(a.get("subject")), slot(a.get("object"))
    dedup = "|".join([C.norm(s["label"]), pred or "", C.norm(o["label"]), C.num_core(str(quals.get("value", ""))) if quals.get("value") not in (None, "") else ""])
    np_id = "np-" + C.short_hash(sha, dedup, C.norm(a.get("text") or ""), a.get("scope") or "this_work", a.get("modality") or "asserted",
                                 a.get("evidence_type") or "direct", json.dumps([C.norm(e.get("quote") or "") for e in ev], ensure_ascii=False), n=12)
    cited = a.get("cited_reference")
    cited = str(cited) if cited not in (None, "") else None
    cited_res = None
    if cited:
        cr = resolve_citations(src, cited if re.match(r"[\[(]", cited) else f"[{cited}]")
        refs = [r for c in cr for r in c["references"] if r.get("text")]
        cited_res = refs or None
    unreported = [u.get("field") for u in list(a.get("unreported") or []) + list(draft_unreported) if isinstance(u, dict)]
    rec = {
        "np_id": np_id,
        "uri": TEMP + np_id,
        "assertion": {
            "id": a.get("id") or f"a{idx}",
            "text": a.get("text"),
            "subject": s, "predicate": {"id": pred, "label": pinfo.get("label"), "relation_type": pinfo.get("relation_type"),
                                        "polarity": pinfo.get("polarity"), "iri": vocab["namespace"] + (pred or "unknown")},
            "object": o,
            "qualifiers": quals,
            "modality": a.get("modality") or "asserted",
            "scope": a.get("scope") or "this_work",
            "cited_reference": cited, "cited_reference_resolved": cited_res,
        },
        "evidence_type": a.get("evidence_type") or "direct",
        "eco": dict(zip(("code", "label"), eco_code(a.get("evidence_type") or "direct", a.get("scope") or "this_work", pinfo.get("relation_type")))),
        "reasoning": a.get("reasoning"),
        "derived_from": a.get("derived_from"),
        "evidence": ev,
        "provenance": {
            "source": {k: md.get(k) for k in ("title", "authors", "year", "venue", "doi", "arxiv_id", "version", "arxiv_date", "pdf_sha256",
                                              "file_sha256", "source_format", "material_role", "page_count", "file_name") if md.get(k) is not None},
            "source_name": getattr(src, "name", None),
            "source_iri": C.source_iri(md),
            "evidence_locations": [{"evidence_id": e["evidence_id"], "page": e.get("page"), "block_id": e.get("block_id"),
                                    "sentence_id": e.get("sentence_id"), "bbox": e.get("bbox")} for e in ev],
            "generated_by": C.TOOL, "method": "LLM agent wrote the assertion; scripts resolved evidence to the PDF and ran rule checks",
            "unreported_fields": unreported,
        },
        "pubinfo": {"created": created, "creator": creator, "status": "draft: unsigned, not published to a nanopub server",
                    "note": "uri uses the nanopub temp namespace; it is not a Trusty URI"},
        "dedup_key": dedup,
        "notes": a.get("notes"),
    }
    return rec


# ------------------------------------------------------------------ RDF
_IRI_BAD = re.compile(r'[\x00-\x20<>"{}|^`\\]')


def iri(x):
    """IRIREF-safe: characters N-Quads/TriG forbid in IRIs are percent-encoded."""
    x = str(x)
    return ("iri", _IRI_BAD.sub(lambda m: "".join("%%%02X" % b for b in m.group(0).encode("utf-8")), x))


def lit(v, dt=None, lang=None):
    return ("lit", str(v), dt, lang)


def typed(v):
    if isinstance(v, bool):
        return lit(str(v).lower(), XSD + "boolean")
    if isinstance(v, int):
        return lit(v, XSD + "integer")
    if isinstance(v, float):
        return lit(repr(v), XSD + "decimal")
    s = str(v)
    if re.fullmatch(r"[-+]?\d+", s):
        return lit(s, XSD + "integer")
    if re.fullmatch(r"[-+]?\d*\.\d+", s):
        return lit(s, XSD + "decimal")
    return lit(s)


def to_quads(rec, vocab):
    T = vocab["namespace"]
    base = rec["uri"]
    sub = base + "#"
    H, A, P, I = sub + "Head", sub + "assertion", sub + "provenance", sub + "pubinfo"
    sha = C.src_hash(rec["provenance"]["source"]) or "unknown"
    ent = TEMP + "entity/" + sha[:12] + "/"
    q = []
    add = lambda s, p, o, g: q.append((iri(s), iri(p), o, iri(g)))
    add(base, RDF + "type", iri(NP + "Nanopublication"), H)
    add(base, NP + "hasAssertion", iri(A), H)
    add(base, NP + "hasProvenance", iri(P), H)
    add(base, NP + "hasPublicationInfo", iri(I), H)
    a = rec["assertion"]
    s_iri = a["subject"].get("iri") or ent + C.slug(a["subject"]["label"])
    o_iri = a["object"].get("iri") or ent + C.slug(a["object"]["label"])
    p_iri = a["predicate"]["iri"]
    claim = sub + "claim"
    if a["modality"] in ("asserted",) and a["scope"] == "this_work":
        add(s_iri, p_iri, iri(o_iri), A)  # the plain triple only for unhedged own findings
    add(s_iri, RDFS + "label", lit(a["subject"]["label"]), A)
    add(o_iri, RDFS + "label", lit(a["object"]["label"]), A)
    add(claim, RDF + "type", iri(T + "AtomicClaim"), A)
    add(claim, RDFS + "label", lit(a["text"] or ""), A)
    add(claim, T + "subject", iri(s_iri), A)
    add(claim, T + "predicate", iri(p_iri), A)
    add(claim, T + "object", iri(o_iri), A)
    add(claim, T + "modality", lit(a["modality"]), A)
    add(claim, T + "scope", lit(a["scope"]), A)
    for k, v in sorted(a["qualifiers"].items()):
        for vv in (v if isinstance(v, list) else [v]):
            add(claim, T + C.slug(k, 40).replace("-", "_"), typed(vv) if k == "value" else lit(vv), A)
    src_iri = rec["provenance"]["source_iri"]
    add(A, PROV + "wasDerivedFrom", iri(src_iri), P)
    add(A, T + "evidenceType", lit(rec["evidence_type"]), P)
    if rec.get("eco"):
        add(A, T + "ecoEvidenceCode", lit(rec["eco"]["code"]), P)
    if rec.get("reasoning"):
        add(A, T + "reasoning", lit(rec["reasoning"]), P)
    act = sub + "extraction"
    add(A, PROV + "wasGeneratedBy", iri(act), P)
    add(act, RDF + "type", iri(PROV + "Activity"), P)
    add(act, PROV + "used", iri(src_iri), P)
    add(act, T + "tool", lit(C.TOOL), P)
    add(act, T + "extractionConfidence", typed(float(rec.get("confidence", {}).get("score", 0.0))), P)
    add(act, T + "qualityVerdict", lit(rec.get("quality", {}).get("verdict", "unchecked")), P)
    for k, e in enumerate(rec["evidence"], 1):
        ev = sub + f"evidence{k}"
        add(A, T + "hasEvidence", iri(ev), P)
        add(ev, RDF + "type", iri(T + "EvidenceSnippet"), P)
        add(ev, PROV + "value", lit(e.get("quote") or ""), P)
        add(ev, PROV + "wasQuotedFrom", iri(src_iri), P)
        add(ev, T + "role", lit(e.get("role", "support")), P)
        add(ev, T + "kind", lit(e.get("kind")), P)
        add(ev, T + "match", lit(e.get("match")), P)
        if e.get("page") is not None:
            add(ev, T + "page", typed(int(e["page"])), P)
        for key, pk in (("block_id", "blockId"), ("sentence_id", "sentenceId"), ("table_id", "tableId")):
            if e.get(key):
                add(ev, T + pk, lit(e[key]), P)
        if e.get("kind") == "table_cell":
            add(ev, T + "rowKey", lit(json.dumps(e.get("row_key") or e["row"], ensure_ascii=False, sort_keys=True)), P)
            add(ev, T + "columnPath", lit(" / ".join(e.get("column_path") or [e["column"]])), P)
        if e.get("bbox"):
            add(ev, T + "bbox", lit(",".join(str(x) for x in e["bbox"])), P)
    srcd = rec["provenance"]["source"]
    def val(k):
        return (srcd.get(k) or {}).get("value")
    if val("title"):
        add(src_iri, DCT + "title", lit(val("title")), P)
    for nme in (val("authors") or []):
        add(src_iri, DCT + "creator", lit(nme), P)
    if val("year") and re.fullmatch(r"\d{4}", str(val("year"))):
        add(src_iri, DCT + "issued", lit(val("year"), XSD + "gYear"), P)
    if val("doi"):
        add(src_iri, DCT + "identifier", lit("doi:" + val("doi")), P)
    if val("arxiv_id"):
        add(src_iri, DCT + "identifier", lit("arXiv:" + val("arxiv_id") + (val("version") or "")), P)
    if val("version"):
        add(src_iri, T + "version", lit(val("version")), P)
    add(src_iri, T + "sha256", lit(sha), P)
    if val("source_format") and val("source_format") != "pdf":
        add(src_iri, DCT + "format", lit(val("source_format")), P)
    add(base, DCT + "created", lit(rec["pubinfo"]["created"], XSD + "dateTime"), I)
    add(base, DCT + "creator", lit(rec["pubinfo"]["creator"]), I)
    add(base, RDFS + "label", lit("nanopub: " + (a["text"] or "")[:120]), I)
    add(base, T + "status", lit(rec["pubinfo"]["status"]), I)
    add(base, T + "contentHash", lit(rec.get("content_hash", "")), I)
    return q


def esc(s):
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]", lambda m: "\\u%04X" % ord(m.group(0)), s)


def term_nt(t):
    if t[0] == "iri":
        return "<" + t[1] + ">"
    s = '"' + esc(t[1]) + '"'
    if t[3]:
        return s + "@" + t[3]
    if t[2]:
        return s + "^^<" + t[2] + ">"
    return s


def nquads(quads):
    return "".join(f"{term_nt(s)} {term_nt(p)} {term_nt(o)} {term_nt(g)} .\n" for s, p, o, g in quads)


def term_ttl(t):
    if t[0] == "iri":
        for pfx, ns in PREFIXES.items():
            if t[1].startswith(ns) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]*", t[1][len(ns):] or "-"):
                return f"{pfx}:{t[1][len(ns):]}"
        return "<" + t[1] + ">"
    return term_nt(t)


def trig(all_quads):
    out = [f"@prefix {p}: <{ns}> ." for p, ns in PREFIXES.items()] + [""]
    graphs = {}
    for s, p, o, g in all_quads:
        graphs.setdefault(g[1], []).append((s, p, o))
    for g, triples in graphs.items():
        out.append(f"<{g}> {{")
        for s, p, o in triples:
            out.append(f"  {term_ttl(s)} {term_ttl(p)} {term_ttl(o)} .")
        out.append("}")
        out.append("")
    return "\n".join(out)


def jsonld(all_quads):
    graphs = {}
    for s, p, o, g in all_quads:
        node = graphs.setdefault(g[1], {}).setdefault(s[1], {"@id": s[1]})
        if p[1] == RDF + "type" and o[0] == "iri":
            node.setdefault("@type", []).append(o[1])
            continue
        v = {"@id": o[1]} if o[0] == "iri" else ({"@value": o[1], "@language": o[3]} if o[3] else
                                                ({"@value": o[1], "@type": o[2]} if o[2] else {"@value": o[1]}))
        node.setdefault(p[1], []).append(v)
    return [{"@id": g, "@graph": list(nodes.values())} for g, nodes in graphs.items()]


_IRIREF = r'<[^\x00-\x20<>"{}|^`\\]*>'
NQ_RE = re.compile(r'^(' + _IRIREF + r') (' + _IRIREF + r') (' + _IRIREF + r'|"(?:[^"\\\n\r]|\\[tbnrf"\'\\]|\\u[0-9A-Fa-f]{4}|\\U[0-9A-Fa-f]{8})*"(?:\^\^' + _IRIREF + r'|@[A-Za-z]+(?:-[A-Za-z0-9]+)*)?) (' + _IRIREF + r') \.$')


def roundtrip(nq_text, jl, n_quads, trig_text):
    lines = [l for l in nq_text.splitlines() if l.strip()]
    bad = [l for l in lines if not NQ_RE.match(l)]
    jl_count = 0
    for g in jl:
        for node in g["@graph"]:
            for k, v in node.items():
                if k == "@id":
                    continue
                jl_count += len(v)
    res = {"nquads_lines": len(lines), "nquads_malformed": len(bad), "jsonld_statements": jl_count, "expected": n_quads,
           "ok": not bad and len(lines) == n_quads == jl_count}
    try:  # optional stronger check
        import rdflib  # type: ignore
        ds = rdflib.Dataset()
        ds.parse(data=trig_text, format="trig")
        n = sum(1 for _ in ds.quads((None, None, None, None)))
        res["rdflib_trig_quads"] = n
        res["ok"] = res["ok"] and n == n_quads
    except ImportError:
        res["rdflib_trig_quads"] = "rdflib not installed (TriG checked only via the N-Quads twin)"
    except Exception as e:
        res["rdflib_trig_quads"] = f"parse error: {e}"
        res["ok"] = False
    return res


# ------------------------------------------------------------------ previews
def render_previews(src, records, outdir, max_pages=15, dpi=110):
    d = src.fitz_doc()
    if not d:
        return []
    os.makedirs(outdir, exist_ok=True)
    per_page = {}
    for r in records:
        for e in r["evidence"]:
            pg = e.get("bbox_page") or e.get("page")
            if pg and (e.get("rects") or e.get("bbox")):
                per_page.setdefault(pg, []).append((r, e))
    files = []
    for pg in sorted(per_page)[:max_pages]:
        page = d[pg - 1]
        shape = page.new_shape()
        labels = []
        for r, e in per_page[pg]:
            col = (0.85, 0.1, 0.1) if e.get("role", "support") == "support" else (0.1, 0.3, 0.9)
            if r.get("quality", {}).get("verdict") == "fail":
                col = (0.9, 0.5, 0.0)
            for rc in (e.get("rects") or [e["bbox"]]):
                shape.draw_rect(C.fitz.Rect(rc))
                shape.finish(color=col, width=1.2)
            b = e.get("bbox")
            labels.append((b, r["assertion"]["id"], col))
        shape.commit()
        for b, lab, col in labels:
            try:
                page.insert_text((max(2, b[0] - 2), max(8, b[1] - 1.5)), lab, fontsize=6.5, color=col)
            except Exception:
                pass
        pix = page.get_pixmap(dpi=dpi)
        fp = os.path.join(outdir, f"page_{pg:03d}.png")
        pix.save(fp)
        files.append(fp)
    # reopen document so drawings do not leak into later reads
    src._fitz = None
    return files


def render_crops(src, records, outdir, max_n=60, dpi=150):
    d = src.fitz_doc()
    if not d:
        return
    os.makedirs(outdir, exist_ok=True)
    n = 0
    for r in records:
        for k, e in enumerate(r["evidence"], 1):
            pg, b = e.get("bbox_page") or e.get("page"), e.get("bbox")
            if not (pg and b) or n >= max_n:
                continue
            page = d[pg - 1]
            clip = C.fitz.Rect(b[0] - 12, b[1] - 10, b[2] + 12, b[3] + 10) & page.rect
            fp = os.path.join(outdir, f"{r['assertion']['id']}_e{k}_p{pg}.png")
            page.get_pixmap(dpi=dpi, clip=clip).save(fp)
            e["crop"] = os.path.relpath(fp, os.path.dirname(outdir)).replace("\\", "/")
            n += 1


# ------------------------------------------------------------------ result.json
def flat_assertion(r):
    a = r["assertion"]
    return {"assertion_id": a["id"], "np_id": r["np_id"], "text": a["text"], "subject": a["subject"]["label"],
            "predicate": a["predicate"]["id"], "relation_type": a["predicate"]["relation_type"], "object": a["object"]["label"],
            "qualifiers": a["qualifiers"], "modality": a["modality"], "scope": a["scope"], "evidence_type": r["evidence_type"],
            "evidence_ids": [e["evidence_id"] for e in r["evidence"]],
            "evidence": [{"evidence_id": e["evidence_id"], "role": e.get("role"), "quote": e.get("quote"), "page": e.get("page"),
                          "block_id": e.get("block_id"), "sentence_id": e.get("sentence_id"), "bbox": e.get("bbox"), "match": e.get("match")}
                         for e in r["evidence"]],
            "confidence": r["confidence"]["score"], "verdict": r["quality"]["verdict"],
            "issues": [f"{i['rule']}:{i['severity']}" for i in r["quality"]["issues"]]}


def flat_evidence(records):
    out = []
    for r in records:
        for e in r["evidence"]:
            d = {k: e.get(k) for k in ("evidence_id", "role", "kind", "quote", "page", "pages", "block_id", "sentence_id", "section_path",
                                       "bbox", "bbox_precision", "match", "table", "row", "column", "crop", "citations")
                 if e.get(k) not in (None, [], "")}
            d["assertion_id"] = r["assertion"]["id"]
            d["evidence_type"] = r["evidence_type"]
            out.append(d)
    return out


def provenance_view(src, others=()):
    extra = [{"source_name": o.name, "source_iri": C.source_iri(o.md), **{k: v.get("value") for k, v in o.md.items() if isinstance(v, dict)}}
             for o in others if o is not src]
    out = provenance_view_one(src)
    if extra:
        out["additional_sources"] = extra
    return out


def provenance_view_one(src):
    return {"source_iri": C.source_iri(src.md), "source_name": getattr(src, "name", None),
            **{k: v.get("value") for k, v in src.md.items() if isinstance(v, dict)},
            "field_sources": {k: {kk: vv for kk, vv in v.items() if kk != "value"} for k, v in src.md.items() if isinstance(v, dict)}}


DT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


RESERVED_FIELDS = {"case_id", "sources", "field_status", "field_evidence", "field_problems", "output_contract_spec",
                   "run_status", "change_log", "status_reason", "build_errors", "exports", "export_roundtrip",
                   "quality_summary", "source_metadata", "quarantined_assertions", "run_warnings", "field_review"}
_KIND_TYPES = {
    "nanopublications": {"array"}, "assertions": {"array"}, "evidence": {"array"},
    "relations": {"array"}, "unreported": {"array"}, "conflicts": {"array"}, "uncertainty": {"array"},
    "provenance": {"object"}, "quality": {"object"}, "summary": {"object"},
    "trig": {"string"}, "nquads": {"string"}, "jsonld": {"string"}, "review": {"string"}, "status": {"string"},
}


def _write_text_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def refuse(out, msg, code=2, **extra):
    """Stop without touching result.json; leave the reason in OUT/build_error.json."""
    info = {"status": "build_refused", "error": msg, "result_json": "unchanged", "tool": C.TOOL, "at": now_iso(), **extra}
    try:
        C.dump_json(info, os.path.join(out, "build_error.json"))
    except Exception:
        pass
    print(json.dumps(info, ensure_ascii=False, indent=1))
    sys.exit(code)


def load_draft(path, out):
    try:
        draft = C.load_json(path)
    except FileNotFoundError:
        refuse(out, f"draft not found: {path}")
    except json.JSONDecodeError as e:
        refuse(out, f"draft is not valid JSON: {e.msg} (line {e.lineno}, column {e.colno})",
               hint="fix the JSON syntax and re-run; nothing was overwritten")
    except (OSError, UnicodeDecodeError) as e:
        refuse(out, f"draft unreadable: {e}")
    if not isinstance(draft, dict):
        refuse(out, f"draft top level must be an object, got {type(draft).__name__}")
    if "assertions" in draft and not isinstance(draft["assertions"], list):
        refuse(out, f"draft.assertions must be a list, got {type(draft['assertions']).__name__}")
    if "fields" in draft and not isinstance(draft["fields"], dict):
        refuse(out, f"draft.fields must be an object mapping contract field -> spec, got {type(draft['fields']).__name__}")
    for rk in draft.get("fields") or {}:
        if rk in RESERVED_FIELDS:
            refuse(out, f"draft.fields cannot overwrite reserved runtime key {rk!r}")
    if "source" in draft and draft["source"] is not None and (not isinstance(draft["source"], str) or not draft["source"].strip()):
        refuse(out, "draft.source must be a nonempty parsed source stem")
    draft.setdefault("assertions", [])
    return draft


def _union(a, b):
    out = list(a or [])
    for x in b or []:
        if x not in out:
            out.append(x)
    return out


def merge_drafts(old, new):
    """New assertions replace old ones with the same id, others are appended; 'delete' removes ids."""
    m = copy.deepcopy(old)
    delete = {str(x) for x in (new.get("delete") or [])}
    new_list = [x for x in new.get("assertions") or []]
    new_by = {x.get("id"): x for x in new_list if isinstance(x, dict) and x.get("id")}
    used, merged = set(), []
    for x in old.get("assertions") or []:
        xid = x.get("id") if isinstance(x, dict) else None
        if xid in delete:
            continue
        if xid in new_by:
            merged.append(new_by[xid])
            used.add(xid)
        else:
            merged.append(x)
    for x in new_list:
        xid = x.get("id") if isinstance(x, dict) else None
        if xid not in used and xid not in delete:
            merged.append(x)
    m["assertions"] = merged
    for k in ("fields", "source_overrides"):
        m[k] = {**(old.get(k) or {}), **(new.get(k) or {})}
    for k in ("unreported", "conflicts", "uncertainty"):
        m[k] = _union(old.get(k), new.get(k))
    for k, v in new.items():
        if k not in ("assertions", "fields", "source_overrides", "unreported", "conflicts", "uncertainty", "delete"):
            m[k] = v
    return m


def built_count(res):
    for c in reversed((res or {}).get("change_log") or []):
        if isinstance(c, dict) and c.get("step") == "np_build":
            return int(c.get("n_assertions") or 0)
    return 0


def main():
    C.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--draft", default="/app/work/draft.json")
    ap.add_argument("--out", default="/app/output")
    ap.add_argument("--parsed", default=None, help="parsed dir (default: OUT/parsed/<draft.source>, else the primary material)")
    ap.add_argument("--creator", default=os.environ.get("NP_CREATOR", "LLM agent with nanopub-evidence-builder"))
    ap.add_argument("--created", default=None, help="fixed timestamp for reproducible output (xsd:dateTime)")
    ap.add_argument("--no-previews", action="store_true")
    ap.add_argument("--merge", action="store_true", help="merge this draft onto OUT/draft.built.json by assertion id")
    ap.add_argument("--allow-shrink", action="store_true", help="allow replacing a built result with far fewer / zero records")
    a = ap.parse_args()
    t0 = time.time()
    os.makedirs(a.out, exist_ok=True)
    draft = load_draft(a.draft, a.out)
    built_path = os.path.join(a.out, "draft.built.json")
    merged_from = None
    if a.merge and os.path.exists(built_path):
        try:
            draft = merge_drafts(C.load_json(built_path), draft)
            merged_from = built_path
        except Exception as e:
            refuse(a.out, f"--merge: cannot read {built_path}: {e}")
    vocab = C.load_vocab()
    notes = []
    created = a.created or os.environ.get("NP_CREATED") or now_iso()
    if not DT_RE.match(str(created)):
        refuse(a.out, f"--created {created!r} is not an xsd:dateTime")
    rp = os.path.join(a.out, "result.json")
    prev = None
    if os.path.exists(rp):
        try:
            prev = C.load_json(rp)
        except Exception as e:
            notes.append({"issue": f"previous result.json unreadable ({e}); it was kept as result.corrupt.json"})
            try:
                import shutil
                shutil.copy(rp, os.path.join(a.out, "result.corrupt.json"))
            except Exception:
                pass
    dirs = find_parsed_dirs(a.out)
    pdir = a.parsed
    if not pdir:
        want = draft.get("source")
        pdir = next((d for d in dirs if os.path.basename(d) == want), None)
        if pdir is None and want:
            refuse(a.out, f"draft.source {want!r} is not one of the parsed sources: {[os.path.basename(d) for d in dirs]}")
        if pdir is None:
            order = [s.get("parsed_dir") for s in (prev or {}).get("sources") or [] if s.get("parse_status") != "failed"]
            pdir = next((d for d in order if d and os.path.exists(os.path.join(d, "doc.json"))), None) or (dirs[0] if len(dirs) == 1 else None)
    if not pdir:
        refuse(a.out, f"cannot choose a parsed source; set draft.source to one of {[os.path.basename(d) for d in dirs]} or pass --parsed")
    try:
        src = Source(pdir, draft.get("source_overrides"))
    except Exception as e:
        refuse(a.out, f"cannot load parsed source {pdir}: {type(e).__name__}: {e}", hint="re-run np_init.py")
    srcs = {os.path.basename(pdir): src}
    for d in dirs:
        nm = os.path.basename(d)
        if nm not in srcs:
            try:
                srcs[nm] = Source(d, draft.get("source_overrides"))
            except Exception as e:
                notes.append({"issue": f"parsed source {nm} unreadable: {e}"})
    records, build_errors, normalized = [], [], []
    ids = [asr.get("id") or f"a{i}" for i, asr in enumerate(draft["assertions"], 1)
           if isinstance(asr, dict)]
    valid_ids = [aid for aid in ids if isinstance(aid, str)]
    if len(valid_ids) != len(set(valid_ids)):
        refuse(a.out, "assertion IDs must be unique to bind output fields unambiguously")
    for i, asr in enumerate(draft.get("assertions", []), 1):
        aid = asr.get("id") if isinstance(asr, dict) else None
        if not isinstance(asr, dict):
            build_errors.append({"index": i, "id": None, "error": f"assertion must be an object, got {type(asr).__name__}"})
            continue
        try:
            if aid is not None and (not isinstance(aid, str) or not aid.strip()):
                raise ValueError("assertion id must be a nonempty string")
            fixes = normalize_assertion(asr, i, vocab)
            if fixes:
                normalized.append({"assertion": asr.get("id"), "changes": fixes})
            s = src
            if asr.get("source") and asr["source"] != os.path.basename(pdir):
                s = srcs.get(asr["source"])
                if s is None:
                    raise ValueError(f"source {asr['source']!r} is not a parsed source ({sorted(srcs)})")
            elif not asr.get("source") and len(srcs) > 1:
                ev_list = asr.get("evidence") if isinstance(asr.get("evidence"), list) else ([asr.get("evidence")] if asr.get("evidence") else [])
                sid = next((e if isinstance(e, str) else e.get("sentence") or e.get("sentence_id") for e in ev_list if isinstance(e, (str, dict))), None)
                if isinstance(sid, str) and sid not in s.by_id:
                    s = next((cand for cand in srcs.values() if sid in cand.by_id), s)
            records.append(build_record(s, asr, i, vocab, a.creator, created, draft.get("unreported") or []))
        except Exception as e:
            build_errors.append({"index": i, "id": aid, "error": f"{type(e).__name__}: {e}",
                                 "fix_hint": "check this assertion's evidence / qualifiers shape against SKILL.md"})
    prev_n = built_count(prev)
    if prev_n and not a.allow_shrink and (not records or (build_errors and len(records) < prev_n / 2)):
        refuse(a.out, f"refusing to replace a result with {prev_n} built records by one with {len(records)} "
                      f"({len(build_errors)} assertion(s) could not be built)", code=3,
               build_errors=build_errors[:20], hint="fix the draft, use --merge for a partial draft, or --allow-shrink if intended")
    seen = {}
    for r in records:  # identical content -> same id; keep URIs unique in the export (R24 reports the duplicate)
        n = seen.get(r["np_id"], 0) + 1
        seen[r["np_id"]] = n
        if n > 1:
            r["np_id"] = f"{r['np_id']}-{n}"
            r["uri"] = TEMP + r["np_id"]
    qrep = np_check.check_all(records, vocab)
    for r in records:
        r["content_hash"] = "sha256:" + C.short_hash(json.dumps({k: r[k] for k in ("assertion", "evidence_type", "reasoning")}, sort_keys=True, ensure_ascii=False),
                                                      json.dumps([(e.get("quote"), e.get("page")) for e in r["evidence"]], ensure_ascii=False), n=64)
    if not a.no_previews:
        for s in srcs.values():
            recs = [r for r in records if C.src_hash(r["provenance"]["source"]) == C.src_hash(s.md)]
            try:
                render_crops(s, recs, os.path.join(a.out, "evidence_crops"))
                s._fitz = None
                render_previews(s, recs, os.path.join(a.out, "evidence_previews"))
            except Exception as e:  # previews are a convenience, never a reason to lose the result
                notes.append({"issue": f"preview rendering failed for {s.name}: {e}"})
    # Quarantine: records that fail a rule stay in nanopubs.json / quality_report / review.md for audit, but are kept out of
    # the RDF exports and contract views, so one bad assertion does not turn a good run into 'partial'.
    all_records, quarantined = records, []
    failing = [r for r in records if r["quality"]["verdict"] == "fail"]
    if failing:
        for r in failing:
            r["exported"] = False
            quarantined.append({"assertion_id": r["assertion"]["id"], "text": r["assertion"].get("text"),
                                "fail_rules": [f"{i['rule']}: {i['message']}" for i in r["quality"]["issues"] if i["severity"] == "fail"]})
        records = [r for r in records if r["quality"]["verdict"] != "fail"]
    if prev_n and not a.allow_shrink and not records:
        refuse(a.out, f"refusing to replace a result with {prev_n} built records when all {len(all_records)} assertion(s) failed quality checks",
               code=3, quarantined_assertions=quarantined[:20], hint="fix the failing assertions or pass --allow-shrink if intended")
    all_q = []
    for r in records:
        all_q += to_quads(r, vocab)
    nq, tg, jl = nquads(all_q), trig(all_q), jsonld(all_q)
    rt = roundtrip(nq, jl, len(all_q), tg)
    qrep["export_roundtrip"] = rt
    # ---- result.json (build and validate in memory before writing any output files)
    res = copy.deepcopy(prev) if isinstance(prev, dict) else {}
    if "case_id" not in res:
        res["case_id"] = draft.get("case_id")
    spec = res.get("output_contract_spec") or {}
    spec_fields = {f["name"]: f for f in spec.get("fields") or [] if isinstance(f, dict) and f.get("name")}
    fields = dict(draft.get("fields") or {})
    auto = []
    for name, f in spec_fields.items():  # contract fields the draft did not map: use the suggested view if compatible
        sk = f.get("suggested_kind")
        ft = f.get("type")
        ft_set = set(ft) if isinstance(ft, list) else ({ft} if ft else set())
        if name != "case_id" and name not in fields and sk not in (None, "agent_value"):
            if not ft_set or (ft_set & _KIND_TYPES.get(sk, set())):
                fields[name] = {"kind": sk}
                auto.append(name)
    res["field_status"] = {f: None for f in spec_fields if f != "case_id"} if spec_fields else (dict(res.get("field_status")) if isinstance(res.get("field_status"), dict) else {})
    res["field_evidence"] = {}
    res["field_problems"] = {}
    unrep = list(draft.get("unreported") or [])
    keep = lambda k: [x for x in (res.get(k) or []) if isinstance(x, dict) and x.get("origin") == "np_init"]
    conflicts = _union(keep("conflicts"), draft.get("conflicts"))
    conflicts += [{"type": "metadata_override", **o} for s in srcs.values() for o in s.overrides_log if o.get("from") not in (None, o.get("to"))]
    uncertainty = _union(keep("uncertainty"), draft.get("uncertainty")) + notes
    uncertainty += [{"assertion": e["id"], "index": e["index"], "issue": "assertion could not be built: " + e["error"]} for e in build_errors]
    views = {"nanopublications": records, "assertions": [flat_assertion(r) for r in records],
             "evidence": flat_evidence(records), "provenance": provenance_view(src, list(srcs.values())), "quality": qrep,
             "relations": [{"assertion_id": r["assertion"]["id"], "subject": r["assertion"]["subject"]["label"],
                            "predicate": r["assertion"]["predicate"]["id"], "object": r["assertion"]["object"]["label"],
                            "qualifiers": r["assertion"]["qualifiers"]} for r in records],
             "trig": "nanopubs.trig", "nquads": "nanopubs.nq", "jsonld": "nanopubs.jsonld",
             "review": "review.md", "unreported": unrep, "conflicts": conflicts, "uncertainty": uncertainty,
             "summary": {"n_assertions": len(records), "quality": qrep["summary"], "export_roundtrip_ok": rt["ok"],
                         "n_quarantined": len(quarantined), "source_iri": C.source_iri(src.md)},
             "status": "pending"}
    by_aid = {r["assertion"]["id"]: r for r in records}
    res["field_review"] = {}
    for f, fs in fields.items():
        fs = fs if isinstance(fs, dict) else {"value": fs}
        kind = fs.get("kind", "agent_value")
        if "value" in fs:
            value = fs["value"]
            refs = fs.get("from")
            ids = refs if isinstance(refs, list) else ([refs] if refs is not None else [])
            invalid = [i for i in ids if not isinstance(i, str) or i not in by_aid]
            res["field_evidence"][f] = flat_evidence([by_aid[i] for i in ids if isinstance(i, str) and i in by_aid])
            if K.is_empty(value):
                res[f] = None
                res["field_status"][f] = "unreported"
            elif not ids or invalid:
                res[f] = None
                res["field_status"][f] = "needs_review"
                reason = "references failed, unbuilt or unknown assertion IDs" if invalid else "missing supporting assertion IDs"
                res["field_review"][f] = {"value": value, "from": ids, "invalid_refs": invalid, "reason": reason}
                uncertainty.append({"field": f, "issue": reason})
            else:
                res[f] = copy.deepcopy(value)
                res["field_status"][f] = "supported"
        elif kind in views:
            sel = fs.get("filter")
            v = copy.deepcopy(views[kind]) if kind != "nanopublications" else views[kind]
            if sel and isinstance(v, list):
                v = [x for x in v if (x.get("assertion_id") or (x.get("assertion") or {}).get("id")) in sel]
            ren = fs.get("rename")
            if isinstance(ren, dict) and isinstance(v, list):
                v = [{ren.get(k, k): val for k, val in x.items()} if isinstance(x, dict) else x for x in v]
            res[f] = v
            res["field_status"][f] = ("generated" if (not K.is_empty(v) or kind in ("unreported", "conflicts", "uncertainty")) else "unreported") \
                + (" (auto: contract field not in draft.fields)" if f in auto else "")
        else:
            res["field_status"][f] = "unreported"
            if res.get(f) is None and not any(u.get("field") == f for u in unrep if isinstance(u, dict)):
                unrep.append({"field": f, "reason": f"agent did not fill this field (unknown kind {kind!r})" if kind != "agent_value" else "agent did not fill this field"})
    for f in res["field_status"]:
        if f not in fields and res["field_status"][f] is None:
            res["field_status"][f] = "unreported"
            res[f] = None
            if not any(isinstance(u, dict) and u.get("field") == f for u in unrep):
                unrep.append({"field": f, "reason": "agent did not fill this field"})
    res["nanopublications"] = res.get("nanopublications") if "nanopublications" in fields else [flat_assertion(r) for r in records]
    res["source_metadata"] = provenance_view(src, list(srcs.values()))
    res["quality_summary"] = qrep["summary"]
    res["export_roundtrip"] = rt
    res["exports"] = {"nanopubs_json": "nanopubs.json", "trig": "nanopubs.trig", "nquads": "nanopubs.nq", "jsonld": "nanopubs.jsonld",
                      "quality_report": "quality_report.json", "review": "review.md", "previews_dir": "evidence_previews", "crops_dir": "evidence_crops",
                      "effective_draft": "draft.built.json"}
    res["unreported"] = unrep
    res["conflicts"] = conflicts
    res["uncertainty"] = uncertainty
    res["build_errors"] = build_errors
    n_fail = sum(1 for r in records if r["quality"]["verdict"] == "fail")
    res["quarantined_assertions"] = quarantined
    required = set(spec.get("required") or []) if spec_fields else set(res["field_status"])
    for f, fsp in spec_fields.items():  # type / nested-schema check against the contract (lossless coercion only)
        if f == "case_id" or f not in res or res["field_status"].get(f) in (None, "unreported", "needs_review"):
            continue
        sch = fsp.get("schema") if isinstance(fsp.get("schema"), dict) else ({"type": fsp["type"]} if fsp.get("type") else None)
        if not sch or not (sch.get("type") or sch.get("properties") or sch.get("items")):
            continue
        val, probs, cnotes = K.validate(copy.deepcopy(res[f]) if f != "nanopublications" or not isinstance(res[f], list) else res[f], sch, f)
        if cnotes and not probs:
            res[f] = val
            res.setdefault("change_log", []).append({"step": "contract_coercion", "field": f, "notes": cnotes[:10], "at": now_iso()})
        if probs:
            res["field_problems"][f] = probs[:30]
            res["field_status"][f] = "schema_mismatch"
    if "case_id" in spec_fields:
        cs = spec_fields["case_id"]
        _, probs, _ = K.validate(res.get("case_id"), cs.get("schema") or ({"type": cs["type"]} if cs.get("type") else {}), "case_id")
        if probs and res.get("case_id") is not None:
            res["field_problems"]["case_id"] = probs
    agent_unrep = {u.get("field") for u in (draft.get("unreported") or []) if isinstance(u, dict) and u.get("reason")}
    declared_unrep_req = [f for f, st in res["field_status"].items() if f in required and st == "unreported" and f in agent_unrep]
    unfilled_req = [f for f, st in res["field_status"].items() if f in required
                    and st in (None, "unreported", "schema_mismatch", "needs_review")
                    and f not in declared_unrep_req]
    needs_review_req = [f for f, st in res["field_status"].items() if f in required and st == "needs_review"]
    unfilled_opt = [f for f, st in res["field_status"].items() if f not in required and st in (None, "unreported", "schema_mismatch")]
    image_only = [r["assertion"]["id"] for r in records if any(i["rule"] == "R26" for i in r["quality"]["issues"])]
    unread_sources = [
        {"stem": s.get("stem"), "unresolved_pages": s.get("unresolved_pages") or [], "pages_not_parsed": s.get("pages_not_parsed") or []}
        for s in (res.get("sources") or [])
        if s.get("unresolved_pages") or s.get("pages_not_parsed")
    ]
    contract_errors = spec.get("errors") or []
    # Only missing core output makes a run 'partial': no record, a failing record set, an unfilled / mistyped required
    # contract field, or a broken RDF export. Everything else is reported in run_warnings and the status stays 'success'.
    warnings = []
    if quarantined:
        warnings.append({"type": "quarantined_assertions", "ids": [q["assertion_id"] for q in quarantined],
                         "note": "failing records kept out of exports; see quarantined_assertions / review.md"})
    if build_errors:
        warnings.append({"type": "assertions_not_built", "ids": [e["id"] or e["index"] for e in build_errors]})
    if needs_review_req:
        warnings.append({"type": "fields_need_review", "fields": needs_review_req, "note": "value given without a passing 'from' assertion"})
    if declared_unrep_req:
        warnings.append({"type": "required_fields_unreported_by_paper", "fields": declared_unrep_req, "note": "null + reason in unreported"})
    if image_only:
        warnings.append({"type": "image_read_evidence", "ids": image_only, "note": "evidence transcribed from an image; human check advised"})
    if unread_sources:
        warnings.append({"type": "unread_source_pages", "sources": unread_sources, "note": "pages without text layer were not read"})
    if contract_errors:
        warnings.append({"type": "contract_errors", "errors": contract_errors})
    if normalized:
        warnings.append({"type": "auto_normalized", "items": normalized})
    res["run_warnings"] = warnings
    if not records:
        res["status"] = "partial" if res.get("status") != "failed" else "failed"
    else:
        res["status"] = "success" if (n_fail == 0 and not unfilled_req and rt["ok"]) else "partial"
    for f, fs in fields.items():
        if isinstance(fs, dict) and fs.get("kind") == "status" and "value" not in fs:
            res[f] = res["status"]
    res["status_reason"] = {"failing_records": n_fail, "unfilled_fields": unfilled_req, "needs_review_fields": needs_review_req,
                            "unfilled_optional_fields": unfilled_opt,
                            "schema_problems": sorted(res["field_problems"]), "export_ok": rt["ok"],
                            "image_read_records_need_human_review": image_only, "unread_source_pages": unread_sources,
                            "contract_errors": contract_errors, "assertions_not_built": [e["id"] or e["index"] for e in build_errors],
                            "quarantined_assertions": [q["assertion_id"] for q in quarantined], "warnings": len(warnings)}
    if not res["field_problems"]:
        res.pop("field_problems")
    res.setdefault("change_log", []).append({"step": "np_build", "tool": C.TOOL, "draft": a.draft, "merged_onto": merged_from,
                                              "n_assertions": len(records), "n_quarantined": len(quarantined), "n_build_errors": len(build_errors),
                                              "auto_filled_fields": auto,
                                              "verdicts": {k: qrep["summary"][k] for k in ("pass", "warn", "fail")}, "at": now_iso()})
    nanopubs_payload = {"nanopublications": all_records, "vocabulary": "vocab/relations.json", "tool": C.TOOL}
    try:
        json.dumps(res, ensure_ascii=False, allow_nan=False)
        json.dumps(jl, ensure_ascii=False, allow_nan=False)
        json.dumps(nanopubs_payload, ensure_ascii=False, allow_nan=False)
    except Exception as e:
        refuse(a.out, f"output contains non-JSON-serializable values: {e}")
    write_review(os.path.join(a.out, "review.md"), all_records, src, qrep)
    if prev is not None:
        C.dump_json(prev, os.path.join(a.out, "result.prev.json"))
    _write_text_atomic(os.path.join(a.out, "nanopubs.nq"), nq)
    _write_text_atomic(os.path.join(a.out, "nanopubs.trig"), tg)
    C.dump_json(jl, os.path.join(a.out, "nanopubs.jsonld"))
    C.dump_json(nanopubs_payload, os.path.join(a.out, "nanopubs.json"))
    C.dump_json(qrep, os.path.join(a.out, "quality_report.json"))
    C.dump_json(res, rp)
    C.dump_json(draft, built_path)
    be = os.path.join(a.out, "build_error.json")
    if os.path.exists(be):
        os.remove(be)
    print(json.dumps({"n_assertions": len(records), "quarantined": [q["assertion_id"] for q in quarantined], "summary": qrep["summary"], "export_roundtrip": rt, "status": res["status"],
                      "status_reason": res["status_reason"], "build_errors": build_errors, "auto_filled_fields": auto,
                      "field_problems": res.get("field_problems"),
                      "records": [{"id": r["assertion"]["id"], "verdict": r["quality"]["verdict"], "confidence": r["confidence"]["score"],
                                   "issues": [f"{i['rule']} {i['severity']}: {i['message']}" + (f" [{i['trigger']}]" if i.get("trigger") else "")
                                              for i in r["quality"]["issues"]],
                                   "evidence": [f"{e.get('match')} p{e.get('page')} {e.get('sentence_id') or e.get('table_id') or ''}" for e in r["evidence"]]}
                                  for r in all_records],
                      "run_warnings": [w["type"] for w in warnings],
                      "seconds": round(time.time() - t0, 2)}, ensure_ascii=False, indent=1))


def write_review(path, records, src, qrep):
    md = src.md
    L = [f"# Nanopublication review — {(md.get('title') or {}).get('value')}", "",
         f"Source: {C.source_iri(md)} · SHA-256 `{C.src_hash(md)}` · tool {C.TOOL}", "",
         f"Verdicts: pass {qrep['summary']['pass']} · warn {qrep['summary']['warn']} · fail {qrep['summary']['fail']} · export round-trip ok: {qrep['export_roundtrip']['ok']}", "",
         "| id | assertion | S · P · O | evidence (page, location, match) | verdict | conf. | issues |", "|---|---|---|---|---|---|---|"]
    for r in records:
        a = r["assertion"]
        ev = "<br>".join(f"{e.get('role')}: p{e.get('page')} {e.get('sentence_id') or e.get('table_id') or e.get('block_id') or ''} ({e.get('match')}) “{(e.get('quote') or '')[:140]}”" for e in r["evidence"])
        iss = "<br>".join(f"{i['rule']} {i['severity']}: {i['message']}" for i in r["quality"]["issues"]) or "—"
        spo = f"{a['subject']['label']} · {a['predicate']['id']} · {a['object']['label']}" + (f" · {a['qualifiers']}" if a["qualifiers"] else "")
        L.append("| " + " | ".join(x.replace("|", "/").replace("\n", " ") for x in
                                    [a["id"], a["text"] or "", spo, ev, r["quality"]["verdict"], str(r["confidence"]["score"]), iss]) + " |")
    L += ["", "Previews: `evidence_previews/page_NNN.png` (red = support quote, blue = context, orange = failing record).",
          "Crops: `evidence_crops/<assertion>_e<k>_p<page>.png`."]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


if __name__ == "__main__":
    try:
        main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:  # never a bare traceback: keep result.json, say what broke and how to continue
        import traceback
        argv = sys.argv[1:]
        out_dir = argv[argv.index("--out") + 1] if "--out" in argv[:-1] else "/app/output"
        os.makedirs(out_dir, exist_ok=True)
        refuse(out_dir, f"unexpected {type(exc).__name__}: {exc}", code=2,
               traceback=traceback.format_exc().splitlines()[-6:],
               hint="result.json was not changed; simplify or delete the assertion named in the traceback and re-run")
