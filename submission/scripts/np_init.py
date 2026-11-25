#!/usr/bin/env python3
"""Step 1 of nanopub-evidence-builder: parse the case's paper(s) and prepare everything
the agent needs to write assertions.

Usage:
    python np_init.py [--case /input/case.json] [--out /app/output] [--work /app/work]
                      [--pdf extra.pdf ...] [--password PW] [--pages 1-8] [--top 40]

Inputs: PDFs (layout-aware parse) and .md / .markdown / .txt / .docx / .html paper texts (text_source.py).
Material choice: files named in case.json are used in this order: role primary / main / paper > unlabelled >
supplement / SI / appendix (role from a sibling 'role'/'type'/'kind' key, from the key name, or from the file name).
File size never decides. task.md, README and case.json are never treated as the paper. Only if case.json names no
material: every PDF under the case dir and /input; and only if there is no PDF: paper-like text files there.

Writes
    OUT/result.json                 skeleton: case_id + every output_contract field (null) + status blocks
                                    + output_contract_spec (required / optional / type of each field)
    OUT/parsed/<stem>/...           parse (doc.json, document.md, tables/, ...)
    OUT/parsed/<stem>/sentences.jsonl   one sentence per line: id, page, block_id, bbox, section, text, cues
    OUT/parsed/<stem>/source_metadata.json  title/authors/year/DOI/arXiv id+version/sha256, each with its source
    WORK/candidates.md              highest-scoring claim sentences (ids + cues) to start from
    WORK/draft.json                 draft template the agent fills (see SKILL.md); never overwritten
Nothing here is an answer: contract fields stay null until np_build.py fills them from the draft.
"""
import argparse
import glob
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import np_common as C  # noqa: E402
import np_contract as K  # noqa: E402
import parse_pdf  # noqa: E402
import text_source  # noqa: E402

CONTRACT_KEYS = K.CONTRACT_KEYS
DOC_EXT = (".pdf",) + text_source.TEXT_EXT
NOT_PAPER = re.compile(r"(?:^|[\\/])(?:task|readme|instructions?|prompt|case|contract|rubric|license|changelog)(?:[._\-][^\\/]*)?\.(?:md|txt|json|html?)$", re.I)
PRIMARY_RE = re.compile(r"primary|main|paper|article|manuscript|publication|source_pdf|full_?text|论文|正文|主文", re.I)
SUPP_RE = re.compile(r"supp|\bsi\b|_si\b|-si\b|appendix|appendices|extended|supporting|additional|esm|mmc\d|补充|附录|附件", re.I)


def find_contract(obj):
    return K.find_contract(obj)[0]


def contract_fields(contract):
    return [f["name"] for f in K.parse_contract(contract)["fields"]]


def _role_of(text):
    if not text:
        return None
    t = str(text)
    if SUPP_RE.search(t):
        return "supplement"
    if PRIMARY_RE.search(t):
        return "primary"
    return None


def find_material_strings(obj, acc, key_path=(), sibling_role=None, contract_ids=()):
    """[(path_string, role_hint, key_path)] for every string in case.json that looks like a paper file."""
    if id(obj) in contract_ids:
        return acc
    if isinstance(obj, dict):
        role = None
        for rk in ("role", "type", "kind", "category", "label"):
            if isinstance(obj.get(rk), str):
                role = _role_of(obj[rk]) or (("primary" if obj[rk].lower() in ("primary", "main") else None))
                if role:
                    break
        if obj.get("primary") is True or obj.get("is_primary") is True or obj.get("main") is True:
            role = "primary"
        for k, v in obj.items():
            find_material_strings(v, acc, key_path + (str(k),), role, contract_ids)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            find_material_strings(v, acc, key_path + (str(i),), sibling_role, contract_ids)
    elif isinstance(obj, str) and obj.lower().rstrip().endswith(DOC_EXT) and len(obj) < 1000:
        key_role = next((r for r in (_role_of(k) for k in reversed(key_path) if not k.isdigit()) if r), None)
        role = sibling_role or key_role or _role_of(os.path.basename(obj))
        acc.append((obj.strip(), role, "/".join(key_path)))
    return acc


def resolve(p, bases):
    if os.path.isabs(p) and os.path.exists(p):
        return p
    for b in bases:
        q = os.path.join(b, p)
        if os.path.exists(q):
            return q
    hits = []
    for b in bases:
        if os.path.isdir(b):
            hits += glob.glob(os.path.join(glob.escape(b), "**", glob.escape(os.path.basename(p))), recursive=True)
    return hits[0] if hits else None


