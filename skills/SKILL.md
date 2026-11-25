---
name: nanopub-evidence-builder
description: Turn claims in a scientific paper (PDF, or .md / .txt / .docx / .html text) into nanopublications - atomic, individually checkable assertions, each bound to the smallest verbatim evidence (page, block, sentence id, line boxes, table cell or image region) and to source provenance (title, authors, year, DOI/arXiv id + version, file SHA-256). Use when a research-reading task asks to answer questions with evidence, extract or split findings/claims into assertions, bind evidence, check claim quality, or export machine-readable knowledge units (JSON, TriG, N-Quads, JSON-LD). Scripts resolve every quote against the source, run 31 reproducible quality rules (compound claim, number or magnitude not in evidence, wrong arithmetic in an inferred comparison, dropped hedge/limiter/condition, negation mismatch, causal claim without causal wording, cited work presented as own, missing provenance ...) and write result.json with honest status, never clobbering a good result.
---

# nanopub-evidence-builder (v0.3.0)

You write the claims; the scripts make them traceable and check them. Every assertion ends up as a
nanopublication: **assertion** (one claim, subject–predicate–object + qualifiers), **provenance**
(verbatim evidence with page/sentence/box, evidence type, source metadata) and **publication info**.
Plain Python 3 + PyMuPDF. No network.

**Every shell call starts fresh** — begin each command block with these two lines (or reuse the absolute paths they print):
```bash
PY=$(command -v python3 || command -v python)
for d in "${SKILL_DIR:-}" /skills/weekly-skill /app/submission /app/skills /app/skills/* "$(pwd)"; do [ -n "$d" ] && [ -f "$d/scripts/np_init.py" ] && SKILL_DIR="$d" && break; done; echo "PY=$PY SKILL_DIR=$SKILL_DIR"
```

## Workflow (about 6–12 model requests; a valid partial result.json exists from step 1 on)

1. **Init** (seconds):
   ```bash
   $PY $SKILL_DIR/scripts/np_init.py --case /input/case.json --out /app/output --work /app/work
   ```
   Prints the materials with their **role** (`primary` first, `supplement` last — chosen from case.json role/type keys, key
   names like `paper_pdf` / `supplementary_pdf`, or file names; never by size), contract fields split into **required /
   optional**, a suggested `kind` per field, the source metadata, and paths. Accepts PDFs and `.md/.txt/.docx/.html` papers
   (task.md / README are never taken as the paper). Writes `/app/output/result.json` (valid partial result),
   `parsed/<stem>/sentences.jsonl` (every sentence with id `p<page>-b<block>-s<k>`, page, section, cues),
   `parsed/<stem>/source_metadata.json`, `/app/work/candidates.md` (top claim sentences with cues: `num`, `mag` (6 billion,
   60亿), `hedge`, `neg`, `cite`, `attrib` (attributed to others), `cmp`, `cause`, `compound`; plus the table list) and
   `/app/work/draft.json` (template; `source` = primary).
2. **Read** `task.md`, `case.json`, then `candidates.md`. To find more:
   ```bash
   $PY $SKILL_DIR/scripts/np_find.py /app/output/parsed/<stem> "words from the question" --top 8   # --page N --section Results --regex
   $PY $SKILL_DIR/scripts/np_find.py /app/output/parsed/<stem> --id p5-b89-s2 --context 1         # neighbours (coreference)
   $PY $SKILL_DIR/scripts/table_query.py /app/output/parsed/<stem> --table "Table 4" --list         # tables: columns + row keys
   ```
3. **Write `/app/work/draft.json`** (format below). One assertion = one claim that can be true or false on its own.
4. **Build + check**:
   ```bash
   $PY $SKILL_DIR/scripts/np_build.py --draft /app/work/draft.json --out /app/output
   ```
   Prints per assertion: verdict (`pass` / `warn` / `fail`), extraction confidence, rule hits with the triggering text,
   and how each evidence item matched. Writes `nanopubs.json|.trig|.nq|.jsonld`, `quality_report.json`, `review.md`,
   `evidence_previews/page_NNN.png` (quotes boxed on the page), `evidence_crops/`, `draft.built.json`, and fills `result.json`.
5. **Fix every `fail`**, then re-run step 4 (ids are content-derived, re-runs are idempotent). To save requests, put only the
   new / fixed assertions in a small draft and add `--merge` (same id replaces, new ids append, `"delete": ["a3"]` removes).
   Either fix an assertion or keep it and explain in `uncertainty`; delete assertions you cannot support. Look at `warn`s.
   Before finishing, verify `result.json` has `status: "success"` (or an honest `partial` with `status_reason`), and inspect
   one `evidence_previews/page_NNN.png` to confirm the boxes sit on the right text.

**Safety:** np_build never damages a good result. Invalid JSON / wrong draft shape → exit 2, `result.json` untouched,
reason in `build_error.json`. A draft that would replace N built records by none (e.g. the empty template) → exit 3
(use `--merge`, or `--allow-shrink` if intended). A malformed assertion is skipped and listed in `build_errors` (status
`partial`); the others are built. The previous result is always copied to `result.prev.json`.

