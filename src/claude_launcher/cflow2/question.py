"""cflow v2 question types: declarations, validation and the shared renderers.

Spec: ``docs/cflow-v2.md`` 2.10. A question type (``question_types:``) says
what fields a question carries; everything that touches a question reads that
one declaration -- :func:`validate` (a ``question:<type>`` return field and an
``ask.brief`` when the ask opens), :func:`render_text` (the mesh ``decide``
message, ``flow_status``, the CLI), :func:`to_json` (the web UI) and
:func:`guide` (the authoring guide ``ask-user`` puts in its frame prompt).
Adding an item to a question is a one-line change to its declaration.

Normalized declaration (JSON-able, stored in IR ``meta.question_types``)::

    {"name": "decision", "description": "...",
     "options": "options", "recommendation": "recommendation" | None,
     "fields": [{"name": "problem", "type": "text", "required": True,
                 "label": "...", "help": "...",
                 # enum: "choices"; list: "item" (list of field decls), "min",
                 # "max"; ref: "of"
                 }, ...]}

Fields are a list so the declaration order (the rendering order) survives a
JSON round trip. Every function here is free of I/O.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

FIELD_TYPES = ("str", "text", "name", "int", "bool", "enum", "list", "ref")
FIELD_KEYS = ("type", "required", "label", "help", "choices", "item", "min", "max", "of")
TYPE_KEYS = ("description", "options", "recommendation", "fields")
#: Return-field type prefix naming a question type (``question:decision``).
RETURN_PREFIX = "question:"
#: The answer meaning "the options do not fit the problem" (2.10).
REFRAME = "reframe"
#: Option names an answer could not tell apart from a built-in answer.
RESERVED_NAMES = ("abstain", "approve", "decline", REFRAME)
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
#: Names a question type cannot take: they are ``returns`` base types.
BASE_TYPES = ("str", "int", "float", "bool", "list", "dict", "any")


class QuestionError(ValueError):
    """A question type declaration is malformed."""


# ---------------------------------------------------------------------------
# declarations


def _field(tname: str, path: str, fname: Any, raw: Any, *, nested: bool) -> Dict[str, Any]:
    where = f"question type {tname!r} field {path}{fname}"
    if not isinstance(fname, str) or not _NAME_RE.match(fname):
        raise QuestionError(f"question type {tname!r}: bad field name {fname!r}")
    if isinstance(raw, str):
        raw = {"type": raw}
    if not isinstance(raw, dict):
        raise QuestionError(f"{where}: must be a mapping")
    for key in raw:
        if key not in FIELD_KEYS:
            raise QuestionError(f"{where}: unknown key {key!r} (allowed: {', '.join(FIELD_KEYS)})")
    ftype = raw.get("type")
    if ftype not in FIELD_TYPES:
        raise QuestionError(f"{where}: unknown type {ftype!r} (allowed: {', '.join(FIELD_TYPES)})")
    out: Dict[str, Any] = {"name": fname, "type": ftype, "required": bool(raw.get("required")),
                           "label": str(raw.get("label") or fname),
                           "help": str(raw.get("help") or "")}
    for key, only in (("choices", "enum"), ("item", "list"), ("min", "list"), ("max", "list"),
                      ("of", "ref")):
        if key in raw and ftype != only:
            raise QuestionError(f"{where}: {key!r} applies to type {only} only")
    if ftype == "enum":
        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices or \
                not all(isinstance(c, str) and c for c in choices):
            raise QuestionError(f"{where}: an enum needs choices (a list of strings)")
        out["choices"] = list(choices)
    elif ftype == "list":
        if nested:
            raise QuestionError(f"{where}: a list item cannot hold another list")
        item = raw.get("item")
        if not isinstance(item, dict) or not item:
            raise QuestionError(f"{where}: a list needs item (a mapping of field declarations)")
        out["item"] = [_field(tname, f"{path}{fname}.", k, v, nested=True) for k, v in item.items()]
        for key in ("min", "max"):
            if key in raw:
                v = raw[key]
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    raise QuestionError(f"{where}: {key} must be a non-negative integer")
                out[key] = v
        if "min" in out and "max" in out and out["min"] > out["max"]:
            raise QuestionError(f"{where}: min is larger than max")
    elif ftype == "ref":
        if nested:
            raise QuestionError(f"{where}: a ref cannot sit inside a list item")
        if not isinstance(raw.get("of"), str):
            raise QuestionError(f"{where}: a ref needs of: <list field>")
        out["of"] = raw["of"]
    return out


def normalize(name: str, raw: Any) -> Dict[str, Any]:
    """One ``question_types`` entry, checked and normalized."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise QuestionError(f"bad question type name {name!r}")
    if name in BASE_TYPES:
        raise QuestionError(f"question type {name!r}: the name is a returns base type")
    if not isinstance(raw, dict):
        raise QuestionError(f"question type {name!r} must be a mapping")
    for key in raw:
        if key not in TYPE_KEYS:
            raise QuestionError(f"question type {name!r}: unknown key {key!r} "
                                f"(allowed: {', '.join(TYPE_KEYS)})")
    fields = raw.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise QuestionError(f"question type {name!r} needs fields")
    decl = [_field(name, "", k, v, nested=False) for k, v in fields.items()]
    by = {f["name"]: f for f in decl}
    for f in decl:
        if f["type"] == "ref" and (by.get(f["of"]) or {}).get("type") != "list":
            raise QuestionError(f"question type {name!r} field {f['name']}: of {f['of']!r} "
                                f"is not a list field of this type")
    opt = raw.get("options")
    if not isinstance(opt, str) or (by.get(opt) or {}).get("type") != "list":
        raise QuestionError(f"question type {name!r}: options must name a list field")
    names = [i for i in by[opt]["item"] if i["name"] == "name"]
    if not names or names[0]["type"] != "name":
        raise QuestionError(f"question type {name!r}: the options field's item needs a "
                            f"'name' field of type name")
    names[0]["required"] = True
    rec = raw.get("recommendation")
    if rec is not None and (by.get(rec) or {}).get("type") != "ref" or \
            rec is not None and by[rec]["of"] != opt:
        raise QuestionError(f"question type {name!r}: recommendation must name a ref field "
                            f"of {opt!r}")
    return {"name": name, "description": str(raw.get("description") or ""),
            "options": opt, "recommendation": rec, "fields": decl}


