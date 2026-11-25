#!/usr/bin/env python3
"""Non-PDF paper sources (.md / .markdown / .txt / .docx / .html) -> the same doc.json layout parse_pdf.py writes,
so np_init / np_find / np_build / table_query work unchanged.

Locators: there are no printed pages, so 'page' is the logical page (1 unless the file has form feeds (.txt)
or explicit page breaks (.docx)); every paragraph is a block 'p<page>-b<k>' (k counts paragraphs on that page,
in file order) with its 1-based line range (md/txt) or paragraph index (docx), so ids are stable across runs and
a reader can find the text again. Sentence ids are '<block>-s<j>' as for PDFs. No bboxes (bbox = null).
Markdown pipe tables / docx tables / html tables become doc.tables (row/column queries via table_query.py).
Standard library only.

    python text_source.py paper.md OUT/parsed/paper        # writes doc.json, prints a summary
"""
import html
import json
import os
import re
import sys
import zipfile
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import np_common as C  # noqa: E402

TEXT_EXT = (".md", ".markdown", ".txt", ".text", ".docx", ".html", ".htm")
PARSER = "nanopub-evidence-builder text_source.py " + C.VERSION
CAPTION_RE = re.compile(r"^\s*(?:\*\*|__)?\s*((?:Table|Tab\.|Figure|Fig\.|Supplementary (?:Table|Figure)|表|图)\s*[A-Z]?\d+[A-Za-z]?(?:\.\d+)?)\s*[.:：]?", re.I)
REF_HEAD_RE = re.compile(r"^(?:references|bibliography|works cited|literature cited|参考文献)\s*$", re.I)
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def read_text(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8-replace"


# ------------------------------------------------------------------ format readers -> items
# item: {"kind": heading|para|list|table|break, "text", "level", "loc", "rows"}
def items_markdown(text, is_md=True):
    items, front = [], {}
    lines = [x.rstrip("\r") for x in text.replace("\r\n", "\n").split("\n")]  # not splitlines(): it eats form feeds
    i = 0
    if is_md and lines and lines[0].strip() == "---":  # YAML front matter (title/authors/date/doi/arxiv)
        j = next((k for k in range(1, min(len(lines), 80)) if lines[k].strip() in ("---", "...")), None)
        if j:
            key = None
            for l in lines[1:j]:
                m = re.match(r"^([A-Za-z_][\w\-]*)\s*:\s*(.*)$", l)
                if m:
                    key, val = m.group(1).lower(), m.group(2).strip().strip("'\"")
                    front[key] = val if val else []
                elif key and re.match(r"^\s*-\s+", l):
                    if not isinstance(front.get(key), list):
                        front[key] = [front[key]] if front.get(key) else []
                    front[key].append(re.sub(r"^\s*-\s+", "", l).strip().strip("'\""))
            i = j + 1
    buf, start, in_code = [], None, False

    def flush(end):
        nonlocal buf, start
        if buf:
            t = " ".join(x.strip() for x in buf).strip()
            if t:
                items.append({"kind": "para", "text": t, "loc": {"line_start": start + 1, "line_end": end}})
        buf, start = [], None

    while i < len(lines):
        l = lines[i]
        if "\f" in l:
            flush(i)
            items.append({"kind": "break"})
            l = l.replace("\f", "")
            if not l.strip():
                i += 1
                continue
        if is_md and re.match(r"^\s*(```|~~~)", l):
            flush(i)
            in_code = not in_code
            i += 1
            continue
        if in_code:
            i += 1
            continue
        if not l.strip():
            flush(i)
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", l) if is_md else None
        if m:
            flush(i)
            items.append({"kind": "heading", "level": len(m.group(1)), "text": m.group(2), "loc": {"line_start": i + 1, "line_end": i + 1}})
            i += 1
            continue
        if is_md and i + 1 < len(lines) and re.match(r"^\s*(=+|-+)\s*$", lines[i + 1]) and not buf and l.strip():
            items.append({"kind": "heading", "level": 1 if lines[i + 1].strip()[0] == "=" else 2, "text": l.strip(),
                          "loc": {"line_start": i + 1, "line_end": i + 2}})
            i += 2
            continue
        if not is_md and not buf and re.match(r"^\s*(?:\d+(?:\.\d+)*\.?|[IVX]+\.)\s+[A-Z][^.]{2,80}$", l) and \
                (i + 1 >= len(lines) or not lines[i + 1].strip()):
            items.append({"kind": "heading", "level": 1 + l.strip().split()[0].count("."), "text": l.strip(),
                          "loc": {"line_start": i + 1, "line_end": i + 1}})
            i += 1
            continue
        if "|" in l and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", lines[i + 1]):
            flush(i)
            rows, k = [], i
            while k < len(lines) and "|" in lines[k] and lines[k].strip():
                if k != i + 1:
                    cells = [c.strip() for c in lines[k].strip().strip("|").split("|")]
                    rows.append([re.sub(r"[*_`]", "", c) for c in cells])
                k += 1
            items.append({"kind": "table", "rows": rows, "loc": {"line_start": i + 1, "line_end": k}})
            i = k
            continue
        lm = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$", l) if is_md else None
        if lm and not buf:
            items.append({"kind": "list", "text": lm.group(1).strip(), "loc": {"line_start": i + 1, "line_end": i + 1}})
            i += 1
            continue
        if start is None:
            start = i
        buf.append(re.sub(r"^\s*>\s?", "", l) if is_md else l)
        i += 1
    flush(len(lines))
    if is_md:
        for it in items:
            if it.get("text"):
                t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", it["text"])
                t = re.sub(r"\[([^\]]+)\]\((?:[^)]*)\)", r"\1", t)
                t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)
                t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", t)
                t = re.sub(r"`([^`]+)`", r"\1", t)
                it["text"] = t.strip()
    return items, front


