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
  R27 magnitude_mismatch        fail  '6 billion' vs evidence '6 million' (same digits, other scale); '60 亿' == '6 billion' passes R09
  R28 reasoning_unsupported     fail  inferred reasoning uses a number absent from the evidence, an arithmetic step that does
                                      not compute (7.40 - 4.49 = 3.91), or a false comparison (7.40 < 4.49)
  R29 cited_scope_suspicious    warn  scope=cited_work but the support evidence is first-person and cites nothing
  R30 image_region_unbound      fail/warn  image_region without page+bbox (fail) / without crop, provenance hash or transcription (warn)
  R31 figure_claim_unbound      warn  the assertion names a figure (Fig. 3 / 图3) but no evidence comes from that figure
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
    (re.compile(r"\b(?:while|whereas)\s+(?:also\s+)?(?:improving|reducing|increasing|decreasing|lowering|raising|achieving|gaining|maintaining|preserving|outperforming|hurting|degrading)\b", re.I), "'while' + second action"),
    (re.compile(r"(?:，|,)\s*(?:并且|而且|同时|但|而|且|并|还|也)"), "Chinese clause conjunction"),
    (re.compile(r"并且|而且|同时还|此外|另外"), "Chinese clause conjunction"),
    (re.compile(r"\brespectively\b|分别"), "'respectively' pairs several results"),
    (re.compile(r"\bas well as\s+(?:\w+\s+){0,2}" + VERB_RE + r"\b", re.I), "'as well as' + second verb"),
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


_LOC_NUM = re.compile(r"(?:table|tab\.|fig(?:ure)?\.?|eq\.?|equation|sec(?:tion)?\.?|row|column|col\.|page|p\.|appendix|表|图|式)\s*\(?[A-Za-z]?\d+", re.I)
_ARITH = re.compile(r"((?:[(\s]*[-+]?\d+(?:\.\d+)?\s*%?[)\s]*[-+*/x×÷−][(\s]*)+[-+]?\d+(?:\.\d+)?\s*%?[)\s]*)=\s*([-+]?\d+(?:\.\d+)?)\s*(%?)")
_CMP = re.compile(r"<=|>=|≤|≥|<|>")


def _value_tokens(t):
    """(start, end, float, raw) of value numbers in t, skipping locators (Table 4) and name parts (ResNet-34, top-5)."""
    masked = _LOC_NUM.sub(lambda m: " " * len(m.group(0)), t)
    toks = []
    for m in re.finditer(r"(?<![\w.\-])([-+]?\d+(?:\.\d+)?)(?![\w\-])", masked):
        toks.append((m.start(), m.end(), float(m.group(1)), m.group(1)))
    for m in re.finditer(r"(?<=[A-Za-z])-(\d+(?:\.\d+)?)", masked):  # 'ResNet-34' is a name, not a value
        toks = [t for t in toks if not (t[0] <= m.start(1) < t[1])]
    return toks


