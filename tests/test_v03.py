"""Regression tests for nanopub-evidence-builder 0.3.0 features.

    python tests/test_v03.py [--skill submission] [--pdfs tests/pdfs] [--out execution/results/v03]

A. rule unit tests on synthetic records (no PDF): magnitude equivalence / mismatch (R09/R27), compound
   claims (R07: respectively, several values, Chinese clauses), attribution (R20/R29), limiters (R16),
   reasoning arithmetic / comparison / operands (R28, incl. the Table 3 vs Table 4 mix-up), image regions (R30),
   figure claims (R31), hedges (R12);
B. contract parsing + validation (np_contract): wrappers, required vs optional, nested items, integer vs number;
C. end-to-end on resnet.pdf / attention.pdf and synthetic .md / .txt / .docx papers: case_id 0, optional fields,
   schema mismatch -> partial, primary vs larger supplement, corrupt / empty / malformed drafts never clobber
   result.json, --merge, strict N-Quads.
Prints one JSON line per test and a summary; exit code 1 if any test fails.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = []


def ok(name, cond, **info):
    RESULTS.append({"test": name, "ok": bool(cond), **info})
    print(json.dumps(RESULTS[-1], ensure_ascii=False, default=str))


# ------------------------------------------------------------------ A. rules
def rec(text, quotes, pred="has_value", quals=None, scope="this_work", et="direct", reasoning=None, ev=None, modality="asserted",
        subject="the model", obj="value"):
    evs = ev if ev is not None else [{"kind": "text", "role": "support", "match": "exact_sentence", "quote": q, "citations": [], "section_path": ["Results"]}
                                     for q in quotes]
    return {"assertion": {"text": text, "subject": {"label": subject}, "predicate": pred, "object": {"label": obj},
                          "qualifiers": quals or {}, "modality": modality, "scope": scope, "cited_reference": "[3]" if scope == "cited_work" else None,
                          "cited_reference_resolved": [{"label": "3"}] if scope == "cited_work" else None},
            "evidence_type": et, "reasoning": reasoning, "evidence": evs,
            "provenance": {"source": {"title": {"value": "T"}, "pdf_sha256": {"value": "ab" * 32}, "authors": {"value": ["A B"]},
                                      "year": {"value": 2020}, "doi": {"value": "10.1/x"}}}}


def rules(r, vocab):
    import np_check
    return sorted({f"{i['rule']}:{i['severity']}" for i in np_check.check_record(r, vocab)})


def cell(v, ctx):
    return {"kind": "table_cell", "role": "support", "match": "table_cell", "quote": v, "table_context": ctx, "citations": []}


def test_rules(vocab):
    has = lambda rs, x: any(r.startswith(x) for r in rs)
    rs = rules(rec("The model has 6 billion parameters.", ["该模型共有 60 亿 个参数。"], quals={"value": "6", "unit": "billion"}, obj="parameters"), vocab)
    ok("R09 accepts 6 billion == 60 亿", not has(rs, "R09") and not has(rs, "R27"), rules=rs)
    rs = rules(rec("ImageNet has 1.28 million training images.", ["The training set has 1,281,167 images."], subject="ImageNet", obj="training images",
                   quals={"value": "1.28", "unit": "million"}), vocab)
    ok("R09 accepts 1.28 million as rounding of 1,281,167", not has(rs, "R09") and not has(rs, "R27"), rules=rs)
    rs = rules(rec("训练集包含 128万 张图像。", ["ImageNet contains 1.28 million training images."], subject="训练集", obj="图像"), vocab)
    ok("R09 accepts 128万 == 1.28 million", not has(rs, "R09") and not has(rs, "R27"), rules=rs)
    rs = rules(rec("The model has 6 million parameters.", ["The model has 6 billion parameters."], quals={"value": "6", "unit": "million"}, obj="parameters"), vocab)
    ok("R27 flags 6 million vs 6 billion", "R27:fail" in rs, rules=rs)
    rs = rules(rec("The model has 6 billion parameters.", ["The model has 6 billion parameters."], quals={"value": "6", "unit": "billion"}, obj="parameters"), vocab)
    ok("R27 silent on identical magnitude", not has(rs, "R27") and not has(rs, "R09"), rules=rs)
    rs = rules(rec("ResNet-50 and ResNet-101 achieve 6.71% and 6.05% top-5 error, respectively.", ["ResNet-50 and ResNet-101 achieve 6.71% and 6.05% top-5 error"]), vocab)
    ok("R07 flags 'respectively'", "R07:fail" in rs, rules=rs)
    rs = rules(rec("ResNet-152 achieves 4.49% top-5 error and 19.38% top-1 error.", ["ResNet-152 19.38 4.49"], pred="achieves", quals={"value": "4.49"}), vocab)
    ok("R07 flags several values in one record", "R07:fail" in rs, rules=rs)
    rs = rules(rec("该方法提高了准确率并且降低了推理延迟。", ["该方法提高了准确率并且降低了推理延迟。"], pred="improves", subject="该方法", obj="准确率"), vocab)
    ok("R07 flags Chinese clause conjunction without comma", "R07:fail" in rs, rules=rs)
    rs = rules(rec("Accuracy stays between 70% and 80% on CIFAR-10.", ["Accuracy stays between 70% and 80% on CIFAR-10."], pred="has_property", subject="Accuracy", obj="range"), vocab)
    ok("R07 silent on a range ('between 70% and 80%')", not has(rs, "R07"), rules=rs)
    rs = rules(rec("Dropout reduces overfitting.", ["Previous studies have shown that dropout reduces overfitting."], pred="reduces", subject="Dropout", obj="overfitting"), vocab)
    ok("R20 flags attribution phrase without citation marker", "R20:warn" in rs, rules=rs)
    rs = rules(rec("Our method reduces error.", ["We show that our method reduces error compared to the baseline."], pred="reduces", subject="our method", obj="error"), vocab)
    ok("R20 silent on first-person finding", not has(rs, "R20"), rules=rs)
    rs = rules(rec("The model reduces error.", ["We find that our model reduces error compared with the baseline."], pred="reduces", scope="cited_work", et="cited",
                   subject="model", obj="error"), vocab)
    ok("R29 flags cited_work on an own first-person result", "R29:warn" in rs, rules=rs)
    rs = rules(rec("The effect holds for models larger than 1B parameters.", ["The effect holds only for models larger than 1B parameters."],
                   pred="has_property", subject="effect", obj="models larger than 1B parameters"), vocab)
    ok("R16 flags a dropped limiter ('only')", "R16:warn" in rs, rules=rs)
    t4 = "Table 4. Error rates (%) of single-model results on the ImageNet validation set"
    ev_ok = [cell("4.49", "ResNet-152 19.38 4.49 top-5 err. " + t4), cell("5.71", "ResNet-34 B 21.84 5.71 top-5 err. " + t4)]
    base = dict(pred="lower_than", et="inferred", subject="ResNet-152", obj="ResNet-34 B")
    rs = rules(rec("ResNet-152 has 1.22 points lower top-5 error than ResNet-34 B.", [], quals={"value": "1.22"}, reasoning="Table 4 top-5 err.: 5.71 - 4.49 = 1.22", ev=ev_ok, **base), vocab)
    ok("R28 accepts a correct inferred difference", not has(rs, "R28") and not has(rs, "R09"), rules=rs)
    rs = rules(rec("ResNet-152 has 3.91 points lower top-5 error than ResNet-34 B.", [], quals={"value": "3.91"}, reasoning="Table 4 top-5 err.: 7.40 - 4.49 = 3.91",
                   ev=[cell("4.49", "ResNet-152 4.49"), cell("7.40", "ResNet-34 C 7.40")], **base), vocab)
    ok("R28 flags an arithmetic step that does not compute", "R28:fail" in rs, rules=rs)
    rs = rules(rec("ResNet-152 has lower top-5 error than ResNet-34 B.", [], reasoning="Table 4: ResNet-152 5.71 < ResNet-34 B 4.49", ev=ev_ok, **base), vocab)
    ok("R28 flags a false comparison", "R28:fail" in rs, rules=rs)
    rs = rules(rec("ResNet-152 has lower top-5 error than ResNet-34 B.", [], reasoning="Table 4 top-5 err.: ResNet-152 4.49 < ResNet-34 B 7.40", ev=ev_ok, **base), vocab)
    ok("R28 flags an operand read from another table (7.40 is Table 3, ResNet-34 C)", "R28:fail" in rs, rules=rs)
    rs = rules(rec("ResNet-152 has 2.91 points lower top-5 error.", [], quals={"value": "2.91"}, reasoning="7.40 - 4.49 = 2.91", ev=ev_ok, **base), vocab)
    ok("R28 closes the 'number only in reasoning' loophole", "R28:fail" in rs, rules=rs)
    img = {"kind": "image_region", "role": "support", "match": "image_read", "quote": "loss 2.3", "transcription": "loss 2.3", "page": 3, "citations": []}
    rs = rules(rec("The loss is 2.3.", [], ev=[img]), vocab)
    ok("R30 fails an image region without bbox", "R30:fail" in rs, rules=rs)
    rs = rules(rec("The loss is 2.3.", [], ev=[dict(img, bbox=[1, 2, 3, 4], crop="c.png", provenance={"source_pdf_sha256": "ab" * 32})]), vocab)
    ok("R30 silent on a reproducible crop (R26 warn stays)", not has(rs, "R30") and "R26:warn" in rs, rules=rs)
    rs = rules(rec("As shown in Fig. 3, the loss decreases with model size.", ["The loss decreases with model size (Fig. 3)."], pred="decreases", subject="loss", obj="model size"), vocab)
    ok("R31 flags a figure claim without figure evidence", "R31:warn" in rs, rules=rs)
    rs = rules(rec("As shown in Fig. 3, the loss decreases with model size.", ["The loss decreases with model size.", "Figure 3: Loss decreases with model size."],
                   pred="decreases", subject="loss", obj="model size"), vocab)
    ok("R31 silent when the caption is bound", not has(rs, "R31"), rules=rs)
    rs = rules(rec("Larger batches improve accuracy.", ["Larger batches probably improve accuracy."], pred="improves", subject="Larger batches", obj="accuracy"), vocab)
    ok("R12 flags dropped 'probably'", "R12:warn" in rs, rules=rs)
    # P1-4 / P1-5 regressions
    ev_mix = [
        {"kind": "table_cell", "role": "support", "match": "table_cell", "table": "Table 4", "table_id": "T4", "column": "top-5 err.", "quote": "4.49", "table_context": "ResNet-152 4.49 top-5 err.", "citations": []},
        {"kind": "table_cell", "role": "support", "match": "table_cell", "table": "Table 7", "table_id": "T7", "column": "error", "quote": "9.38", "table_context": "Maxout 9.38 error", "citations": []},
    ]
    rs = rules(rec("ResNet-152 has 4.89 points lower error than ResNet-34 B.", [], pred="lower_than", et="inferred", subject="ResNet-152", obj="ResNet-34 B",
                   quals={"value": "4.89"}, reasoning="9.38 - 4.49 = 4.89", ev=ev_mix), vocab)
    ok("R28 flags cross-table inferred comparison (P1-4)", "R28:fail" in rs, rules=rs)
    rs = rules(rec("Treatment has HR 1.5 for survival.", ["Treatment has OR 1.5 for survival."], quals={"value": "1.5", "metric": "HR"}), vocab)
    ok("R09 flags metric swap HR vs OR (P1-5)", "R09:fail" in rs, rules=rs)
    rs = rules(rec("Method reduces latency while lowering power.", ["Method reduces latency while lowering power."], pred="reduces", subject="Method", obj="latency"), vocab)
    ok("R07 flags 'while lowering' compound clause (P1-5)", "R07:fail" in rs, rules=rs)


# ------------------------------------------------------------------ B. contract
def test_contract():
    import np_contract as K
    sp = K.parse_contract({"type": "object", "properties": {"assertions": {"type": "array", "items": {"type": "object", "required": ["claim"]}},
                                                             "n": {"type": "integer"}, "notes": {"type": "string"}}, "required": ["assertions", "n"]})
    req = {f["name"]: f["required"] for f in sp["fields"]}
    ok("contract: JSON Schema required vs optional", req == {"assertions": True, "n": True, "notes": False}, got=req)
    raw, key = K.find_contract({"case_id": 0, "task": {"output_fields": [{"name": "a", "type": "string"}, {"name": "b", "required": False}]}})
    sp = K.parse_contract(raw)
    ok("contract: nested output_fields list with required:false", key == "output_fields" and [(f["name"], f["required"]) for f in sp["fields"]] == [("a", True), ("b", False)], got=sp["fields"])
    raw, key = K.find_contract({"deliverables": [{"file": "result.json", "fields": ["x", "y"]}, {"file": "preview.png"}]})
    sp = K.parse_contract(raw)
    ok("contract: deliverables with fields + file-only items", [f["name"] for f in sp["fields"]] == ["x", "y"] and sp["files"] == ["result.json", "preview.png"], got=sp)
    raw, key = K.find_contract({"schema": {"properties": {"z": {"type": "number"}}, "required": []}})
    sp = K.parse_contract(raw)
    ok("contract: 'schema' wrapper, empty required -> optional", key == "schema" and sp["fields"][0]["required"] is False, got=sp["fields"])
    sp = K.parse_contract({"required_fields": ["a"], "optional_fields": ["b"]})
    ok("contract: required_fields / optional_fields", [(f["name"], f["required"]) for f in sp["fields"]] == [("a", True), ("b", False)])
    v, p, n = K.validate("12", {"type": "integer"})
    ok("validate: '12' -> 12 for integer", v == 12 and not p, v=v, p=p)
    v, p, n = K.validate(3.5, {"type": "integer"})
    ok("validate: 3.5 is not an integer (no truncation)", bool(p) and v == 3.5, p=p)
    v, p, n = K.validate(3, {"type": "number"})
    ok("validate: integer is a number", not p)
    v, p, n = K.validate(True, {"type": "integer"})
    ok("validate: boolean is not an integer", bool(p), p=p)
    v, p, n = K.validate([{"claim": "x"}, {"text": "y"}], {"type": "array", "items": {"type": "object", "required": ["claim"], "properties": {"claim": {"type": "string"}}}}, "a")
    ok("validate: nested items required key missing", p == ["a[1].claim: required key missing"], p=p)
    v, p, n = K.validate({"k": {"n": "2"}}, {"type": "object", "properties": {"k": {"type": "object", "properties": {"n": {"type": "integer"}}}}})
    ok("validate: nested properties coercion", v == {"k": {"n": 2}} and not p, v=v)
    # P0-1, P0-2, P0-4 contract regressions
    u_sch = {"type": ["array", "object"], "items": {"type": "object", "required": ["claim"]}}
    sp_u = K.parse_contract({"properties": {"items_or_obj": u_sch}, "required": ["items_or_obj"]})
    ok("contract: union type ['array', 'object'] does not crash (P0-1)", sp_u["fields"][0]["type"] == ["array", "object"])
    v, p, n = K.validate([{"text": "x"}], u_sch, "items_or_obj")
    ok("validate: union type continues recursive items check (P0-1)", p == ["items_or_obj[0].claim: required key missing"], p=p)
    mixed_c = K.find_contracts({"output_contract": ["a"], "output_fields": ["b"], "schema": {"properties": {"c": {"type": "string"}}, "required": ["c"]}, "deliverables": [{"file": "r.json", "fields": ["d"]}]})
    sp_m = K.parse_contracts(mixed_c)
    ok("contract: mixed wrappers merged without dropping fields (P0-2)", [f["name"] for f in sp_m["fields"]] == ["a", "b", "c", "d"], got=[f["name"] for f in sp_m["fields"]])
    v, p, n = K.validate("1e999", {"type": "number"}, "big")
    ok("validate: '1e999' rejected (no Infinity) (P0-4)", bool(p) and v == "1e999", p=p, v=v)


# ------------------------------------------------------------------ C. end to end
def run(scripts, script, *args):
    return subprocess.run([sys.executable, os.path.join(scripts, script)] + list(args), capture_output=True, text=True, encoding="utf-8")


def jout(r):
    return json.loads(r.stdout[r.stdout.find("{"):]) if "{" in r.stdout else {}


def fhash(p):
    with open(p, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def make_case(base, name, files, case):
    d = os.path.join(base, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(d, "input"))
    for dst, src in files.items():
        if isinstance(src, bytes):
            with open(os.path.join(d, "input", dst), "wb") as fh:
                fh.write(src)
        elif isinstance(src, str) and os.path.exists(src):
            shutil.copy(src, os.path.join(d, "input", dst))
        else:
            with open(os.path.join(d, "input", dst), "w", encoding="utf-8") as fh:
                fh.write(src)
    with open(os.path.join(d, "input", "case.json"), "w", encoding="utf-8") as fh:
        json.dump(case, fh, ensure_ascii=False)
    return d, os.path.join(d, "input", "case.json"), os.path.join(d, "output"), os.path.join(d, "work")


def write(p, obj):
    with open(p, "w", encoding="utf-8") as fh:
        if isinstance(obj, str):
            fh.write(obj)
        else:
            json.dump(obj, fh, ensure_ascii=False)


def load(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def docx_bytes(paras):
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = ""
    for style, text, brk in paras:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        br = '<w:r><w:br w:type="page"/></w:r>' if brk else ""
        body += f"<w:p>{ppr}{br}<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p>"
    doc = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {W}><w:body>{body}</w:body></w:document>'
    import io
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", doc)
        z.writestr("docProps/core.xml", '<?xml version="1.0"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                                        'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>Ada Lovelace; Alan Turing</dc:creator></cp:coreProperties>')
    return bio.getvalue()


MD_PAPER = """---
title: Sparse Widgets Improve Gadget Recall
authors:
  - Ada Lovelace
  - Alan Turing