def discover_materials(case, case_path, bases, extra):
    """Ordered [{"file", "role", "named_in"}]: primary first, supplements last. Size is never used."""
    cids = set()
    for craw, _, _ in K.find_contracts(case):
        if isinstance(craw, (dict, list)):
            cids.add(id(craw))
    found, seen = [], set()
    for s, role, kp in find_material_strings(case, [], contract_ids=cids) + [(x, None, "--pdf") for x in extra]:
        r = resolve(s, bases)
        if not r or NOT_PAPER.search(r.replace("\\", "/")) or os.path.abspath(r) == os.path.abspath(case_path):
            continue
        ab = os.path.abspath(r)
        if ab in seen:
            continue
        seen.add(ab)
        found.append({"file": r, "role": role or "unlabelled", "named_in": kp})
    if not found:
        for b in bases[:2]:
            for p in sorted(glob.glob(os.path.join(glob.escape(b), "**", "*.pdf"), recursive=True)):
                if os.path.abspath(p) not in seen and "/output/" not in p.replace("\\", "/") and "/work/" not in p.replace("\\", "/"):
                    seen.add(os.path.abspath(p))
                    found.append({"file": p, "role": _role_of(os.path.basename(p)) or "unlabelled", "named_in": "glob"})
            for ext in text_source.TEXT_EXT:
                for p in sorted(glob.glob(os.path.join(glob.escape(b), "**", "*" + ext), recursive=True)):
                    if NOT_PAPER.search(p.replace("\\", "/")) or os.path.abspath(p) in seen or os.path.abspath(p) == os.path.abspath(case_path) \
                            or "/output/" in p.replace("\\", "/") or "/work/" in p.replace("\\", "/"):
                        continue
                    seen.add(os.path.abspath(p))
                    found.append({"file": p, "role": _role_of(os.path.basename(p)) or "unlabelled", "named_in": "glob"})
    rank = {"primary": 0, "unlabelled": 1, "supplement": 2}
    found.sort(key=lambda m: rank.get(m["role"], 1))  # stable: keeps case.json order inside a role
    if found and not any(m["role"] == "primary" for m in found):
        first = next((m for m in found if m["role"] == "unlabelled"), found[0])
        first["role_inferred"] = "primary (first non-supplement material)"
    return found


def guess_kind(field, desc=""):
    """Suggest which np_build output a contract field probably wants (agent decides)."""
    for t in (field.lower(), (json.dumps(desc, ensure_ascii=False) if desc else "").lower()):
        k = _kind_of(t)
        if k:
            return k
    return "agent_value"


def _kind_of(t):
    toks = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(t)).replace("_", " ").replace("-", " ").lower()
    for kind, keys in (("nanopublications", ("nanopub", "nanopubs", "nanopublication", "nanopublications", "knowledge unit")),
                       ("assertions", ("assertion", "assertions", "claim", "claims", "statement", "statements", "finding", "findings", "断言", "结论")),
                       ("evidence", ("evidence", "support", "quote", "quotes", "snippet", "snippets", "证据")),
                       ("provenance", ("provenance", "source", "sources", "metadata", "citation", "citations", "bibliography", "来源", "元数据")),
                       ("quality", ("quality", "check", "checks", "issue", "issues", "flag", "flags", "validation", "质量", "校验")),
                       ("relations", ("relation", "relations", "triple", "triples", "edge", "edges", "关系")),
                       ("nquads", ("nquads", "n quads", ".nq")),
                       ("trig", ("trig", "rdf", "turtle")),
                       ("jsonld", ("json ld", "jsonld")),
                       ("unreported", ("unreported", "missing", "未报告", "缺失")),
                       ("conflicts", ("conflict", "conflicts", "contradiction", "contradictions", "冲突", "矛盾")),
                       ("uncertainty", ("uncertainty", "limitation", "limitations", "边界", "不确定")),
                       ("review", ("review", "report", "报告")),
                       ("summary", ("summary", "overview", "总结", "摘要说明", "综述"))):
        if any((k in t if re.search(r"[\u4e00-\u9fff.]", k) else re.search(r"\b" + re.escape(k) + r"\b", toks)) for k in keys):
            return kind
    if re.search(r"\bstatus\b|运行状态", toks):
        return "status"
    return None


