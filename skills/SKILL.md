---
name: nanopub-evidence-builder
description: Turn claims in a scientific PDF into nanopublications - atomic, individually checkable assertions, each bound to the smallest verbatim evidence (page, block, sentence id, line boxes, or table cell) and to source provenance (title, authors, year, DOI/arXiv id + version, PDF SHA-256). Use when a research-reading task asks to answer questions with evidence, extract or split findings/claims into assertions, bind evidence, check claim quality, or export machine-readable knowledge units (JSON, TriG, N-Quads, JSON-LD). Scripts resolve every quote against the PDF, run 26 reproducible quality rules (compound claim, number not in evidence, dropped hedge/condition, negation mismatch, causal claim without causal wording, cited work presented as own, missing provenance ...) and write result.json with honest status.
---

# nanopub-evidence-builder (v0.1.0)

You write the claims; the scripts make them traceable and check them. Every assertion ends up as a
nanopublication: **assertion** (one claim, subject–predicate–object + qualifiers), **provenance**
(verbatim evidence with page/sentence/box, evidence type, source metadata) and **publication info**.
Plain Python 3 + PyMuPDF. No network. `SKILL_DIR` = the directory of this file (`/skills/weekly-skill`).

## Workflow (about 6–12 model requests; save early)

1. **Init** (seconds):
   ```bash
   python $SKILL_DIR/scripts/np_init.py --case /input/case.json --out /app/output --work /app/work
   ```
   Prints contract fields, a suggested `kind` per field, the source metadata found in the PDF, and paths.
   Writes `/app/output/result.json` (valid partial result), `parsed/<stem>/sentences.jsonl`
   (every sentence with id `p<page>-b<block>-s<k>`, page, section, cues), `parsed/<stem>/source_metadata.json`,
   `/app/work/candidates.md` (top claim sentences with cues: `num`, `hedge`, `neg`, `cite`, `cmp`, `cause`, `compound`)
   and `/app/work/draft.json` (template).
2. **Read** `task.md`, `case.json`, then `candidates.md`. To find more:
   ```bash
   python $SKILL_DIR/scripts/np_find.py /app/output/parsed/<stem> "words from the question" --top 8   # --page N --section Results --regex
   python $SKILL_DIR/scripts/np_find.py /app/output/parsed/<stem> --id p5-b89-s2 --context 1         # neighbours (coreference)
   python $SKILL_DIR/scripts/table_query.py /app/output/parsed/<stem> --table "Table 4" --list         # tables: columns + row keys
   ```
3. **Write `/app/work/draft.json`** (format below). One assertion = one claim that can be true or false on its own.
4. **Build + check**:
   ```bash
   python $SKILL_DIR/scripts/np_build.py --draft /app/work/draft.json --out /app/output
   ```
   Prints per assertion: verdict (`pass` / `warn` / `fail`), extraction confidence, rule hits with the triggering text,
   and how each evidence item matched. Writes `nanopubs.json|.trig|.nq|.jsonld`, `quality_report.json`, `review.md`,
   `evidence_previews/page_NNN.png` (quotes boxed on the page), `evidence_crops/`, and fills `result.json`.
5. **Fix every `fail`**, then re-run step 4 (ids are content-derived, re-runs are idempotent). Either fix the assertion
   or keep it and explain in `uncertainty`; delete assertions you cannot support. Look at `warn`s: fix, or accept with a reason.
   Look at one preview PNG to confirm the boxes sit on the right text.

## draft.json

```json
{
 "source": "<stem of the parsed PDF>",
 "assertions": [
  {"id": "a1",
   "text": "An ensemble of residual nets achieves 3.57% error on the ImageNet test set.",
   "subject": "ensemble of residual nets", "predicate": "achieves", "object": "error on the ImageNet test set",
   "qualifiers": {"value": "3.57", "unit": "%", "metric": "error", "dataset": "ImageNet test set"},
   "modality": "asserted", "scope": "this_work", "evidence_type": "direct",
   "evidence": ["p1-b5-s6"]},
  {"id": "a2", "text": "...", "subject": "...", "predicate": "lower_than", "object": "...",
   "evidence_type": "inferred", "reasoning": "Table 4: 4.49 < 7.40",
   "evidence": [{"type": "table_cell", "table": "Table 4", "row": {"method": "ResNet-152"}, "column": "top-5 err.", "value": "4.49"},
                {"type": "table_cell", "table": "Table 4", "row": {"method": "ResNet-34 B"}, "column": "top-5 err."}],
   "context": [{"page": 5, "quote": "verbatim sentence that defines a term or resolves 'it'"}]},
  {"id": "a3", "scope": "cited_work", "cited_reference": "[41]", "evidence_type": "cited", "...": "..."}
 ],
 "fields": {
  "<contract field>": {"kind": "assertions"},
  "<question field>": {"value": "answer as printed / normalised", "from": ["a1", "a2"]}
 },
 "source_overrides": {"authors": {"value": ["A. Author", "B. Author"], "reason": "title page p1-b2", "evidence": "p1-b2"}},
 "unreported": [{"field": "<field>", "reason": "the paper does not report it (searched: ...)"}],
 "conflicts": [], "uncertainty": []
}
```
- **evidence** items: a sentence id (best), `{"page": N, "quote": "verbatim text"}` (a sub-span of a sentence is fine and preferred
  when the sentence holds several claims), `{"type": "table_cell", "table", "row": {KEY: VALUE}, "column", "value"}`, or for pages
  without text layer `{"type": "image_region", "page", "bbox", "crop", "transcription", "provenance"}` (crop with `crop_region.py`).
  `role` defaults to `support`; put definitions / coreference sentences in `context`.