## draft.json

```json
{
 "source": "<stem of the parsed primary source>",
 "assertions": [
  {"id": "a1",
   "text": "An ensemble of residual nets achieves 3.57% error on the ImageNet test set.",
   "subject": "ensemble of residual nets", "predicate": "achieves", "object": "error on the ImageNet test set",
   "qualifiers": {"value": "3.57", "unit": "%", "metric": "error", "dataset": "ImageNet test set"},
   "modality": "asserted", "scope": "this_work", "evidence_type": "direct",
   "evidence": ["p1-b5-s6"]},
  {"id": "a2", "text": "...", "subject": "...", "predicate": "lower_than", "object": "...",
   "evidence_type": "inferred", "reasoning": "Table 4 top-5 err.: 4.49 < 5.71",
   "evidence": [{"type": "table_cell", "table": "Table 4", "row": {"method": "ResNet-152"}, "column": "top-5 err.", "value": "4.49"},
                {"type": "table_cell", "table": "Table 4", "row": {"method": "ResNet-34 B"}, "column": "top-5 err."}],
   "context": [{"page": 5, "quote": "verbatim sentence that defines a term or resolves 'it'"}]},
  {"id": "a3", "scope": "cited_work", "cited_reference": "[41]", "evidence_type": "cited", "...": "..."}
 ],
 "fields": {
  "<contract field>": {"kind": "assertions"},
  "<contract field wanting other keys>": {"kind": "assertions", "rename": {"text": "claim"}},
  "<question field>": {"value": "answer as printed / normalised", "from": ["a1", "a2"]}
 },
 "source_overrides": {"authors": {"value": ["A. Author", "B. Author"], "reason": "title page p1-b2", "evidence": "p1-b2"}},
 "unreported": [{"field": "<field>", "reason": "the paper does not report it (searched: ...)"}],
 "conflicts": [], "uncertainty": []
}
```
- **evidence** items: a sentence id (best), `{"page": N, "quote": "verbatim text"}` (a sub-span of a sentence is fine and preferred
  when the sentence holds several claims), `{"type": "table_cell", "table", "row": {KEY: VALUE}, "column", "value"}`, or for
  figures / pages without text layer `{"type": "image_region", "page", "bbox", "crop", "transcription", "provenance"}` (all from
  `crop_region.py PARSED --page N --bbox x0,y0,x1,y1`). `role` defaults to `support`; definitions / coreference go in `context`.
- **predicate**: must be one of `vocab/relations.json` (`has_value achieves outperforms underperforms higher_than lower_than
  comparable_to improves reduces increases decreases causes enables prevents correlates_with associated_with has_property is_a
  part_of has_part defined_as uses evaluated_on trained_on proposes observed_in detected_in does_not_improve not_associated_with
  does_not_cause lacks_property`).
- **qualifiers** (free keys, these are checked): `value`, `unit`, `metric`, `dataset`, `condition`, `population`, `setting`, `comparator`, `time`.
- **modality**: `asserted` | `hedged` (keep the hedge word: "suggests", "may", "probably") | `negated` | `hypothesis`.
- **scope**: `this_work` | `cited_work` (+ `cited_reference` marker, resolved to the reference list) | `background`.
- **evidence_type**: `direct` (quote states it) | `inferred` (+ `reasoning` that shows the operands and the step, e.g. `5.71 - 4.49 = 1.22`
  or `4.49 < 5.71`; R28 recomputes it) | `cited`.
- **fields** `kind`: `nanopublications` (full records) | `assertions` (flat list) | `evidence` (flat list with assertion ids) |
  `provenance` (source metadata + where each value was read) | `quality` (rule report) | `relations` (triples) |
  `trig` / `nquads` / `jsonld` / `review` (file path) | `summary` / `unreported` / `conflicts` / `uncertainty` / `status` | or give
  `value` (+ `from` assertion ids) for answers. `"filter": ["a1"]` restricts a list; `"rename": {"old": "new"}` renames item keys.
  Contract fields you do not map are auto-filled from the suggested kind when there is one (marked `auto` in `field_status`).

## Output contract

np_init reads the contract from `output_contract` / `output_fields` / `output_schema` / `deliverables` / `schema` (also nested),
as JSON Schema (`properties` + `required`, nested `items` / `properties`), a name→description map, or a list of names / objects
with `required: false`. `case_id` is copied as given (also `0`). np_build checks each filled field's type (`integer` ≠ `number`,
`"12"` → 12 only when lossless, 3.5 is never truncated) and nested required keys. A required field that is empty or does not
match its schema makes the status `partial` (listed in `status_reason.unfilled_fields`, problems in `field_problems`);
unfilled optional fields are only listed.

## How to split (拆得细)