date: 2024-05-01
doi: 10.5555/widgets.2024.7
---

# Sparse Widgets Improve Gadget Recall

## Abstract

We show that sparse widgets improve gadget recall by 4.2 points on the GadgetBench test set. The widget model has 1.3 billion parameters.

## Results

| Model | Recall | Params |
|---|---|---|
| Dense | 71.0 | 1.3B |
| Sparse | 75.2 | 1.3B |

Table 1: Recall on GadgetBench test.

Prior studies have shown that gadget recall saturates at large scale [2].

## References

[1] A. Lovelace. Widgets. 2020.
[2] A. Turing. Gadgets saturate. 2021.
"""


def test_e2e(scripts, pdfs, base):
    resnet, attention = os.path.join(pdfs, "resnet.pdf"), os.path.join(pdfs, "attention.pdf")
    ok_draft = {"source": "paper", "assertions": [
        {"id": "a1", "text": "An ensemble of residual nets achieves 3.57% error on the ImageNet test set.", "subject": "ensemble of residual nets",
         "predicate": "achieves", "object": "error on the ImageNet test set", "qualifiers": {"value": "3.57", "unit": "%", "metric": "error", "dataset": "ImageNet test set"},
         "evidence": ["p1-b5-s6"]}],
        "fields": {"assertions": {"kind": "assertions"}, "n_assertions": {"value": "1", "from": ["a1"]}, "status": {"kind": "status"}}}
    schema = {"type": "object", "properties": {"assertions": {"type": "array", "items": {"type": "object", "required": ["assertion_id", "text"]}},
                                               "n_assertions": {"type": "integer"}, "status": {"type": "string"}, "notes": {"type": "string"}},
              "required": ["assertions", "n_assertions", "status"]}
    # C1 case_id 0 + optional field + integer coercion
    d, case, out, work = make_case(base, "c1_case0", {"paper.pdf": resnet}, {"case_id": 0, "pdf": "paper.pdf", "output_contract": schema})
    r = jout(run(scripts, "np_init.py", "--case", case, "--out", out, "--work", work))
    write(os.path.join(work, "draft.json"), ok_draft)
    run(scripts, "np_build.py", "--draft", os.path.join(work, "draft.json"), "--out", out, "--created", "2026-11-25T00:00:00Z", "--no-previews")
    res = load(os.path.join(out, "result.json"))
    ok("e2e: case_id 0 kept", res.get("case_id") == 0 and r.get("case_id") == 0, case_id=res.get("case_id"))
    ok("e2e: optional field unfilled -> still success", res["status"] == "success" and res["status_reason"]["unfilled_optional_fields"] == ["notes"]
       and "notes" in res and res["notes"] is None, status=res["status"], reason=res["status_reason"])
    ok("e2e: '1' coerced to integer 1 for an integer field", res.get("n_assertions") == 1, v=res.get("n_assertions"))
    ok("e2e: status field filled", res.get("status") == "success")
    # C2 type mismatch on a required field -> partial
    bad = json.loads(json.dumps(ok_draft))
    bad["fields"]["n_assertions"] = {"value": "one", "from": ["a1"]}
    write(os.path.join(work, "draft_bad.json"), bad)
    run(scripts, "np_build.py", "--draft", os.path.join(work, "draft_bad.json"), "--out", out, "--created", "2026-11-25T00:00:00Z", "--no-previews")
    res = load(os.path.join(out, "result.json"))
    ok("e2e: required integer field given 'one' -> partial + field_problems", res["status"] == "partial" and "n_assertions" in res["status_reason"]["unfilled_fields"]
       and "n_assertions" in (res.get("field_problems") or {}), reason=res["status_reason"])
    # C3 nested item schema not met -> partial; rename fixes it
    sch2 = json.loads(json.dumps(schema))
    sch2["properties"]["assertions"]["items"]["required"] = ["claim"]
    d, case, out, work = make_case(base, "c3_nested", {"paper.pdf": resnet}, {"case_id": "c3", "pdf": "paper.pdf", "output_contract": sch2})
    run(scripts, "np_init.py", "--case", case, "--out", out, "--work", work)
    write(os.path.join(work, "draft.json"), ok_draft)
    run(scripts, "np_build.py", "--draft", os.path.join(work, "draft.json"), "--out", out, "--no-previews")
    st1 = load(os.path.join(out, "result.json"))["status"]
    ren = json.loads(json.dumps(ok_draft))
    ren["fields"]["assertions"] = {"kind": "assertions", "rename": {"text": "claim"}}
    write(os.path.join(work, "draft.json"), ren)
    run(scripts, "np_build.py", "--draft", os.path.join(work, "draft.json"), "--out", out, "--no-previews")
    res = load(os.path.join(out, "result.json"))
    ok("e2e: nested items.required enforced, 'rename' satisfies it", st1 == "partial" and res["status"] == "success" and res["assertions"][0].get("claim"),
       before=st1, after=res["status"])
    # C4 primary vs larger supplement
    big, small = (attention, resnet) if os.path.getsize(attention) > os.path.getsize(resnet) else (resnet, attention)
    d, case, out, work = make_case(base, "c4_roles", {"supp_big.pdf": big, "main.pdf": small},
                                   {"case_id": "c4", "materials": [{"path": "supp_big.pdf", "role": "supplement"}, {"path": "main.pdf", "role": "primary"}],
                                    "output_contract": ["assertions"]})
    r = jout(run(scripts, "np_init.py", "--case", case, "--out", out, "--work", work))
    ok("e2e: role=primary beats a larger supplement", r.get("primary_source") == "main" and load(os.path.join(work, "draft.json"))["source"] == "main",
       materials=r.get("materials"))
    d, case, out, work = make_case(base, "c4b_keys", {"SI_appendix.pdf": big, "article.pdf": small},
                                   {"case_id": "c4b", "supplementary_pdf": "SI_appendix.pdf", "paper_pdf": "article.pdf", "output_contract": ["assertions"]})
    r = jout(run(scripts, "np_init.py", "--case", case, "--out", out, "--work", work))
    ok("e2e: key names (paper_pdf vs supplementary_pdf) decide the primary", r.get("primary_source") == "article", materials=r.get("materials"))
    # C5 draft protection
    d, case, out, work = make_case(base, "c5_protect", {"paper.pdf": resnet}, {"case_id": "c5", "pdf": "paper.pdf", "output_contract": ["assertions"]})
    run(scripts, "np_init.py", "--case", case, "--out", out, "--work", work)
    write(os.path.join(work, "draft.json"), ok_draft)
    run(scripts, "np_build.py", "--draft", os.path.join(work, "draft.json"), "--out", out, "--no-previews")
    rp = os.path.join(out, "result.json")
    h0 = fhash(rp)
    write(os.path.join(work, "broken.json"), '{"assertions": [ {"id": "a1", ')
    rb = run(scripts, "np_build.py", "--draft", os.path.join(work, "broken.json"), "--out", out, "--no-previews")
    ok("e2e: corrupt draft -> exit 2, result.json unchanged, build_error.json written", rb.returncode == 2 and fhash(rp) == h0
       and os.path.exists(os.path.join(out, "build_error.json")), code=rb.returncode)
    write(os.path.join(work, "empty.json"), {"assertions": [], "fields": {}})
    rb = run(scripts, "np_build.py", "--draft", os.path.join(work, "empty.json"), "--out", out, "--no-previews")
    ok("e2e: empty draft after a good build -> exit 3, result.json unchanged", rb.returncode == 3 and fhash(rp) == h0, code=rb.returncode)
    write(os.path.join(work, "wrongtype.json"), {"assertions": {"id": "a1"}})
    rb = run(scripts, "np_build.py", "--draft", os.path.join(work, "wrongtype.json"), "--out", out, "--no-previews")
    ok("e2e: assertions not a list -> exit 2, unchanged", rb.returncode == 2 and fhash(rp) == h0, code=rb.returncode)
    mixed = json.loads(json.dumps(ok_draft))
    mixed["assertions"] += ["not an object", {"id": "a3", "text": "x", "subject": "s", "predicate": "achieves", "object": "o", "evidence": [["nested", "list"]]}]
    write(os.path.join(work, "mixed.json"), mixed)
    rb = run(scripts, "np_build.py", "--draft", os.path.join(work, "mixed.json"), "--out", out, "--no-previews")
    res = load(rp)
    ok("e2e: malformed assertions isolated -> others built, status success + run_warnings, build_errors listed",
       rb.returncode == 0 and len(res["assertions"]) == 1 and len(res["build_errors"]) == 2 and res["status"] == "success"
       and any(w["type"] == "assertions_not_built" for w in res["run_warnings"])
       and os.path.exists(os.path.join(out, "result.prev.json")), build_errors=res.get("build_errors"))
    # C5b failing record quarantined; sloppy predicate / enum spelling auto-normalised
    qd = json.loads(json.dumps(ok_draft))
    qd["assertions"][0].update({"predicate": "Achieve", "modality": "Assert", "evidence_type": "Explicit"})
    qd["assertions"].append({"id": "a9", "text": "An ensemble of residual nets achieves 9.99% error on the ImageNet test set.", "subject": "ensemble of residual nets",
                             "predicate": "achieves", "object": "error", "qualifiers": {"value": "9.99", "unit": "%"}, "evidence": ["p1-b5-s6"]})
    write(os.path.join(work, "quar.json"), qd)
    rb = run(scripts, "np_build.py", "--draft", os.path.join(work, "quar.json"), "--out", out, "--no-previews")
    res = load(rp)
    nq = open(os.path.join(out, "nanopubs.nq"), encoding="utf-8").read()
    allrec = load(os.path.join(out, "nanopubs.json"))["nanopublications"]
    ok("e2e: failing record quarantined (kept for audit, not exported) -> success",
       rb.returncode == 0 and res["status"] == "success" and [q["assertion_id"] for q in res["quarantined_assertions"]] == ["a9"]
       and [x["assertion_id"] for x in res["assertions"]] == ["a1"] and "9.99" not in nq and len(allrec) == 2,
       status=res["status"], q=res.get("quarantined_assertions"))
    ok("e2e: predicate 'Achieve' / modality 'Assert' / evidence_type 'Explicit' auto-normalised",
       res["assertions"][0].get("predicate") == "achieves" and any(w["type"] == "auto_normalized" for w in res["run_warnings"]),
       a1=res["assertions"][0], warnings=res.get("run_warnings"))
    d, case, out, work = make_case(base, "c5c_unrep", {"paper.pdf": resnet}, {"case_id": "u1", "pdf": "paper.pdf", "output_contract": schema})
    run(scripts, "np_init.py", "--case", case, "--out", out, "--work", work)
    rp = os.path.join(out, "result.json")
    ud = json.loads(json.dumps(ok_draft))
    ud["fields"]["n_assertions"] = {"value": None}
    ud["unreported"] = [{"field": "n_assertions", "reason": "not reported in the paper (searched: abstract, tables)"}]
    write(os.path.join(work, "unrep.json"), ud)
    run(scripts, "np_build.py", "--draft", os.path.join(work, "unrep.json"), "--out", out, "--no-previews")
    res = load(rp)
    ok("e2e: required field declared unreported with a reason -> success + warning, value null",
       res["status"] == "success" and res.get("n_assertions") is None
       and any(w["type"] == "required_fields_unreported_by_paper" for w in res["run_warnings"]), reason=res.get("status_reason"))
    ud["unreported"] = []
    write(os.path.join(work, "unrep2.json"), ud)
    run(scripts, "np_build.py", "--draft", os.path.join(work, "unrep2.json"), "--out", out, "--no-previews")
    ok("e2e: required field left empty without a reason -> still partial", load(rp)["status"] == "partial")
    # C6 --merge
    write(os.path.join(work, "draft.json"), ok_draft)
    run(scripts, "np_build.py", "--draft", os.path.join(work, "draft.json"), "--out", out, "--no-previews")
    add = {"assertions": [{"id": "a2", "text": "ResNet-152 has a single-model top-5 error of 4.49% on the ImageNet validation set.", "subject": "ResNet-152",
                           "predicate": "has_value", "object": "top-5 error", "qualifiers": {"value": "4.49", "unit": "%", "metric": "top-5 err.", "dataset": "ImageNet validation set", "condition": "single model"},
                           "evidence": [{"type": "table_cell", "table": "Table 4", "row": {"method": "ResNet-152"}, "column": "top-5 err.", "value": "4.49"}]}]}
    write(os.path.join(work, "add.json"), add)
    run(scripts, "np_build.py", "--draft", os.path.join(work, "add.json"), "--out", out, "--no-previews", "--merge")
    res = load(rp)
    ok("e2e: --merge appends a2 to the built a1", [x["assertion_id"] for x in res["assertions"]] == ["a1", "a2"] and res["status"] == "success", ids=[x["assertion_id"] for x in res["assertions"]])
    write(os.path.join(work, "del.json"), {"assertions": [], "delete": ["a2"]})
    run(scripts, "np_build.py", "--draft", os.path.join(work, "del.json"), "--out", out, "--no-previews", "--merge")
    ok("e2e: --merge with delete removes a2", [x["assertion_id"] for x in load(rp)["assertions"]] == ["a1"])
    # C7 markdown paper
    d, case, out, work = make_case(base, "c7_md", {"paper.md": MD_PAPER, "task.md": "# Task\nExtract claims."},
                                   {"case_id": "c7", "paper": "paper.md", "output_contract": {"assertions": "claims", "provenance": "source"}})
    r = jout(run(scripts, "np_init.py", "--case", case, "--out", out, "--work", work))
    pdir = os.path.join(out, "parsed", "paper")
    sents = [json.loads(l) for l in open(os.path.join(pdir, "sentences.jsonl"), encoding="utf-8")]
    sid = next((s["id"] for s in sents if "4.2 points" in s["text"]), None)
    sid2 = next((s["id"] for s in sents if "1.3 billion" in s["text"]), None)
    md = r.get("metadata", [{}])[0]
    ok("e2e: markdown paper parsed with stable ids + front-matter metadata",
       r.get("primary_source") == "paper" and sid and re.match(r"p1-b\d+-s\d+$", sid) and md.get("title") == "Sparse Widgets Improve Gadget Recall"
       and md.get("authors") == ["Ada Lovelace", "Alan Turing"] and md.get("doi") == "10.5555/widgets.2024.7" and md.get("year") == 2024 and md.get("file_sha256"),
       sid=sid, md={k: md.get(k) for k in ("title", "authors", "doi", "year")})
    mdd = {"source": "paper", "assertions": [
        {"id": "m1", "text": "Sparse widgets improve gadget recall by 4.2 points on the GadgetBench test set.", "subject": "sparse widgets", "predicate": "improves",
         "object": "gadget recall", "qualifiers": {"value": "4.2", "unit": "points", "dataset": "GadgetBench test set"}, "evidence": [sid]},
        {"id": "m2", "text": "The widget model has 13 亿 parameters.", "subject": "widget model", "predicate": "has_value", "object": "parameters",
         "qualifiers": {"value": "13", "unit": "亿"}, "evidence": [sid2]},
        {"id": "m3", "text": "The sparse model has a recall of 75.2 on GadgetBench test.", "subject": "sparse model", "predicate": "has_value", "object": "recall",
         "qualifiers": {"value": "75.2", "metric": "Recall", "dataset": "GadgetBench test"},
         "evidence": [{"type": "table_cell", "table": "Table 1", "row": {"Model": "Sparse"}, "column": "Recall", "value": "75.2"}]}],
        "fields": {"assertions": {"kind": "assertions"}, "provenance": {"kind": "provenance"}}}
    write(os.path.join(work, "draft.json"), mdd)
    rb = jout(run(scripts, "np_build.py", "--draft", os.path.join(work, "draft.json"), "--out", out))
    res = load(os.path.join(out, "result.json"))
    verd = {x["id"]: (x["verdict"], x["issues"]) for x in rb.get("records", [])}
    ok("e2e: markdown sentence + 亿-scaled value + table cell all bind, status success",
       res["status"] == "success" and all(v[0] != "fail" for v in verd.values()) and res["provenance"].get("file_sha256") and res["provenance"].get("source_format") == "markdown",
       verdicts=verd, reason=res.get("status_reason"))
    # C8 txt with form feed -> page 2 ids; task.md is ignored
    d, case, out, work = make_case(base, "c8_txt", {"notes.txt": "A Study of Things\n\nWe find that things work well in 90% of trials.\n\f\nOn page two we observe that things fail at night.\n",
                                                    "task.md": "irrelevant"}, {"case_id": "c8", "output_contract": ["assertions"]})
    r = jout(run(scripts, "np_init.py", "--case", case, "--out", out, "--work", work))
    sents = [json.loads(l) for l in open(os.path.join(out, "parsed", "notes", "sentences.jsonl"), encoding="utf-8")] if r.get("primary_source") == "notes" else []
    ok("e2e: .txt fallback (task.md skipped), form feed -> logical page 2", r.get("primary_source") == "notes" and any(s["page"] == 2 and s["id"].startswith("p2-") for s in sents),
       materials=r.get("materials"), ids=[s["id"] for s in sents])
    # C9 docx
    dx = docx_bytes([("Title", "Docx Paper on Widgets", False), ("Heading1", "Results", False), (None, "We observe that widgets reduce latency by 12%.", False),
                     (None, "A second page claim about widgets.", True)])
    d, case, out, work = make_case(base, "c9_docx", {"paper.docx": dx}, {"case_id": "c9", "main_paper": "paper.docx", "output_contract": ["assertions"]})
    r = jout(run(scripts, "np_init.py", "--case", case, "--out", out, "--work", work))
    sents = [json.loads(l) for l in open(os.path.join(out, "parsed", "paper", "sentences.jsonl"), encoding="utf-8")] if r.get("parse_status") == ["ok"] else []
    s1 = next((s for s in sents if "12%" in s["text"]), {})
    ok("e2e: .docx parsed: title, heading section, page break, core authors", r.get("metadata", [{}])[0].get("title") == "Docx Paper on Widgets"
       and s1.get("section_path") == ["Results"] and any(s["page"] == 2 for s in sents) and r["metadata"][0].get("authors") == ["Ada Lovelace", "Alan Turing"],
       s1=s1.get("id"), md=r.get("metadata"))
    # C10 strict export: hostile metadata / text characters
    d, case, out, work = make_case(base, "c10_export", {"paper.pdf": resnet}, {"case_id": "c10", "pdf": "paper.pdf", "output_contract": ["assertions"]})
    run(scripts, "np_init.py", "--case", case, "--out", out, "--work", work)
    hd = json.loads(json.dumps(ok_draft))
    hd["source_overrides"] = {"doi": {"value": "10.1000/a{b}|c d^e", "reason": "test"}, "title": {"value": 'Tab\tand "quote" and \\ and \x0c ff', "reason": "test"}}
    hd["assertions"][0]["subject"] = 'ensemble <of> "residual" nets'
    write(os.path.join(work, "draft.json"), hd)
    rb = jout(run(scripts, "np_build.py", "--draft", os.path.join(work, "draft.json"), "--out", out, "--no-previews"))
    sys.path.insert(0, scripts)
    import np_build
    lines = [l for l in open(os.path.join(out, "nanopubs.nq"), encoding="utf-8").read().splitlines() if l.strip()]
    ok("e2e: hostile chars -> every N-Quads line is strictly valid, round trip ok", lines and all(np_build.NQ_RE.match(l) for l in lines) and rb["export_roundtrip"]["ok"],
       bad=[l for l in lines if not np_build.NQ_RE.match(l)][:2])
    # C11 P0/P1 e2e regressions: reserved field protection, mixed contracts, 1e999, broken case, md+supp.pdf, needs_review, unresolved_pages
    d, case, out, work = make_case(base, "c11_p0p1", {"paper.md": MD_PAPER, "appendix.pdf": resnet},
                                   {"case_id": 0, "output_contract": {"properties": {"assertions": {"type": "array"}, "resource_token": {"type": "object"}, "review_note": {"type": "string"}, "score": {"type": "number"}}, "required": ["assertions", "review_note", "score"]},
                                    "output_fields": ["provenance"]})
    r = jout(run(scripts, "np_init.py", "--case", case, "--out", out, "--work", work))
    ok("e2e: paper.md beats appendix.pdf without manifest (P1-2)", r.get("primary_source") == "paper", primary=r.get("primary_source"))
    ok("e2e: resource_token not auto-filled as provenance (P1-3)", (load(os.path.join(work, "draft.json")).get("fields") or {}).get("resource_token", {}).get("kind") == "agent_value")
    sents = [json.loads(l) for l in open(os.path.join(out, "parsed", "paper", "sentences.jsonl"), encoding="utf-8")]
    sid = next((s["id"] for s in sents if "4.2 points" in s["text"]), None)
    d_bad = {"source": "paper", "assertions": [{"id": "m1", "text": "Sparse widgets improve gadget recall by 4.2 points on the GadgetBench test set.", "subject": "sparse widgets", "predicate": "improves", "object": "gadget recall", "qualifiers": {"value": "4.2", "unit": "points", "dataset": "GadgetBench test set"}, "evidence": [sid]}],
             "fields": {"assertions": {"kind": "assertions"}, "provenance": {"kind": "provenance"}, "review_note": {"value": "no from id"}, "score": {"value": "1e999", "from": ["m1"]}}}
    write(os.path.join(work, "draft.json"), d_bad)
    run(scripts, "np_build.py", "--draft", os.path.join(work, "draft.json"), "--out", out, "--no-previews")
    res11 = load(os.path.join(out, "result.json"))
    ok("e2e: 1e999 rejected & needs_review required field -> partial, valid JSON (P0-4, P1-3)",
       res11["status"] == "partial" and "score" in (res11.get("field_problems") or {}) and "review_note" in res11["status_reason"]["needs_review_fields"],
       reason=res11["status_reason"])
    # Fix score & review_note -> success, then test reserved key protection & broken re-init
    d_bad["fields"]["score"] = {"value": "1.5", "from": ["m1"]}
    d_bad["fields"]["review_note"] = {"value": "ok", "from": ["m1"]}
    write(os.path.join(work, "draft.json"), d_bad)
    run(scripts, "np_build.py", "--draft", os.path.join(work, "draft.json"), "--out", out, "--no-previews")
    rp11, nqp11 = os.path.join(out, "result.json"), os.path.join(out, "nanopubs.nq")
    h_res, h_nq = fhash(rp11), fhash(nqp11)
    d_resv = json.loads(json.dumps(d_bad))
    d_resv["fields"]["field_status"] = ["bad"]
    write(os.path.join(work, "draft_resv.json"), d_resv)
    rr = run(scripts, "np_build.py", "--draft", os.path.join(work, "draft_resv.json"), "--out", out, "--no-previews")
    ok("e2e: reserved field_status in draft.fields -> exit 2, result.json and nanopubs.nq untouched (P0-3)",
       rr.returncode == 2 and fhash(rp11) == h_res and fhash(nqp11) == h_nq, code=rr.returncode)
    write(case, "{broken")
    run(scripts, "np_init.py", "--case", case, "--out", out, "--work", work)
    ok("e2e: broken case.json re-init does not clobber built result.json (P1-1)", fhash(rp11) == h_res and os.path.exists(os.path.join(out, "init_error.json")))
    # P0-5: unresolved_pages -> reported as a run warning (limit), records built from readable pages keep status success
    res_unres = load(rp11)
    res_unres["sources"][0]["unresolved_pages"] = [2]
    write(rp11, res_unres)
    run(scripts, "np_build.py", "--draft", os.path.join(work, "draft.json"), "--out", out, "--no-previews")
    res_after = load(rp11)
    ok("e2e: unresolved_pages on source -> success + unread_source_pages warning (P0-5)", res_after["status"] == "success"
       and bool(res_after["status_reason"]["unread_source_pages"]) and any(w["type"] == "unread_source_pages" for w in res_after["run_warnings"]),
       reason=res_after["status_reason"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", default=os.path.join(ROOT, "submission"))
    ap.add_argument("--pdfs", default=os.path.join(HERE, "pdfs"))
    ap.add_argument("--out", default=os.path.join(ROOT, "execution", "results", "v03"))
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    scripts = os.path.join(a.skill, "scripts")
    sys.path.insert(0, scripts)
    import np_common as C
    vocab = C.load_vocab()
    test_rules(vocab)
    test_contract()
    if os.path.exists(os.path.join(a.pdfs, "resnet.pdf")):
        os.makedirs(a.out, exist_ok=True)
        test_e2e(scripts, a.pdfs, a.out)
    else:
        ok("e2e skipped: tests/pdfs/resnet.pdf missing (see tests/PAPERS.md)", True)
    n_ok = sum(r["ok"] for r in RESULTS)
    summary = {"n_tests": len(RESULTS), "passed": n_ok, "failed": [r["test"] for r in RESULTS if not r["ok"]]}
    print(json.dumps(summary, ensure_ascii=False))
    if a.out and os.path.isdir(a.out):
        with open(os.path.join(a.out, "test_v03.json"), "w", encoding="utf-8") as fh:
            json.dump({"summary": summary, "tests": RESULTS}, fh, ensure_ascii=False, indent=1, default=str)
    sys.exit(0 if n_ok == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
