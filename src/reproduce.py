#!/usr/bin/env python3
"""Reproduce the self-evaluation of the nanopub-evidence-builder Skill.

Run from the bundle root:   python src/reproduce.py

1. labelled evaluation (tests/run_eval.py): 47 assertions on 4 arXiv papers (one held out at a code freeze),
   28 with planted defects; checks rule detection, clean records, evidence location, metadata, export round trip,
   determinism, and two baselines (no checks / quote-exists-only);
2. robustness: encrypted PDF, case without PDF, missing case.json, page-image evidence;
3. writes execution/run.log, execution/results/*, trace/trace.jsonl, characterization.json, arm_manifest.json.

The papers are public arXiv PDFs chosen by the author; they are not the platform's fixed cases and
no official answer is produced here.
"""
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "execution", "results")
SCRIPTS = os.path.join(ROOT, "submission", "scripts")
TRACE = []


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def step(kind, **kw):
    TRACE.append(dict(step_type=kind, timestamp=now(), step_order=len(TRACE) + 1, **kw))


def run(cmd, log):
    step("tool_call", tool_name="python", tool_call_id=f"c{len(TRACE) + 1}", tool_args={"argv": [os.path.relpath(c, ROOT) if os.path.exists(str(c)) else c for c in cmd[1:]]})
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    log.write(f"$ {' '.join(cmd[1:])}\n{r.stdout}\n{r.stderr}\n[exit {r.returncode}]\n\n")
    step("tool_result", tool_call_id=f"c{len(TRACE)}", exit_code=r.returncode, stdout_tail=r.stdout[-400:])
    return r


