"""Shared helpers for the nanopub-evidence-builder scripts.

Text normalisation (for verbatim quote matching), sentence splitting with
source anchors, linguistic cue detection (numbers, hedges, negation,
citations, comparison, causality, coordination) and source-metadata
extraction from the PDF itself. Standard library + PyMuPDF only.
"""
import hashlib
import json
import os
import re
import unicodedata

VERSION = "0.3.0"
TOOL = f"nanopub-evidence-builder {VERSION}"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24
except Exception:  # pragma: no cover
    import fitz  # type: ignore


def utf8_stdout():
    import sys
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def load_json(p):
    with open(p, encoding="utf-8-sig") as fh:
        return json.load(fh)


def relative_locator(path, base):
    """Serialize a local path without embedding the host filesystem root."""
    if path is None:
        return None
    try:
        return os.path.relpath(path, base).replace(os.sep, "/")
    except ValueError:  # Windows paths on different drives have no relative route.
        return os.path.basename(path)


def dump_json(obj, p):
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1, allow_nan=False)
        os.replace(tmp, p)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_vocab():
    return load_json(os.path.join(SKILL_DIR, "vocab", "relations.json"))


# ---------------------------------------------------------------- normalise
_TRANS = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    "‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"',
    " ": " ", " ": " ", " ": " ", "​": "", "­": "", "ﬀ": "ff",
    "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "×": "x", "∗": "*",
}


