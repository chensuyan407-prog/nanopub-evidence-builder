"""Evaluation of nanopub-evidence-builder on the labelled drafts in tests/eval/.

    python tests/run_eval.py [--skill submission] [--pdfs tests/pdfs] [--out execution/results]

For each paper: np_init.py on a fresh case, np_build.py on the labelled draft (twice, to check determinism),
then compares with the labels:
  * planted defects: detected when every expected rule id is reported;
  * clean assertions: should not fail; their support evidence should be located at quote / table-cell level;
  * metadata: title / authors / year / arXiv id / version against hand-checked truth;
  * exports: N-Quads / JSON-LD / TriG round trip;
  * baselines on the same drafts: B0 export without checks, B1 'quote exists in the PDF' check only.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PAPERS = {
    "resnet": {"pdf": "resnet.pdf", "truth": {"title": "Deep Residual Learning for Image Recognition",
                                                "authors": ["Kaiming He", "Xiangyu Zhang", "Shaoqing Ren", "Jian Sun"],
                                                "year": 2015, "arxiv_id": "1512.03385", "version": "v1"}},
    "attention": {"pdf": "attention.pdf", "truth": {"title": "Attention Is All You Need",
                                                     "authors": ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar", "Jakob Uszkoreit", "Llion Jones",
                                                                 "Aidan N. Gomez", "Łukasz Kaiser", "Illia Polosukhin"],
                                                     "year": 2023, "arxiv_id": "1706.03762", "version": "v7"}},
    "bn": {"pdf": "1502.03167.pdf", "truth": {"title": "Batch Normalization: Accelerating Deep Network Training by Reducing Internal Covariate Shift",
                                              "authors": ["Sergey Ioffe", "Christian Szegedy"], "year": 2015, "arxiv_id": "1502.03167", "version": "v3"}},
    # held-out: draft written after the code freeze (tests/FREEZE_holdout.sha256); reported separately
    "kaplan_holdout": {"pdf": "2001.08361.pdf", "holdout": True,
                       "contract": {"answer_alpha_n": "fitted exponent alpha_N with evidence", "answer_shape": "does performance depend strongly on model shape",
                                    "nanopublications": "atomic assertions as nanopubs", "provenance": "source metadata"},
                       "truth": {"title": "Scaling Laws for Neural Language Models",
                                 "authors": ["Jared Kaplan", "Sam McCandlish", "Tom Henighan", "Tom B. Brown", "Benjamin Chess", "Rewon Child",
                                             "Scott Gray", "Alec Radford", "Jeffrey Wu", "Dario Amodei"],
                                 "year": 2020, "arxiv_id": "2001.08361", "version": "v1"}},
}
LOCATE_OK = {"exact_sentence", "exact_substring", "table_cell"}
PREC_OK = {"quote", "table_cell"}


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}\n{r.stderr[-2000:]}")
    return r.stdout[r.stdout.find("{"):]  # PyMuPDF may print a notice line first


def sha(p):
    with open(p, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", default=os.path.join(ROOT, "submission"))
    ap.add_argument("--pdfs", default=os.path.join(HERE, "pdfs"))
    ap.add_argument("--out", default=os.path.join(ROOT, "execution", "results"))
    a = ap.parse_args()
    scripts = os.path.join(a.skill, "scripts")
    os.makedirs(a.out, exist_ok=True)
    t_all = time.time()
    rows, papers = [], {}
    for name, info in PAPERS.items():
        pdf = os.path.join(a.pdfs, info["pdf"])
        case = os.path.join(a.out, "cases", name)
        shutil.rmtree(case, ignore_errors=True)
        os.makedirs(os.path.join(case, "input"))
        shutil.copy(pdf, os.path.join(case, "input", "paper.pdf"))
        with open(os.path.join(case, "input", "case.json"), "w", encoding="utf-8") as fh:
            json.dump({"case_id": f"eval-{name}", "pdf": "paper.pdf",
                       "output_contract": info.get("contract") or {"assertions": "atomic assertions", "evidence": "evidence per assertion", "provenance": "source metadata"}}, fh)
        with open(os.path.join(HERE, "eval", name, "draft.json"), encoding="utf-8") as fh:
            draft = json.load(fh)
        expect = {x["id"]: x.pop("expect") for x in draft["assertions"]}
        os.makedirs(os.path.join(case, "work"))
        dpath = os.path.join(case, "work", "draft.json")
        with open(dpath, "w", encoding="utf-8") as fh:
            json.dump(draft, fh, ensure_ascii=False, indent=1)
        out = os.path.join(case, "output")
        t0 = time.time()
        init = json.loads(run([sys.executable, os.path.join(scripts, "np_init.py"), "--case", os.path.join(case, "input", "case.json"),
                               "--out", out, "--work", os.path.join(case, "work")]))
        t1 = time.time()
        build = json.loads(run([sys.executable, os.path.join(scripts, "np_build.py"), "--draft", dpath, "--out", out, "--created", "2026-11-23T00:00:00Z"]))
        t2 = time.time()
        h1 = sha(os.path.join(out, "nanopubs.nq"))
        run([sys.executable, os.path.join(scripts, "np_build.py"), "--draft", dpath, "--out", out, "--created", "2026-11-23T00:00:00Z", "--no-previews"])
        h2 = sha(os.path.join(out, "nanopubs.nq"))
        with open(os.path.join(out, "nanopubs.json"), encoding="utf-8") as fh:
            recs = {r["assertion"]["id"]: r for r in json.load(fh)["nanopublications"]}
        for aid, ex in expect.items():
            r = recs[aid]
            got = sorted({i["rule"] for i in r["quality"]["issues"]})
            sup = [e for e in r["evidence"] if e.get("role", "support") == "support"]
            row = {"paper": name, "holdout": bool(info.get("holdout")), "id": aid, "clean": ex["clean"], "expected_rules": ex.get("rules", []), "verdict": r["quality"]["verdict"],
                   "rules_found": got, "confidence": r["confidence"]["score"],
                   "evidence_match": [e.get("match") for e in sup], "bbox_precision": [e.get("bbox_precision") for e in sup],
                   "pages": [e.get("page") for e in sup]}
            if ex["clean"]:
                row["ok"] = r["quality"]["verdict"] != "fail"
                row["strict_pass"] = r["quality"]["verdict"] == "pass"
                row["located"] = all(m in LOCATE_OK for m in row["evidence_match"]) and all(p in PREC_OK for p in row["bbox_precision"])
            else:
                row["ok"] = set(ex["rules"]) <= set(got)
                row["b1_catches"] = set(ex["rules"]) <= {"R04", "R05"}
            rows.append(row)
        md = init["metadata"][0]
        tr = info["truth"]
        mres = {k: (md.get(k) == tr[k]) for k in ("title", "year", "arxiv_id", "version")}
        got_a = md.get("authors") or []
        mres["authors_recall"] = round(sum(1 for x in tr["authors"] if x in got_a) / len(tr["authors"]), 3)
        mres["authors_precision"] = round(sum(1 for x in got_a if x in tr["authors"]) / max(1, len(got_a)), 3)
        papers[name] = {"pdf_sha256": sha(pdf), "init_seconds": round(t1 - t0, 2), "build_seconds": round(t2 - t1, 2),
                        "deterministic_nquads": h1 == h2, "export_roundtrip": build["export_roundtrip"], "metadata": mres,
                        "summary": build["summary"], "status": build["status"]}
        with open(os.path.join(out, "result.json"), encoding="utf-8") as fh:
            res = json.load(fh)
        papers[name]["result_status"] = res.get("status")
        papers[name]["result_field_status"] = res.get("field_status")
        papers[name]["result_fields"] = {f: (res.get(f) if isinstance(res.get(f), (str, int, float)) or res.get(f) is None else f"<{type(res.get(f)).__name__} len {len(res.get(f))}>")
                                         for f in (info.get("contract") or {})}

    def block(rs):
        clean = [r for r in rs if r["clean"]]
        bad = [r for r in rs if not r["clean"]]
        return {"n_assertions": len(rs), "n_clean": len(clean), "n_planted_defects": len(bad),
                "defect_detection_rate": round(sum(r["ok"] for r in bad) / max(1, len(bad)), 3),
                "clean_not_failed_rate": round(sum(r["ok"] for r in clean) / max(1, len(clean)), 3),
                "clean_strict_pass_rate": round(sum(r["strict_pass"] for r in clean) / max(1, len(clean)), 3),
                "clean_evidence_located_at_quote_or_cell": round(sum(r["located"] for r in clean) / max(1, len(clean)), 3),
                "baseline_B0_no_checks_detection": 0.0,
                "baseline_B1_quote_exists_only_detection": round(sum(r["b1_catches"] for r in bad) / max(1, len(bad)), 3)}
    dev = block([r for r in rows if not r["holdout"]])
    hold = block([r for r in rows if r["holdout"]])
    clean = [r for r in rows if r["clean"]]
    bad = [r for r in rows if not r["clean"]]
    metrics = {"dev_set (rules tuned on it)": dev, "holdout_set (after freeze)": hold,
        "n_assertions": len(rows), "n_clean": len(clean), "n_planted_defects": len(bad),
        "defect_detection_rate": round(sum(r["ok"] for r in bad) / len(bad), 3),
        "clean_not_failed_rate": round(sum(r["ok"] for r in clean) / len(clean), 3),
        "clean_strict_pass_rate": round(sum(r["strict_pass"] for r in clean) / len(clean), 3),
        "clean_evidence_located_at_quote_or_cell": round(sum(r["located"] for r in clean) / len(clean), 3),
        "baseline_B0_no_checks_detection": 0.0,
        "baseline_B1_quote_exists_only_detection": round(sum(r["b1_catches"] for r in bad) / len(bad), 3),
        "metadata_exact": {k: sum(p["metadata"][k] for p in papers.values()) / len(papers) for k in ("title", "year", "arxiv_id", "version")},
        "authors_recall_mean": round(sum(p["metadata"]["authors_recall"] for p in papers.values()) / len(papers), 3),
        "authors_precision_mean": round(sum(p["metadata"]["authors_precision"] for p in papers.values()) / len(papers), 3),
        "exports_roundtrip_ok": all(p["export_roundtrip"]["ok"] for p in papers.values()),
        "deterministic": all(p["deterministic_nquads"] for p in papers.values()),
        "papers": papers, "wall_seconds": round(time.time() - t_all, 2),
    }
    with open(os.path.join(a.out, "metrics.json"), "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(a.out, "eval_rows.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(json.dumps({k: v for k, v in metrics.items() if k != "papers"}, ensure_ascii=False, indent=1))
    for r in rows:
        if not r["ok"] or (r["clean"] and not (r["strict_pass"] and r["located"])):
            print("CHECK", json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
