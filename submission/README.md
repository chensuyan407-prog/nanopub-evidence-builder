# nanopub-evidence-builder 0.3.1 — field dictionary, method, limits

Agent Skill for **阅读14｜Nanopublication**: turn the conclusions of a scientific PDF into
nanopublications — one atomic, checkable assertion per record, each bound to the smallest verbatim
evidence in the PDF and to source provenance — and export them as JSON, TriG, N-Quads and JSON-LD.
Sub-problem chosen: **assertion splitting + evidence binding + rule-based quality checks**, with source
metadata and export as supporting parts. Not a nanopub server client: records are unsigned drafts.

## Files

| path | role |
|---|---|
| `SKILL.md` | agent instructions (workflow, draft format, rules, failure handling) |
| `scripts/np_init.py` | find the case materials (primary / supplement by role, never by size), parse PDFs and text papers, sentence index with ids, source metadata, claim candidates, result.json skeleton + contract spec, draft template |
| `scripts/np_contract.py` | output-contract parsing (all wrappers, JSON Schema required/optional, nested items) and lossless type validation |
| `scripts/text_source.py` | `.md` / `.txt` / `.docx` / `.html` papers → the same `doc.json` (logical pages, paragraph blocks with line ranges, tables, references, front-matter metadata) |
| `scripts/np_find.py` | search sentences (BM25-style, `--page`, `--section`, `--regex`, `--id --context`) |
| `scripts/np_build.py` | resolve evidence → records → quality check → TriG / N-Quads / JSON-LD → previews → result.json (never clobbers a good result; `--merge`) |
| `scripts/np_check.py` | the 31 quality rules + extraction confidence (also runs standalone on `nanopubs.json`) |
| `scripts/np_common.py` | normalisation, sentence splitter, cue lexicons, magnitudes (thousand/million/billion/万/亿/10^n), attribution cues, metadata extraction |
| `scripts/parse_pdf.py`, `mathlayout.py`, `table_query.py`, `crop_region.py`, `provenance.py`, `find_evidence.py` | layout-aware PDF parser and helpers (from pdf-evidence-reader 0.3.0, unchanged): reading order, tables by row/column name, equations, page-image crops bound by hash |
| `vocab/relations.json` | controlled predicates (31), relation types, evidence types, scopes, modalities |
| `examples/minimal_example.md` | smallest end-to-end example (input, calls, output excerpt) |
| `requirements.txt` | `pymupdf>=1.23`, `numpy`; optional `fonttools`, `rdflib` (stronger TriG check), tesseract (OCR) |

## Record (`nanopubs.json` → `nanopublications[]`)

| field | meaning |
|---|---|
| `np_id`, `uri` | content-derived id (`np-` + 12 hex of SHA-256 over source hash, subject/predicate/object/value, text, scope, modality, evidence type, quotes); `uri` in `http://purl.org/nanopub/temp/`. Same draft → same ids. |
| `assertion.text` | the claim in one sentence (keeps hedges, negation, conditions) |
| `assertion.subject/object` | `{label, type, iri?}` |
| `assertion.predicate` | `{id, label, relation_type, polarity, iri}` from `vocab/relations.json` |
| `assertion.qualifiers` | `value`, `unit`, `metric`, `dataset`, `condition`, `population`, `setting`, `comparator`, `time` … |
| `assertion.modality` | `asserted` / `hedged` / `negated` / `hypothesis` |
| `assertion.scope` | `this_work` / `cited_work` / `background`; `cited_reference` + `cited_reference_resolved` (reference-list entry with page/block) |
| `evidence_type` | `direct` / `inferred` (+ `reasoning`) / `cited`; `eco` = one ECO code hint (0000006 experimental, 0000304 traceable author statement, 0000305 curator inference, 0000303 author statement without traceable support) |
| `evidence[]` | `evidence_id`, `role` (support / context), `kind` (text / table_cell / image_region), `quote` (as printed), `page`, `pages`, `block_id`, `sentence_id`, `section_path`, `bbox` + `rects` (PDF points, top-left origin; line boxes of the quote), `bbox_precision` (quote / quote_partial / block / table_cell / image_region), `match` (exact_sentence / exact_substring / exact_paragraph / exact_page_text / table_cell / fuzzy / image_read / not_found), `page_corrected_from`, `citations` (marker → reference text), `crop` |
| `provenance.source` | `title`, `authors`, `year`, `venue`, `doi`, `arxiv_id`, `version`, `arxiv_date`, `pdf_sha256` (PDF) or `file_sha256` + `source_format` (text papers), `material_role` (primary / supplement), `page_count`, `file_name` — each `{value, source{kind, page, block_id, quote}, confidence, needs_review?, note?}` |
| `provenance.source_iri` | DOI URL, else arXiv abs URL with version, else `urn:sha256:<file hash>` (characters illegal in IRIs are percent-encoded in the RDF) |
| `pubinfo` | `created`, `creator`, `status` (unsigned draft) |
| `quality` | `verdict` (pass / warn / fail) + `issues[]` (`rule`, `severity`, `message`, `trigger`, `fix_hint`) |
| `confidence` | extraction confidence `score`, `level`, `basis`, `formula` |
| `content_hash` | SHA-256 over assertion + evidence (not a Trusty URI) |

