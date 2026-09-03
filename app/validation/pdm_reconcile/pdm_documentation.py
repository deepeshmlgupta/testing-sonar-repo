"""
PDM Documentation Mapping
=========================
Builds the side-by-side documentation rows for the PDM report's DOCUMENTATION
sheet, mirroring what cdm_reconcile/documentation.py does for conceptual models.

Unlike FINDINGS — which only ever shows what went wrong — this lists MATCHED
rows too, so a reviewer can tell an object whose documentation was verified
identical from one that was never checked at all.

The mappings are the ones confirmed at the UI level between the two tools:

    SAP PD Comment      → erwin Comment          (always; a physical model's
                                                  Comment is carried straight
                                                  into erwin's Comment field
                                                  and is visible there in the
                                                  erwin UI)
    SAP PD Description  → erwin Definition       (Definition tab, Description
                                                  sub-tab)
    SAP PD Annotation   → erwin Extended Notes   (Definition tab, Annotation
                                                  sub-tab)

PDM differs from CDM and LDM on the Comment mapping only: there is no
Comment→Note preprocessing step for physical models, so Comments are validated
directly against erwin's own Comment field.  The two Definition-tab mappings
are identical across all three model types.

Reading the two source files directly keeps the PDM validator's own modules
untouched: nothing here imports from pdm_erwin_validator, and the parsed model
dicts it produces do not carry documentation fields.
"""

import difflib
import logging
import os
import re
import xml.etree.ElementTree as ET  # nosec B405
from defusedxml.ElementTree import parse as safe_parse, iterparse as safe_iterparse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── STATUS VOCABULARY (identical to the CDM/LDM documentation sheets) ────────
MATCHED = "MATCHED"
MISMATCH = "MISMATCH"
MISSING_IN_ERWIN = "MISSING_IN_ERWIN"
MISSING_IN_SAP_PD = "MISSING_IN_SAP_PD"
BOTH_EMPTY = "BOTH_EMPTY"

VALUE_DISPLAY_LIMIT = 500

COMMENT_TO_COMMENT = "Comment → Comment"
DESCRIPTION_TO_DEFINITION = "Description → Definition"
ANNOTATION_TO_EXTENDED_NOTES = "Annotation → Extended Notes"

# PowerDesigner namespaces.
_PD_NS = {"o": "object", "a": "attribute", "c": "collection"}


@dataclass
class DocumentationRow:
    """One object, one mapping, both sides."""
    model: str = ""
    object_type: str = ""          # TABLE | COLUMN
    object_name: str = ""          # table name, or "Table.Column"
    object_code: str = ""
    mapping: str = ""
    source_field: str = ""         # e.g. "SAP PD Comment"
    target_field: str = ""         # e.g. "erwin Definition"
    source_value: str = ""
    target_value: str = ""
    status: str = ""
    similarity: float = 0.0        # 0-100, only meaningful when both populated

    @property
    def is_problem(self) -> bool:
        return self.status in (MISMATCH, MISSING_IN_ERWIN, MISSING_IN_SAP_PD)


# ─── TEXT NORMALISATION ───────────────────────────────────────────────────────

_RTF_HEAD = re.compile(r"^\s*\{\\rtf", re.IGNORECASE)
_WS = re.compile(r"\s+")


def _strip_rtf(text: str) -> str:
    """PowerDesigner sometimes stores rich text; compare the plain words only."""
    if not text or not _RTF_HEAD.match(text):
        return text or ""
    plain = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
    plain = re.sub(r"\\[a-zA-Z]+-?\d*\s?", " ", plain)
    plain = plain.replace("{", " ").replace("}", " ")
    return plain


def _clean(text: str) -> str:
    return _WS.sub(" ", _strip_rtf(text or "")).strip()


def _comparable(text: str) -> str:
    """Case- and whitespace-insensitive form used for the equality test."""
    return _clean(text).casefold()