def sha(p):
    with open(p, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def robustness(log):
    base = os.path.join(RES, "robustness")
    shutil.rmtree(base, ignore_errors=True)
    out = {}
    enc = os.path.join(ROOT, "tests", "pdfs", "encrypted_resnet.pdf")
    cases = {"encrypted_pdf": enc if os.path.exists(enc) else None, "no_pdf": None}
    for name, pdf in cases.items():
        d = os.path.join(base, name)
        os.makedirs(os.path.join(d, "input"))
        case = {"case_id": name, "output_contract": ["assertions"]}
        if pdf:
            shutil.copy(pdf, os.path.join(d, "input", "paper.pdf"))
            case["pdf"] = "paper.pdf"
        with open(os.path.join(d, "input", "case.json"), "w") as fh:
            json.dump(case, fh)
        run([sys.executable, os.path.join(SCRIPTS, "np_init.py"), "--case", os.path.join(d, "input", "case.json"),
             "--out", os.path.join(d, "output"), "--work", os.path.join(d, "work")], log)
        with open(os.path.join(d, "output", "result.json"), encoding="utf-8") as fh:
            r = json.load(fh)
        out[name] = {"status": r["status"], "reason": r["run_status"].get("reason"), "expected_status": "failed",
                     "ok": r["status"] == "failed" and bool(r["run_status"].get("reason"))}
    d = os.path.join(base, "missing_case")
    r = run([sys.executable, os.path.join(SCRIPTS, "np_init.py"), "--case", os.path.join(d, "nope.json"),
             "--out", os.path.join(d, "output"), "--work", os.path.join(d, "work")], log)
    with open(os.path.join(d, "output", "result.json"), encoding="utf-8") as fh:
        rr = json.load(fh)
    out["missing_case_json"] = {"status": rr["status"], "case_error": rr["run_status"]["case_error"],
                                "ok": rr["status"] == "failed" and "not found" in (rr["run_status"]["case_error"] or "")}
    # image-region evidence on a real page (crop bound by hash), must stay partial
    d = os.path.join(base, "image_evidence")
    os.makedirs(os.path.join(d, "input"))
    shutil.copy(os.path.join(ROOT, "tests", "pdfs", "1502.03167.pdf"), os.path.join(d, "input", "paper.pdf"))
    with open(os.path.join(d, "input", "case.json"), "w") as fh:
        json.dump({"case_id": "image", "pdf": "paper.pdf", "output_contract": {"assertions": ""}}, fh)
    run([sys.executable, os.path.join(SCRIPTS, "np_init.py"), "--case", os.path.join(d, "input", "case.json"),
         "--out", os.path.join(d, "output"), "--work", os.path.join(d, "work")], log)
    c = run([sys.executable, os.path.join(SCRIPTS, "crop_region.py"), os.path.join(d, "output", "parsed", "paper"),
             "--page", "1", "--bbox", "150,100,460,160", "--dpi", "120"], log)
    crop = json.loads(c.stdout[c.stdout.find("{"):])
    draft = {"source": "paper", "assertions": [{"id": "i1", "text": "Sergey Ioffe is affiliated with Google Inc.", "subject": "Sergey Ioffe",
                                                "predicate": "part_of", "object": "Google Inc.",
                                                "evidence": [{"type": "image_region", "page": 1, "bbox": crop["bbox"], "crop": crop["crop"],
                                                              "provenance": crop["provenance"], "transcription": "Sergey Ioffe Google Inc., sioffe@google.com"}]}],
             "fields": {"assertions": {"kind": "assertions"}}}
    with open(os.path.join(d, "work", "draft.json"), "w", encoding="utf-8") as fh:
        json.dump(draft, fh)
    run([sys.executable, os.path.join(SCRIPTS, "np_build.py"), "--draft", os.path.join(d, "work", "draft.json"),
         "--out", os.path.join(d, "output"), "--created", "2026-11-23T00:00:00Z"], log)
    with open(os.path.join(d, "output", "result.json"), encoding="utf-8") as fh:
        r = json.load(fh)
    out["image_region_evidence"] = {"status": r["status"], "status_reason": r["status_reason"],
                                    "ok": r["status"] == "partial" and r["status_reason"]["image_read_records_need_human_review"] == ["i1"]}
    # v0.2.0: JSON schema contract, nquads/summary/unreported views, inferred+reasoning R09, paren semicolon / adj participle R07
    d = os.path.join(base, "v02_features")
    os.makedirs(os.path.join(d, "input"))
    shutil.copy(os.path.join(ROOT, "tests", "pdfs", "resnet.pdf"), os.path.join(d, "input", "paper.pdf"))
    with open(os.path.join(d, "input", "case.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "case_id": "v02_schema",
            "pdf": "paper.pdf",
            "output_contract": {
                "type": "object",
                "properties": {
                    "assertions": {"type": "array"},
                    "nquads": {"type": "string"},
                    "summary": {"type": "object"},
                    "unreported": {"type": "array"},
                },
                "required": ["assertions", "nquads"],
            },
        }, fh)
    run([sys.executable, os.path.join(SCRIPTS, "np_init.py"), "--case", os.path.join(d, "input", "case.json"),
         "--out", os.path.join(d, "output"), "--work", os.path.join(d, "work")], log)
    with open(os.path.join(d, "work", "draft.json"), encoding="utf-8") as fh:
        init_draft = json.load(fh)
    draft5 = {
        "source": "paper",
        "assertions": [
            {"id": "v1", "text": "ResNet-152 achieves higher accuracy and reduced latency (He et al., 2015; ImageNet).",
             "subject": "ResNet-152", "predicate": "outperforms", "object": "shallower models",
             "evidence_type": "direct", "evidence": ["p1-b5-s6"]},
            {"id": "v2", "text": "ResNet-152 lowers top-5 error by 1.22 percentage points relative to ResNet-34 B.",
             "subject": "ResNet-152", "predicate": "lower_than", "object": "ResNet-34 B",
             "qualifiers": {"value": "1.22", "unit": "percentage points"},
             "evidence_type": "inferred", "reasoning": "Table 4 top-5 err.: 5.71 - 4.49 = 1.22",
             "evidence": [{"type": "table_cell", "table": "Table 4", "row": {"method": "ResNet-152"}, "column": "top-5 err."},
                          {"type": "table_cell", "table": "Table 4", "row": {"method": "ResNet-34 B"}, "column": "top-5 err."}]},
        ],
        "fields": init_draft["fields"],
        "unreported": [{"field": "doi", "reason": "no DOI printed on page 1"}],
    }
    with open(os.path.join(d, "work", "draft.json"), "w", encoding="utf-8") as fh:
        json.dump(draft5, fh)
    rb5 = run([sys.executable, os.path.join(SCRIPTS, "np_build.py"), "--draft", os.path.join(d, "work", "draft.json"),
               "--out", os.path.join(d, "output"), "--created", "2026-11-25T00:00:00Z"], log)
    with open(os.path.join(d, "output", "result.json"), encoding="utf-8") as fh:
        r5 = json.load(fh)
    out["v02_schema_nquads_inferred_r07"] = {
        "status": r5.get("status"),
        "nquads": r5.get("nquads"),
        "has_summary": isinstance(r5.get("summary"), dict),
        "fails": r5.get("quality_summary", {}).get("fail"),
        "ok": (rb5.returncode == 0 and r5.get("status") == "success" and r5.get("nquads") == "nanopubs.nq"
               and isinstance(r5.get("summary"), dict) and isinstance(r5.get("unreported"), list)
               and r5.get("quality_summary", {}).get("fail") == 0),
    }
    return out


def main():
    t0 = time.time()
    os.makedirs(RES, exist_ok=True)
    os.makedirs(os.path.join(ROOT, "trace"), exist_ok=True)
    step("thought", title="Plan", body="Run the labelled nanopublication evaluation (dev papers + one held-out paper), "
                                       "robustness checks, then record metrics, manifest and trace.")
    with open(os.path.join(ROOT, "execution", "run.log"), "w", encoding="utf-8") as log:
        r = run([sys.executable, os.path.join(ROOT, "tests", "run_eval.py")], log)
        if r.returncode != 0:
            print(r.stderr)
            sys.exit(r.returncode)
        rob = robustness(log)
        # v0.3.0 regression suite: rules, contract handling, materials, draft safety, text papers, strict export
        rv = run([sys.executable, os.path.join(ROOT, "tests", "test_v03.py")], log)
        last = (rv.stdout.strip().splitlines() or ["{}"])[-1]
        try:
            sv = json.loads(last)
        except ValueError:
            sv = {"parse_error": last[-300:]}
        rob["v03_regression_suite"] = {"n_tests": sv.get("n_tests"), "passed": sv.get("passed"), "failed": sv.get("failed"),
                                       "ok": rv.returncode == 0}
    with open(os.path.join(RES, "metrics.json"), encoding="utf-8") as fh:
        m = json.load(fh)
    m["robustness"] = rob
    frozen = os.path.join(RES, "metrics_at_freeze.json")
    if os.path.exists(frozen):
        with open(frozen, encoding="utf-8") as fh:
            m["holdout_at_freeze (before post-freeze fixes)"] = json.load(fh).get("holdout_set (after freeze)")
    with open(os.path.join(RES, "metrics.json"), "w", encoding="utf-8") as fh:
        json.dump(m, fh, ensure_ascii=False, indent=1)
    wall = round(time.time() - t0, 2)
    step("observation", title="Result", body=json.dumps({k: m[k] for k in ("defect_detection_rate", "clean_not_failed_rate", "exports_roundtrip_ok", "deterministic")}))

    hold_f = m.get("holdout_at_freeze (before post-freeze fixes)") or {}
    devs = m["dev_set (rules tuned on it)"]
    hold = m["holdout_set (after freeze)"]
    dev_rows = [
        ("planted defects detected (dev set, rules tuned on it)", devs["defect_detection_rate"], 1.0, 0.1,
         f"{devs['n_planted_defects']} defects on ResNet/Transformer/BatchNorm; baseline quote-exists-only = {devs['baseline_B1_quote_exists_only_detection']}, no checks = 0"),
        ("planted defects detected (held-out paper, at code freeze)", hold_f.get("defect_detection_rate"), 1.0, 0.2,
         "Kaplan 2020; draft written after the freeze; miss = compound claim joined by ', and' + verb outside the list"),
        ("planted defects detected (held-out paper, after post-freeze fix)", hold["defect_detection_rate"], 1.0, 0.2,
         "same paper after the fix; it is no longer a clean held-out measure"),
        ("clean assertions not failed (all papers)", m["clean_not_failed_rate"], 1.0, 0.1, f"{m['n_clean']} clean assertions"),
        ("clean assertions strict pass (no warning)", m["clean_strict_pass_rate"], 1.0, 0.2, "one remaining warning is a true duplicate (R24) in the eval set"),
        ("clean evidence located at quote / table-cell level", m["clean_evidence_located_at_quote_or_cell"], 1.0, 0.1, "line boxes from PDF words; table cells by row/column name"),
        ("metadata title exact", m["metadata_exact"]["title"], 1.0, 0.0, "4 papers"),
        ("metadata arXiv id + version exact", min(m["metadata_exact"]["arxiv_id"], m["metadata_exact"]["version"]), 1.0, 0.0, "from the arXiv stamp on page 1"),
        ("author list recall", m["authors_recall_mean"], 1.0, 0.1, "heuristic split, flagged needs_review"),
        ("author list precision", m["authors_precision_mean"], 1.0, 0.1, "held-out paper was 0.833 before the post-freeze fix"),
        ("RDF export round trip (N-Quads / JSON-LD / TriG)", 1.0 if m["exports_roundtrip_ok"] else 0.0, 1.0, 0.0, "TriG parsed by rdflib when installed"),
        ("deterministic output (same draft -> same N-Quads)", 1.0 if m["deterministic"] else 0.0, 1.0, 0.0, "content-derived ids"),
        ("robustness checks passed", sum(v["ok"] for v in rob.values()) / len(rob), 1.0, 0.0, ", ".join(rob)),
    ]
    char = {"deviations_from_paper": [{"target": t, "metric": "absolute_error", "actual_value": a, "reference_value": ref,
                                       "score": (1.0 if a is not None and abs(a - ref) <= tol else 0.0), "explanation": ex,
                                       "evidence_artifact": "metrics", "tolerance": tol} for t, a, ref, tol, ex in dev_rows],
            "notes": "Self-evaluation on author-chosen public papers with planted defects; the platform's three fixed cases are not used here."}
    with open(os.path.join(ROOT, "characterization.json"), "w", encoding="utf-8") as fh:
        json.dump(char, fh, ensure_ascii=False, indent=1)
    arts = [("metrics", "execution/results/metrics.json", "metric", "json"),
            ("eval_rows", "execution/results/eval_rows.json", "data", "json"),
            ("metrics_at_freeze", "execution/results/metrics_at_freeze.json", "metric", "json")]
    for name in ("resnet", "attention", "bn", "kaplan_holdout"):
        o = f"execution/results/cases/{name}/output"
        arts += [(f"{name}_nanopubs", f"{o}/nanopubs.json", "data", "json"), (f"{name}_trig", f"{o}/nanopubs.trig", "data", "other"),
                 (f"{name}_review", f"{o}/review.md", "data", "txt")]
        prev = sorted(os.listdir(os.path.join(ROOT, o, "evidence_previews")))[:2]
        arts += [(f"{name}_preview_{p[:-4]}", f"{o}/evidence_previews/{p}", "figure", "png") for p in prev]
    manifest = {
        "arm_version": "1.1",
        "paper": {"title": "阅读14｜Nanopublication — nanopub-evidence-builder Skill", "challenge_id": "product-arena-09-0e8befd4"},
        "entrypoint": "src/reproduce.py",
        "environment": {"dependencies": "requirements.txt", "python_version": ">=3.9", "gpu_required": False,
                        "extra_packages": ["PyMuPDF>=1.23", "numpy", "rdflib (optional, TriG check)", "tesseract (optional, OCR)"]},
        "execution": {"log_path": "execution/run.log", "exit_code": 0, "ran_at": now(), "wall_time_s": wall, "ran_by": "agent",
                      "artifacts": [{"id": i, "path": p, "type": t, "format": f, "checksum_sha256": sha(os.path.join(ROOT, p))} for i, p, t, f in arts]},
        "expected_outputs": [{"name": r[0], "type": "metric", "produced_by": ["metrics"], "comparison_method": "numeric_tolerance"} for r in dev_rows],
        "handoff": {"status": "partial", "next_suggestion": "Run on the platform's three fixed cases; publish the GitHub repository; extend Chinese cue lexicons and compound-claim detection.",
                    "hypothesis": "Binding each atomic assertion to a verbatim, boxed quote plus deterministic rule checks catches most splitting, number, hedge, scope and provenance errors that a quote-exists check misses."},
    }
    with open(os.path.join(ROOT, "arm_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(ROOT, "trace", "trace.jsonl"), "w", encoding="utf-8") as fh:
        for t in TRACE:
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")
    print(json.dumps({"wall_s": wall, "defects_detected": m["defect_detection_rate"], "clean_not_failed": m["clean_not_failed_rate"],
                      "holdout_at_freeze": hold_f.get("defect_detection_rate"), "robustness": {k: v["ok"] for k, v in rob.items()}}, indent=1))


if __name__ == "__main__":
    main()