def _safe_eval(expr):
    import ast
    import operator as op
    ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv, ast.USub: op.neg, ast.UAdd: op.pos}

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return float(n.value)
        if isinstance(n, ast.BinOp) and type(n.op) in ops:
            return ops[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in ops:
            return ops[type(n.op)](ev(n.operand))
        raise ValueError("unsupported")
    e = expr.replace("×", "*").replace("x", "*").replace("÷", "/").replace("−", "-").replace("%", "")
    if len(e) > 200:
        raise ValueError("too long")
    return ev(ast.parse(e.strip(), mode="eval"))


def check_reasoning(reasoning, ev_text, ev=None):
    """R28: every operand of an inferred record's reasoning must be in the evidence, every printed arithmetic
    step must compute, every printed comparison must hold."""
    out = []
    r = C.norm(reasoning, lower=False)
    ev_cores = {C.num_core(n) for n in C.numbers(ev_text)}
    ev_norm = C.norm(ev_text, lower=False)
    results, bad_steps = set(), []
    for m in _ARITH.finditer(r):
        lhs, rhs, pct = m.group(1), m.group(2), m.group(3)
        try:
            got = _safe_eval(lhs)
        except Exception:
            continue
        dec = len(rhs.split(".")[1]) if "." in rhs else 0
        want = float(rhs)
        tol = 0.5 * 10 ** -dec + 1e-9
        ok = abs(got - want) <= tol or (pct and abs(got * 100 - want) <= tol)
        if ok:
            results.add(C.num_core(rhs))
        else:
            bad_steps.append(f"{lhs.strip()} = {rhs}{pct} (computes to {round(got, 6)})")
    if bad_steps:
        out.append(issue("R28", "fail", "an arithmetic step in the reasoning does not compute", bad_steps, "redo the calculation"))
    toks = _value_tokens(r)
    for m in _CMP.finditer(r):
        left = [t for t in toks if t[1] <= m.start() and not _CMP.search(r[t[1]:m.start()])]
        right = [t for t in toks if t[0] >= m.end() and not _CMP.search(r[m.end():t[0]])]
        if not left or not right:
            continue
        a, b, sym = left[-1][2], right[0][2], m.group(0)
        holds = {"<": a < b, ">": a > b, "<=": a <= b, "≤": a <= b, ">=": a >= b, "≥": a >= b}[sym]
        if not holds:
            out.append(issue("R28", "fail", "a comparison in the reasoning is false", f"{left[-1][3]} {sym} {right[0][3]}",
                             "check the direction of the comparison and the predicate"))
    missing = sorted({t[3] for t in toks if C.num_core(t[3]) not in results and not num_in(t[3], ev_cores, ev_norm)})
    if missing:
        out.append(issue("R28", "fail", "the reasoning uses number(s) that are in no evidence item", missing,
                         "add the table cell / sentence that holds each operand, or fix the number"))
    if ev:
        tcells = [e for e in ev if e.get("kind") == "table_cell" and e.get("match") == "table_cell"]
        if len(tcells) >= 2:
            tables = {C.norm(e.get("table_id") or e.get("table") or "") for e in tcells if (e.get("table_id") or e.get("table"))}
            cols = {C.norm(e.get("column") or "") for e in tcells if e.get("column")}
            if len(tables) > 1 and ("-" in r or _CMP.search(r)):
                out.append(issue("R28", "fail", "inferred comparison/difference mixes cells from different tables",
                                 sorted(tables), "compare cells from the same table and metric"))
            elif len(cols) > 1 and ("-" in r or _CMP.search(r)):
                out.append(issue("R28", "fail", "inferred comparison/difference mixes cells from different columns",
                                 sorted(cols), "compare cells from the same metric/column"))
    return out


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
    if not any(i["rule"] == "R07" and i["severity"] == "fail" for i in out):
        mv = re.search(r"(\d+(?:\.\d+)?\s*%?)\s+(?:[A-Za-z\-]+\s+){0,4}(?:and|,)\s+(?:(?:a|an|the)\s+)?(\d+(?:\.\d+)?\s*%?)(?!\s*(?:-|layer|times))", text_no_paren)
        if mv and not re.search(r"\bbetween\b|\bfrom\s+\d", text_no_paren[:mv.end()], re.I) \
                and C.num_core(mv.group(1)) != C.num_core(mv.group(2)):
            out.append(issue("R07", "fail", "assertion reports several results in one record", mv.group(0),
                             "one assertion per value: give each its own qualifiers.value and evidence"))
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
    if quals.get("value") not in (None, "") and quals.get("unit") not in (None, ""):
        qual_text += f" {quals['value']} {quals['unit']}"
    vals, ids = value_numbers(text + " " + qual_text)
    raw_ev = " ".join((e.get("quote") or "") + " " + (e.get("transcription") or "") + " " + (e.get("table_context") or "") for e in ev)
    ev_mag = [m[2] for m in C.magnitudes(raw_ev)]
    ev_mag_all = ev_mag + C.plain_values(raw_ev)
    a_mag = C.magnitudes(text + " " + qual_text)
    by_scale = {mant for (_, mant, v) in a_mag if C.same_magnitude(mant, v, ev_mag_all)}
    scale_ok = {C.num_core(x) for x in by_scale}
    miss = sorted({n for n in vals if not num_in(n, ev_cores, ev_norm) and C.num_core(n) not in scale_ok})
    if miss:
        out.append(issue("R09", "fail", "number(s) in the assertion not found in any evidence item", miss,
                         "fix the number, or add the evidence (sentence/table cell) that states it"))
    wrong_scale = sorted({raw for (raw, mant, v) in a_mag
                          if mant not in by_scale and num_in(mant, ev_cores, ev_norm) and ev_mag})
    if wrong_scale:
        out.append(issue("R27", "fail", "number has another magnitude than in the evidence (thousand / million / billion / 万 / 亿 / 10^n)",
                         wrong_scale, "copy the magnitude as printed, or convert exactly (6 billion = 60 亿 = 6x10^9)"))
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
    lim_ev = C.find_words(sup_text, C.LIMITERS)
    lim_as = set(C.find_words(text + " " + json.dumps(quals, ensure_ascii=False), C.LIMITERS))
    dropped += [f"limiter '{w}'" for w in lim_ev if w not in lim_as]
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
    attrib = [m.group(0) for e in support for m in [C.ATTRIBUTION_RE.search(e.get("quote") or "")]
              if m and not C.FIRST_PERSON_RE.search(e.get("quote") or "")]
    if scope == "this_work" and (sup_cites or related or attrib):
        out.append(issue("R20", "warn", "scope=this_work but the support evidence cites other work" + (" / is in a related-work section" if related else "")
                         + (" / attributes the finding to others" if attrib and not sup_cites else ""),
                         sup_cites[:5] or attrib[:3] or "related-work section",
                         "if the finding belongs to the cited paper set scope=cited_work and cited_reference"))
    if scope == "cited_work" and not (a.get("cited_reference_resolved") or a.get("cited_reference")):
        out.append(issue("R21", "warn", "scope=cited_work but no cited_reference given", None, "give the reference marker, e.g. [41]"))
    elif scope == "cited_work" and a.get("cited_reference") and not a.get("cited_reference_resolved"):
        out.append(issue("R21", "warn", "cited_reference could not be resolved to the reference list", a.get("cited_reference")))
    text_sup = [e for e in support if e.get("kind") == "text"]
    if scope == "cited_work" and text_sup and not sup_cites and not any(C.ATTRIBUTION_RE.search(e.get("quote") or "") for e in text_sup) \
            and all(C.FIRST_PERSON_RE.search(e.get("quote") or "") for e in text_sup):
        out.append(issue("R29", "warn", "scope=cited_work but the support evidence reads as this paper's own result (first person, no citation)",
                         None, "use scope=this_work unless the quote reports another paper's finding"))
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
    if et == "inferred" and (np_rec.get("reasoning") or "").strip():
        out += check_reasoning(np_rec["reasoning"], raw_ev, ev)
    # same-value statistical metric swap (e.g. HR 1.5 vs OR 1.5)
    _METRIC_ABBR = re.compile(r"\b(HR|OR|RR|AUC|F1|BLEU|RMSE|MAE|MSE|R2)\b")
    a_metrics = set(_METRIC_ABBR.findall(text + " " + str(quals.get("metric") or "")))
    e_metrics = set(_METRIC_ABBR.findall(raw_ev))
    if a_metrics and e_metrics and not (a_metrics & e_metrics):
        out.append(issue("R09", "fail", f"metric mismatch between assertion ({sorted(a_metrics)}) and evidence ({sorted(e_metrics)})",
                         {"assertion": sorted(a_metrics), "evidence": sorted(e_metrics)},
                         "use the exact metric stated in the evidence (do not swap HR/OR/RR/etc.)"))
    for e in ev:
        if e.get("kind") != "image_region":
            continue
        if not e.get("page") or not e.get("bbox"):
            out.append(issue("R30", "fail", "image_region evidence without page and bbox: nobody can find the region again",
                             {"page": e.get("page"), "bbox": e.get("bbox")}, "crop it with crop_region.py and pass page + bbox"))
        else:
            lack = [k for k, ok in (("crop", e.get("crop")), ("provenance.source_pdf_sha256", (e.get("provenance") or {}).get("source_pdf_sha256")),
                                    ("transcription", (e.get("transcription") or "").strip())) if not ok]
            if lack:
                out.append(issue("R30", "warn", "image_region evidence is not fully reproducible", lack,
                                 "use crop_region.py output (crop + provenance) and write what you read in transcription"))
    figs = set(re.findall(r"(?:\bfig(?:ure)?\.?\s*|图\s*)(\d+)", C.norm(text)))
    if figs:
        has_img = any(e.get("kind") == "image_region" for e in ev)
        ev_fig = " ".join(C.norm(" ".join(filter(None, [e.get("quote"), e.get("transcription"), e.get("table_caption")]))) for e in ev)
        unbound = sorted(f for f in figs if not has_img and not re.search(r"(?:^|\s)(?:fig(?:ure)?\.?\s*|图\s*)" + f + r"\s*[:.]", ev_fig))
        if unbound:
            out.append(issue("R31", "warn", "assertion refers to a figure but no evidence item comes from it (caption or image region)",
                             [f"Fig. {f}" for f in unbound], "add the caption (quote) or an image_region crop of the plotted value"))
    key = np_rec.get("dedup_key")
    if all_keys is not None and key and all_keys.get(key, 0) > 1:
        out.append(issue("R24", "warn", "duplicate of another assertion (same subject|predicate|object|value)", key, "merge them"))
    # provenance
    def pv(k):
        return ((prov.get("source") or {}).get(k) or {}).get("value") if isinstance((prov.get("source") or {}).get(k), dict) else (prov.get("source") or {}).get(k)
    unrep = set(prov.get("unreported_fields") or [])
    for k in ("title", "pdf_sha256"):
        if k == "pdf_sha256" and C.src_hash(prov.get("source") or {}):
            continue
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
