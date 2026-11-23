# nanopub-evidence-builder 0.1.0 — field dictionary, method, limits

Agent Skill for **阅读14｜Nanopublication**: turn the conclusions of a scientific PDF into
nanopublications — one atomic, checkable assertion per record, each bound to the smallest verbatim
evidence in the PDF and to source provenance — and export them as JSON, TriG, N-Quads and JSON-LD.
Sub-problem chosen: **assertion splitting + evidence binding + rule-based quality checks**, with source
metadata and export as supporting parts. Not a nanopub server client: records are unsigned drafts.

## Files

| path | role |
|---|---|
| `SKILL.md` | agent instructions (workflow, draft format, rules, failure handling) |
| `scripts/np_init.py` | parse case PDFs, sentence index with ids, source metadata, claim candidates, result.json skeleton, draft template |
| `scripts/np_find.py` | search sentences (BM25-style, `--page`, `--section`, `--regex`, `--id --context`) |
| `scripts/np_build.py` | resolve evidence → records → quality check → TriG / N-Quads / JSON-LD → previews → result.json |
| `scripts/np_check.py` | the 26 quality rules + extraction confidence (also runs standalone on `nanopubs.json`) |
| `scripts/np_common.py` | normalisation, sentence splitter, cue lexicons, metadata extraction |
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
| `provenance.source` | `title`, `authors`, `year`, `venue`, `doi`, `arxiv_id`, `version`, `arxiv_date`, `pdf_sha256`, `page_count`, `file_name` — each `{value, source{kind, page, block_id, quote}, confidence, needs_review?, note?}` |
| `provenance.source_iri` | DOI URL, else arXiv abs URL with version, else `urn:sha256:<pdf hash>` |
| `pubinfo` | `created`, `creator`, `status` (unsigned draft) |
| `quality` | `verdict` (pass / warn / fail) + `issues[]` (`rule`, `severity`, `message`, `trigger`, `fix_hint`) |
| `confidence` | extraction confidence `score`, `level`, `basis`, `formula` |
| `content_hash` | SHA-256 over assertion + evidence (not a Trusty URI) |

RDF layout per nanopub: `Head` (np:hasAssertion / hasProvenance / hasPublicationInfo), `assertion` graph
(plain triple `subject predicate object` only for unhedged own findings, plus an `npeb:AtomicClaim` node carrying
text, slots, modality, scope and qualifiers), `provenance` graph (`prov:wasDerivedFrom` source, `prov:wasGeneratedBy`
extraction activity, one `npeb:EvidenceSnippet` per evidence item with `prov:value` quote, page, block, sentence, bbox,
plus source title/creators/issued/identifiers/version/sha256), `pubinfo` graph. `npeb:` = `http://purl.org/nanopub/temp/npeb-terms#`
(local, not dereferenceable). Export check: N-Quads lines parsed, JSON-LD statements counted, TriG parsed with rdflib
when installed; counts must agree (`export_roundtrip.ok`).

## result.json

`case_id`, every `output_contract` field (filled as mapped in `draft.fields`), `status` (`success` only if all fields
filled, no failing record, export ok, no image-only evidence), `status_reason`, `field_status`
(`supported` / `generated` / `needs_review` / `unreported`), `field_evidence` (evidence of the assertions an answer was
built from), `nanopublications`, `source_metadata` (values + where each was read), `quality_summary`, `export_roundtrip`,
`exports` (file names), `sources` (PDF path, SHA-256, parse status, unreadable pages), `unreported`, `conflicts`,
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
5. Rules R01–R26 (see SKILL.md) are lexical/structural and deterministic; each finding names the triggering text.

## Self-evaluation (tests/, bundle root)

47 labelled assertions on 4 arXiv papers (ResNet, Transformer, BatchNorm; Kaplan 2020 held out), 28 with a planted
defect. Dev set (rules tuned on it): 21/21 defects detected, 14/14 clean not failed. Held-out set at code freeze:
6/7 defects detected (missed a compound claim joined by ", and" + a verb outside the list), 5/5 clean not failed,
4/5 strict pass; after the post-freeze fix 7/7 and 5/5 (so the held-out paper is now also a dev paper).
All clean evidence located at quote or table-cell level; metadata title/year/arXiv id/version 4/4, authors
recall/precision 1.0/1.0 after fixes (0.83 precision on the held-out paper before). Exports round-trip, outputs
deterministic. A "quote exists in the PDF" checker alone catches 4/28 of the planted defects. Numbers: `execution/results/metrics*.json`.

## Limits

- Rules are lexical: they cannot judge meaning. A claim can pass every rule and still misread the paper; a `pass`
  means "consistent with the cited text by these checks". Compound detection uses a verb list plus a ", and" warning.
- English cue lexicons are fuller than the Chinese ones; Chinese text is supported for matching and splitting.
- Sentences next to equations or inline tables can be merged (parser limitation): quote the exact sub-span.
- Values that live only in display equations are not in `sentences.jsonl`; use `table_query.py`, `{page, quote}`
  (raw page text search) or `find_evidence.py --equation N`.
- Author splitting is heuristic (flagged `needs_review`); correct with `source_overrides`. Years from `pdf_info`
  are file dates. DOI only if printed in the PDF; no network lookup.
- Pages without text layer need OCR (tesseract, if installed) or image reading; image-read evidence is never
  machine-verified (R26) and keeps the result `partial`.
- No signing, no Trusty URI, no publishing to the nanopub network.

## Maintenance

Version 0.1.0 (2026-11-23). Change vocabulary in `vocab/relations.json` (predicates need `relation_type` and
`polarity`); rules live in `np_check.py` (one block per rule id, stable ids). Re-run `python tests/run_eval.py`
in the bundle after any change; add a labelled case to `tests/eval/` for every bug found.
Maintainer: the submitting participant (see the attempt page on play.bohrium.com).