RDF layout per nanopub: `Head` (np:hasAssertion / hasProvenance / hasPublicationInfo), `assertion` graph
(plain triple `subject predicate object` only for unhedged own findings, plus an `npeb:AtomicClaim` node carrying
text, slots, modality, scope and qualifiers), `provenance` graph (`prov:wasDerivedFrom` source, `prov:wasGeneratedBy`
extraction activity, one `npeb:EvidenceSnippet` per evidence item with `prov:value` quote, page, block, sentence, bbox,
plus source title/creators/issued/identifiers/version/sha256), `pubinfo` graph. `npeb:` = `http://purl.org/nanopub/temp/npeb-terms#`
(local, not dereferenceable). Export check: every N-Quads line must match the strict W3C grammar (IRIREF without
`<>"{}|^`\` or spaces, escaped literals, `^^` datatypes), JSON-LD statements counted, TriG parsed with rdflib when
installed; counts must agree (`export_roundtrip.ok`). Control characters in quotes are `\uXXXX`-escaped.

## result.json

`case_id` (as given, `0` included), every `output_contract` field (filled as mapped in `draft.fields`; unmapped fields
from the suggested kind), `status` (`success` when at least one record passes, every *required* field is filled and matches its schema
(or is `null` and listed in `unreported` with a reason), and the RDF export round-trips; failing records are
quarantined out of the exports into `quarantined_assertions`; build errors, image-read evidence, unread pages,
fields needing review and auto-normalised spellings are listed in `run_warnings`), `status_reason` (`unfilled_fields`,
`unfilled_optional_fields`, `schema_problems`, `assertions_not_built`, ...), `field_status`
(`supported` / `generated` / `needs_review` / `unreported` / `schema_mismatch`), `field_problems` (path-level schema
problems, e.g. `assertions[3].claim: required key missing`), `output_contract_spec` (where the contract was found,
required / optional / type per field), `build_errors`, `field_evidence` (evidence of the assertions an answer was
built from), `nanopublications`, `source_metadata` (values + where each was read), `quality_summary`, `export_roundtrip`,
`exports` (file names), `sources` (file, role, format, SHA-256, parse status, unreadable pages), `unreported`, `conflicts`,
`uncertainty`, `run_status`, `change_log`.

## Method

1. Parse with the layout-aware parser (multi-column reading order, header/footer removal, de-hyphenation, tables,
   equations, references). Sentences are split per paragraph (abbreviation-aware, Chinese punctuation) and keep the
   block they start in: id `p<page>-b<block>-s<k>`.
2. Claim candidates are ranked by cues (claim verbs, numbers, comparison, causality, section) — a reading aid only.
3. The agent writes atomic assertions in `draft.json`; the scripts never invent claims.
4. Evidence resolution: sentence id → exact text; quote → normalised (NFKC, ligatures, dashes, line-end hyphens,
   spaces) substring search in sentences (cited page first), paragraphs, raw page text; fuzzy (≥ 0.85) only as a flagged
   fallback. Line boxes come from the PDF word list inside the cited blocks, so previews box the exact quote.
5. Rules R01–R31 (see SKILL.md) are lexical/structural/arithmetic and deterministic; each finding names the triggering text.
   Numbers are compared as values with their magnitude (`6 billion` = `60 亿` = `6×10^9`; `1.28 million` matches
   `1,281,167` at the printed precision; same digits with another magnitude → R27). For `inferred` records the reasoning is
   re-executed: `a - b = c` steps are recomputed, `a < b` comparisons evaluated, and every operand must occur in an evidence
   item (R28) — numbers written only in the reasoning no longer satisfy R09.
6. Non-PDF papers: paragraphs become blocks `p<page>-b<k>` (page = logical page from form feeds / docx page breaks),
   each with its line range or paragraph index; pipe / docx / html tables become queryable tables.

## Self-evaluation (tests/, bundle root)

47 labelled assertions on 4 arXiv papers (ResNet, Transformer, BatchNorm; Kaplan 2020 held out), 28 with a planted
defect. Dev set (rules tuned on it): 21/21 defects detected, 14/14 clean not failed. Held-out set at code freeze:
6/7 defects detected (missed a compound claim joined by ", and" + a verb outside the list), 5/5 clean not failed,
4/5 strict pass; after the post-freeze fix 7/7 and 5/5 (so the held-out paper is now also a dev paper).
All clean evidence located at quote or table-cell level; metadata title/year/arXiv id/version 4/4, authors
recall/precision 1.0/1.0 after fixes (0.83 precision on the held-out paper before). Exports round-trip, outputs
deterministic. A "quote exists in the PDF" checker alone catches 4/28 of the planted defects. Numbers: `execution/results/metrics*.json`.

v0.3.0 regression suite (`tests/test_v03.py`, 53 checks): magnitude equivalence / mismatch, compound claims
(respectively, several values, Chinese clauses, no false fire on ranges), attribution and cited-scope checks,
limiters, reasoning arithmetic / comparison / operands, image-region binding, figure claims; contract parsing
(wrappers, required vs optional, nested items, integer vs number); end-to-end: `case_id: 0`, optional fields,
schema mismatch → partial, primary vs a larger supplement, corrupt / empty / malformed drafts never clobber
`result.json`, `--merge`, `.md` / `.txt` / `.docx` papers, strict N-Quads with hostile characters.
Label fix found by R28: dev assertion r04 had been labelled clean with reasoning "ResNet-34 B 7.40", but 7.40 is
ResNet-34 C in Table 3; Table 4 prints 5.71. The label (and the reproduce.py v0.2 case) now use 5.71.

## Limits

- Rules are lexical (plus simple arithmetic): they cannot judge meaning. A claim can pass every rule and still misread the paper; a `pass`
  means "consistent with the cited text by these checks". Compound detection uses a verb list plus a ", and" warning.
- English cue lexicons are fuller than the Chinese ones; Chinese text is supported for matching, splitting, magnitudes
  (万 / 亿) and clause conjunctions. Chinese numerals written as characters (六十亿) are not parsed.
- Text papers have no printed pages or boxes: locations are logical page + paragraph block + line range.
- R28 only recomputes reasoning written as `a op b = c` or `a < b`; prose reasoning is checked for operands only.
- Sentences next to equations or inline tables can be merged (parser limitation): quote the exact sub-span.
- Values that live only in display equations are not in `sentences.jsonl`; use `table_query.py`, `{page, quote}`
  (raw page text search) or `find_evidence.py --equation N`.
- Author splitting is heuristic (flagged `needs_review`); correct with `source_overrides`. Years from `pdf_info`
  are file dates. DOI only if printed in the PDF; no network lookup.
- Pages without text layer need OCR (tesseract, if installed) or image reading; image-read evidence is never
  machine-verified (R26) and is listed in `run_warnings` (`image_read_evidence`) for human review.
- No signing, no Trusty URI, no publishing to the nanopub network.

## Maintenance

Version 0.3.1 (2026-11-27). Lower-friction run: SKILL.md cut to a 4-step path (80 lines); np_build auto-normalises predicate / modality / scope / evidence_type spellings and missing ids, quarantines failing records out of the exports instead of marking the whole run `partial`, reports build errors / image-read evidence / unread pages / fields needing review in `run_warnings`, accepts a required field that is `null` with a reasoned `unreported` entry, and turns any unexpected exception into exit 2 with `build_error.json` (result.json untouched).

Version 0.3.0 (2026-11-25). Added magnitude-aware numbers (R09/R27), reasoning recomputation (R28), cited-scope / image-region / figure rules (R29–R31), respectively / multi-value / Chinese compound detection, limiter check, attribution cues, role-based primary/supplement selection, .md/.txt/.docx/.html papers, contract required/optional + nested type validation, `case_id: 0`, draft-safe builds (`build_error.json`, refuse-to-shrink, `result.prev.json`, `--merge`), strict IRI/literal serialisation, unified `PY`/`SKILL_DIR` discovery. Fixed 6 lost `\b` anchors in the author-block parser.
Version 0.2.0: added JSON Schema `output_contract` support, `nquads`/`summary`/`unreported`/`conflicts`/`uncertainty`/`review` view mappings, `inferred` + `reasoning` numeric verification, parenthetical semicolon / adjectival participle `R07` false-fail fixes, Chinese non-negation exclusions, multi-PDF resolution, and `python3` `$SKILL_DIR` guidance. Change vocabulary in `vocab/relations.json` (predicates need `relation_type` and `polarity`); rules live in `np_check.py` (one block per rule id, stable ids). Re-run `python tests/run_eval.py` in the bundle after any change; add a labelled case to `tests/eval/` for every bug found.
Maintainer: the submitting participant (see the attempt page on play.bohrium.com).