def get_case_id(case):
    for k in ("case_id", "caseId", "id", "case"):
        v = case.get(k) if isinstance(case, dict) else None
        if v is not None and not isinstance(v, (dict, list)) and v != "":
            return v
    return os.environ.get("CASE_ID")


def parse_material(m, out, work, used_stems, a):
    p = m["file"]
    stem = re.sub(r"[^\w.\-]+", "_", os.path.splitext(os.path.basename(p))[0]) or "source"
    base, n = stem, 2
    while stem in used_stems:
        stem = f"{base}_{n}"
        n += 1
    used_stems.add(stem)
    pdir = os.path.join(out, "parsed", stem)
    is_pdf = p.lower().endswith(".pdf")
    try:
        if is_pdf:
            r = parse_pdf.parse(p, pdir, password=a.password, pages=parse_pdf.parse_pages(a.pages),
                                cache_dir=os.path.join(work, "parse_cache"))
        else:
            r = text_source.parse(p, pdir)
    except Exception as e:  # parser crash: record, continue with other materials
        r = {"status": "failed", "error": f"parser exception: {type(e).__name__}: {e}"}
    src = {"file": p, "stem": stem, "role": m["role"], "named_in": m.get("named_in"), "format": "pdf" if is_pdf else os.path.splitext(p)[1].lower().lstrip("."),
           "sha256": C.sha256_file(p), "parsed_dir": pdir, "parse_status": r.get("status"),
           "parse_error": r.get("error"), "page_count": (r.get("meta") or {}).get("page_count"),
           "pages_not_parsed": r.get("pages_not_parsed"), "unresolved_pages": r.get("unresolved_pages", []),
           "ocr_pages": r.get("ocr_pages", []), "n_warnings": len(r.get("warnings", []))}
    if m.get("role_inferred"):
        src["role_inferred"] = m["role_inferred"]
    if not is_pdf:
        src["locator_note"] = "no printed pages: page = logical page (form feed / page break), block = paragraph; see blocks[].location"
    return src, r, pdir, stem, is_pdf