def _truncate(text: str, limit: int = VALUE_DISPLAY_LIMIT) -> str:
    text = _clean(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _classify(source_text: str, target_text: str) -> Tuple[str, float]:
    """Return (status, similarity) for one side-by-side pair."""
    source_cmp, target_cmp = _comparable(source_text), _comparable(target_text)

    if not source_cmp and not target_cmp:
        return BOTH_EMPTY, 0.0
    if source_cmp and not target_cmp:
        return MISSING_IN_ERWIN, 0.0
    if target_cmp and not source_cmp:
        return MISSING_IN_SAP_PD, 0.0
    if source_cmp == target_cmp:
        return MATCHED, 100.0

    ratio = difflib.SequenceMatcher(None, source_cmp, target_cmp).ratio()
    return MISMATCH, round(ratio * 100, 2)


def _row(model: str, object_type: str, object_name: str, object_code: str,
         mapping: str, source_field: str, target_field: str,
         source_text: str, target_text: str) -> DocumentationRow:
    status, similarity = _classify(source_text, target_text)
    return DocumentationRow(
        model=model, object_type=object_type, object_name=object_name,
        object_code=object_code, mapping=mapping,
        source_field=source_field, target_field=target_field,
        source_value=_truncate(source_text), target_value=_truncate(target_text),
        status=status, similarity=similarity,
    )


# ─── POWERDESIGNER SIDE ───────────────────────────────────────────────────────

def _pd_tag(tag: str) -> str:
    prefix, local = tag.split(":")
    return f"{{{_PD_NS[prefix]}}}{local}"


def _pd_attr(elem: ET.Element, name: str) -> str:
    """
    Value of a PD scalar field, accepting both the plain (<a:Comment>) and the
    class-qualified (<a:Table.Comment>) spellings PowerDesigner uses.
    """
    uri = _PD_NS["a"]
    suffix = "." + name
    for child in list(elem):
        tag = child.tag
        if isinstance(tag, str) and tag.startswith(f"{{{uri}}}"):
            local = tag.split("}", 1)[1]
            if local == name or local.endswith(suffix):
                if child.text and child.text.strip():
                    return child.text.strip()
    return ""


def _parse_pd_documentation(path: str) -> Dict[str, Any]:
    """{'model': str, 'tables': {CODE: {...}}, 'columns': {(TBL, COL): {...}}}"""
    out: Dict[str, Any] = {"model": "", "tables": {}, "columns": {}}
    try:
        root = safe_parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        logger.warning("Cannot read documentation from %s: %s", path, exc)
        return out

    model_elem = root.find(f".//{_pd_tag('o:Model')}")
    if model_elem is not None:
        out["model"] = _pd_attr(model_elem, "Name")

    for table in root.iter(_pd_tag("o:Table")):
        if table.get("Ref"):
            continue
        name = _pd_attr(table, "Name")
        code = (_pd_attr(table, "Code") or name).upper()
        if not code:
            continue
        out["tables"][code] = {
            "name": name or code,
            "code": code,
            "comment": _pd_attr(table, "Comment"),
            "description": _pd_attr(table, "Description"),
            "annotation": _pd_attr(table, "Annotation"),
        }
        for column in table.iter(_pd_tag("o:Column")):
            if column.get("Ref"):
                continue
            col_name = _pd_attr(column, "Name")
            col_code = (_pd_attr(column, "Code") or col_name).upper()
            if not col_code:
                continue
            out["columns"][(code, col_code)] = {
                "name": col_name or col_code,
                "code": col_code,
                "comment": _pd_attr(column, "Comment"),
                "description": _pd_attr(column, "Description"),
                "annotation": _pd_attr(column, "Annotation"),
            }
    return out


# ─── ERWIN SIDE ───────────────────────────────────────────────────────────────

def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _props(elem: ET.Element) -> Optional[ET.Element]:
    for child in list(elem):
        if _local(child.tag).endswith("Props"):
            return child
    return None


def _erwin_val(elem: ET.Element, *names: str) -> str:
    """Scalar from an XML attribute, a direct child, or the <XxxProps> wrapper."""
    for name in names:
        raw = elem.get(name)
        if raw and raw.strip():
            return raw.strip()
    for name in names:
        for child in list(elem):
            if _local(child.tag) == name and child.text and child.text.strip():
                return child.text.strip()
    props = _props(elem)
    if props is not None:
        for name in names:
            for child in list(props):
                if _local(child.tag) == name and child.text and child.text.strip():
                    return child.text.strip()
    return ""


def _erwin_note(elem: ET.Element) -> str:
    """
    Text of an object's Note, when the export carries one.  erwin stores notes
    in a Note_List_Array / Note_List structure whose payload field differs
    between releases, so several field names are accepted.
    """
    for node in elem.iter():
        if not _local(node.tag).startswith("Note_List"):
            continue
        text = _erwin_val(node, "Note_Text", "Note", "Text", "Comment", "Value")
        if text:
            return text
    return ""


def _erwin_extended_notes(elem: ET.Element) -> str:
    """
    Text of an object's Extended Notes, when the export carries any.

    Structurally unrelated to the Notes tab above: the payload sits in
    <Extended_Notes_Groups>/<Extended_Notes>/<Extended_NotesProps>/<Comment>
    as plain text, and an object may carry several.  Read from DIRECT children
    only, so reading an Entity never picks up an Attribute's Extended Notes.
    """
    texts = []
    containers = [elem]
    props = _props(elem)
    if props is not None:
        containers.append(props)
    for container in containers:
        for child in container:
            if _local(child.tag) != "Extended_Notes_Groups":
                continue
            for note in child:
                if _local(note.tag) != "Extended_Notes":
                    continue
                text = _erwin_val(note, "Comment")
                if text.strip():
                    texts.append(text.strip())
    return "\n".join(texts)


def _parse_erwin_documentation(path: str) -> Dict[str, Any]:
    """{'model': str, 'tables': {CODE: {...}}, 'columns': {(TBL, COL): {...}}}"""
    out: Dict[str, Any] = {"model": "", "tables": {}, "columns": {},
                           "has_notes": False}
    try:
        root = safe_parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        logger.warning("Cannot read documentation from %s: %s", path, exc)
        return out

    for candidate in root.iter():
        if _local(candidate.tag) == "Model" and candidate.get("id"):
            out["model"] = (candidate.get("name")
                            or _erwin_val(candidate, "Name", "Model_Name"))
            break

    for entity in root.iter():
        if _local(entity.tag) != "Entity" or not entity.get("id"):
            continue
        name = _erwin_val(entity, "Name") or entity.get("name", "")
        code = (_erwin_val(entity, "Physical_Name") or name).upper()
        if not code or code in out["tables"]:
            continue
        note = _erwin_note(entity)
        if note:
            out["has_notes"] = True
        out["tables"][code] = {
            "name": name or code,
            "code": code,
            "definition": _erwin_val(entity, "Definition"),
            "comment": _erwin_val(entity, "Comment"),
            "note": note,
            "extended_notes": _erwin_extended_notes(entity),
        }
        for attribute in entity.iter():
            if _local(attribute.tag) != "Attribute" or not attribute.get("id"):
                continue
            col_name = _erwin_val(attribute, "Name") or attribute.get("name", "")
            col_code = (_erwin_val(attribute, "Physical_Name") or col_name).upper()
            if not col_code:
                continue
            col_note = _erwin_note(attribute)
            if col_note:
                out["has_notes"] = True
            out["columns"].setdefault((code, col_code), {
                "name": col_name or col_code,
                "code": col_code,
                "definition": _erwin_val(attribute, "Definition"),
                "comment": _erwin_val(attribute, "Comment"),
                "note": col_note,
                "extended_notes": _erwin_extended_notes(attribute),
            })
    return out


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def build_rows(pd_path: str, erwin_path: str,
               model_name: str = "") -> List[DocumentationRow]:
    """
    Side-by-side documentation rows for every table and column found on either
    side.  Report-only: nothing here feeds a finding, the fidelity score or the
    PASS/WARN/FAIL status.  Never raises — an unreadable file yields no rows.
    """
    try:
        pd_doc = _parse_pd_documentation(pd_path)
        erwin_doc = _parse_erwin_documentation(erwin_path)
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("Documentation mapping unavailable for %s: %s",
                       pd_path, exc)
        return []

    model = (model_name or pd_doc.get("model")
             or os.path.splitext(os.path.basename(pd_path))[0])

    # Only report the two Definition-tab mappings when the data actually uses
    # them, so the sheet does not fill with BOTH_EMPTY rows for a sub-tab this
    # pair never populates. Comment → Comment is always reported: it is the
    # PDM Comment mapping and its absence would itself be the finding.
    def _pd_side_uses(field: str) -> bool:
        return (any(_comparable(t.get(field, "")) for t in pd_doc["tables"].values())
                or any(_comparable(c.get(field, "")) for c in pd_doc["columns"].values()))

    # Gated on the SAP PD side only, as before. erwin's Definition is filled by
    # the importer from the PD Comment, so a model with no Descriptions at all
    # would otherwise report every object as "Description missing in SAP PD" --
    # 152 rows describing a mapping this model never uses.
    use_description = _pd_side_uses("description")
    use_annotation = _pd_side_uses("annotation")

    rows: List[DocumentationRow] = []

    def emit(object_type: str, object_name: str, object_code: str,
             pd_object: Dict[str, Any], erwin_object: Dict[str, Any]) -> None:
        pd_comment = pd_object.get("comment", "")
        pd_description = pd_object.get("description", "")
        pd_annotation = pd_object.get("annotation", "")
        er_definition = erwin_object.get("definition", "")
        er_comment = erwin_object.get("comment", "")
        er_extended_notes = erwin_object.get("extended_notes", "")

        # PDM validates Comments straight against erwin's Comment field. There
        # is no Comment→Note preprocessing step for physical models, so Note is
        # not a carrier this mapping can be measured against.
        pairs = [
            (COMMENT_TO_COMMENT, "SAP PD Comment", "erwin Comment",
             pd_comment, er_comment),
        ]
        if use_description:
            pairs.append((DESCRIPTION_TO_DEFINITION, "SAP PD Description",
                          "erwin Definition", pd_description, er_definition))
        if use_annotation:
            pairs.append((ANNOTATION_TO_EXTENDED_NOTES, "SAP PD Annotation",
                          "erwin Extended Notes", pd_annotation,
                          er_extended_notes))

        for mapping, source_field, target_field, source, target in pairs:
            rows.append(_row(model, object_type, object_name, object_code,
                             mapping, source_field, target_field,
                             source, target))

    empty: Dict[str, Any] = {}

    for code in sorted(set(pd_doc["tables"]) | set(erwin_doc["tables"])):
        pd_table = pd_doc["tables"].get(code, empty)
        er_table = erwin_doc["tables"].get(code, empty)
        display = pd_table.get("name") or er_table.get("name") or code
        emit("TABLE", display, code, pd_table, er_table)

    for key in sorted(set(pd_doc["columns"]) | set(erwin_doc["columns"])):
        table_code, column_code = key
        pd_column = pd_doc["columns"].get(key, empty)
        er_column = erwin_doc["columns"].get(key, empty)
        # Columns with documentation on neither side would triple the sheet for
        # no information, so they are left out; tables are always listed.
        if not any(_comparable(value) for value in (
                pd_column.get("comment", ""), pd_column.get("description", ""),
                pd_column.get("annotation", ""),
                er_column.get("definition", ""), er_column.get("comment", ""),
                er_column.get("extended_notes", ""))):
            continue
        display = (f"{table_code}."
                   f"{pd_column.get('name') or er_column.get('name') or column_code}")
        emit("COLUMN", display, column_code, pd_column, er_column)

    return rows


def summarise(rows: List[DocumentationRow]) -> Dict[str, Dict[str, int]]:
    """{mapping: {status: count}} — the DOCUMENTATION sheet's summary block."""
    summary: Dict[str, Dict[str, int]] = {}
    for row in rows:
        bucket = summary.setdefault(row.mapping, {})
        bucket[row.status] = bucket.get(row.status, 0) + 1
    return summary
