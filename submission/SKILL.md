---
name: nanopub-evidence-builder
description: Turn claims in a scientific paper (PDF, .md, .txt, .docx, .html) into nanopublications - atomic, checkable assertions, each bound to the smallest verbatim evidence (page, sentence id, line boxes, table cell or image region) and to source provenance (title, authors, year, DOI/arXiv id + version, SHA-256). Use when a research-reading task asks to answer questions with evidence, split findings into assertions, bind evidence, check claim quality, or export machine-readable knowledge units (JSON, TriG, N-Quads, JSON-LD). Scripts resolve every quote against the source, run 31 reproducible quality rules and write result.json with honest status, never clobbering a good result.
---

# nanopub-evidence-builder (v0.3.2)

You write the claims; the scripts bind them to the source, check them and export nanopublications
(assertion + provenance + pubinfo, unsigned, `http://purl.org/nanopub/temp/`). Python 3 + PyMuPDF, no network.
Rules R01–R31, field dictionary, limits: `README.md`. **Target: 4 steps, one build, done.**

Start every shell call with (each call is a fresh shell):
```bash
PY=$(command -v python3 || command -v python); for d in "${SKILL_DIR:-}" /skills/weekly-skill /app/submission /app/skills /app/skills/* "$(pwd)"; do [ -n "$d" ] && [ -f "$d/scripts/np_init.py" ] && SKILL_DIR="$d" && break; done
```

1. **Init** — `$PY $SKILL_DIR/scripts/np_init.py --case /input/case.json --out /app/output --work /app/work`
   Prints the required-question checklist, `output_contract` skeleton, draft field mappings and material index (primary and supplements).
   Writes a valid partial `/app/output/result.json`, `/app/work/candidates.md` (top claim sentences with ids
   `p<page>-b<block>-s<k>` and cues num/hedge/neg/cite/cmp/cause/compound, plus tables) and `/app/work/draft.json` (template).
2. **Read** `task.md`, `case.json`, `candidates.md`. Only if something is missing:
   `$PY $SKILL_DIR/scripts/np_find.py /app/output "question words" --top 8` searches all sources, tables and equations. For one parsed source, use its directory (`--id p5-b89-s2 --context 1` for neighbours;
   `table_query.py /app/output/parsed/<stem> --table "Table 4" --list` for tables).
3. **Write `/app/work/draft.json`** in one go (format below). Let the number of questions and evidence determine the assertion count.
4. **Build** — `$PY $SKILL_DIR/scripts/np_build.py --draft /app/work/draft.json --out /app/output`
   Writes `nanopubs.trig|.nq|.jsonld|.json`, `quality_report.json`, `review.md`, `evidence_previews/`, and fills `result.json`.
   Spelling of predicate / modality / scope / evidence_type is auto-normalised. Records with a `fail` verdict are
   **quarantined** (kept in `review.md` / `quarantined_assertions`, out of the exports) and do not block `success`.
   Optional: fix quarantined ids in a small draft and re-run with `--merge` (same id replaces, `"delete": ["a3"]` removes).

## Evidence and coverage checklist

- [ ] Match the Init checklist against the original case and task. Cover every required question and subpart, including instructions without a question mark. Use the exact contract keys, nested field shapes and filenames. Link each supported answer via `fields.<key>.from` to the assertions that support all its parts. For missing or conflicting facts, record the specific gap and where you searched.
- [ ] Bind each atomic claim to the smallest verbatim sentence or clause and its page. Cite a sentence ID or `{page, quote}`. For a table, verify the exact table, page, row key, full column path, printed cell value and unit. Check denominator, dataset, comparator and conditions. For an equation or figure, verify its number and page against the PDF and bind the caption or the reproducible crop and transcription.
- [ ] Keep value, scale and unit exact, including percentage versus percentage points. For inferred values or comparisons, cite each operand in the matching row and column and state the calculation explicitly. Use each supplement's parsed stem when its evidence supports an assertion.
- [ ] Check `review.md` and `result.json` for quarantine, fields needing review and missing required items. Confirm each exported nanopublication has the head links and all three named graphs: assertion (claim), provenance (evidence locator and source identity), pubinfo (creator and timestamp). Verify a preview against the original page and report remaining uncertainty.

