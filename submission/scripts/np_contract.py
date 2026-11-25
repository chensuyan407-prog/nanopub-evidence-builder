"""Output-contract handling for nanopub-evidence-builder.

case.json may describe the wanted result fields in many shapes; this module turns all of them
into one list of field specs and validates filled values against it.

Accepted wrappers (first found, depth-first): output_contract, outputs_contract, output_schema,
output_fields, required_outputs, result_schema, deliverables, outputs, schema.
Accepted shapes inside a wrapper:
  * JSON Schema: {"type": "object", "properties": {...}, "required": [...]}   (nested items/properties checked)
  * {"schema": {...JSON Schema...}} / {"fields": [...] | {...}} / {"required_fields": [...], "optional_fields": [...]}
  * {"field": "description", ...} or {"field": {"type": "array", "required": false, ...}, ...}
  * ["field", ...] or [{"name"|"field"|"key"|"id": "field", "type": ..., "required": bool}, ...]
  * deliverables: [{"file": "result.json", "fields": [...]}, {"file": "report.pdf"}]  (file-only items are listed, not fields)
Without a 'required' list every field counts as required (the honest default: missing -> partial).
"""
import math
import re

CONTRACT_KEYS = ("output_contract", "outputs_contract", "output_schema", "output_fields", "required_outputs",
                 "result_schema", "deliverables", "outputs", "schema")
NAME_KEYS = ("name", "field", "key", "id")
META_KEYS = {"type", "description", "title", "$schema", "$id", "additionalProperties", "examples", "default", "format",
             "properties", "required", "items", "enum", "minItems", "maxItems", "minimum", "maximum", "pattern", "nullable",
             "anyOf", "oneOf", "allOf", "const", "definitions", "$defs"}
JSON_TYPES = {"string", "integer", "number", "boolean", "array", "object", "null"}


def find_contract(obj, _depth=0):
    """(contract, key) of the first contract wrapper found in case.json (breadth-first by key priority)."""
    if _depth > 6:
        return None, None
    if isinstance(obj, dict):
        for k in CONTRACT_KEYS:
            if k in obj and obj[k] not in (None, "", [], {}):
                if k == "schema" and not _looks_like_contract(obj[k]):
                    continue
                return obj[k], k
        for v in obj.values():
            r = find_contract(v, _depth + 1)
            if r[0] is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_contract(v, _depth + 1)
            if r[0] is not None:
                return r
    return None, None


