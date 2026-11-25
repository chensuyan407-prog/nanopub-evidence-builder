#!/usr/bin/env python3
"""Step 1 of nanopub-evidence-builder: parse the case PDFs and prepare everything
the agent needs to write assertions.

Usage:
    python np_init.py [--case /input/case.json] [--out /app/output] [--work /app/work]
                      [--pdf extra.pdf ...] [--password PW] [--pages 1-8] [--top 40]

Writes
    OUT/result.json                 skeleton: case_id + every output_contract field (null) + status blocks
    OUT/parsed/<stem>/...           layout-aware parse (doc.json, document.md, tables/, blocks.jsonl)
    OUT/parsed/<stem>/sentences.jsonl   one sentence per line: id, page, block_id, bbox, section, text, cues
    OUT/parsed/<stem>/source_metadata.json  title/authors/year/DOI/arXiv id+version/sha256, each with its source
    WORK/candidates.md              highest-scoring claim sentences (ids + cues) to start from
    WORK/draft.json                 draft template the agent fills (see SKILL.md)
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
import parse_pdf  # noqa: E402

CONTRACT_KEYS = ("output_contract", "outputs_contract", "output_schema", "required_outputs", "output_fields", "outputs", "deliverables")


def find_contract(obj):
    if isinstance(obj, dict):
        for k in CONTRACT_KEYS:
            if k in obj:
                return obj[k]
        for v in obj.values():
            r = find_contract(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_contract(v)
            if r is not None:
                return r
    return None


def contract_fields(contract):
    if contract is None:
        return []
    if isinstance(contract, dict):
        for k in ("fields", "properties", "required_fields", "required"):
            if k in contract and isinstance(contract[k], (dict, list)):
                return contract_fields(contract[k])
        return list(contract.keys())
    if isinstance(contract, list):
        out = []
        for it in contract:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict):
                n = it.get("name") or it.get("field") or it.get("key") or it.get("id")
                if n:
                    out.append(n)
        return out
    if isinstance(contract, str):
        return [contract]
    return []


def find_pdf_strings(obj, acc):
    if isinstance(obj, dict):
        for v in obj.values():
            find_pdf_strings(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            find_pdf_strings(v, acc)
    elif isinstance(obj, str) and obj.lower().endswith(".pdf"):
        acc.append(obj)
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
        hits += glob.glob(os.path.join(b, "**", os.path.basename(p)), recursive=True)
    return hits[0] if hits else None


def guess_kind(field, desc=""):
    """Suggest which np_build output a contract field probably wants (agent decides)."""
    for t in (field.lower(), (json.dumps(desc, ensure_ascii=False) if desc else "").lower()):
        k = _kind_of(t)
        if k:
            return k
    return "agent_value"


def _kind_of(t):
    for kind, keys in (("nanopublications", ("nanopub", "knowledge_unit", "knowledge unit")),
                       ("assertions", ("assertion", "claim", "statement", "finding", "断言", "结论")),
                       ("evidence", ("evidence", "support", "quote", "snippet", "证据")),
                       ("provenance", ("provenance", "source", "metadata", "citation", "bibliograph", "来源", "元数据")),
                       ("quality", ("quality", "check", "issue", "flag", "validation", "质量", "校验")),
                       ("relations", ("relation", "triple", "edge", "关系")),
                       ("nquads", ("nquads", "n-quads", ".nq")),
                       ("trig", ("trig", "rdf", "turtle")),
                       ("jsonld", ("json-ld", "jsonld")),
                       ("unreported", ("unreported", "missing", "未报告", "缺失")),
                       ("conflicts", ("conflict", "contradiction", "冲突", "矛盾")),
                       ("uncertainty", ("uncertainty", "limitation", "边界", "不确定")),
                       ("review", ("review", "report", "报告")),
                       ("summary", ("summary", "overview", "总结", "摘要说明", "综述"))):
        if any(k in t for k in keys):
            return kind
    if re.search(r"\bstatus\b|运行状态", t):
        return "status"
    return None


def main():
    C.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", default="/input/case.json")
    ap.add_argument("--task", default=None)
    ap.add_argument("--out", default="/app/output")
    ap.add_argument("--work", default="/app/work")
    ap.add_argument("--pdf", nargs="*", default=[])
    ap.add_argument("--password", default=None)
    ap.add_argument("--pages", default=None, help="parse only these pages, e.g. 1-8 (unread pages are listed)")
    ap.add_argument("--top", type=int, default=40, help="claim candidates listed in candidates.md")
    a = ap.parse_args()
    t0 = time.time()
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(a.work, exist_ok=True)
    case, case_err = {}, None
    if os.path.exists(a.case):
        try:
            case = C.load_json(a.case)
        except Exception as e:
            case_err = f"case.json unreadable: {e}"
    else:
        case_err = f"{a.case} not found"
    case_dir = os.path.dirname(os.path.abspath(a.case))
    task_path = a.task or next((p for p in (os.path.join(case_dir, "task.md"), "/input/task.md", "task.md") if os.path.exists(p)), None)
    bases = [case_dir, "/input", os.getcwd()]
    pdfs = []
    for s in find_pdf_strings(case, []) + list(a.pdf):
        r = resolve(s, bases)
        if r and os.path.abspath(r) not in [os.path.abspath(x) for x in pdfs]:
            pdfs.append(r)
    if not pdfs:
        for b in (case_dir, "/input"):
            for p in sorted(glob.glob(os.path.join(b, "**", "*.pdf"), recursive=True)):
                if p not in pdfs:
                    pdfs.append(p)
    sources, change_log, cand_lines = [], [], []
    for p in pdfs:
        stem = os.path.splitext(os.path.basename(p))[0]
        pdir = os.path.join(a.out, "parsed", stem)
        try:
            r = parse_pdf.parse(p, pdir, password=a.password, pages=parse_pdf.parse_pages(a.pages),
                                cache_dir=os.path.join(a.work, "parse_cache"))
        except Exception as e:  # parser crash: record, continue with other PDFs
            r = {"status": "failed", "error": f"parser exception: {e}"}
        src = {"file": p, "sha256": C.sha256_file(p), "parsed_dir": pdir, "parse_status": r.get("status"),
               "parse_error": r.get("error"), "page_count": (r.get("meta") or {}).get("page_count"),
               "pages_not_parsed": r.get("pages_not_parsed"), "unresolved_pages": r.get("unresolved_pages", []),
               "ocr_pages": r.get("ocr_pages", []), "n_warnings": len(r.get("warnings", []))}
        docp = os.path.join(pdir, "doc.json")
        if r.get("status") != "failed" and os.path.exists(docp):
            doc = C.load_json(docp)
            md = C.extract_metadata(p, doc)
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
            cand_lines.append(f"# {stem}  ({len(sents)} sentences; parsed dir {pdir})\n")
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
                sec = " > ".join(s.get("section_path") or [])[:40]
                cand_lines.append(f"{s['id']} | {sec} | {' '.join(tag)} | {s['text'][:300].replace('|', '/')}")
            cand_lines.append("")
            if r.get("unresolved_pages"):
                cand_lines.append(f"WARNING: pages without a usable text layer: {r['unresolved_pages']} (no sentences from them; read page images, see SKILL.md)\n")
        sources.append(src)
        change_log.append({"step": "parse+index", "file": p, "status": r.get("status"), "tool": C.TOOL,
                           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    contract = find_contract(case)
    fields = contract_fields(contract)
    case_id = case.get("case_id") or case.get("id") or os.environ.get("CASE_ID")
    result = {"case_id": case_id}
    for f in fields:
        result.setdefault(f, None)
    descs = contract.get("properties") if (isinstance(contract, dict) and isinstance(contract.get("properties"), dict)) else (contract if isinstance(contract, dict) else {})
    result.update({
        "status": "pending_agent_fill",
        "nanopublications": result.get("nanopublications") if "nanopublications" in fields else [],
        "sources": sources,
        "field_status": {f: None for f in fields if f != "case_id"},
        "unreported": [], "uncertainty": [], "conflicts": [],
        "run_status": {"init_seconds": round(time.time() - t0, 2), "case_error": case_err, "n_pdfs": len(pdfs),
                       "task_file": task_path, "tool": C.TOOL},
        "change_log": change_log,
    })
    if not pdfs:
        result["status"] = "failed"
        result["run_status"]["reason"] = "no PDF found in case.json or the input directory"
    elif all(s["parse_status"] == "failed" for s in sources):
        result["status"] = "failed"
        result["run_status"]["reason"] = "; ".join(f"{s['file']}: {s['parse_error']}" for s in sources)
    C.dump_json(result, os.path.join(a.out, "result.json"))
    with open(os.path.join(a.work, "candidates.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(cand_lines) + "\n")
    suggestions = {f: guess_kind(f, descs.get(f) if isinstance(descs, dict) else "") for f in fields if f != "case_id"}
    draft_path = os.path.join(a.work, "draft.json")
    if not os.path.exists(draft_path):
        C.dump_json({
            "_help": "Fill 'assertions' (one atomic claim each) and 'fields'. Evidence = sentence ids from sentences.jsonl "
                     "or {page, quote}. Then: python3 np_build.py --draft WORK/draft.json --out OUT. See SKILL.md.",
            "source": os.path.splitext(os.path.basename(pdfs[0]))[0] if pdfs else None,
            "source_overrides": {},
            "assertions": [],
            "fields": {f: {"kind": k} for f, k in suggestions.items()},
            "unreported": [], "conflicts": [], "uncertainty": []}, draft_path)
    print(json.dumps({"case_id": case_id, "pdfs": pdfs, "contract_fields": fields,
                      "field_kind_suggestions": suggestions,
                      "parse_status": [s["parse_status"] for s in sources],
                      "metadata": [s.get("metadata_preview") for s in sources],
                      "result": os.path.join(a.out, "result.json"),
                      "sentences": [os.path.join(s["parsed_dir"], "sentences.jsonl") for s in sources],
                      "candidates": os.path.join(a.work, "candidates.md"), "draft": draft_path,
                      "task_file": task_path, "case_error": case_err, "seconds": round(time.time() - t0, 2)},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