def items_docx(path):
    items, front = [], {}
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
        try:
            core = ET.fromstring(z.read("docProps/core.xml"))
            for el in core:
                tag = el.tag.split("}")[-1]
                if tag in ("title", "creator", "created", "identifier", "subject") and (el.text or "").strip():
                    front[tag] = el.text.strip()
        except KeyError:
            pass
    body = root.find(W + "body")
    n = 0
    for el in list(body) if body is not None else []:
        tag = el.tag.split("}")[-1]
        if tag == "p":
            n += 1
            if el.findall(f".//{W}br[@{W}type='page']") or el.findall(f".//{W}lastRenderedPageBreak"):
                items.append({"kind": "break"})
            t = "".join((x.text or "") if x.tag == W + "t" else ("\t" if x.tag == W + "tab" else "") for x in el.iter())
            if not t.strip():
                continue
            st = el.find(f"{W}pPr/{W}pStyle")
            sval = (st.get(W + "val") if st is not None else "") or ""
            hm = re.match(r"(?i)heading\s*(\d)", sval)
            if re.match(r"(?i)title$", sval):
                items.append({"kind": "heading", "level": 0, "text": t.strip(), "loc": {"paragraph": n}})
            elif hm:
                items.append({"kind": "heading", "level": int(hm.group(1) or 1), "text": t.strip(), "loc": {"paragraph": n}})
            elif el.find(f"{W}pPr/{W}numPr") is not None:
                items.append({"kind": "list", "text": t.strip(), "loc": {"paragraph": n}})
            else:
                items.append({"kind": "para", "text": t.strip(), "loc": {"paragraph": n}})
        elif tag == "tbl":
            rows = []
            for tr in el.findall(f"{W}tr"):
                rows.append([" ".join("".join(x.text or "" for x in p.iter(W + "t")) for p in tc.findall(f"{W}p")).strip()
                             for tc in tr.findall(f"{W}tc")])
            if rows:
                items.append({"kind": "table", "rows": rows, "loc": {"after_paragraph": n}})
    return items, front