def norm(s, lower=True):
    """Normalisation used for quote matching: NFKC, ligatures, dashes/quotes,
    line-end hyphenation, whitespace. Case-folded unless lower=False."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = "".join(_TRANS.get(c, c) for c in s)
    s = re.sub(r"(\w)-\s+(\w)", r"\1\2", s)          # hyphenation at line ends
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([,.;:!?%)\]])", r"\1", s)
    s = re.sub(r"([(\[])\s+", r"\1", s)
    return s.lower() if lower else s


def norm_loose(s):
    """Even looser: drop all non-alphanumerics (used for fuzzy location only)."""
    return re.sub(r"[^0-9a-z一-鿿]+", "", norm(s))


def slug(s, n=48):
    s = norm(s)
    s = re.sub(r"[^0-9a-z一-鿿]+", "-", s).strip("-")
    return (s[:n].strip("-") or "x")


def short_hash(*parts, n=12):
    h = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return h[:n]


# ---------------------------------------------------------------- sentences
_ABBR = ["e.g", "i.e", "et al", "fig", "figs", "eq", "eqs", "tab", "sec", "secs", "vs", "cf", "approx", "no",
         "resp", "ref", "refs", "dr", "prof", "inc", "ltd", "etc", "al", "ca", "viz", "a.k.a", "ch", "app", "nos",
         "st", "mr", "ms", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"]
_ABBR_RE = re.compile(r"(?:\b(?:%s)|\b[A-Z])\.$" % "|".join(re.escape(a) for a in _ABBR), re.I)


def split_sentences(text):
    """Return list of (start, end) char spans of sentences in text."""
    spans, start = [], 0
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if c in "。！？":
            j = i + 1
            while j < n and text[j] in "”’\")）":
                j += 1
            spans.append((start, j))
            start = j
            i = j
            continue
        if c in ".!?":
            j = i + 1
            while j < n and text[j] in "\"')]”’":
                j += 1
            if j >= n:
                break
            if not text[j].isspace():
                i += 1
                continue
            k = j
            while k < n and text[k].isspace():
                k += 1
            nxt = text[k] if k < n else ""
            head = text[start:i + 1]
            if c == "." and _ABBR_RE.search(head[-12:]):
                i += 1
                continue
            if c == "." and re.search(r"\d\.$", head) and nxt.isdigit():
                i += 1
                continue
            if nxt and (nxt.isupper() or nxt.isdigit() or nxt in "[(\"“‘'" or "一" <= nxt <= "鿿"):
                spans.append((start, j))
                start = k
                i = k
                continue
        i += 1
    if start < n and text[start:].strip():
        spans.append((start, n))
    out = []
    for a, b in spans:
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if b > a:
            out.append((a, b))
    return out


# ---------------------------------------------------------------- cues
NUM_RE = re.compile(r"(?<![\w.])[-+]?\d+(?:[.,]\d+)*(?:\s?%|\s?[x×]\b)?")
HEDGE = ["may", "might", "could", "suggest", "suggests", "suggested", "likely", "possibly", "possible", "appear",
         "appears", "seem", "seems", "potentially", "hypothesize", "hypothesise", "presumably", "perhaps",
         "indicate", "indicates", "suggesting", "indicating", "suggestive", "unclear", "unknown", "tend to", "tends to", "we believe", "we conjecture", "conjecture", "plausibly",
         "probably", "speculate", "we speculate", "putative", "putatively", "preliminary",
         "可能", "或许", "推测", "似乎", "提示", "倾向于", "或可", "有望", "大概", "疑似", "初步"]
NEGATION = ["not", "no", "never", "none", "neither", "nor", "cannot", "can't", "fail", "fails", "failed",
            "without", "unable", "lack", "lacks", "n't", "absence", "不", "未", "没有", "无法", "并非"]
UNIVERSAL = ["all", "always", "every", "any", "never", "universally", "entirely", "completely", "invariably",
             "所有", "总是", "一切", "任何", "完全"]
COMPARE = ["than", "outperform", "outperforms", "outperformed", "better", "worse", "higher", "lower", "fewer",
           "more", "less", "compared", "comparison", "versus", "vs", "surpass", "surpasses", "exceeds", "improve",
           "improves", "improved", "improvement", "reduces", "reduced", "increase", "increases", "decrease",
           "superior", "inferior", "within", "similar", "comparable", "same", "match", "matches", "on par", "gain", "gains", "state-of-the-art", "relative", "优于", "高于", "低于", "相比",
           "提高", "降低", "改善"]
CAUSAL = ["because", "due to", "cause", "causes", "caused", "lead to", "leads to", "led to", "result in",
          "results in", "resulted in", "resulting from", "result from", "results from", "resulting in", "therefore", "thus", "hence", "consequently", "owing to", "induces",
          "induce", "drives", "enable", "enables", "allows", "attributable", "导致", "因此", "由于", "引起", "使得"]
CONDITION = ["when", "if", "under", "unless", "only", "in our experiments", "in this setting", "on the", "for the",
             "at", "with", "given", "provided", "whereas", "while", "在", "当", "如果", "条件下"]
CLAIM_CUE = ["we show", "we find", "we found", "we demonstrate", "we observe", "we report", "results show",
             "results indicate", "this shows", "we propose", "we present", "we introduce", "achieve", "achieves",
             "achieved", "outperform", "state-of-the-art", "significantly", "conclude", "evidence", "demonstrate",
             "is associated", "correlat", "increase", "decrease", "improv", "reduc", "error", "accuracy",
             "表明", "发现", "证明", "提出", "显著", "提高", "降低"]
CITE_RE = re.compile(r"\[\s*\d+(?:\s*[-–,]\s*\d+)*\s*\]|\(\s*[A-Z][A-Za-z\-]+(?: et al\.?| and [A-Z][A-Za-z\-]+)?,?\s*\d{4}[a-z]?(?:;[^)]*)?\)|\b[A-Z][a-z]+ et al\.?\s*(?:\[\d+\]|\(\d{4}\))")
COORD_RE = re.compile(r";|\band\b|\bwhile\b|\bwhereas\b|\bbut\b|\bas well as\b|\balso\b|以及|并且|同时|而且|，而")


NEG_EXCLUDE_ZH = ("不同", "不断", "不仅", "不但", "不过", "不少", "无论", "不论", "未来", "未知",
                  "是否", "能否", "可否", "与否", "无监督", "无偏", "无损", "无创", "无毒", "非参数", "非线性")


def _has_word(text_l, w):
    if re.match(r"[一-鿿]", w):
        return w in text_l
    return re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", text_l) is not None


def find_words(text, words):
    t = norm(text)
    if words is NEGATION:
        t_zh = t
        for ex in NEG_EXCLUDE_ZH:
            t_zh = t_zh.replace(ex, " " * len(ex))
        return [w for w in words if _has_word(t_zh if re.match(r"[一-鿿]", w) else t, w)]
    return [w for w in words if _has_word(t, w)]


def numbers(text):
    out = []
    for m in NUM_RE.finditer(CITE_RE.sub(" ", norm(text, lower=False))):
        v = m.group(0).strip().replace(" ", "")
        if re.fullmatch(r"(19|20)\d\d", v):  # keep years as numbers too, but tagged
            out.append(v)
        else:
            out.append(v)
    return out


def num_core(v):
    """'3.57%' -> '3.57'; '1,000' -> '1000'; '8x' -> '8'."""
    v = v.strip().rstrip("%xX×").replace(",", "")
    v = v.lstrip("+")
    try:
        f = float(v)
        return ("%g" % f)
    except ValueError:
        return v


# ---------------------------------------------------------------- magnitudes (6 billion == 60 亿 == 6x10^9)
SCALE_WORDS = {"thousand": 1e3, "thousands": 1e3, "million": 1e6, "millions": 1e6, "billion": 1e9, "billions": 1e9,
               "trillion": 1e12, "trillions": 1e12, "k": 1e3, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12, "bn": 1e9, "mn": 1e6,
               "千": 1e3, "万": 1e4, "十万": 1e5, "百万": 1e6, "千万": 1e7, "亿": 1e8, "十亿": 1e9, "百亿": 1e10, "千亿": 1e11, "万亿": 1e12}
_SCALE_ALT = "|".join(sorted((re.escape(k) for k in SCALE_WORDS), key=len, reverse=True))
MAG_RE = re.compile(r"(?<![\w.])(\d+(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
                    r"(?:(?P<sc>" + _SCALE_ALT + r")(?![A-Za-z])|(?:[x×*·]\s*)?10\s*(?:\^|\*\*)\s*\{?(?P<e1>-?\d+)\}?|(?:[x×*·]\s*)10(?P<e2>[⁻]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+)|[eE](?P<e3>[-+]?\d+)(?![\w.]))")
_SUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")


def magnitudes(text):
    """Numbers with an explicit magnitude: [(raw, mantissa_str, value_float)].
    '6 billion', '60亿', '1.28 million', '128万', '6x10^9', '6e9', '60M' (single capital letters only
    when glued to the number: '60M', not '60 M')."""
    out = []
    t = norm(text, lower=False)
    for m in MAG_RE.finditer(t):
        mant = m.group(1).replace(",", "")
        sc = m.group("sc")
        if sc and len(sc) == 1 and sc.isascii() and sc.isalpha() and t[m.end(1):m.start("sc")].strip() == "" and m.start("sc") != m.end(1):
            continue  # '60 M' (space + single letter) is too ambiguous
        try:
            v = float(mant)
        except ValueError:
            continue
        if sc:
            f = SCALE_WORDS[sc]
        else:
            e = m.group("e1") or (m.group("e2") or "").translate(_SUP) or m.group("e3")
            try:
                f = 10.0 ** int(e)
            except (TypeError, ValueError):
                continue
        out.append((m.group(0).strip(), mant, v * f))
    return out


def plain_values(text):
    """Every number in the text as float (no magnitude), for equivalence with magnitudes."""
    out = []
    for m in re.finditer(r"(?<![\w.])(\d+(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)", norm(text, lower=False)):
        try:
            out.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return out


def same_magnitude(a_mant, a_val, ev_vals):
    """True if a_val equals one of ev_vals, allowing the rounding implied by the mantissa's printed precision
    ('1.28 million' matches 1,281,167; '6 billion' does not match 6 million)."""
    dec = len(a_mant.split(".")[1]) if "." in a_mant else 0
    try:
        m = float(a_mant)
    except ValueError:
        return False
    unit = (a_val / m) if m else 1.0
    tol = 0.5 * (10 ** -dec) * unit * 1.0000001
    return any(abs(v - a_val) <= max(tol, abs(a_val) * 1e-9) for v in ev_vals)


ATTRIBUTION_RE = re.compile(
    r"\b[A-Z][a-z]+(?:\s+and\s+[A-Z][a-z]+)?\s+et\s+al\.?|\b(?:previous|prior|earlier|recent|existing|past)\s+(?:work|works|studies|study|research|methods|approaches|literature|reports?)\b|"
    r"\b(?:has|have|had)\s+been\s+(?:shown|reported|demonstrated|observed|proposed|found|suggested)\b|\bit\s+is\s+(?:well\s+)?known\b|"
    r"\bwas\s+(?:shown|reported|proposed|introduced|demonstrated)\s+(?:by|in)\b|\baccording\s+to\b|\bas\s+(?:reported|shown)\s+(?:by|in)\b|"
    r"已有研究|前人|文献\s*\[|据报道|先前的?研究|既往研究|有研究(?:表明|发现|报道)", re.I)
FIRST_PERSON_RE = re.compile(r"\b(?:we|our|ours|us)\b|本文|我们|本研究|本工作", re.I)
LIMITERS = ["only", "solely", "except", "at least", "at most", "up to", "as long as", "provided that", "unless", "if and only if",
            "in some", "in most", "in part", "partially", "仅", "只有", "只在", "除了", "至少", "最多", "部分", "当且仅当"]


def src_hash(md):
    """SHA-256 of the source file (PDF or text/docx source)."""
    md = md or {}
    for k in ("pdf_sha256", "file_sha256"):
        v = md.get(k)
        v = v.get("value") if isinstance(v, dict) else v
        if v:
            return v
    return None


def cues(text):
    t_cites = CITE_RE.findall(text)
    return {
        "numbers": numbers(text),
        "hedges": find_words(text, HEDGE),
        "negations": find_words(text, NEGATION),
        "universals": find_words(text, UNIVERSAL),
        "comparatives": find_words(text, COMPARE),
        "causal": find_words(text, CAUSAL),
        "conditions": find_words(text, CONDITION),
        "citations": t_cites,
        "claim_cues": find_words(text, CLAIM_CUE),
        "coordination": len(COORD_RE.findall(norm(text))),
    }


# ---------------------------------------------------------------- metadata
ARXIV_RE = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?\s*(?:\[([^\]]+)\])?\s*(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})?")
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)
YEAR_RE = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")
AFFIL_WORDS = re.compile(r"universit|institut|college|school|department|dept\.|laborator|\blab\b|research|inc\.|ltd|"
                         r"corporation|google|microsoft|facebook|meta ai|openai|deepmind|academy|center|centre|hospital|"
                         r"\bfaculty\b|@|\bemail\b|大学|研究所|学院|实验室|医院|中心|公司", re.I)


def _src(kind, page=None, block_id=None, quote=None):
    d = {"kind": kind}
    if page is not None:
        d["page"] = page
    if block_id:
        d["block_id"] = block_id
    if quote:
        d["quote"] = quote[:300]
    return d


def _cap(t):
    return bool(t) and t[0].isupper() and re.fullmatch(r"[\w.\-'’]+", t) is not None


def split_authors(raw):
    """Best-effort author split of a title-page author block (always needs review)."""
    lines = re.split(r"\s{2,}|\n", raw)
    names = []
    raw = re.sub(r"\S+@\S+|\{[^}]*\}@\S+", " ", raw)
    for part in re.split(r",|\band\b|;|、|，|\s{2,}|\*|∗|†|‡|§|¶", raw):
        p = re.sub(r"[\d∗*†‡§¶]+$", "", part.strip()).strip()
        p = re.sub(r"\s+", " ", p)
        if "@" in p or not p:
            continue
        m = AFFIL_WORDS.search(p)
        if m:  # "Kaiming He Jian Sun Microsoft Research" -> keep the names before the affiliation
            toks = p[:m.start()].split()
            if len(toks) <= 2 and re.match(r"universit|institut|college|school|academy|hospital|cent(er|re)|laborator|department", m.group(0), re.I)                     and p[m.start():m.start() + 1].isupper():
                continue  # "Johns Hopkins University": the words before the keyword are the institution's name
            if len(toks) > 4 and len(toks) % 2 == 1:  # trailing organisation word ("Stanford University")
                toks.pop()
            p = " ".join(toks).strip()
            if not p:
                continue
        toks = p.split(" ")
        if 2 <= len(toks) <= 4 and all(_cap(t) for t in toks):
            names.append(p)
        elif re.fullmatch(r"[一-鿿·]{2,4}", p):
            names.append(p)
        elif len(toks) > 4 and all(_cap(t) for t in toks):
            # "Kaiming He Xiangyu Zhang Shaoqing Ren" -> pair up tokens (flagged)
            for i in range(0, len(toks) - 1, 2):
                names.append(toks[i] + " " + toks[i + 1])
    seen, out = set(), []
    for nme in names:
        if nme.lower() not in seen:
            seen.add(nme.lower())
            out.append(nme)
    return out


def dedup_names(names):
    """Drop 'Tom Brown' when 'Tom B. Brown' is also present (same first and last token)."""
    out = []
    for n in names:
        t = n.split()
        if any(m != n and len(m.split()) > len(t) and m.split()[0] == t[0] and m.split()[-1] == t[-1] for m in names):
            continue
        if n not in out:
            out.append(n)
    return out


def extract_metadata(pdf_path, doc_json):
    """Source metadata with a source record for every value. Never invents:
    a field that cannot be found is value=None with kind 'not_found'."""
    meta = doc_json.get("meta", {}) if doc_json else {}
    info = meta.get("metadata") or {}
    blocks = (doc_json or {}).get("blocks", [])
    md = {}
    p1 = [b for b in blocks if b.get("page") == 1]
    raw1 = raw2 = ""
    try:
        d = fitz.open(pdf_path)
        if d.needs_pass:
            raise RuntimeError("encrypted")
        raw1 = d[0].get_text() if d.page_count else ""
        raw2 = d[1].get_text() if d.page_count > 1 else ""
        d.close()
    except Exception:
        pass
    # title
    tbs = []
    for b in sorted(p1, key=lambda b: b.get("order") or 0):
        if b.get("role") == "title":
            tbs.append(b)
        elif tbs:
            break
    tb = tbs[0] if tbs else None
    if tb:
        ttl = " ".join(norm(b["text"], lower=False) for b in tbs)
        md["title"] = {"value": ttl, "source": _src("pdf_text", 1, ",".join(b["id"] for b in tbs), ttl), "confidence": "high"}
    elif info.get("title"):
        md["title"] = {"value": info["title"].strip(), "source": _src("pdf_info"), "confidence": "medium"}
    else:
        cand = next((b for b in sorted(p1, key=lambda b: b.get("order") or 0)
                     if 8 <= len(norm(b.get("text", ""), lower=False)) <= 180
                     and not re.search(r"arXiv:|http://|https://|^abstract\b|^summary\b", b.get("text", ""), re.I)
                     and not re.fullmatch(r"[\d\s.\-/]+", b.get("text", "").strip())), None)
        if cand:
            ttl = norm(cand["text"], lower=False)
            md["title"] = {"value": ttl, "source": _src("pdf_text", 1, cand.get("id"), ttl), "confidence": "low", "needs_review": True}
        else:
            md["title"] = {"value": None, "source": _src("not_found"), "confidence": None}
    # authors
    ab = [b for b in p1 if b.get("role") == "author_affiliation"]
    if tb:  # everything between the title and the first heading is the author block
        o0 = max(b.get("order") or 0 for b in tbs)
        o1 = min([b.get("order") or 10 ** 6 for b in p1 if b.get("role") == "heading" and (b.get("order") or 0) > o0] or [o0 + 40])
        ab = [b for b in sorted(p1, key=lambda b: b.get("order") or 0) if o0 < (b.get("order") or 0) < o1] or ab
    if info.get("author") and len(info["author"].strip()) > 2:
        names = [a.strip() for a in re.split(r",|;| and ", info["author"]) if a.strip()]
        md["authors"] = {"value": names, "source": _src("pdf_info", quote=info["author"]), "confidence": "medium", "needs_review": True}
    elif ab:
        raw = " ".join(b["text"] for b in ab)
        names = []
        for b in ab:
            for nme in split_authors(b["text"]):
                if nme not in names:
                    names.append(nme)
        # second reading: raw text lines between the title and 'Abstract' (catches author blocks in other columns)
        lines = raw1.splitlines()
        tail = norm_loose((tbs[-1]["text"] if tb else ""))[-15:]
        start = next((i for i, l in enumerate(lines) if tail and tail in norm_loose(l)), None)
        if start is not None:
            names2 = []
            for l in lines[start + 1:start + 60]:
                if re.match(r"\s*(abstract|summary|1\.?\s+introduction|摘要)\b", l, re.I):
                    break
                if re.search(r"conference|proceedings|workshop|symposium|\bUSA\b|\b[A-Z]{2}, [A-Z]|\b(19|20)\d\d\b", l, re.I):
                    continue  # venue / place lines
                for nme in split_authors(l):
                    if nme not in names2:
                        names2.append(nme)
            if len(names2) > len(names):
                names = names2
        names = dedup_names(names)
        md["authors"] = {"value": names or None, "raw": raw[:500],
                         "source": _src("pdf_text", 1, ab[0]["id"], raw), "confidence": "low" if names else None,
                         "needs_review": True,
                         "note": "split from the title-page author block by heuristics; check against 'raw'"}
    else:
        md["authors"] = {"value": None, "source": _src("not_found"), "confidence": None}
    # identifiers
    am = ARXIV_RE.search(raw1) or ARXIV_RE.search(raw2)
    if am:
        md["arxiv_id"] = {"value": am.group(1), "source": _src("arxiv_stamp", 1, quote=am.group(0)), "confidence": "high"}
        md["version"] = {"value": am.group(2), "source": _src("arxiv_stamp", 1, quote=am.group(0)), "confidence": "high"} if am.group(2) \
            else {"value": None, "source": _src("not_found"), "confidence": None, "note": "arXiv id without version suffix"}
        if am.group(3):
            md["arxiv_category"] = {"value": am.group(3), "source": _src("arxiv_stamp", 1, quote=am.group(0)), "confidence": "high"}
        if am.group(4):
            md["arxiv_date"] = {"value": am.group(4), "source": _src("arxiv_stamp", 1, quote=am.group(0)), "confidence": "high"}
    else:
        md["arxiv_id"] = {"value": None, "source": _src("not_found"), "confidence": None}
        md["version"] = {"value": None, "source": _src("not_found"), "confidence": None,
                         "note": "no version statement found in the PDF"}
    dm = None
    for txt, pg in ((raw1, 1), (raw2, 2), (info.get("subject") or "", None), (info.get("keywords") or "", None)):
        dm = DOI_RE.search(txt)
        if dm:
            doi = dm.group(1).rstrip(".,;)]}")
            md["doi"] = {"value": doi, "source": _src("pdf_text" if pg else "pdf_info", pg, quote=txt[max(0, dm.start() - 30):dm.end() + 10]), "confidence": "high"}
            break
    if not dm:
        md["doi"] = {"value": None, "source": _src("not_found"), "confidence": None}
    # year
    if md.get("arxiv_date"):
        md["year"] = {"value": int(md["arxiv_date"]["value"][-4:]), "source": md["arxiv_date"]["source"], "confidence": "medium",
                      "note": "year of this arXiv version, may differ from the venue year"}
    else:
        m = re.search(r"(?:©|\(c\)|copyright|published|received|accepted)[^\n]{0,40}?((?:19|20)\d\d)", raw1, re.I)
        if m:
            md["year"] = {"value": int(m.group(1)), "source": _src("pdf_text", 1, quote=m.group(0)), "confidence": "medium"}
        else:
            cd = info.get("creationDate") or ""
            ym = re.search(r"D:(\d{4})", cd)
            md["year"] = ({"value": int(ym.group(1)), "source": _src("pdf_info", quote=cd), "confidence": "low",
                           "note": "PDF file creation date, not necessarily the publication year"} if ym else
                          {"value": None, "source": _src("not_found"), "confidence": None})
    vm = re.search(r"(Proceedings of [^\n]{3,80}|[A-Z]{2,8}\s?'?(?:19|20)\d\d|Journal of [^\n]{3,60}|Nature [A-Z][a-z]+|Science Advances|PLOS [A-Z][a-z]+)", raw1)
    md["venue"] = ({"value": vm.group(1).strip(), "source": _src("pdf_text", 1, quote=vm.group(0)), "confidence": "low",
                    "needs_review": True} if vm else {"value": None, "source": _src("not_found"), "confidence": None})
    md["pdf_sha256"] = {"value": meta.get("sha256") or (sha256_file(pdf_path) if os.path.exists(pdf_path) else None),
                        "source": _src("file_hash"), "confidence": "high"}
    md["page_count"] = {"value": meta.get("page_count"), "source": _src("file"), "confidence": "high"}
    md["file_name"] = {"value": os.path.basename(pdf_path), "source": _src("file"), "confidence": "high"}
    return md


def source_iri(md):
    def v(k):
        x = md.get(k) or {}
        return x.get("value")
    if v("doi"):
        return "https://doi.org/" + v("doi")
    if v("arxiv_id"):
        return "https://arxiv.org/abs/" + v("arxiv_id") + (v("version") or "")
    return "urn:sha256:" + (src_hash(md) or "unknown")


# ---------------------------------------------------------------- sentence index
def build_sentence_index(doc):
    """Sentences of body paragraphs (+captions, footnotes) with page/block/bbox anchors.
    id = '<first block id>-s<k>' (k counts sentences starting in that block)."""
    blocks = {b["id"]: b for b in doc.get("blocks", [])}
    out = []
    counters = {}
    paras = list(doc.get("paragraphs", []))
    covered = set()
    for p in paras:
        for s in p.get("sources", []):
            covered.add(s.get("block_id"))
    # captions / footnotes / blocks not in paragraphs become their own units
    extra = [b for b in doc.get("blocks", []) if b["id"] not in covered and b.get("role") in ("caption", "footnote", "body", "list_item", "abstract")]
    units = [(p.get("text", ""), p.get("sources", []), p.get("role", "body"), p.get("section_path", []), p.get("id")) for p in paras]
    units += [(b.get("text", ""), [{"block_id": b["id"], "page": b["page"], "bbox": b["bbox"]}], b.get("role"), b.get("section_path", []), None) for b in extra]
    for text, srcs, role, spath, pid in units:
        if not text or not srcs:
            continue
        # map paragraph offsets to source blocks: locate each block's normalised head inside the paragraph
        starts = []
        nt = norm_loose(text)
        for s in srcs:
            bt = norm_loose((blocks.get(s["block_id"]) or {}).get("text", ""))
            pos = nt.find(bt[:30]) if bt else -1
            starts.append((pos if pos >= 0 else 0, s))
        starts.sort(key=lambda x: x[0])
        for a, b in split_sentences(text):
            st = text[a:b]
            loose_a = len(norm_loose(text[:a]))
            loose_b = len(norm_loose(text[:b]))
            first = starts[0][1]
            involved = []
            for i, (pos, s) in enumerate(starts):
                nxt = starts[i + 1][0] if i + 1 < len(starts) else 10 ** 9
                if pos <= loose_a:
                    first = s
                if pos < loose_b and nxt > loose_a:
                    involved.append(s)
            bid = first["block_id"]
            k = counters.get(bid, 0) + 1
            counters[bid] = k
            out.append({"id": f"{bid}-s{k}", "page": first["page"], "block_id": bid,
                        "blocks": [s["block_id"] for s in involved] or [bid],
                        "pages": sorted({s["page"] for s in involved} or {first["page"]}),
                        "block_bbox": first.get("bbox"), "paragraph_id": pid, "role": role,
                        "section_path": spath, "text": st, "cues": cues(st)})
    return out


def claim_score(s):
    c = s["cues"]
    sec = " ".join(s.get("section_path") or []).lower()
    score = 0.0
    score += 1.0 * min(len(c["claim_cues"]), 3)
    score += 0.8 * min(len(c["numbers"]), 3)
    score += 0.6 * bool(c["comparatives"]) + 0.5 * bool(c["causal"])
    if re.search(r"abstract|conclusion|result|experiment|discussion|摘要|结论|结果|讨论", sec):
        score += 1.0
    if re.search(r"related work|background|reference|acknowledg|相关工作|致谢|参考文献", sec):
        score -= 1.5
    if c["citations"]:
        score -= 0.5
    if len(s["text"]) < 40:
        score -= 1.0
    return round(score, 2)
