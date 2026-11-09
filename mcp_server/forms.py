"""Moveworks forms (modals) as data an MCP client can fill in.

A form is fetched with GetModal, shown to the model as a field list, filled
from a ``{field: value}`` map, and sent with SubmitModal. Value shapes per
field type follow the Conversations API: TEXT ``text``, DATE ``date``,
DATETIME ``datetime``, BOOLEAN ``bool``, CHOICE ``option`` / ``options``.
FILE fields can't be set here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from moveworks_capi.rendering import clean_label, modal_html_to_markdown

SETTABLE = ("TEXT", "DATE", "DATETIME", "BOOLEAN", "CHOICE")
PORTAL_LINK = re.compile(r'href="([^"]+)"[^>]*>[^<]*portal', re.IGNORECASE)


class FormInputError(Exception):
    """A value the form can't take; the message says how to fix it."""


@dataclass
class PendingForm:
    option_id: str
    path: str
    name: str
    fields: List[Dict[str, Any]]
    values: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    portal_url: Optional[str] = None


def form_name(modal: Dict[str, Any], fallback: str) -> str:
    """The HEADER display block names catalog forms; the title is a generic "New request"."""
    for item in modal.get("fields") or []:
        text = ((item.get("display") or {}).get("text") or {})
        if item.get("type") == "DISPLAY" and text.get("style") == "HEADER" and text.get("text"):
            return clean_label(modal_html_to_markdown(text["text"]))
    for item in modal.get("fields") or []:
        text = ((item.get("display") or {}).get("text") or {}).get("text")
        if item.get("type") == "DISPLAY" and text:
            return clean_label(modal_html_to_markdown(text))
    return clean_label(modal.get("title") or fallback)


def open_form(option_id: str, path: str, modal: Dict[str, Any], fallback_name: str) -> PendingForm:
    fields = modal.get("fields") or []
    form = PendingForm(option_id=option_id, path=path, name=form_name(modal, fallback_name), fields=fields)
    for item in _inputs(fields):
        prefill = _prefill(item)
        if prefill is not None:
            form.values[item["name"]] = prefill
    for item in fields:
        match = PORTAL_LINK.search(((item.get("display") or {}).get("text") or {}).get("text") or "")
        if match:
            form.portal_url = match.group(1)
    return form


def describe(form: PendingForm) -> Dict[str, Any]:
    details = []
    for item in form.fields:
        text = ((item.get("display") or {}).get("text") or {}).get("text")
        if item.get("type") == "DISPLAY" and item.get("visible", True) and text:
            line = modal_html_to_markdown(text).strip()
            if line and line != form.name and "portal" not in line.lower():
                details.append(line)
    return {
        "name": form.name,
        "details": details,
        "fields": [_field_view(item, form.values.get(item["name"])) for item in _inputs(form.fields)],
        "portal_url": form.portal_url,
        "blockers": blockers(form),
    }


def render(form: PendingForm) -> str:
    view = describe(form)
    lines = [f"**{view['name']}**"] + view["details"] + [""]
    for item in view["fields"]:
        flag = " (required)" if item["required"] else ""
        if not item["settable"]:
            shown = f"_{item['note']}_"
        else:
            shown = item["value"] if item["value"] not in (None, "") else "_(empty)_"
        choices = f" Choices: {', '.join(c['label'] for c in item['choices'])}." if item["choices"] else ""
        lines.append(f"- **{item['label']}** [`{item['name']}`, {item['type'].lower()}]{flag}: {shown}{choices}")
    lines.append("")
    if view["blockers"]:
        where = f" Complete it on the portal: {view['portal_url']}" if view["portal_url"] else ""
        lines.append("This form can't be submitted from here: " + "; ".join(view["blockers"]) + "." + where)
    else:
        lines.append("To send it, call submit_moveworks_form with values for the fields to set (by field name), "
                     "or action \"cancel\".")
    return "\n".join(lines)


def apply_values(form: PendingForm, values: Dict[str, Any]) -> None:
    by_key: Dict[str, Dict[str, Any]] = {}
    for item in _inputs(form.fields):
        by_key[_key(item["name"])] = item
        by_key[_key(_label(item))] = item
    for key, raw in (values or {}).items():
        item = by_key.get(_key(str(key)))
        if item is None:
            raise FormInputError(f"'{key}' is not a field on this form.")
        if not _settable(item):
            raise FormInputError(f"'{_label(item)}' can't be set from here.")
        form.values[item["name"]] = _encode(item, raw)


