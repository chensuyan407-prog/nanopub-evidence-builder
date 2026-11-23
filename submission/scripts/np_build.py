#!/usr/bin/env python3
"""Step 2 of nanopub-evidence-builder: turn the agent's draft into checked,
exportable nanopublications and fill result.json.

Usage:
    python np_build.py --draft WORK/draft.json --out OUT [--parsed OUT/parsed/<stem>]
                       [--creator "agent name"] [--created 2026-01-01T00:00:00Z] [--no-previews]

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
        self.pdf = (self.doc.get("meta") or {}).get("source_file")
        if not self.pdf or not os.path.exists(self.pdf):
            self.pdf = None
        self.md = C.load_json(mp) if os.path.exists(mp) else (C.extract_metadata(self.pdf, self.doc) if self.pdf else {})
        self.overrides_log = []
        for k, v in (overrides or {}).items():
            val = v.get("value") if isinstance(v, dict) else v
            old = (self.md.get(k) or {}).get("value")
            self.md[k] = {"value": val, "source": {"kind": "agent_override", "reason": (v.get("reason") if isinstance(v, dict) else None),
                                                    "evidence": (v.get("evidence") if isinstance(v, dict) else None)},
                          "confidence": "medium", "previous_value": old}
            self.overrides_log.append({"field": k, "from": old, "to": val})
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
                if C.norm_loose(quote) and C.norm_loose(quote) in C.norm_loose(src.page_text(pg)):
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
    rec.update({"match": "table_cell" if (not given or C.norm(given) == C.norm(val) or C.norm_loose(given) == C.norm_loose(val)) else "not_found",
                "quote": val, "value_pretty": r.get("value_pretty"), "page": r.get("page"), "pages": [r.get("page")],
                "bbox": r.get("cell_bbox"), "bbox_page": r.get("page"), "bbox_precision": "table_cell",
                "table_id": r.get("table_id"), "column_path": r.get("column_path"), "row_key": r.get("row_key"),
                "table_caption": next((t.get("caption") for t in src.doc.get("tables", []) if t.get("id") == r.get("table_id")), None),
                "table_context": " ".join(str(x) for x in (r.get("row_values") or [])) + " " + " ".join(str(x) for x in (r.get("column_path") or []))
                                 + " " + (next((t.get("caption") or "" for t in src.doc.get("tables", []) if t.get("id") == r.get("table_id")), "")),
                "citations": []})
    if rec["match"] == "not_found":
        rec["error"] = f"given value {given!r} != parsed cell {val!r}"
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
    if isinstance(item, str):
        item = {"sentence": item}
    role = item.get("role", default_role)
    t = item.get("type")
    if t == "table_cell" or "table" in item and "row" in item:
        r = resolve_table(src, item, role)
    elif t == "image_region":
        r = resolve_image(src, item, role)
    else:
        r = resolve_text(src, item, role)
    r["evidence_id"] = "ev-" + C.short_hash(src.md.get("pdf_sha256", {}).get("value"), r.get("page"), C.norm(r.get("quote") or ""), r.get("kind"), n=10)
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


def build_record(src, a, idx, vocab, creator, created, draft_unreported=()):
    pred = a.get("predicate")
    pinfo = vocab["predicates"].get(pred, {})
    quals = {k: v for k, v in (a.get("qualifiers") or {}).items() if v not in (None, "", [])}
    ev_items = a.get("evidence") or []
    ev = [resolve_item(src, e) for e in ev_items] + [resolve_item(src, e, "context") for e in (a.get("context") or [])]
    md = src.md
    sha = (md.get("pdf_sha256") or {}).get("value")
    s, o = slot(a.get("subject")), slot(a.get("object"))
    dedup = "|".join([C.norm(s["label"]), pred or "", C.norm(o["label"]), C.num_core(str(quals.get("value", ""))) if quals.get("value") not in (None, "") else ""])
    np_id = "np-" + C.short_hash(sha, dedup, C.norm(a.get("text") or ""), a.get("scope") or "this_work", a.get("modality") or "asserted",
                                 a.get("evidence_type") or "direct", json.dumps([C.norm(e.get("quote") or "") for e in ev], ensure_ascii=False), n=12)
    cited = a.get("cited_reference")
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
            "source": {k: md.get(k) for k in ("title", "authors", "year", "venue", "doi", "arxiv_id", "version", "arxiv_date", "pdf_sha256", "page_count", "file_name") if md.get(k) is not None},
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
def iri(x):
    return ("iri", x)


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
    sha = ((rec["provenance"]["source"].get("pdf_sha256") or {}).get("value")) or "unknown"
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
        if e.get("bbox"):
            add(ev, T + "bbox", lit(",".join(str(x) for x in e["bbox"])), P)
    srcd = rec["provenance"]["source"]
    def val(k):
        return (srcd.get(k) or {}).get("value")
    if val("title"):
        add(src_iri, DCT + "title", lit(val("title")), P)
    for nme in (val("authors") or []):
        add(src_iri, DCT + "creator", lit(nme), P)
    if val("year"):
        add(src_iri, DCT + "issued", lit(val("year"), XSD + "gYear"), P)
    if val("doi"):
        add(src_iri, DCT + "identifier", lit("doi:" + val("doi")), P)
    if val("arxiv_id"):
        add(src_iri, DCT + "identifier", lit("arXiv:" + val("arxiv_id") + (val("version") or "")), P)
    if val("version"):
        add(src_iri, T + "version", lit(val("version")), P)
    add(src_iri, T + "sha256", lit(sha), P)
    add(base, DCT + "created", lit(rec["pubinfo"]["created"], XSD + "dateTime"), I)
    add(base, DCT + "creator", lit(rec["pubinfo"]["creator"]), I)
    add(base, RDFS + "label", lit("nanopub: " + (a["text"] or "")[:120]), I)
    add(base, T + "status", lit(rec["pubinfo"]["status"]), I)
    add(base, T + "contentHash", lit(rec.get("content_hash", "")), I)
    return q


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


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


NQ_RE = re.compile(r'^(<[^>]*>)\s+(<[^>]*>)\s+(<[^>]*>|"(?:[^"\\]|\\.)*"(?:\^\^<[^>]*>|@[A-Za-z\-]+)?)\s+(<[^>]*>)\s+\.$')


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


def provenance_view(src):
    return {"source_iri": C.source_iri(src.md),
            **{k: v.get("value") for k, v in src.md.items() if isinstance(v, dict)},
            "field_sources": {k: {kk: vv for kk, vv in v.items() if kk != "value"} for k, v in src.md.items() if isinstance(v, dict)}}


def main():
    C.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--draft", default="/app/work/draft.json")
    ap.add_argument("--out", default="/app/output")
    ap.add_argument("--parsed", default=None, help="parsed dir (default: OUT/parsed/<draft.source or the only one>)")
    ap.add_argument("--creator", default=os.environ.get("NP_CREATOR", "LLM agent with nanopub-evidence-builder"))
    ap.add_argument("--created", default=None, help="fixed timestamp for reproducible output")
    ap.add_argument("--no-previews", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    draft = C.load_json(a.draft)
    vocab = C.load_vocab()
    created = a.created or os.environ.get("NP_CREATED") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    dirs = find_parsed_dirs(a.out)
    pdir = a.parsed
    if not pdir:
        want = draft.get("source")
        pdir = next((d for d in dirs if os.path.basename(d) == want), None) or (dirs[0] if len(dirs) == 1 else None)
    if not pdir:
        sys.exit(f"cannot choose a parsed dir; pass --parsed (found {dirs})")
    src = Source(pdir, draft.get("source_overrides"))
    # group assertions by source if several PDFs are used
    srcs = {os.path.basename(pdir): src}
    records = []
    for i, asr in enumerate(draft.get("assertions", []), 1):
        s = src
        if asr.get("source") and asr["source"] != os.path.basename(pdir):
            sd = os.path.join(a.out, "parsed", asr["source"])
            if asr["source"] not in srcs and os.path.exists(os.path.join(sd, "doc.json")):
                srcs[asr["source"]] = Source(sd)
            s = srcs.get(asr["source"], src)
        records.append(build_record(s, asr, i, vocab, a.creator, created, draft.get("unreported") or []))
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
            recs = [r for r in records if (r["provenance"]["source"].get("pdf_sha256") or {}).get("value") == (s.md.get("pdf_sha256") or {}).get("value")]
            render_crops(s, recs, os.path.join(a.out, "evidence_crops"))
            s._fitz = None
            render_previews(s, recs, os.path.join(a.out, "evidence_previews"))
    all_q = []
    for r in records:
        all_q += to_quads(r, vocab)
    nq, tg, jl = nquads(all_q), trig(all_q), jsonld(all_q)
    with open(os.path.join(a.out, "nanopubs.nq"), "w", encoding="utf-8") as fh:
        fh.write(nq)
    with open(os.path.join(a.out, "nanopubs.trig"), "w", encoding="utf-8") as fh:
        fh.write(tg)
    C.dump_json(jl, os.path.join(a.out, "nanopubs.jsonld"))
    rt = roundtrip(nq, jl, len(all_q), tg)
    qrep["export_roundtrip"] = rt
    C.dump_json({"nanopublications": records, "vocabulary": "vocab/relations.json", "tool": C.TOOL}, os.path.join(a.out, "nanopubs.json"))
    C.dump_json(qrep, os.path.join(a.out, "quality_report.json"))
    # ---- result.json
    rp = os.path.join(a.out, "result.json")
    res = C.load_json(rp) if os.path.exists(rp) else {"case_id": draft.get("case_id")}
    fields = draft.get("fields") or {}
    res.setdefault("field_status", {})
    res["field_evidence"] = res.get("field_evidence") or {}
    views = {"nanopublications": records, "assertions": [flat_assertion(r) for r in records],
             "evidence": flat_evidence(records), "provenance": provenance_view(src), "quality": qrep,
             "relations": [{"assertion_id": r["assertion"]["id"], "subject": r["assertion"]["subject"]["label"],
                            "predicate": r["assertion"]["predicate"]["id"], "object": r["assertion"]["object"]["label"],
                            "qualifiers": r["assertion"]["qualifiers"]} for r in records],
             "trig": "nanopubs.trig", "nquads": "nanopubs.nq", "jsonld": "nanopubs.jsonld"}
    by_aid = {r["assertion"]["id"]: r for r in records}
    unrep = list(draft.get("unreported") or [])
    for f, spec in fields.items():
        spec = spec if isinstance(spec, dict) else {"value": spec}
        kind = spec.get("kind", "agent_value")
        if "value" in spec:
            res[f] = spec["value"]
            ids = spec.get("from") or []
            res["field_evidence"][f] = [e for i in ids if i in by_aid for e in flat_evidence([by_aid[i]])]
            bad = [i for i in ids if i not in by_aid]
            ok = res[f] not in (None, "", []) and (ids and not bad and all(by_aid[i]["quality"]["verdict"] != "fail" for i in ids))
            res["field_status"][f] = "supported" if ok else ("unreported" if res[f] in (None, "", []) else "needs_review")
            if bad:
                res.setdefault("uncertainty", []).append({"field": f, "issue": f"'from' names unknown assertion ids {bad}"})
        elif kind in views:
            sel = spec.get("filter")
            v = views[kind]
            if sel and isinstance(v, list):
                v = [x for x in v if (x.get("assertion_id") or (x.get("assertion") or {}).get("id")) in sel]
            res[f] = v
            res["field_status"][f] = "generated" if v not in (None, [], "") else "unreported"
        else:
            res["field_status"][f] = "unreported"
            if res.get(f) is None and not any(u.get("field") == f for u in unrep if isinstance(u, dict)):
                unrep.append({"field": f, "reason": "agent did not fill this field"})
    res["nanopublications"] = res.get("nanopublications") if "nanopublications" in fields else [flat_assertion(r) for r in records]
    res["source_metadata"] = provenance_view(src)
    res["quality_summary"] = qrep["summary"]
    res["export_roundtrip"] = rt
    res["exports"] = {"nanopubs_json": "nanopubs.json", "trig": "nanopubs.trig", "nquads": "nanopubs.nq", "jsonld": "nanopubs.jsonld",
                      "quality_report": "quality_report.json", "review": "review.md", "previews_dir": "evidence_previews", "crops_dir": "evidence_crops"}
    res["unreported"] = unrep
    for k in ("conflicts", "uncertainty"):
        res[k] = (res.get(k) or []) + [x for x in (draft.get(k) or []) if x not in (res.get(k) or [])]
    if src.overrides_log:
        res["conflicts"] += [{"type": "metadata_override", **o} for o in src.overrides_log if o["from"] not in (None, o["to"])]
    n_fail = qrep["summary"]["fail"]
    missing = [f for f, st in res["field_status"].items() if st in (None, "unreported")]
    image_only = [r["assertion"]["id"] for r in records if any(i["rule"] == "R26" for i in r["quality"]["issues"])]
    if not records:
        res["status"] = "partial" if res.get("status") != "failed" else "failed"
    else:
        res["status"] = "success" if (n_fail == 0 and not missing and rt["ok"] and not image_only) else "partial"
    res["status_reason"] = {"failing_records": n_fail, "unfilled_fields": missing, "export_ok": rt["ok"],
                            "image_read_records_need_human_review": image_only}
    res.setdefault("change_log", []).append({"step": "np_build", "tool": C.TOOL, "draft": a.draft, "n_assertions": len(records),
                                              "verdicts": {k: qrep["summary"][k] for k in ("pass", "warn", "fail")},
                                              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    C.dump_json(res, rp)
    write_review(os.path.join(a.out, "review.md"), records, src, qrep)
    print(json.dumps({"n_assertions": len(records), "summary": qrep["summary"], "export_roundtrip": rt, "status": res["status"],
                      "status_reason": res["status_reason"],
                      "records": [{"id": r["assertion"]["id"], "verdict": r["quality"]["verdict"], "confidence": r["confidence"]["score"],
                                   "issues": [f"{i['rule']} {i['severity']}: {i['message']}" + (f" [{i['trigger']}]" if i.get("trigger") else "")
                                              for i in r["quality"]["issues"]],
                                   "evidence": [f"{e.get('match')} p{e.get('page')} {e.get('sentence_id') or e.get('table_id') or ''}" for e in r["evidence"]]}
                                  for r in records],
                      "seconds": round(time.time() - t0, 2)}, ensure_ascii=False, indent=1))


def write_review(path, records, src, qrep):
    md = src.md
    L = [f"# Nanopublication review — {(md.get('title') or {}).get('value')}", "",
         f"Source: {C.source_iri(md)} · SHA-256 `{(md.get('pdf_sha256') or {}).get('value')}` · tool {C.TOOL}", "",
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
    main()