def find_contracts(obj, _depth=0, _path=()):
    """Collect all (contract, key, path) wrappers in case.json without recursing inside a matched contract."""
    out = []
    if _depth > 6:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in CONTRACT_KEYS and v not in (None, "", [], {}):
                if k == "schema" and not _looks_like_contract(v):
                    continue
                out.append((v, k, "/".join(_path + (k,))))
            else:
                out.extend(find_contracts(v, _depth + 1, _path + (str(k),)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(find_contracts(v, _depth + 1, _path + (str(i),)))
    return out


def _looks_like_contract(x):
    return isinstance(x, (dict, list)) and (not isinstance(x, dict) or "properties" in x or "fields" in x or
                                            any(not k.startswith("$") for k in x))


def _type_of(sch):
    if not isinstance(sch, dict):
        return None
    t = sch.get("type")
    if isinstance(t, list):
        t = [x for x in t if x != "null"]
        t = t[0] if len(t) == 1 else (t or None)
    if t is None:
        for k in ("anyOf", "oneOf"):
            opts = [o.get("type") for o in sch.get(k) or [] if isinstance(o, dict) and o.get("type") not in (None, "null")]
            if len(opts) == 1:
                t = opts[0]
            elif len(opts) > 1:
                t = opts
    if t is None and "properties" in sch:
        t = "object"
    if t is None and "items" in sch:
        t = "array"
    return t if (isinstance(t, list) or t in JSON_TYPES) else None


def _nullable(sch):
    if not isinstance(sch, dict):
        return False
    t = sch.get("type")
    return sch.get("nullable") is True or (isinstance(t, list) and "null" in t) or \
        any(isinstance(o, dict) and o.get("type") == "null" for o in (sch.get("anyOf") or []) + (sch.get("oneOf") or []))


def _field(name, schema=None, required=True, description=None):
    sch = schema if isinstance(schema, dict) else {}
    if description is None:
        description = sch.get("description") if sch else (schema if isinstance(schema, str) else None)
    return {"name": str(name), "required": bool(required), "type": _type_of(sch), "nullable": _nullable(sch),
            "schema": sch or None, "description": description}


def parse_contract(contract):
    """-> {"fields": [field spec], "files": [deliverable files], "style": str}. Order follows the contract."""
    files = []
    fields = _parse(contract, files)
    seen, out = set(), []
    for f in fields:
        if f["name"] in seen:  # first wins; a later 'required' flag can only strengthen it
            for g in out:
                if g["name"] == f["name"]:
                    g["required"] = g["required"] or f["required"]
            continue
        seen.add(f["name"])
        out.append(f)
    style = ("json_schema" if isinstance(contract, dict) and isinstance(contract.get("properties"), dict) else
             type(contract).__name__)
    return {"fields": out, "files": files, "style": style}


def parse_contracts(found):
    """Merge multiple (contract, key, path) declarations into one contract spec."""
    if not found:
        return {**parse_contract(None), "found_under": None, "errors": []}
    if len(found) == 1:
        sp = parse_contract(found[0][0])
        return {**sp, "found_under": found[0][1], "errors": []}
    by_name, out, files, styles, keys, errors = {}, [], [], [], [], []
    for c, key, _path in found:
        keys.append(key)
        sp = parse_contract(c)
        if sp["style"] not in styles:
            styles.append(sp["style"])
        for fn in sp["files"]:
            if fn not in files:
                files.append(fn)
        for f in sp["fields"]:
            nm = f["name"]
            if nm not in by_name:
                by_name[nm] = dict(f)
                out.append(by_name[nm])
            else:
                g = by_name[nm]
                g["required"] = g["required"] or f["required"]
                g["nullable"] = g["nullable"] or f["nullable"]
                if not g["description"] and f["description"]:
                    g["description"] = f["description"]
                if g["type"] is None and f["type"] is not None:
                    g["type"] = f["type"]
                if not g["schema"] and f["schema"]:
                    g["schema"] = f["schema"]
                elif g["type"] is not None and f["type"] is not None and g["type"] != f["type"]:
                    pair = {g["type"], f["type"]} if not isinstance(g["type"], list) and not isinstance(f["type"], list) else set()
                    if pair != {"integer", "number"}:
                        errors.append({"field": nm, "issue": f"conflicting types in mixed contracts: {g['type']} vs {f['type']}"})
    return {"fields": out, "files": files, "style": styles[0] if len(styles) == 1 else "mixed",
            "found_under": keys, "errors": errors}


def _parse(c, files, depth=0):
    if depth > 5 or c is None:
        return []
    if isinstance(c, str):
        return [_field(c)]
    if isinstance(c, list):
        out = []
        for it in c:
            if isinstance(it, str):
                out.append(_field(it))
            elif isinstance(it, dict):
                nm = next((it[k] for k in NAME_KEYS if isinstance(it.get(k), (str, int)) and str(it.get(k)).strip()), None)
                if nm is None and (it.get("fields") or it.get("properties") or it.get("schema")):
                    if it.get("file") or it.get("path"):
                        files.append(it.get("file") or it.get("path"))
                    out += _parse(it.get("fields") or it.get("schema") or it, files, depth + 1)
                elif nm is not None:
                    req = it.get("required", it.get("optional") is not True)
                    out.append(_field(nm, it.get("schema") if isinstance(it.get("schema"), dict) else it, req is not False,
                                      it.get("description")))
                elif it.get("file") or it.get("path"):
                    files.append(it.get("file") or it.get("path"))
        return out
    if isinstance(c, dict):
        if isinstance(c.get("properties"), dict):
            req = c.get("required")
            req_set = set(req) if isinstance(req, list) else None
            return [_field(k, v if isinstance(v, dict) else {"description": v},
                           (k in req_set) if req_set is not None else not (isinstance(v, dict) and v.get("required") is False))
                    for k, v in c["properties"].items()]
        if isinstance(c.get("schema"), (dict, list)):
            return _parse(c["schema"], files, depth + 1)
        if "fields" in c and isinstance(c["fields"], (dict, list)):
            return _parse(c["fields"], files, depth + 1)
        if "required_fields" in c or "optional_fields" in c:
            return [_field(n) for n in _names(c.get("required_fields"))] + \
                   [_field(n, required=False) for n in _names(c.get("optional_fields"))]
        if isinstance(c.get("required"), list) and all(isinstance(x, str) for x in c["required"]) and len(c) <= 3:
            return [_field(n) for n in c["required"]] + [_field(n, required=False) for n in _names(c.get("optional"))]
        out = []
        for k, v in c.items():
            if k in META_KEYS or k.startswith("$"):
                continue
            if isinstance(v, dict):
                req = v.get("required")
                out.append(_field(k, v, req is not False and v.get("optional") is not True))
            else:
                out.append(_field(k, None, True, v if isinstance(v, str) else None))
        return out
    return []


def _names(x):
    if isinstance(x, list):
        return [str(n.get("name") if isinstance(n, dict) else n) for n in x]
    if isinstance(x, dict):
        return list(x)
    return []


# ------------------------------------------------------------------ validation
def _is(v, t):
    if t == "string":
        return isinstance(v, str)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    if t == "boolean":
        return isinstance(v, bool)
    if t == "array":
        return isinstance(v, list)
    if t == "object":
        return isinstance(v, dict)
    if t == "null":
        return v is None
    return True


def coerce(v, t):
    """Lossless coercions only. -> (value, note|None). 'integer' never silently truncates 3.5."""
    if t is None or isinstance(t, list) or _is(v, t) or v is None:
        return v, None
    if t == "integer":
        if isinstance(v, float) and math.isfinite(v) and v.is_integer():
            return int(v), "float -> integer"
        if isinstance(v, str) and re.fullmatch(r"\s*[-+]?\d+\s*", v):
            return int(v), "string -> integer"
    if t == "number" and isinstance(v, str) and re.fullmatch(r"\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\s*", v):
        f = float(v)
        if not math.isfinite(f):
            return v, None
        return (int(f) if f.is_integer() and "." not in v and "e" not in v.lower() else f), "string -> number"
    if t == "string" and isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v), "number -> string"
    if t == "array" and isinstance(v, (dict, str, int, float)) and not isinstance(v, bool):
        return [v], "single value -> one-item array"
    if t == "boolean" and isinstance(v, str) and v.lower() in ("true", "false"):
        return v.lower() == "true", "string -> boolean"
    return v, None


def validate(v, sch, path="", depth=0, notes=None):
    """Check v against a (subset of) JSON Schema; coerce where lossless.
    -> (value, problems[list of str], notes[list of str])."""
    notes = [] if notes is None else notes
    probs = []
    if not isinstance(sch, dict) or depth > 8:
        return v, probs, notes
    if v is None:
        if not _nullable(sch) and _type_of(sch) not in (None, "null"):
            probs.append(f"{path or '$'}: null")
        return v, probs, notes
    if isinstance(v, float) and not math.isfinite(v):
        probs.append(f"{path or '$'}: non-finite number")
        return v, probs, notes
    t = _type_of(sch)
    if isinstance(t, list):
        if not any(_is(v, x) for x in t):
            matched = False
            for x in t:
                v2, note = coerce(v, x)
                if note and _is(v2, x):
                    notes.append(f"{path or '$'}: {note}")
                    v = v2
                    matched = True
                    break
            if not matched:
                probs.append(f"{path or '$'}: {type(v).__name__} is none of {t}")
                return v, probs, notes
    else:
        v2, note = coerce(v, t)
        if note:
            notes.append(f"{path or '$'}: {note}")
            v = v2
        if t and not _is(v, t):
            probs.append(f"{path or '$'}: expected {t}, got {type(v).__name__}")
            return v, probs, notes
    if "enum" in sch and isinstance(sch["enum"], list) and v not in sch["enum"]:
        probs.append(f"{path or '$'}: {v!r} not in enum {sch['enum'][:8]}")
    if isinstance(v, dict):
        props = sch.get("properties") if isinstance(sch.get("properties"), dict) else {}
        for k in sch.get("required") or []:
            if isinstance(k, str) and (k not in v or v[k] in (None, "") and not _nullable(props.get(k) or {})):
                probs.append(f"{path}.{k}: required key missing" if path else f"{k}: required key missing")
        for k, ps in props.items():
            if k in v:
                v[k], p2, _ = validate(v[k], ps, f"{path}.{k}" if path else k, depth + 1, notes)
                probs += p2
    elif isinstance(v, list) and isinstance(sch.get("items"), dict):
        for i, it in enumerate(v):
            v[i], p2, _ = validate(it, sch["items"], f"{path}[{i}]", depth + 1, notes)
            probs += p2
            if len(probs) > 30:
                probs.append("... (more problems truncated)")
                break
        if isinstance(sch.get("minItems"), int) and len(v) < sch["minItems"]:
            probs.append(f"{path or '$'}: {len(v)} items < minItems {sch['minItems']}")
    return v, probs, notes


def is_empty(v):
    return v is None or (isinstance(v, (str, list, dict)) and len(v) == 0)