def return_type(ftype: str) -> Optional[str]:
    """The question type a ``returns`` field type names, or None."""
    base = ftype[:-1] if ftype.endswith("?") else ftype
    if base.startswith(RETURN_PREFIX):
        return base[len(RETURN_PREFIX):]
    return None


# ---------------------------------------------------------------------------
# validation


def _scalar(f: Dict[str, Any], v: Any, where: str, errors: List[str]) -> Any:
    t = f["type"]
    if t in ("str", "text", "name", "ref"):
        if not isinstance(v, str):
            errors.append(f"{where}: must be a string, got {type(v).__name__}")
            return v
        v = v.strip()
        if t == "str" and "\n" in v:
            errors.append(f"{where}: must be one line")
        if t == "name" and (not _NAME_RE.match(v) or v.lower() in RESERVED_NAMES):
            errors.append(f"{where}: {v!r} is not a usable option name "
                          f"(letters, digits, _ . -; not {', '.join(RESERVED_NAMES)})")
        if f["required"] and not v:
            errors.append(f"{where}: required")
        return v
    if t == "int":
        if not isinstance(v, int) or isinstance(v, bool):
            errors.append(f"{where}: must be an integer")
        return v
    if t == "bool":
        if not isinstance(v, bool):
            errors.append(f"{where}: must be true or false")
        return v
    if t == "enum":
        if v not in f["choices"]:
            errors.append(f"{where}: must be one of {', '.join(f['choices'])}, got {v!r}")
        return v
    return v


