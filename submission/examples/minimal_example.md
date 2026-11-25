# Minimal example (example paper, not an evaluation case)

Paper: arXiv 2001.08361v1 (Kaplan et al., *Scaling Laws for Neural Language Models*), saved as `/input/paper.pdf`.

`/input/case.json`
```json
{"case_id": "example-kaplan", "pdf": "paper.pdf",
 "questions": ["What is the fitted exponent alpha_N of loss vs. non-embedding parameters?",
               "Does performance depend strongly on model shape at fixed parameter count?"],
 "output_contract": {"answer_alpha_n": "value with evidence", "answer_shape": "yes/no with evidence",
                     "nanopublications": "atomic assertions as nanopubs", "provenance": "source metadata"}}
```

## Calls

(`PY` / `SKILL_DIR` set by the two discovery lines at the top of SKILL.md.)

```bash
$PY $SKILL_DIR/scripts/np_init.py --case /input/case.json --out /app/output --work /app/work
#  -> contract fields + kind suggestions; metadata: title, 10 authors (needs_review), arXiv 2001.08361 v1, year 2020
$PY $SKILL_DIR/scripts/np_find.py /app/output/parsed/paper "depends very weakly on model shape" --top 5
#  p8-b5-s1 | p8 | Empirical Results ... | Transformer performance depends very weakly on the shape parameters ...
$PY $SKILL_DIR/scripts/table_query.py /app/output/parsed/paper --table "Table 5" --list
#  row keys: ["α N = 0.076", "N c = 8.8 × 10^13 params (non-embed)"], ...
```

`/app/work/draft.json` (excerpt)
```json
{"source": "paper",
 "assertions": [
  {"id": "k1", "text": "The fitted exponent alpha_N of the power law between test loss and non-embedding parameter count N is 0.076.",
   "subject": "power law of test loss in non-embedding parameter count N", "predicate": "has_value", "object": "exponent alpha_N",
   "qualifiers": {"value": "0.076", "metric": "alpha_N"},
   "evidence": [{"type": "table_cell", "table": "Table 5", "row": {"Power Law": "α N = 0.076"}, "column": "Power Law"}]},
  {"id": "k2", "text": "Transformer performance depends very weakly on the shape parameters when the total non-embedding parameter count N is held fixed.",
   "subject": "Transformer performance", "predicate": "associated_with", "object": "model shape (nlayer, nheads, dff)",
   "qualifiers": {"condition": "total non-embedding parameter count N held fixed", "strength": "very weak"},
   "evidence": ["p8-b5-s1"], "context": ["p3-b3-s2"]}
 ],
 "fields": {"answer_alpha_n": {"value": "0.076", "from": ["k1"]},
            "answer_shape": {"value": "No. Within reasonable limits performance depends very weakly on model shape when N is held fixed.", "from": ["k2"]},
            "nanopublications": {"kind": "nanopublications"},
            "provenance": {"kind": "provenance"}}}
```

```bash
$PY $SKILL_DIR/scripts/np_build.py --draft /app/work/draft.json --out /app/output
```

## Output (excerpt of `/app/output/result.json`)

```json
{"case_id": "example-kaplan", "answer_alpha_n": "0.076", "answer_shape": "No. Within reasonable limits ...",
 "status": "success",
 "field_status": {"answer_alpha_n": "supported", "answer_shape": "supported", "nanopublications": "generated", "provenance": "generated"},
 "field_evidence": {"answer_shape": [
   {"role": "support", "kind": "text", "quote": "Transformer performance depends very weakly on the shape parameters nlayer, nheads, and dff when we hold the total non-embedding parameter count N fixed.",
    "page": 8, "block_id": "p8-b5", "sentence_id": "p8-b5-s1", "bbox": [90.0, 513.36, 522.0, 534.46], "bbox_precision": "quote", "match": "exact_sentence"},
   {"role": "context", "quote": "Within reasonable limits, performance depends very weakly on other architectural hyperparameters such as depth vs. width.",
    "page": 3, "sentence_id": "p3-b3-s2", "match": "exact_sentence"}]},
 "nanopublications": [{"np_id": "np-…", "uri": "http://purl.org/nanopub/temp/np-…",
   "assertion": {"id": "k2", "predicate": {"id": "associated_with", "relation_type": "associative"}, "...": "..."},
   "evidence_type": "direct", "eco": {"code": "ECO:0000006"},
   "confidence": {"score": 0.9, "level": "high"}, "quality": {"verdict": "pass", "issues": []}}],
 "source_metadata": {"title": "Scaling Laws for Neural Language Models", "arxiv_id": "2001.08361", "version": "v1", "year": 2020,
                     "field_sources": {"version": {"source": {"kind": "arxiv_stamp", "page": 1, "quote": "arXiv:2001.08361v1 [cs.LG] 23 Jan 2020"}}}},
 "export_roundtrip": {"ok": true}}
```
Also written: `nanopubs.trig|.nq|.jsonld`, `quality_report.json`, `review.md`, `evidence_previews/page_008.png` (quote boxed in red).

A failing record looks like this (planted error, value changed from 13% to 15%):
```
k6 fail  R09 fail: number(s) in the assertion not found in any evidence item ['15%']
```