## draft.json
```json
{"source": "<stem printed by np_init>",
 "assertions": [
  {"id": "a1", "text": "An ensemble of residual nets achieves 3.57% error on the ImageNet test set.",
   "subject": "ensemble of residual nets", "predicate": "achieves", "object": "error on the ImageNet test set",
   "qualifiers": {"value": "3.57", "unit": "%", "metric": "error", "dataset": "ImageNet test set"},
   "modality": "asserted", "scope": "this_work", "evidence_type": "direct", "evidence": ["p1-b5-s6"]},
  {"id": "a2", "text": "ResNet-152 has lower top-5 error than ResNet-34 B.", "subject": "ResNet-152", "predicate": "lower_than",
   "object": "ResNet-34 B", "evidence_type": "inferred", "reasoning": "Table 4 top-5 err.: 4.49 < 5.71",
   "evidence": [{"type": "table_cell", "table": "Table 4", "row": {"method": "ResNet-152"}, "column": "top-5 err."},
                {"type": "table_cell", "table": "Table 4", "row": {"method": "ResNet-34 B"}, "column": "top-5 err."}]}],
 "fields": {"<contract list field>": {"kind": "assertions"}, "<question field>": {"value": "answer", "from": ["a1"]}},
 "unreported": [{"field": "<field>", "reason": "not reported (searched: abstract, Table 2)"}],
 "conflicts": [], "uncertainty": []}
```
- **evidence**: sentence id (best) · `{"page": N, "quote": "verbatim sub-span"}` · `table_cell` as above ·
  `{"type": "image_region", ...}` from `crop_region.py PARSED --page N --bbox x0,y0,x1,y1` for figures / scanned pages.
  Definitions or coreference sentences go in `"context"`.
- **predicate** (vocab/relations.json): `has_value achieves outperforms underperforms higher_than lower_than comparable_to improves
  reduces increases decreases causes enables prevents correlates_with associated_with has_property is_a part_of has_part defined_as
  uses evaluated_on trained_on proposes observed_in detected_in does_not_improve not_associated_with does_not_cause lacks_property`.
- **modality** `asserted|hedged|negated|hypothesis` · **scope** `this_work|cited_work` (+ `cited_reference: "[12]"`)`|background` ·
  **evidence_type** `direct|inferred` (+ `reasoning` with operands, recomputed) `|cited`.
- **fields kind**: `nanopublications assertions evidence provenance quality relations trig nquads jsonld review summary unreported
  conflicts uncertainty status`, or `value` + `from` ids for answers; `rename` / `filter` adjust lists. Unmapped contract
  fields are auto-filled from the suggested kind. `source_overrides` fixes authors etc. with the block id you read.

## Splitting and honesty rules
- One subject, one predicate, one object, one result; "A and B reach 6.7% and 6.1%, respectively" → two assertions.
- Keep conditions (dataset, setting, sample size, "only"), hedges ("may", "suggests") and negation exactly as printed.
- Every number must appear in its evidence with the same magnitude; computed claims are `inferred` + `reasoning`.
- Claims of other papers ("[12]", "Smith et al.") → `scope: cited_work`; figure claims need the caption or a crop.
- Never invent values, entities, references or DOIs. Not in the paper → `null` + `unreported` entry with a reason;
  contradictions (abstract vs table) → `conflicts` with both locations.

## Status (set by np_build, never by hand)
- `success`: ≥1 passing record, every required contract field filled and schema-valid (or `null` + reasoned `unreported`),
  RDF export round-trips. Unsupported answer values are held in `field_review` and the result field stays null.
  Non-blocking issues go to `run_warnings` (quarantined ids, assertions not built, image-read
  evidence, unread pages, fields needing review, auto-normalised spellings).
- `partial` (+ `status_reason`): no passing record, a required field empty / wrong type, or broken export.
  `failed`: no source readable (encrypted PDF: `--password` only if given).
- Safety: bad draft JSON / shape / unexpected error → exit 2, `result.json` untouched, reason in `build_error.json`;
  replacing N built records by none → exit 3 (use `--merge`). Previous result → `result.prev.json`.

| Situation | Do |
|---|---|
| Page without text / data only in a figure | `crop_region.py`, read the crop, `image_region` evidence |
| Paper + supplement | primary is parsed first; set `"source"` on assertions from the supplement |
| Quote not found (R04) / long paper | cite the sentence id or a verbatim sub-span / `np_init.py ... --pages 1-10`, note skipped parts in `uncertainty` |