def items_html(text):
    text = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", text)
    items = []
    for m in re.finditer(r"(?is)<(h[1-6]|p|li|caption|figcaption|table|title)[^>]*>(.*?)</\1>", text):
        tag, inner = m.group(1).lower(), m.group(2)
        if tag == "table":
            rows = []
            for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", inner):
                rows.append([html.unescape(re.sub(r"(?s)<[^>]+>", " ", c)).strip() for c in re.findall(r"(?is)<t[hd][^>]*>(.*?)</t[hd]>", tr)])
            if rows:
                items.append({"kind": "table", "rows": rows, "loc": {"char": m.start()}})
            continue
        t = re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", inner))).strip()
        if not t:
            continue
        if tag == "title":
            items.append({"kind": "heading", "level": 0, "text": t, "loc": {"char": m.start()}, "html_title": True})
        elif tag.startswith("h"):
            items.append({"kind": "heading", "level": int(tag[1]), "text": t, "loc": {"char": m.start()}})
        else:
            items.append({"kind": "list" if tag == "li" else "para", "text": t, "loc": {"char": m.start()}})
    if sum(1 for i in items if i.get("html_title")) and any(i["kind"] == "heading" and i.get("level") == 1 for i in items):
        items = [i for i in items if not i.get("html_title")]
    return items, {}


# ------------------------------------------------------------------ items -> doc.json
def build_doc(path, items, front, fmt, enc=None):
    blocks, paragraphs, tables, refs = [], [], [], []
    page, k_on_page, order = 1, 0, 0
    stack, title_done, in_refs = [], False, False
    pending_caption = None
    for it in items:
        if it["kind"] == "break":
            if k_on_page:
                page += 1
                k_on_page = 0
            continue
        if it["kind"] == "table":
            k_on_page += 1
            order += 1
            bid = f"p{page}-b{k_on_page}"
            rows = it["rows"]
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            names = rows[0]
            tid = f"T{len(tables) + 1}"
            cap = pending_caption
            tables.append({"id": tid, "page": page, "bbox": None, "n_rows": len(rows), "n_cols": width, "rows": rows,
                           "cells": [{"row": ri, "col": ci, "text": c, "colspan": 1, "rowspan": 1, "bbox": None, "header": ri == 0}
                                     for ri, r in enumerate(rows) for ci, c in enumerate(r)],
                           "header": {"n_header_rows": 1, "names": names},
                           "columns": [{"index": i, "path": [n] if n else [], "name": n or f"col{i}"} for i, n in enumerate(names)],
                           "key_columns": [0], "caption": cap, "caption_block": None, "block_id": bid, "location": it.get("loc"),
                           "detector": f"{fmt} table"})
            blocks.append({"id": bid, "page": page, "bbox": None, "text": " | ".join(" ".join(r) for r in rows)[:2000], "role": "table",
                           "order": order, "section_path": list(stack), "location": it.get("loc"), "source": "text_file"})
            pending_caption = None
            continue
        text = it["text"]
        k_on_page += 1
        order += 1
        bid = f"p{page}-b{k_on_page}"
        if it["kind"] == "heading":
            lvl = it.get("level", 1)
            if not title_done and (lvl <= 1):
                role = "title"
                title_done = True
            else:
                role = "heading"
                stack = stack[:max(0, lvl - 2)] + [text]
                in_refs = bool(REF_HEAD_RE.match(text.strip().rstrip(":")))
            blocks.append({"id": bid, "page": page, "bbox": None, "text": text, "role": role, "order": order,
                           "section_path": list(stack) if role == "heading" else [], "location": it.get("loc"), "source": "text_file"})
            continue
        role = "body"
        if in_refs:
            role = "reference"
            m = re.match(r"^\s*\[(\d+)\]|^\s*(\d+)\.\s", text)
            refs.append({"label": (m.group(1) or m.group(2)) if m else None, "text": text, "page": page, "block_id": bid})
        elif CAPTION_RE.match(text):
            role = "caption"
            if re.match(r"\s*(?:\*\*)?\s*(?:table|tab\.|表)", text, re.I):
                pending_caption = text
                if tables and tables[-1]["caption"] is None and blocks and blocks[-1]["role"] == "table":
                    tables[-1]["caption"] = text
                    pending_caption = None
        elif it["kind"] == "list":
            role = "list_item"
        elif not title_done and order == 1 and fmt == "txt" and len(text) < 200:
            role = "title"
            title_done = True
        b = {"id": bid, "page": page, "bbox": None, "text": text, "role": role, "order": order, "section_path": list(stack),
             "location": it.get("loc"), "source": "text_file"}
        blocks.append(b)
        if role in ("body", "list_item", "caption"):
            paragraphs.append({"id": f"para{len(paragraphs) + 1}", "role": role, "section_path": list(stack), "text": text,
                               "sources": [{"block_id": bid, "page": page, "bbox": None}]})
    sha = C.sha256_file(path)
    meta = {"source_file": os.path.abspath(path), "parser": PARSER, "sha256": sha, "page_count": page, "format": fmt,
            "encoding": enc, "metadata": {k: (v if isinstance(v, str) else "; ".join(v)) for k, v in front.items()},
            "locator": "logical page (form feed / page break) + paragraph block; line_start/line_end or paragraph index in blocks[].location"}
    return {"status": "ok", "meta": meta, "pages_parsed": list(range(1, page + 1)), "pages_not_parsed": [], "warnings": [],
            "pages": [{"page": p, "source": "text_file"} for p in range(1, page + 1)], "ocr_pages": [], "unresolved_pages": [],
            "sections": [], "blocks": blocks, "headers_footers": [], "paragraphs": paragraphs, "tables": tables, "figures": [],
            "equations": [], "footnotes": [], "references": refs,
            "stats": {"n_blocks": len(blocks), "n_paragraphs": len(paragraphs), "n_tables": len(tables), "front_matter": front}}


