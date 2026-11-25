#!/usr/bin/env python3
"""Rule-based quality checks for nanopublication records (assertion + evidence + provenance).

Every finding has a stable rule id, a severity (fail | warn), the exact strings
that triggered it and a fix hint, so the same input always gives the same report.

Usage (standalone; np_build.py calls it automatically):
    python np_check.py OUT/nanopubs.json [--report OUT/quality_report.json]

Rules (see README for rationale):
  R01 schema_missing_field      fail  required key empty (assertion.text, subject, predicate, object, evidence, provenance)
  R02 predicate_not_in_vocab    fail  predicate not in vocab/relations.json
  R03 no_support_evidence       fail  no evidence item with role=support
  R04 quote_not_found           fail  quote not found verbatim in the PDF text layer
  R05 quote_fuzzy_or_moved      warn  quote only fuzzily found, or found on another page than cited (page corrected)
  R06 evidence_not_minimal      warn  support quote > 2 sentences or > 450 chars
  R07 compound_assertion        fail  assertion text joins >1 claim (';' / 'and' + verb / list of results)
  R08 multi_entity_slot         warn  subject or object lists several entities ('A and B', 'A, B')
  R09 value_not_in_evidence     fail  a number in the assertion/qualifiers is absent from all evidence
  R10 identifier_number_missing warn  number inside a name (top-5, ResNet-152) absent from evidence
  R11 quantitative_without_value fail quantitative predicate but qualifiers.value empty
  R12 hedge_dropped             warn  evidence is hedged (may/suggest/...) but assertion is stated as fact
  R13 hedge_added               warn  assertion marked hedged but evidence has no hedge
  R14 negation_mismatch         fail/warn  polarity of predicate vs negation in evidence
  R15 overgeneralization        warn  universal quantifier (all/always/...) not in evidence
  R16 condition_dropped         warn  evidence names a dataset/setting/condition the assertion leaves out
  R17 causal_without_cue        warn  causal predicate, evidence has no causal wording
  R18 comparison_without_cue    warn  comparative predicate, evidence has no comparison wording
  R19 subject_not_in_evidence   warn  no subject word (nor we/our/it) in the evidence
  R20 cited_claim_as_own        warn  scope=this_work but the evidence cites other work / sits in related work
  R21 cited_reference_missing   warn  scope=cited_work without a resolvable cited_reference
  R22 inferred_without_reasoning fail evidence_type=inferred without 'reasoning'
  R23 low_lexical_support       warn  evidence_type=direct but few assertion words occur in the evidence
  R24 duplicate_assertion       warn  same subject|predicate|object|value as another record
  R25 provenance_incomplete     fail/warn  missing title or pdf hash (fail) / authors, year, identifier, version (warn)
  R26 image_evidence_unverified warn  evidence read from a page image (not machine-checkable)
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import np_common as C  # noqa: E402

STOP = set("""a an the of in on for to and or with by from as at is are was were be been being this that these those
it its we our they their which who whom than then into over under via per using use used based both each such
also can may more most less very much new two one three set sets result results show shows shown paper work""".split())
VERB_RE = r"(?:is|are|was|were|has|have|had|can|could|achieves?|achieved|improves?|improved|reduces?|reduced|shows?|showed|" \
          r"outperforms?|outperformed|exhibits?|converges?|gains?|increases?|decreases?|leads?|causes?|enables?|" \
          r"yields?|produces?|obtains?|reaches?|requires?|performs?|surpass(?:es)?|remains?|becomes?|does|did|beats?|matches|exceeds?|" \
          r"establish(?:es)?|allows?|lowers?|raises?|helps?|hurts?|provides?|generalizes?|needs?|takes?|trains?|learns?|" \
          r"will|should|might|may|must)"
COMPOUND_RES = [
    (re.compile(r";"), "semicolon joins clauses"),
    (re.compile(r",?\s+(?:and|but|while|whereas)\s+(?:(?:it|they|this|these|the\s+\w+|\w+)\s+)?(?:also\s+)?" + VERB_RE + r"\b", re.I), "conjunction + second verb"),
    (re.compile(r"(?:，|,)\s*(?:并且|而且|同时|但|而)"), "Chinese clause conjunction"),
    (re.compile(r"\b(?:1\)|\(i\)|first,).*\b(?:2\)|\(ii\)|second,)", re.I), "enumerated findings"),
]
COND_RE = re.compile(r"\b(on|in|under|when|for|with|at|using|across)\s+(?:the\s+|a\s+|an\s+)?((?:[A-Z][\w\-./]*|\d[\w\-./]*)(?:\s+(?:[A-Z0-9][\w\-./]*))*)")
PRONOUNS = {"we", "our", "it", "its", "they", "their", "this", "these", "them", "which", "ours", "us"}
SEV = {"fail": 2, "warn": 1}


def words(s):
    return [w for w in re.findall(r"[a-z一-鿿][a-z0-9一-鿿\-]*", C.norm(s)) if w not in STOP and len(w) > 2]


def stem(w):
    return w[:5]


def issue(rule, sev, msg, trigger=None, hint=None):
    d = {"rule": rule, "severity": sev, "message": msg}
    if trigger is not None:
        d["trigger"] = trigger
    if hint:
        d["fix_hint"] = hint
    return d


def slot_label(x):
    if isinstance(x, dict):
        return x.get("label") or ""
    return x or ""


def value_numbers(text):
    """Numbers that are values (not parts of names like top-5, CIFAR-10, 34-layer)."""
    t = C.norm(text, lower=False)
    t = C.CITE_RE.sub(" ", t)
    vals, ids = [], []
    # powers of ten: '10^6', '10⁶', '10**6' -> one token '10^6' (the PDF text layer often flattens it to '106')
    sup = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
    for m in re.finditer(r"10\s*(?:\^|\*\*)\s*\{?(-?\d+)\}?|10([⁻]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+)", t):
        vals.append("10^" + (m.group(1) or m.group(2).translate(sup)))
    t = re.sub(r"10\s*(?:\^|\*\*)\s*\{?-?\d+\}?|10[⁻]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+", " ", t)
    for m in re.finditer(r"(?<![\w.])([-+]?\d+(?:[.,]\d+)*)(\s?%|\s?[x×])?", t):
        a, b = m.span()
        before = t[max(0, a - 1):a]
        after = t[b:b + 1]
        raw = m.group(0).replace(" ", "")
        if before == "-" and a >= 2 and t[a - 2].isalnum():
            ids.append(raw)
        elif after == "-" and not m.group(2):
            ids.append(raw)           # 34-layer, 18-layer
        elif re.fullmatch(r"(19|20)\d\d", m.group(1)) and not m.group(2):
            ids.append(raw)           # years treated as identifiers
        else:
            vals.append(raw)
    for m in re.finditer(r"[A-Za-z]+-(\d+(?:\.\d+)?)", t):
        ids.append(m.group(1))
    return vals, ids


def num_in(num, ev_cores, ev_text_norm):
    if num.startswith("10^"):
        e = num[3:]
        flat = ev_text_norm.replace(" ", "")
        return any(x in flat for x in ("10^" + e, "10" + e, "10**" + e, "e" + e, "E" + e, "1e" + e))
    core = C.num_core(num)
    if core in ev_cores:
        return True
    return re.search(r"(?<![\d.])" + re.escape(num.replace(",", "").rstrip("%x×")) + r"(?![\d])", ev_text_norm.replace(",", "")) is not None


def check_record(np_rec, vocab, all_keys=None, references=None):
    out = []
    a = np_rec.get("assertion", {})
    ev = np_rec.get("evidence", [])
    prov = np_rec.get("provenance", {})
    text = a.get("text") or ""
    subj, obj = slot_label(a.get("subject")), slot_label(a.get("object"))
    pred = (a.get("predicate") or {}).get("id") if isinstance(a.get("predicate"), dict) else a.get("predicate")
    quals = a.get("qualifiers") or {}
    # R01
    for k, v in (("assertion.text", text), ("assertion.subject", subj), ("assertion.predicate", pred), ("assertion.object", obj), ("evidence", ev)):
        if not v:
            out.append(issue("R01", "fail", f"{k} is empty", k, "fill it; an assertion needs subject, predicate, object and evidence"))
    pinfo = (vocab.get("predicates") or {}).get(pred or "")
    if pred and not pinfo:
        out.append(issue("R02", "fail", f"predicate '{pred}' is not in the controlled vocabulary", pred,
                         "choose one of vocab/relations.json predicates"))
    rtype = (pinfo or {}).get("relation_type")
    polarity = (pinfo or {}).get("polarity", "positive")
    support = [e for e in ev if e.get("role", "support") == "support"]
    if ev and not support:
        out.append(issue("R03", "fail", "no evidence item with role=support (only context)", None, "mark the quote that states the claim as support"))
    for e in ev:
        m = e.get("match")
        if m == "not_found" and e.get("kind") == "table_cell":
            out.append(issue("R04", "fail", f"table cell evidence does not match: {e.get('error')}", {"table": e.get("table"), "row": e.get("row"), "column": e.get("column")},
                             "query the cell with table_query.py and use the printed value"))
        elif m == "not_found":
            out.append(issue("R04", "fail", f"quote not found in the PDF text layer (cited page {e.get('cited_page')})", e.get("quote", "")[:160],
                             "copy the quote verbatim from sentences.jsonl, or cite the sentence id"))
        elif m in ("fuzzy",):
            out.append(issue("R05", "warn", f"quote matched only approximately (similarity {e.get('similarity')})", e.get("quote", "")[:160], "use the exact sentence text"))
        if e.get("page_corrected_from") is not None:
            out.append(issue("R05", "warn", f"quote cited on page {e['page_corrected_from']} but found on page {e.get('page')}; page corrected", e.get("quote", "")[:80]))
        if e.get("kind") == "image_region":
            out.append(issue("R26", "warn", "evidence read from a page image; reading not machine-checked", e.get("transcription", "")[:80], "keep the crop for human review"))
    for e in support:
        q = e.get("quote") or ""
        if e.get("kind") == "text" and (len(q) > 450 or len(C.split_sentences(q)) > 2):
            out.append(issue("R06", "warn", f"support quote is long ({len(q)} chars, {len(C.split_sentences(q))} sentences)", q[:80] + "...",
                             "cite the single sentence (or clause) that states the claim; put the rest as role=context"))
    # R07/R08 granularity
    text_no_paren = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", C.CITE_RE.sub(" ", text))
    adj_part = re.compile(r"^\s*(?:and|or)\s+(?:improved|reduced|increased|decreased|achieved|obtained)\b", re.I)
    for rx, why in COMPOUND_RES:
        hay = text_no_paren if why == "semicolon joins clauses" else text
        mm = rx.search(hay)
        if mm:
            if why == "conjunction + second verb" and adj_part.match(mm.group(0)):
                tail = hay[mm.end():].lstrip()
                if tail and not re.match(r"(?:by|from|to|the|in|on|under|than|that|which)\b", tail, re.I):
                    continue
            out.append(issue("R07", "fail", f"assertion looks compound ({why})", mm.group(0),
                             "split into one assertion per claim, each with its own evidence"))
            break
    else:
        mm = re.search(r",\s+(?:and|but)\s+[\w\-]+(?:\s+[\w\-]+){2,}", text)
        if mm and len(text.split()) > 12:
            out.append(issue("R07", "warn", "possible compound assertion (', and' starts a second clause?)", mm.group(0),
                             "if the part after ', and' is a separate claim, split it"))
    for slot, val in (("subject", subj), ("object", obj)):
        if re.search(r"\b(and|or)\b|、|和|及|以及|(?<=[A-Za-z])/(?=[A-Za-z])", val) and not re.search(r"\bbetween\b|\bboth\b|trade-off|input/output|signal-to-noise", val, re.I):
            out.append(issue("R08", "warn", f"{slot} may contain several entities", val,
                             "if the claim holds for each entity separately, make one assertion per entity"))
    # numbers
    ev_text = " ".join((e.get("quote") or "") + " " + (e.get("transcription") or "") + " " + (e.get("table_context") or "") for e in ev)
    if (np_rec.get("evidence_type") == "inferred") and (np_rec.get("reasoning") or "").strip():
        rsn = np_rec["reasoning"]
        if C.numbers(ev_text):
            ev_text = ev_text + " " + rsn
    ev_norm = C.norm(ev_text, lower=False)
    ev_cores = {C.num_core(n) for n in C.numbers(ev_text)}
    qual_text = " ".join(str(v) for k, v in quals.items() if k not in ("condition", "note") and v not in (None, ""))
    vals, ids = value_numbers(text + " " + qual_text)
    miss = sorted({n for n in vals if not num_in(n, ev_cores, ev_norm)})
    if miss:
        out.append(issue("R09", "fail", "number(s) in the assertion not found in any evidence item", miss,
                         "fix the number, or add the evidence (sentence/table cell) that states it"))
    miss_id = sorted({n for n in ids if not num_in(n, ev_cores, ev_norm)} - set(miss))
    if miss_id:
        out.append(issue("R10", "warn", "number inside a name not found in evidence (may come from elsewhere in the paper)", miss_id,
                         "add a role=context evidence item that defines it, or drop it"))
    if rtype == "quantitative" and quals.get("value") in (None, ""):
        out.append(issue("R11", "fail", "quantitative predicate without qualifiers.value", pred, "give value (+unit, metric)"))
    # modality / polarity
    sup_text = " ".join((e.get("quote") or "") + " " + (e.get("transcription") or "") for e in support)
    ev_hedges = C.find_words(sup_text, C.HEDGE)
    as_hedges = C.find_words(text, C.HEDGE)
    modality = a.get("modality") or "asserted"
    if ev_hedges and not as_hedges and modality == "asserted":
        out.append(issue("R12", "warn", "evidence is hedged but the assertion is stated as fact", ev_hedges,
                         "set modality=hedged and keep the hedge word in the assertion text"))
    if modality == "hedged" and not ev_hedges:
        out.append(issue("R13", "warn", "assertion marked hedged but the evidence has no hedge word", None))
    ev_neg = [w for w in C.find_words(sup_text, C.NEGATION) if w != "no" or re.search(r"\bno\s+(?!\.)\w", C.norm(sup_text))]
    as_neg = C.find_words(text, C.NEGATION)
    if polarity == "negative" and not ev_neg:
        out.append(issue("R14", "fail", "negative predicate but no negation in the support evidence", pred, "check polarity"))
    elif polarity == "positive" and ev_neg and not as_neg and modality != "negated":
        out.append(issue("R14", "warn", "support evidence contains negation not reflected in the assertion", ev_neg,
                         "verify the negation does not apply to this claim; otherwise use a negative predicate"))
    elif polarity == "positive" and as_neg and not ev_neg:
        out.append(issue("R14", "fail", "assertion is negated but the evidence is not", as_neg))
    uni = [w for w in C.find_words(text, C.UNIVERSAL) if w not in C.find_words(ev_text, C.UNIVERSAL)]
    if uni:
        out.append(issue("R15", "warn", "universal wording not supported by the evidence", uni, "narrow the claim to what the evidence states"))
    a_scope_text = C.norm(text + " " + subj + " " + obj + " " + json.dumps(quals, ensure_ascii=False))
    dropped = []
    for m in COND_RE.finditer(C.norm(sup_text, lower=False)):
        name = m.group(2).strip(" .,;")
        if len(name) < 3 or re.fullmatch(r"(Table|Fig|Figure|Sec|Section|Eq|Appendix)\.?\s*\S*", name, re.I) or name.lower() in ("the", "this"):
            continue
        key = C.norm(name.split()[0]).strip(".,;:")
        if key and key not in a_scope_text:
            dropped.append(m.group(0).strip())
    if dropped:
        out.append(issue("R16", "warn", "evidence names a setting/dataset/condition the assertion does not carry", sorted(set(dropped))[:6],
                         "add it to qualifiers (dataset / condition) or to the assertion text"))
    if rtype == "causal" and not C.find_words(sup_text, C.CAUSAL):
        out.append(issue("R17", "warn", "causal predicate but no causal wording in the evidence", pred,
                         "use associated_with / correlates_with unless the paper claims causation"))
    if rtype == "comparative" and (np_rec.get("evidence_type") != "inferred") and not C.find_words(ev_text, C.COMPARE):
        out.append(issue("R18", "warn", "comparative predicate but no comparison wording in the evidence", pred))
    # subject
    sw = [stem(w) for w in words(subj)]
    ew = {stem(w) for w in words(ev_text)}
    ev_tokens = set(re.findall(r"[a-z]+", C.norm(ev_text)))
    if sw and not (set(sw) & ew) and not (PRONOUNS & ev_tokens):
        out.append(issue("R19", "warn", "no word of the subject occurs in the evidence", subj, "check the subject is the entity the quote talks about"))
    # scope
    scope = a.get("scope") or "this_work"
    sup_cites = [c.get("marker") if isinstance(c, dict) else c for e in support for c in (e.get("citations") or [])]
    related = any(re.search(r"related work|background|previous work|prior work|相关工作", " ".join(e.get("section_path") or []), re.I) for e in support)
    if scope == "this_work" and (sup_cites or related):
        out.append(issue("R20", "warn", "scope=this_work but the support evidence cites other work" + (" / is in a related-work section" if related else ""),
                         sup_cites[:5] or "related-work section",
                         "if the finding belongs to the cited paper set scope=cited_work and cited_reference"))
    if scope == "cited_work" and not (a.get("cited_reference_resolved") or a.get("cited_reference")):
        out.append(issue("R21", "warn", "scope=cited_work but no cited_reference given", None, "give the reference marker, e.g. [41]"))
    elif scope == "cited_work" and a.get("cited_reference") and not a.get("cited_reference_resolved"):
        out.append(issue("R21", "warn", "cited_reference could not be resolved to the reference list", a.get("cited_reference")))
    et = np_rec.get("evidence_type") or "direct"
    if et == "inferred" and not (np_rec.get("reasoning") or "").strip():
        out.append(issue("R22", "fail", "evidence_type=inferred without a reasoning statement", None, "write the reasoning step"))
    if et == "direct" and support:
        aw = {stem(w) for w in words(text)}
        if aw:
            frac = len(aw & {stem(w) for w in words(ev_text)}) / len(aw)
            if frac < 0.3:
                out.append(issue("R23", "warn", f"only {frac:.0%} of the assertion's content words occur in the evidence", None,
                                 "if the claim is your inference set evidence_type=inferred + reasoning"))
    key = np_rec.get("dedup_key")
    if all_keys is not None and key and all_keys.get(key, 0) > 1:
        out.append(issue("R24", "warn", "duplicate of another assertion (same subject|predicate|object|value)", key, "merge them"))
    # provenance
    def pv(k):
        return ((prov.get("source") or {}).get(k) or {}).get("value") if isinstance((prov.get("source") or {}).get(k), dict) else (prov.get("source") or {}).get(k)
    unrep = set(prov.get("unreported_fields") or [])
    for k in ("title", "pdf_sha256"):
        if not pv(k):
            sev = "warn" if (k == "title" and k in unrep) else "fail"
            out.append(issue("R25", sev, f"provenance: {k} missing", k))
    for k in ("authors", "year"):
        if not pv(k) and k not in unrep:
            out.append(issue("R25", "warn", f"provenance: {k} missing and not marked unreported", k))
    if not (pv("doi") or pv("arxiv_id")) and "identifier" not in unrep:
        out.append(issue("R25", "warn", "provenance: no DOI / arXiv id (only the file hash identifies the source)", None))
    if pv("arxiv_id") and not pv("version") and "version" not in unrep:
        out.append(issue("R25", "warn", "provenance: arXiv id without version", None))
    return out


def verdict(issues):
    if any(i["severity"] == "fail" for i in issues):
        return "fail"
    if issues:
        return "warn"
    return "pass"


def confidence(np_rec, issues):
    """Extraction confidence (how well the record is backed by the cited text), not truth of the claim."""
    base = {"direct": 0.9, "cited": 0.75, "inferred": 0.6}.get(np_rec.get("evidence_type") or "direct", 0.6)
    basis = [f"evidence_type={np_rec.get('evidence_type') or 'direct'} -> {base}"]
    matches = [e.get("match") for e in np_rec.get("evidence", []) if e.get("role", "support") == "support"]
    f = 1.0
    if any(m == "fuzzy" for m in matches):
        f = min(f, 0.85)
    if any(m == "image_read" for m in matches):
        f = min(f, 0.7)
    if matches and all(m == "not_found" for m in matches):
        f = 0.0
    if f != 1.0:
        basis.append(f"evidence match factor x{f}")
    score = base * f
    nw = sum(1 for i in issues if i["severity"] == "warn")
    if nw:
        score -= 0.1 * nw
        basis.append(f"-0.1 x {nw} warning(s)")
    if any(i["severity"] == "fail" for i in issues):
        score = min(score, 0.2)
        basis.append("capped at 0.2: failing rule(s)")
    score = max(0.0, min(1.0, round(score, 2)))
    level = "high" if score >= 0.75 else "medium" if score >= 0.5 else "low"
    return {"score": score, "level": level, "basis": basis,
            "formula": "base(direct .9|cited .75|inferred .6) x match(exact 1|fuzzy .85|image .7|none 0) - .1/warn; fail -> <= .2"}


def check_all(records, vocab):
    keys = {}
    for r in records:
        if r.get("dedup_key"):
            keys[r["dedup_key"]] = keys.get(r["dedup_key"], 0) + 1
    report = []
    for r in records:
        iss = check_record(r, vocab, keys)
        iss.sort(key=lambda i: (-SEV[i["severity"]], i["rule"]))
        r["quality"] = {"verdict": verdict(iss), "issues": iss}
        r["confidence"] = confidence(r, iss)
        report.append({"np_id": r.get("np_id"), "assertion_id": (r.get("assertion") or {}).get("id"),
                       "verdict": r["quality"]["verdict"], "issues": [f"{i['rule']}:{i['severity']}" for i in iss]})
    summary = {"n": len(records),
               "pass": sum(1 for r in records if r["quality"]["verdict"] == "pass"),
               "warn": sum(1 for r in records if r["quality"]["verdict"] == "warn"),
               "fail": sum(1 for r in records if r["quality"]["verdict"] == "fail"),
               "rule_counts": {}}
    for r in records:
        for i in r["quality"]["issues"]:
            k = f"{i['rule']}:{i['severity']}"
            summary["rule_counts"][k] = summary["rule_counts"].get(k, 0) + 1
    return {"summary": summary, "records": report, "checker": C.TOOL}


def main():
    C.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nanopubs")
    ap.add_argument("--report", default=None)
    a = ap.parse_args()
    data = C.load_json(a.nanopubs)
    recs = data.get("nanopublications", data) if isinstance(data, dict) else data
    rep = check_all(recs, C.load_vocab())
    if a.report:
        C.dump_json(rep, a.report)
    print(json.dumps(rep, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