def main():
    C.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", default="/input/case.json")
    ap.add_argument("--task", default=None)
    ap.add_argument("--out", default="/app/output")
    ap.add_argument("--work", default="/app/work")
    ap.add_argument("--pdf", nargs="*", default=[], help="extra material files (PDF or text), appended after case.json's")
    ap.add_argument("--password", default=None)
    ap.add_argument("--pages", default=None, help="parse only these pages, e.g. 1-8 (unread pages are listed)")
    ap.add_argument("--top", type=int, default=40, help="claim candidates listed in candidates.md")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(a.work, exist_ok=True)
    try:
        _run_init(a)
    except Exception as e:
        rp = os.path.join(a.out, "result.json")
        prev = None
        if os.path.exists(rp):
            try:
                prev = C.load_json(rp)
            except Exception:
                prev = None
        has_built = bool(prev and any(isinstance(c, dict) and c.get("step") == "np_build" for c in prev.get("change_log") or []))
        if has_built:
            C.dump_json({"status": "init_refused", "error": f"{type(e).__name__}: {e}", "result_json": "unchanged",
                         "tool": C.TOOL, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                        os.path.join(a.out, "init_error.json"))
        else:
            C.dump_json({"case_id": None, "status": "failed", "nanopublications": [], "sources": [], "field_status": {},
                         "unreported": [], "uncertainty": [{"issue": f"np_init fatal error: {type(e).__name__}: {e}", "origin": "np_init"}],
                         "conflicts": [], "run_status": {"reason": f"{type(e).__name__}: {e}", "tool": C.TOOL},
                         "change_log": [{"step": "np_init", "status": "failed", "tool": C.TOOL,
                                         "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}]}, rp)
        raise


def _run_init(a):
    t0 = time.time()
    case, case_err = {}, None
    if os.path.exists(a.case):
        try:
            case = C.load_json(a.case)
        except Exception as e:
            case_err = f"case.json unreadable: {e}"
    else:
        case_err = f"{a.case} not found"
    if not isinstance(case, dict):
        case_err = case_err or f"case.json top level is {type(case).__name__}, expected an object"
        case = {"_items": case}
    case_dir = os.path.dirname(os.path.abspath(a.case))
    task_path = a.task or next((p for p in (os.path.join(case_dir, "task.md"), "/input/task.md", "task.md") if os.path.exists(p)), None)
    bases = [case_dir, "/input", os.getcwd()]
    materials = discover_materials(case, os.path.abspath(a.case), bases, list(a.pdf))
    sources, change_log, cand_lines, used = [], [], [], set()
    for m in materials:
        src, r, pdir, stem, is_pdf = parse_material(m, a.out, a.work, used, a)
        docp = os.path.join(pdir, "doc.json")
        if r.get("status") != "failed" and os.path.exists(docp):
            doc = C.load_json(docp)
            md = C.extract_metadata(m["file"], doc) if is_pdf else text_source.extract_metadata(m["file"], doc)
            md["material_role"] = {"value": src["role"], "source": {"kind": "case_json" if m.get("named_in") not in ("glob", None) else "file_name",
                                                                     "named_in": m.get("named_in")}, "confidence": "high"}
            C.dump_json(md, os.path.join(pdir, "source_metadata.json"))
            sents = C.build_sentence_index(doc)
            with open(os.path.join(pdir, "sentences.jsonl"), "w", encoding="utf-8") as fh:
                for s in sents:
                    fh.write(json.dumps(s, ensure_ascii=False) + "\n")
            src["n_sentences"] = len(sents)
            src["metadata_preview"] = {k: (v or {}).get("value") for k, v in md.items()}
            src["source_iri"] = C.source_iri(md)
            ranked = sorted(sents, key=lambda s: -C.claim_score(s))[: a.top]
            ranked.sort(key=lambda s: (s["page"], s["id"]))
            cand_lines.append(f"# {stem}  [{src['role']}, {src['format']}]  ({len(sents)} sentences; parsed dir {pdir})\n")
            cand_lines.append("id | section | cues | text")
            cand_lines.append("---|---|---|---")
            for s in ranked:
                c = s["cues"]
                tag = []
                if c["numbers"]:
                    tag.append("num:" + ",".join(c["numbers"][:4]))
                for k, lab in (("hedges", "hedge"), ("negations", "neg"), ("citations", "cite"), ("comparatives", "cmp"), ("causal", "cause")):
                    if c[k]:
                        tag.append(lab)
                if c["coordination"] >= 2:
                    tag.append("compound")
                if C.ATTRIBUTION_RE.search(s["text"]) and not C.FIRST_PERSON_RE.search(s["text"]):
                    tag.append("attrib")
                if C.magnitudes(s["text"]):
                    tag.append("mag")
                sec = " > ".join(s.get("section_path") or [])[:40]
                cand_lines.append(f"{s['id']} | {sec} | {' '.join(tag)} | {s['text'][:300].replace('|', '/')}")
            cand_lines.append("")
            if doc.get("tables"):
                cand_lines.append("tables: " + "; ".join(f"{t['id']} p{t['page']} {(t.get('caption') or '')[:60]}" for t in doc["tables"][:30]) + "\n")
            if r.get("unresolved_pages"):
                cand_lines.append(f"WARNING: pages without a usable text layer: {r['unresolved_pages']} (no sentences from them; read page images, see SKILL.md)\n")
        sources.append(src)
        change_log.append({"step": "parse+index", "file": m["file"], "role": src["role"], "status": r.get("status"), "tool": C.TOOL,
                           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    found_contracts = K.find_contracts(case)
    spec = K.parse_contracts(found_contracts)
    ckey = spec.get("found_under")
    fields = [f["name"] for f in spec["fields"]]
    case_id = get_case_id(case)
    result = {"case_id": case_id}
    for f in fields:
        if f != "case_id":
            result.setdefault(f, None)
    suggestions = {f["name"]: guess_kind(f["name"], f.get("description") or (f.get("schema") or {}).get("description") or "")
                   for f in spec["fields"] if f["name"] != "case_id"}
    for f in spec["fields"]:
        f["suggested_kind"] = suggestions.get(f["name"])
    result.update({
        "status": "pending_agent_fill",
        "nanopublications": result.get("nanopublications") if "nanopublications" in fields else [],
        "sources": sources,
        "field_status": {f: None for f in fields if f != "case_id"},
        "unreported": [], "uncertainty": [], "conflicts": [],
        "output_contract_spec": {"found_under": ckey, "style": spec["style"], "fields": spec["fields"], "deliverable_files": spec["files"],
                                 "errors": spec.get("errors") or [],
                                 "required": [f["name"] for f in spec["fields"] if f["required"] and f["name"] != "case_id"],
                                 "optional": [f["name"] for f in spec["fields"] if not f["required"] and f["name"] != "case_id"]},
        "run_status": {"init_seconds": round(time.time() - t0, 2), "case_error": case_err, "n_materials": len(materials),
                       "n_pdfs": sum(1 for s in sources if s["format"] == "pdf"), "task_file": task_path, "tool": C.TOOL},
        "change_log": change_log,
    })
    for err in spec.get("errors") or []:
        result["uncertainty"].append({**err, "origin": "np_init"})
    if case_id is None:
        result["uncertainty"].append({"field": "case_id", "issue": "case.json has no case_id / id; CASE_ID env not set", "origin": "np_init"})
    if case_err:
        result["status"] = "failed"
        result["run_status"]["reason"] = case_err
    elif not materials:
        result["status"] = "failed"
        result["run_status"]["reason"] = "no paper file (PDF / md / txt / docx / html) found in case.json or the input directory"
    elif all(s["parse_status"] == "failed" for s in sources):
        result["status"] = "failed"
        result["run_status"]["reason"] = "; ".join(f"{s['file']}: {s['parse_error']}" for s in sources)
    rp = os.path.join(a.out, "result.json")
    prev = None
    if os.path.exists(rp):
        try:
            prev = C.load_json(rp)
        except Exception:
            prev = None
    has_built = bool(prev and any(isinstance(c, dict) and c.get("step") == "np_build" for c in prev.get("change_log") or []))
    if has_built:
        id_mismatch = case_id is not None and prev.get("case_id") is not None and case_id != prev.get("case_id")
        if case_err or not materials or all(s["parse_status"] == "failed" for s in sources) or id_mismatch:
            err_msg = case_err or ("case_id mismatch on re-run" if id_mismatch else result["run_status"].get("reason") or "no readable materials")
            C.dump_json({"status": "init_refused", "error": err_msg, "result_json": "unchanged", "tool": C.TOOL,
                         "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, os.path.join(a.out, "init_error.json"))
        else:
            # re-running init after a build must not wipe the built result: keep it, refresh sources only
            C.dump_json(prev, os.path.join(a.out, "result.prev.json"))
            prev["sources"] = sources
            prev["output_contract_spec"] = result["output_contract_spec"]
            prev.setdefault("change_log", []).append({"step": "np_init re-run", "note": "built result kept; sources refreshed", "tool": C.TOOL,
                                                      "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            C.dump_json(prev, rp)
    else:
        C.dump_json(result, rp)
    with open(os.path.join(a.work, "candidates.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(cand_lines) + "\n")
    draft_path = os.path.join(a.work, "draft.json")
    primary = next((s for s in sources if s.get("parse_status") != "failed"), sources[0] if sources else None)
    if not os.path.exists(draft_path):
        C.dump_json({
            "_help": "Fill 'assertions' (one atomic claim each) and 'fields'. Evidence = sentence ids from sentences.jsonl "
                     "or {page, quote}. Then: $PY $SKILL_DIR/scripts/np_build.py --draft WORK/draft.json --out OUT. See SKILL.md.",
            "source": primary["stem"] if primary else None,
            "source_overrides": {},
            "assertions": [],
            "fields": {f: {"kind": k} for f, k in suggestions.items()},
            "unreported": [], "conflicts": [], "uncertainty": []}, draft_path)
    print(json.dumps({"case_id": case_id, "materials": [{"file": s["file"], "role": s["role"], "stem": s["stem"], "format": s["format"]} for s in sources],
                      "primary_source": primary["stem"] if primary else None,
                      "contract_fields": fields, "required_fields": result["output_contract_spec"]["required"],
                      "optional_fields": result["output_contract_spec"]["optional"],
                      "field_kind_suggestions": suggestions,
                      "parse_status": [s["parse_status"] for s in sources],
                      "metadata": [s.get("metadata_preview") for s in sources],
                      "result": rp,
                      "sentences": [os.path.join(s["parsed_dir"], "sentences.jsonl") for s in sources],
                      "candidates": os.path.join(a.work, "candidates.md"), "draft": draft_path,
                      "task_file": task_path, "case_error": case_err, "seconds": round(time.time() - t0, 2)},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