- **predicate**: must be one of `vocab/relations.json` (`has_value achieves outperforms underperforms higher_than lower_than
  comparable_to improves reduces increases decreases causes enables prevents correlates_with associated_with has_property is_a
  part_of has_part defined_as uses evaluated_on trained_on proposes observed_in detected_in does_not_improve not_associated_with
  does_not_cause lacks_property`).
- **qualifiers** (free keys, these are checked): `value`, `unit`, `metric`, `dataset`, `condition`, `population`, `setting`, `comparator`, `time`.
- **modality**: `asserted` | `hedged` (keep the hedge word: "suggests", "may") | `negated` | `hypothesis`.
- **scope**: `this_work` | `cited_work` (+ `cited_reference` marker, resolved to the reference list) | `background`.
- **evidence_type**: `direct` (quote states it) | `inferred` (+ `reasoning`, e.g. comparing two cells) | `cited`.
- **fields** `kind`: `nanopublications` (full records) | `assertions` (flat list) | `evidence` (flat list with assertion ids) |
  `provenance` (source metadata + where each value was read) | `quality` (rule report) | `relations` (triples) |
  `trig` / `nquads` / `jsonld` (file path) | or give `value` (+ `from` assertion ids) for answers. Use `"filter": ["a1"]` to restrict.
  If the contract wants a different shape, fill it with `value` built from the same assertions; keep the ids.

## How to split (拆得细)

- One subject, one predicate, one object, one result. "A is easier to optimise **and** gains accuracy with depth" → two assertions.
- Keep conditions: dataset, setting, population, sample size, model size go into `qualifiers` (rule R16 lists any you dropped).
- Keep hedges and negation exactly (R12/R14). Do not generalise ("all", "always") beyond the quote (R15).
- Numbers must be in the evidence (R09). If you computed a number or a comparison, `evidence_type: inferred` + `reasoning`.
- Claims about other papers (sentence with `[12]` / "Smith et al.") → `scope: cited_work`, `cited_reference` (R20/R21).
- Answer questions with the same assertions: the field value plus `from` ids gives the answer its evidence chain.

## Rules (np_check.py; same input → same report)

`fail` (must fix): R01 missing field · R02 predicate not in vocabulary · R03 no support evidence · R04 quote / cell not found in
the PDF · R07 compound assertion · R09 number not in evidence · R11 quantitative predicate without value · R14 negation mismatch
(assertion negated, evidence not; negative predicate without negation) · R22 inferred without reasoning · R25 no title / PDF hash.
`warn` (decide): R05 fuzzy match or page corrected · R06 evidence not minimal · R08 several entities in a slot · R10 number in a name
(top-5) not in evidence · R12 hedge dropped · R13 hedge added · R14 negation in evidence not reflected · R15 universal wording ·
R16 condition dropped · R17 causal predicate without causal wording · R18 comparative predicate without comparison wording ·
R19 subject not in evidence · R20 cited work presented as own · R21 cited reference unresolved · R23 low lexical support for
"direct" · R24 duplicate · R25 authors / year / identifier / version missing · R26 evidence read from an image.
Extraction confidence = base (direct .9 / cited .75 / inferred .6) × match (exact 1 / fuzzy .85 / image .7 / none 0) − .1 per warn;
any fail caps it at .2. It measures how well the record is backed by the cited text, not whether the claim is true.

## Status and honesty

- `result.json.status`: `success` only if every contract field is filled, no record fails, and the RDF export round-trips;
  otherwise `partial` (with `status_reason`), `failed` if no PDF could be read. Never set it by hand to hide a fail.
- Metadata values carry their source (`pdf_text` page/block, `arxiv_stamp`, `pdf_info`, `file_hash`, `agent_override`).
  Authors are split heuristically (`needs_review`); correct them via `source_overrides` with the block id you read.
  A year from `pdf_info` is a file date, not a publication year. Not found → `null`, never guessed.
- No value without evidence. Unknown / not reported / unreadable → `null` + `unreported` with the reason and what you searched.
  Contradictions between parts of the paper (abstract vs table) → `conflicts` with both evidence locations.
- The nanopubs are unsigned drafts in the `http://purl.org/nanopub/temp/` namespace (not Trusty URIs, not published).

## Failure handling

| Situation | Scripts | You |
|---|---|---|
| Encrypted PDF | init: `failed`, `parse_error=encrypted` | `--password` only if given; else report |
| Page without text layer | `unresolved_pages` in sources, no sentences there | `crop_region.py PARSED --page N --bbox ...`, read the crop, `image_region` evidence (R26 warn stays) |
| Quote not found (R04) | closest sentence + similarity in the evidence item | cite the sentence id or copy verbatim |
| Sentence split wrong (formula/table text merged) | — | quote the exact sub-span with `{page, quote}` |
| Long paper / time short | `--pages 1-10` on init | fewer, well-supported assertions; list skipped parts in `uncertainty` |
| Several PDFs | one parsed dir each | set `"source"` per assertion |

See `README.md` for field dictionary, method, limits and maintenance.
