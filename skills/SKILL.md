---
name: nanopub-evidence-builder
description: Turn claims in a scientific paper (PDF, .md, .txt, .docx, .html) into nanopublications — atomic, individually checkable assertions, each bound to verbatim evidence (page, sentence_id, bbox, table_cell, or image_region) and source provenance (title, authors, year, DOI/arXiv id + version, SHA-256). Use when asked to answer questions with evidence, split findings into atomic claims, check claim quality, or export JSON/TriG/N-Quads/JSON-LD.
---

# nanopub-evidence-builder (v0.3.1)

You write the claims; the scripts bind verbatim evidence, run 31 quality rules, and export `result.json` + RDF. Plain Python 3 + PyMuPDF. No network.

**Every shell call starts fresh** — begin each command block with:
```bash
PY=$(command -v python3 || command -v python)
for d in "${SKILL_DIR:-}" /skills/weekly-skill /app/submission /app/skills /app/skills/* "$(pwd)"; do [ -n "$d" ] && [ -f "$d/scripts/np_init.py" ] && SKILL_DIR="$d" && break; done; echo "PY=$PY SKILL_DIR=$SKILL_DIR"
```

## Workflow (about 6–10 model requests)

1. **Init** (writes valid partial `result.json`, `candidates.md`, and pre-scaffolded `draft.json` with `required_questions` + `_pre_draft_assertions`):
   ```bash
   $PY $SKILL_DIR/scripts/np_init.py --case /input/case.json --out /app/output --work /app/work
   ```
2. **Read** `task.md`, `case.json`, `candidates.md`, and `draft.json`. Search more evidence as needed:
   ```bash
   $PY $SKILL_DIR/scripts/np_find.py /app/output/parsed/<stem> "query words" --top 8
   $PY $SKILL_DIR/scripts/np_find.py /app/output/parsed/<stem> --id p5-b89-s2 --context 1
   $PY $SKILL_DIR/scripts/table_query.py /app/output/parsed/<stem> --table "Table 4" --list
   ```
3. **Edit `/app/work/draft.json`** (use `_pre_draft_assertions` as starting points; one atomic claim per assertion).
4. **Build + check**:
   ```bash
   $PY $SKILL_DIR/scripts/np_build.py --draft /app/work/draft.json --out /app/output
   ```
   Outputs `nanopubs.json|.trig|.nq|.jsonld`, `quality_report.json`, `review.md`, `evidence_previews/`, and `result.json` (with `confidence_basis`, `identifier`, inline evidence, and `case_id`).
5. **Optional `--merge` fix**: Fix any `fail` in a small draft and run `np_build.py --draft /app/work/fix.json --out /app/output --merge`. Keep weak/warned assertions with `uncertainty` notes rather than deleting valid leads.

## draft.json & Evidence

```json
{
 "source": "<primary stem>",
 "assertions": [
  {"id": "a1", "text": "ResNet-152 achieves 3.57% top-5 error on ImageNet test set.",
   "subject": "ResNet-152", "predicate": "achieves", "object": "top-5 error on ImageNet test set",
   "qualifiers": {"value": "3.57", "unit": "%", "metric": "top-5 error", "dataset": "ImageNet test set"},
   "modality": "asserted", "scope": "this_work", "evidence_type": "direct", "evidence": ["p1-b5-s6"]}
 ],
 "fields": {"<contract_field>": {"kind": "assertions"}, "<question_field>": {"value": "...", "from": ["a1"]}},
 "source_overrides": {}, "unreported": [], "conflicts": [], "uncertainty": []
}
```
- **evidence**: sentence id (`p1-b5-s6`), `{"page": N, "quote": "exact text"}`, `{"type": "table_cell", "table": "Table 4", "row": {...}, "column": "...", "value": "..."}`, or `image_region` from `crop_region.py`.
- **predicate** (`vocab/relations.json`): `has_value`, `achieves`, `outperforms`, `underperforms`, `higher_than`, `lower_than`, `comparable_to`, `improves`, `reduces`, `increases`, `decreases`, `causes`, `enables`, `prevents`, `correlates_with`, `associated_with`, `has_property`, `is_a`, `part_of`, `has_part`, `defined_as`, `uses`, `evaluated_on`, `trained_on`, `proposes`, `observed_in`, `detected_in`, `does_not_improve`, `not_associated_with`, `does_not_cause`, `lacks_property`.
- **modality**: `asserted` | `hedged` | `negated` | `hypothesis`. **scope**: `this_work` | `cited_work` (+ `cited_reference`) | `background`. **evidence_type**: `direct` | `inferred` (+ `reasoning`) | `cited`.

## Compact Evidence & Quality Checklist

- **Split atomically (R07)**: One subject, one predicate, one object, one value. Split `and` / `;` / `respectively` / `并且`.
- **Preserve qualifiers, hedges, negation (R12–R16)**: Keep dataset, setting, sample size, `only`/`at least`, hedges (`may`/`suggests`), and negation exact.
- **Numbers & reasoning (R09, R27, R28)**: Every number and inferred operand must appear in cited evidence with matching magnitude (`6 billion` = `60亿`).
- **Scope & figures (R20, R21, R31)**: Mark cited claims `cited_work` + `cited_reference`; bind figure claims to caption or `image_region`.
- **Honesty**: Unknown/unreported -> `null` + `unreported`. Contradictions -> `conflicts`. Weak/warned assertions -> keep with `uncertainty` note. Never fabricate data or overwrite `status` by hand.
- **Safety**: Exit 2/3 leaves `result.json` untouched (`build_error.json`). Previous result is saved to `result.prev.json`.

## Output Contract & Field Mapping

- `np_init.py` reads `output_contract` / `output_fields` / `output_schema` / `deliverables` / `schema`, copies `case_id` as given, and writes `required_questions` / `optional_questions` into `draft.json`.
- Map each contract field in `draft.fields`:
  - `{"kind": "assertions"}` (or `"nanopublications"`, `"evidence"`, `"provenance"`, `"quality"`, `"relations"`, `"summary"`, `"unreported"`, `"conflicts"`, `"uncertainty"`).
  - `{"value": <answer>, "from": ["a1", "a2"]}` for direct question fields; `result.json` attaches `field_evidence` (quote, page, table/row/column, unit) automatically.
  - Optional `"filter": ["a1"]` or `"rename": {"text": "claim"}` to match custom schemas.
- Every built assertion includes `confidence`, `confidence_basis` (text explanation of evidence type, match, and rule penalties), and `identifier` (`nanopub_uri`, `doi`, `arxiv_id`, `source_iri`).

## Failure Handling

| Situation | Script behaviour | Action |
|---|---|---|
| Encrypted PDF | `init` marks `failed` (`parse_error=encrypted`) | Pass `--password` if provided, else report |
| Page without text layer | `unresolved_pages` listed in `sources` | Use `crop_region.py`, bind `image_region` |
| Main paper + supplement | Primary first; one parsed dir each | Set `"source"` on supplement assertions |
| Quote not found (R04) | Closest sentence + similarity printed | Use exact `sentence_id` or verbatim `{page, quote}` |
| Broken draft / shrink | Exit 2/3, `result.json` untouched | Fix JSON or use `--merge` / `--allow-shrink` |
| Long paper / time short | `--pages 1-10` on `np_init.py` | Build core assertions; list skipped pages in `uncertainty` |

See `README.md` for full field dictionary, 31-rule reference, and maintenance notes.