def _record(fields: List[Dict[str, Any]], value: Any, prefix: str,
            errors: List[str]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{prefix or 'brief'}: must be an object, got {type(value).__name__}")
        return {}
    known = {f["name"] for f in fields}
    for key in value:
        if key not in known:
            errors.append(f"{prefix}{key}: not a field of this question type "
                          f"(fields: {', '.join(f['name'] for f in fields)})")
    out: Dict[str, Any] = {}
    for f in fields:
        where = f"{prefix}{f['name']}"
        v = value.get(f["name"])
        if v is None or v == "" or v == []:
            if f["required"]:
                errors.append(f"{where}: required")
            continue
        if f["type"] == "list":
            if not isinstance(v, list):
                errors.append(f"{where}: must be a list")
                continue
            if "min" in f and len(v) < f["min"]:
                errors.append(f"{where}: needs at least {f['min']} items, got {len(v)}")
            if "max" in f and len(v) > f["max"]:
                errors.append(f"{where}: takes at most {f['max']} items, got {len(v)}")
            items = [_record(f["item"], it, f"{where}[{i}].", errors) for i, it in enumerate(v)]
            if any(i["name"] == "name" for i in f["item"]):
                seen: Dict[str, int] = {}
                for i, it in enumerate(items):
                    n = it.get("name")
                    if isinstance(n, str) and n:
                        if n.lower() in seen:
                            errors.append(f"{where}[{i}].name: {n!r} repeats item "
                                          f"{seen[n.lower()]} (names are compared without case)")
                        seen.setdefault(n.lower(), i)
            out[f["name"]] = items
        else:
            out[f["name"]] = _scalar(f, v, where, errors)
    return out


def validate(brief: Any, qtype: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """``(normalized, errors)`` for ``brief`` against ``qtype``. ``ref`` values
    are matched without case and stored in the option's own spelling."""
    errors: List[str] = []
    out = _record(qtype["fields"], brief, "", errors)
    for f in qtype["fields"]:
        if f["type"] != "ref" or f["name"] not in out:
            continue
        names = {str(o.get("name")).lower(): o.get("name") for o in out.get(f["of"]) or []}
        v = out[f["name"]]
        if isinstance(v, str) and v.lower() in names:
            out[f["name"]] = names[v.lower()]
        elif isinstance(v, str):
            errors.append(f"{f['name']}: {v!r} is not one of the {f['of']} names "
                          f"({', '.join(map(str, names.values())) or 'none'})")
    return out, errors


def option_names(brief: Dict[str, Any], qtype: Dict[str, Any]) -> List[str]:
    return [str(o.get("name")) for o in brief.get(qtype["options"]) or []]


def ask_options(brief: Dict[str, Any], qtype: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The ask record's ``options`` built from a validated brief: every option
    finishes the task ``ok``; ``description`` is the option's summary."""
    out = []
    for o in brief.get(qtype["options"]) or []:
        desc = o.get("summary")
        if not isinstance(desc, str):
            desc = next((str(o[f["name"]]) for f in _item_fields(qtype)
                         if f["name"] != "name" and isinstance(o.get(f["name"]), str)), "")
        out.append({"name": str(o.get("name")), "outcome": "ok", "description": desc})
    return out


def recommended(brief: Dict[str, Any], qtype: Dict[str, Any]) -> Optional[str]:
    rec = qtype.get("recommendation")
    v = brief.get(rec) if rec else None
    return v if isinstance(v, str) and v else None


def _item_fields(qtype: Dict[str, Any]) -> List[Dict[str, Any]]:
    return next(f["item"] for f in qtype["fields"] if f["name"] == qtype["options"])


# ---------------------------------------------------------------------------
# renderers


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _indent(text: str, pad: str) -> str:
    return "\n".join(pad + line if line else line for line in text.splitlines())


def render_text(brief: Dict[str, Any], qtype: Dict[str, Any]) -> str:
    """Plain text of a question, in declaration order (2.10)."""
    out: List[str] = []
    rec = recommended(brief, qtype)
    for f in qtype["fields"]:
        v = brief.get(f["name"])
        if f["name"] == qtype["options"]:
            out.append(f"{f['label']}:")
            for o in v or []:
                mark = "  (권장)" if rec and o.get("name") == rec else ""
                head = f"- {o.get('name')}{mark}"
                if isinstance(o.get("summary"), str):
                    head += f": {o['summary']}"
                out.append(head)
                for i in _item_fields(qtype):
                    if i["name"] in ("name", "summary") or o.get(i["name"]) in (None, ""):
                        continue
                    text = _fmt(o[i["name"]])
                    if "\n" in text:
                        out.append(f"    {i['label']}:")
                        out.append(_indent(text, "      "))
                    else:
                        out.append(f"    {i['label']}: {text}")
            out.append("")
        elif f["name"] == qtype.get("recommendation"):
            if v:
                out.append(f"{f['label']}: {v}")
                out.append("")
        elif v not in (None, ""):
            text = _fmt(v)
            if "\n" in text or len(text) > 60:
                out.append(f"{f['label']}:")
                out.append(_indent(text, "  "))
            else:
                out.append(f"{f['label']}: {text}")
            out.append("")
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def to_json(brief: Dict[str, Any], qtype: Dict[str, Any]) -> Dict[str, Any]:
    """The web UI shape: ``{format, sections, options, recommended}``; each
    section is ``{field, label, kind: text|table|choice, value}``."""
    rec = recommended(brief, qtype)
    sections: List[Dict[str, Any]] = []
    for f in qtype["fields"]:
        v = brief.get(f["name"])
        if f["name"] == qtype["options"]:
            cols = [{"field": i["name"], "label": i["label"]}
                    for i in _item_fields(qtype) if i["name"] != "name"]
            rows = [{"name": o.get("name"), "recommended": bool(rec and o.get("name") == rec),
                     "cells": {c["field"]: o.get(c["field"]) for c in cols}} for o in v or []]
            sections.append({"field": f["name"], "label": f["label"], "kind": "table",
                             "value": {"columns": cols, "rows": rows}})
        elif f["name"] == qtype.get("recommendation"):
            sections.append({"field": f["name"], "label": f["label"], "kind": "choice", "value": v})
        elif v not in (None, ""):
            sections.append({"field": f["name"], "label": f["label"], "kind": "text",
                             "value": _fmt(v)})
    return {"format": qtype["name"], "sections": sections,
            "options": option_names(brief, qtype), "recommended": rec}


def _guide_line(f: Dict[str, Any], pad: str) -> List[str]:
    extra = []
    if f["type"] == "enum":
        extra.append("값: " + " | ".join(f["choices"]))
    if f["type"] == "ref":
        extra.append(f"{f['of']}의 name 중 하나")
    if "min" in f:
        extra.append(f"최소 {f['min']}개")
    if "max" in f:
        extra.append(f"최대 {f['max']}개")
    req = "필수" if f["required"] else "선택"
    text = f"{pad}- {f['label']}({f['name']}, {f['type']}, {req})"
    if f["help"]:
        text += f": {f['help']}"
    if extra:
        text += f" [{'; '.join(extra)}]"
    lines = [text]
    for i in f.get("item") or []:
        lines.extend(_guide_line(i, pad + "  "))
    return lines


def _skeleton(fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for f in fields:
        if f["type"] == "list":
            out[f["name"]] = [_skeleton(f["item"])]
        elif f["type"] == "int":
            out[f["name"]] = 0
        elif f["type"] == "bool":
            out[f["name"]] = False
        elif f["type"] == "enum":
            out[f["name"]] = f["choices"][0]
        else:
            out[f["name"]] = "…"
    return out


def guide(qtype: Dict[str, Any]) -> str:
    """The authoring guide generated from the declaration (2.10)."""
    lines = [f"질문 형식 {qtype['name']}: {qtype['description']}".rstrip(": "), "", "항목:"]
    for f in qtype["fields"]:
        lines.extend(_guide_line(f, ""))
    lines += ["", "보고 형식(JSON):",
              json.dumps(_skeleton(qtype["fields"]), ensure_ascii=False, indent=2)]
    return "\n".join(lines)