- One subject, one predicate, one object, one result. "A is easier to optimise **and** gains accuracy with depth" → two
  assertions; "A and B reach 6.7% and 6.1%, respectively" → two; "提高了准确率并且降低了延迟" → two (R07).
- Keep conditions and limiters: dataset, setting, population, sample size, model size, "only", "at least", "仅" go into
  `qualifiers` or the text (R16 lists any you dropped).
- Keep hedges and negation exactly (R12/R14). Do not generalise ("all", "always") beyond the quote (R15).
- Numbers must be in the evidence (R09) with the same magnitude (R27): "6 billion" = "60 亿" = "6×10^9" passes, "6 million"
  vs "6 billion" fails; "1.28 million" is accepted as the rounding of "1,281,167".
- Computed numbers or comparisons: `evidence_type: inferred` + `reasoning`; every operand must be in an evidence item
  (cells from the right table — R28 caught a Table 3 value used as a Table 4 value), every step must compute.
- Claims about other papers (`[12]`, "Smith et al.", "previous studies have shown") → `scope: cited_work`, `cited_reference`
  (R20/R21); do not mark first-person results as cited (R29).
- A claim read off a figure ("Fig. 3 shows …") needs the caption or an `image_region` crop as evidence (R31).
- Answer questions with the same assertions: the field value plus `from` ids gives the answer its evidence chain.

## Rules (np_check.py; same input → same report)

`fail` (must fix): R01 missing field · R02 predicate not in vocabulary · R03 no support evidence · R04 quote / cell not found in
the source · R07 compound assertion · R09 number not in evidence · R11 quantitative predicate without value · R14 negation mismatch
(assertion negated, evidence not; negative predicate without negation) · R22 inferred without reasoning · R25 no title / file hash ·
R27 magnitude mismatch · R28 reasoning operand not in evidence / arithmetic wrong / comparison false · R30 image region without page+bbox.
`warn` (decide): R05 fuzzy match or page corrected · R06 evidence not minimal · R08 several entities in a slot · R10 number in a name
(top-5) not in evidence · R12 hedge dropped · R13 hedge added · R14 negation in evidence not reflected · R15 universal wording ·
R16 condition / limiter dropped · R17 causal predicate without causal wording · R18 comparative predicate without comparison wording ·
R19 subject not in evidence · R20 cited / attributed work presented as own · R21 cited reference unresolved · R23 low lexical support for
"direct" · R24 duplicate · R25 authors / year / identifier / version missing · R26 evidence read from an image · R29 cited_work on a
first-person result · R30 image region without crop / hash / transcription · R31 figure claim without figure evidence.
Extraction confidence = base (direct .9 / cited .75 / inferred .6) × match (exact 1 / fuzzy .85 / image .7 / none 0) − .1 per warn;
any fail caps it at .2. It measures how well the record is backed by the cited text, not whether the claim is true.

## Status and honesty

- `result.json.status`: `success` only if every required contract field is filled and matches its schema, no record fails, no
  assertion failed to build, and the RDF export round-trips; otherwise `partial` (with `status_reason`), `failed` if no source
  could be read. Never set it by hand to hide a fail.
- Metadata values carry their source (`pdf_text` page/block, `arxiv_stamp`, `front_matter`, `pdf_info`, `file_hash`, `agent_override`).
  Authors are split heuristically (`needs_review`); correct them via `source_overrides` with the block id you read (the file hash
  cannot be overridden). A year from `pdf_info` / docx properties is a file date, not a publication year. Not found → `null`, never guessed.
- No value without evidence. Unknown / not reported / unreadable → `null` + `unreported` with the reason and what you searched.
  Contradictions between parts of the paper (abstract vs table) → `conflicts` with both evidence locations.
- The nanopubs are unsigned drafts in the `http://purl.org/nanopub/temp/` namespace (not Trusty URIs, not published).

## Failure handling

| Situation | Scripts | You |
|---|---|---|
| Encrypted PDF | init: `failed`, `parse_error=encrypted` | `--password` only if given; else report |
| Page without text layer / data only in a figure | `unresolved_pages` in sources, no sentences there | `crop_region.py PARSED --page N --bbox ...`, read the crop, `image_region` evidence (R26 warn stays) |
| Paper given as .md / .txt / .docx / .html | parsed like a PDF; page = logical page, no boxes | cite sentence ids as usual |
| Main paper + supplement | primary first; one parsed dir each | set `"source"` per assertion that uses the supplement |
| Quote not found (R04) | closest sentence + similarity in the evidence item | cite the sentence id or copy verbatim |
| Sentence split wrong (formula/table text merged) | — | quote the exact sub-span with `{page, quote}` |
| Broken draft / build refused | exit 2/3, `build_error.json`, result.json unchanged | fix the JSON, or `--merge` a small draft |
| Long paper / time short | `--pages 1-10` on init | fewer, well-supported assertions; list skipped parts in `uncertainty` |

See `README.md` for field dictionary, method, limits and maintenance.