def blockers(form: PendingForm) -> List[str]:
    """Reasons this form can't be submitted from here, regardless of the values given."""
    problems = []
    for item in _inputs(form.fields):
        if not item.get("required"):
            continue
        choice = item.get("choice") or {}
        if item.get("type") == "FILE" and not form.values.get(item["name"]):
            problems.append(f"'{_label(item)}' needs a file attachment")
        elif item.get("type") == "CHOICE" and not choice.get("options") and not choice.get("allows_free_input"):
            # A required CHOICE with no listed choices and no free input (for example a user
            # reference): this server has no value it can send, so the user finishes in the portal.
            problems.append(f"'{_label(item)}' is a lookup field this server can't fill in")
    return problems


def missing_required(form: PendingForm) -> List[str]:
    return [_label(item) for item in _inputs(form.fields)
            if item.get("required") and _settable(item) and not form.values.get(item["name"])]


def _key(text: str) -> str:
    """Field lookup ignores case, spaces and punctuation: models write "Longdescription" or "form_data.description"."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _inputs(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [f for f in fields if f.get("name") and f.get("type") != "DISPLAY" and f.get("visible", True)]


def _label(item: Dict[str, Any]) -> str:
    return clean_label(item.get("label") or item.get("name") or "")


def _settable(item: Dict[str, Any]) -> bool:
    if item.get("type") not in SETTABLE or item.get("read_only"):
        return False
    if item.get("type") == "CHOICE":
        choice = item.get("choice") or {}
        return bool(choice.get("options") or choice.get("allows_free_input"))
    return True


def _prefill(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kind = item.get("type")
    body = item.get(kind.lower()) if kind else None
    body = body or {}
    if kind == "TEXT" and body.get("prefill"):
        return {"text": body["prefill"]}
    if kind in ("DATE", "DATETIME") and body.get("prefill"):
        return {kind.lower(): body["prefill"]}
    if kind == "BOOLEAN" and "prefill" in body:
        return {"bool": bool(body["prefill"])}
    if kind == "CHOICE" and body.get("prefill"):
        chosen = [p.get("value") for p in body["prefill"] if p.get("value")]
        if chosen:
            return {"options": chosen} if body.get("multi") else {"option": chosen[0]}
    return None


def _encode(item: Dict[str, Any], raw: Any) -> Dict[str, Any]:
    kind = item["type"]
    if kind == "TEXT":
        return {"text": str(raw)}
    if kind in ("DATE", "DATETIME"):
        return {kind.lower(): str(raw)}
    if kind == "BOOLEAN":
        if isinstance(raw, bool):
            return {"bool": raw}
        text = str(raw).strip().lower()
        if text in ("true", "yes", "y", "1"):
            return {"bool": True}
        if text in ("false", "no", "n", "0"):
            return {"bool": False}
        raise FormInputError(f"'{_label(item)}' takes yes or no.")
    choice = item.get("choice") or {}
    wanted = raw if isinstance(raw, list) else [raw]
    values = [_choice_value(item, choice, str(w)) for w in wanted]
    if choice.get("multi"):
        return {"options": values}
    if len(values) != 1:
        raise FormInputError(f"'{_label(item)}' takes one choice.")
    return {"option": values[0]}


def _choice_value(item: Dict[str, Any], choice: Dict[str, Any], wanted: str) -> str:
    target = wanted.strip().lower()
    for option in choice.get("options") or []:
        if target in (str(option.get("value", "")).lower(), str(option.get("display_value", "")).lower()):
            return option["value"]
    if choice.get("allows_free_input"):
        return wanted
    labels = ", ".join(o.get("display_value") or o.get("value") for o in choice.get("options") or [])
    raise FormInputError(f"'{wanted}' is not a choice for '{_label(item)}'. Choices: {labels}.")


def _field_view(item: Dict[str, Any], value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    choice = item.get("choice") or {}
    settable = _settable(item)
    note = None
    if not settable:
        note = "needs a file; can't be set here" if item.get("type") == "FILE" else "can't be set from here"
    return {
        "name": item["name"],
        "label": _label(item),
        "type": item.get("type"),
        "required": bool(item.get("required")),
        "settable": settable,
        "value": _display(item, value),
        "choices": [{"value": o.get("value"), "label": o.get("display_value") or o.get("value")}
                    for o in choice.get("options") or []],
        "multi": bool(choice.get("multi")),
        "note": note,
    }


def _display(item: Dict[str, Any], value: Optional[Dict[str, Any]]) -> Any:
    if not value:
        return None
    if "text" in value:
        return value["text"]
    if "bool" in value:
        return "yes" if value["bool"] else "no"
    for key in ("date", "datetime"):
        if key in value:
            return value[key]
    choice = item.get("choice") or {}
    labels = {o.get("value"): o.get("display_value") for o in (choice.get("options") or []) + (choice.get("prefill") or [])}
    chosen = value.get("options") or [value.get("option")]
    return ", ".join(labels.get(v) or str(v) for v in chosen)