def parse(path, out_dir):
    ext = os.path.splitext(path)[1].lower()
    enc = None
    try:
        if ext == ".docx":
            items, front = items_docx(path)
            fmt = "docx"
        elif ext in (".html", ".htm"):
            text, enc = read_text(path)
            items, front = items_html(text)
            fmt = "html"
        else:
            text, enc = read_text(path)
            fmt = "markdown" if ext in (".md", ".markdown") else "txt"
            items, front = items_markdown(text, is_md=(fmt == "markdown"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        return {"status": "failed", "error": f"cannot read {ext} file: {e}"}
    if not any(i.get("text") for i in items):
        return {"status": "failed", "error": "no text found in the file"}
    doc = build_doc(path, items, front, fmt, enc)
    os.makedirs(out_dir, exist_ok=True)
    C.dump_json(doc, os.path.join(out_dir, "doc.json"))
    with open(os.path.join(out_dir, "document.md"), "w", encoding="utf-8") as fh:
        for b in doc["blocks"]:
            pre = "# " if b["role"] == "title" else ("## " if b["role"] == "heading" else "")
            fh.write(f"{pre}{b['text']}  <!-- {b['id']} -->\n\n")
    return {"status": "ok", "meta": doc["meta"], "unresolved_pages": [], "pages_not_parsed": [], "warnings": []}


# ------------------------------------------------------------------ metadata
def extract_metadata(path, doc):
    """Same keys as np_common.extract_metadata; every value carries where it was read."""
    blocks = doc.get("blocks", [])
    front = (doc.get("meta") or {}).get("metadata") or {}
    fmt = (doc.get("meta") or {}).get("format")
    md = {}

    def src(kind, b=None, quote=None):
        d = {"kind": kind}
        if b:
            d.update({"page": b["page"], "block_id": b["id"], "location": b.get("location")})
        if quote:
            d["quote"] = quote[:300]
        return d
    head = blocks[:40]
    head_text = "\n".join(b["text"] for b in head)
    tb = next((b for b in blocks if b["role"] == "title"), None)
    if front.get("title"):
        md["title"] = {"value": front["title"], "source": src("front_matter", quote="title: " + front["title"]), "confidence": "high"}
    elif tb:
        md["title"] = {"value": C.norm(tb["text"], lower=False), "source": src("text", tb, tb["text"]), "confidence": "medium", "needs_review": True}
    else:
        md["title"] = {"value": None, "source": {"kind": "not_found"}, "confidence": None}
    fa = front.get("author") or front.get("authors") or front.get("creator")
    if fa:
        names = fa if isinstance(fa, list) else [x.strip() for x in re.split(r";|,| and |、|，", fa) if x.strip()]
        names = [n for n in (x.strip() for x in names) if n]
        md["authors"] = {"value": names or None, "source": src("front_matter", quote=str(fa)), "confidence": "high" if names else None}
    else:
        ab = None
        for b in head:
            m = re.match(r"^\s*(?:authors?|by|作者)\s*[:：]\s*(.+)$", b["text"], re.I)
            if m:
                ab = (b, m.group(1))
                break
        if ab is None and tb:
            nxt = next((b for b in blocks if b["order"] == tb["order"] + 1), None)
            if nxt and nxt["role"] in ("body", "list_item") and len(nxt["text"]) < 300 and not re.search(r"[.。]\s|abstract|摘要", nxt["text"], re.I):
                ab = (nxt, nxt["text"])
        if ab:
            names = C.dedup_names(C.split_authors(ab[1]))
            md["authors"] = {"value": names or None, "raw": ab[1][:500], "source": src("text", ab[0], ab[1]),
                             "confidence": "low" if names else None, "needs_review": True,
                             "note": "split by heuristics from the line after the title / 'Authors:' line"}
        else:
            md["authors"] = {"value": None, "source": {"kind": "not_found"}, "confidence": None}
    am = C.ARXIV_RE.search(head_text) or (C.ARXIV_RE.search(str(front.get("arxiv") or "")) if front.get("arxiv") else None)
    if not am and front.get("arxiv"):
        am = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", str(front["arxiv"]))
    if am:
        md["arxiv_id"] = {"value": am.group(1), "source": src("text_stamp", quote=am.group(0)), "confidence": "high"}
        md["version"] = ({"value": am.group(2), "source": src("text_stamp", quote=am.group(0)), "confidence": "high"} if am.group(2) else
                         {"value": None, "source": {"kind": "not_found"}, "confidence": None, "note": "arXiv id without version suffix"})
    else:
        md["arxiv_id"] = {"value": None, "source": {"kind": "not_found"}, "confidence": None}
        vv = front.get("version")
        md["version"] = ({"value": str(vv), "source": src("front_matter", quote=f"version: {vv}"), "confidence": "medium"} if vv else
                         {"value": None, "source": {"kind": "not_found"}, "confidence": None, "note": "no version statement found"})
    dsrc = str(front.get("doi") or "") + "\n" + head_text
    dm = C.DOI_RE.search(dsrc)
    md["doi"] = ({"value": dm.group(1).rstrip(".,;)]}"), "source": src("front_matter" if front.get("doi") else "text", quote=dsrc[max(0, dm.start() - 30):dm.end() + 10]),
                  "confidence": "high"} if dm else {"value": None, "source": {"kind": "not_found"}, "confidence": None})
    ys = None
    for key in ("year", "date", "published", "created"):
        if front.get(key):
            ym = C.YEAR_RE.search(str(front[key]))
            if ym:
                ys = {"value": int(ym.group(1)), "source": src("front_matter" if key != "created" or fmt != "docx" else "docx_core_properties",
                                                                quote=f"{key}: {front[key]}"),
                      "confidence": "medium" if key != "created" else "low"}
                if key == "created":
                    ys["note"] = "document creation date, not necessarily the publication year"
                break
    if ys is None:
        m = re.search(r"(?:©|\(c\)|copyright|published|received|accepted|发表|出版)[^\n]{0,40}?((?:19|20)\d\d)", head_text, re.I)
        ys = ({"value": int(m.group(1)), "source": src("text", quote=m.group(0)), "confidence": "medium"} if m else
              {"value": None, "source": {"kind": "not_found"}, "confidence": None})
    md["year"] = ys
    md["venue"] = {"value": front.get("venue") or front.get("journal"), "source": src("front_matter") if (front.get("venue") or front.get("journal")) else {"kind": "not_found"},
                   "confidence": "medium" if (front.get("venue") or front.get("journal")) else None}
    md["file_sha256"] = {"value": (doc.get("meta") or {}).get("sha256") or C.sha256_file(path), "source": {"kind": "file_hash"}, "confidence": "high"}
    md["source_format"] = {"value": fmt, "source": {"kind": "file"}, "confidence": "high"}
    md["page_count"] = {"value": (doc.get("meta") or {}).get("page_count"), "source": {"kind": "file"}, "confidence": "high",
                        "note": "logical pages (form feeds / page breaks), not printed pages"}
    md["file_name"] = {"value": os.path.basename(path), "source": {"kind": "file"}, "confidence": "high"}
    return md


if __name__ == "__main__":
    C.utf8_stdout()
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    r = parse(sys.argv[1], sys.argv[2])
    print(json.dumps({k: v for k, v in r.items() if k != "meta"} | {"format": (r.get("meta") or {}).get("format")}, ensure_ascii=False))
