"""
PowerDesigner PDM Parser
------------------------
Parses a SAP PowerDesigner Physical Data Model (.pdm) XML file and returns a
structured Python dict representing tables, columns, PKs, FKs, and indexes.

PDM files use three XML namespaces:
  o:  objects   (o:Table, o:Column, o:Key, o:Reference, o:Index …)
  a:  attributes (a:Name, a:Code, a:DataType …)
  c:  collections (c:Columns, c:Keys, c:References …)

The Id/Ref attribute scheme is used for cross-references (foreign keys,
key-column membership, index-column membership).
"""

import xml.etree.ElementTree as ET  # nosec B405
from defusedxml.ElementTree import parse as safe_parse, iterparse as safe_iterparse
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# XML namespaces used in PDM files
OBJECT_TABLE = "o:Table"
OBJECT_COLUMN = "o:Column"

NS = {
    "o": "object",
    "a": "attribute",
    "c": "collection",
}

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _ns(tag: str) -> str:
    """Return tag wrapped in its namespace URI for ElementTree find()."""
    prefix, local = tag.split(":")
    return f"{{{NS[prefix]}}}{local}"


def _attr(elem, name: str, default: str = "") -> str:
    """
    Get the text of a child attribute element.

    PowerDesigner writes scalar fields two ways depending on version/field:
      * plain               <a:Name>…</a:Name>
      * class-qualified     <a:Column.Mandatory>…</a:Column.Mandatory>,
                            <a:Table.Owner>…</a:Table.Owner>
    The original parser only looked for the plain form, so class-qualified
    fields (notably Column.Mandatory — the NOT NULL flag) were never read and
    every column looked nullable.  This now matches either form, then falls
    back to an XML attribute of the same name.
    """
    # 1. exact <a:name>
    child = elem.find(_ns(f"a:{name}"))
    if child is not None and child.text and child.text.strip():
        return child.text.strip()

    # 2. class-qualified <a:Something.name>  (direct children only)
    attr_uri = NS["a"]
    suffix = "." + name
    for ch in elem:
        tag = ch.tag
        if isinstance(tag, str) and tag.startswith(f"{{{attr_uri}}}"):
            local = tag.split("}", 1)[1]
            if local == name or local.endswith(suffix):
                if ch.text and ch.text.strip():
                    return ch.text.strip()

    # 3. XML attribute fallback (very old PDM files)
    return elem.get(name, default)


def _get_name(elem) -> str:
    return _attr(elem, "Name") or elem.get("Name", "")


def _get_code(elem) -> str:
    return _attr(elem, "Code") or elem.get("Code", "") or _get_name(elem)


def _build_id_map(root: ET.Element) -> Dict[str, ET.Element]:
    """Build a map of Id → element for all objects in the file."""
    id_map: Dict[str, ET.Element] = {}
    for elem in root.iter():
        eid = elem.get("Id")
        if eid:
            id_map[eid] = elem
    return id_map


# ─── PARSE FUNCTIONS ──────────────────────────────────────────────────────────

def _parse_column(col_elem: ET.Element) -> Dict[str, Any]:
    dt = _attr(col_elem, "DataType")
    length = _attr(col_elem, "Length")
    precision = _attr(col_elem, "Precision")

    # Build full data type string if length/precision are separate
    if length and "(" not in dt:
        if precision:
            dt = f"{dt}({length},{precision})"
        else:
            dt = f"{dt}({length})"

    mandatory = _attr(col_elem, "Mandatory")
    not_null = mandatory in ("1", "true", "True", "yes")

    return {
        "id":           col_elem.get("Id", ""),
        "name":         _get_name(col_elem),
        "code":         _get_code(col_elem),      # physical name
        "data_type":    dt,
        "not_null":     not_null,
        "default":      _attr(col_elem, "DefaultValue"),
        "comment":      _attr(col_elem, "Comment"),
    }


def _parse_key(key_elem: ET.Element, id_map: Dict[str, ET.Element]) -> Dict[str, Any]:
    is_pk = _attr(key_elem, "PrimaryKey") in ("1", "true", "True")
    is_unique = _attr(key_elem, "UniqueConstraint") in ("1", "true", "True")

    col_codes: List[str] = []
    for ref in key_elem.iter(_ns(OBJECT_COLUMN)):
        ref_id = ref.get("Ref")
        if ref_id and ref_id in id_map:
            col_codes.append(_get_code(id_map[ref_id]))

    return {
        "id":         key_elem.get("Id", ""),
        "name":       _get_name(key_elem),
        "code":       _get_code(key_elem),
        "is_pk":      is_pk,
        "is_unique":  is_unique,
        "columns":    col_codes,
    }


def _parse_index(idx_elem: ET.Element, id_map: Dict[str, ET.Element]) -> Dict[str, Any]:
    is_unique = _attr(idx_elem, "Unique") in ("1", "true", "True")

    col_codes: List[str] = []
    seen = set()
    # Two possible structures in different PD versions; dedupe so a column that
    # appears under both <c:Column> and a nested ref is not counted twice.
    for ic in idx_elem.iter(_ns("o:IndexColumn")):
        for ref in ic.iter(_ns(OBJECT_COLUMN)):
            ref_id = ref.get("Ref")
            if ref_id and ref_id in id_map and ref_id not in seen:
                seen.add(ref_id)
                col_codes.append(_get_code(id_map[ref_id]))

    # PowerDesigner auto-creates an index to back every primary key, alternate
    # key and foreign key, linking it to that object via <c:LinkedObject>.  These
    # "mirror" indexes are not user-defined indexes — they duplicate keys/FKs that
    # are already compared elsewhere — and erwin does not export them as indexes,
    # so comparing them produces hundreds of phantom "index only in PowerDesigner"
    # rows.  Flag them so the comparator can skip them.
    is_mirror = idx_elem.find(_ns("c:LinkedObject")) is not None

    return {
        "name":      _get_name(idx_elem),
        "code":      _get_code(idx_elem),
        "is_unique": is_unique,
        "columns":   col_codes,
        "mirror":    is_mirror,
    }


def _parse_table(tbl_elem: ET.Element, id_map: Dict[str, ET.Element]) -> Dict[str, Any]:
    columns: List[Dict] = []
    col_id_to_code: Dict[str, str] = {}

    # Columns
    for col in tbl_elem.iter(_ns(OBJECT_COLUMN)):
        if col.get("Ref"):   # skip cross-references
            continue
        parsed = _parse_column(col)
        columns.append(parsed)
        col_id_to_code[parsed["id"]] = parsed["code"]

    # Which key is the PRIMARY key?  PowerDesigner marks this with a table-level
    # <c:PrimaryKey><o:Key Ref="…"/></c:PrimaryKey> reference, NOT with a flag on
    # the key itself — so is_pk was always False before and no PK was ever
    # compared.  Resolve that reference here.
    pk_ref_id = None
    pk_container = tbl_elem.find(_ns("c:PrimaryKey"))
    if pk_container is not None:
        kref = pk_container.find(_ns("o:Key"))
        if kref is not None:
            pk_ref_id = kref.get("Ref")

    # Keys (PK + AK)
    keys: List[Dict] = []
    for key in tbl_elem.iter(_ns("o:Key")):
        if key.get("Ref"):
            continue
        parsed_key = _parse_key(key, id_map)
        if pk_ref_id and key.get("Id") == pk_ref_id:
            parsed_key["is_pk"] = True
        keys.append(parsed_key)

    # Indexes
    indexes: List[Dict] = []
    for idx in tbl_elem.iter(_ns("o:Index")):
        if idx.get("Ref"):
            continue
        indexes.append(_parse_index(idx, id_map))

    return {
        "id":       tbl_elem.get("Id", ""),
        "name":     _get_name(tbl_elem),
        "code":     _get_code(tbl_elem),          # physical table name
        "owner":    _attr(tbl_elem, "Owner"),
        "columns":  columns,
        "keys":     keys,
        "indexes":  indexes,
        "col_id_map": col_id_to_code,             # id → physical name (used for FKs)
    }


def _first_table_ref(ref_elem: ET.Element, container_tag: str) -> Optional[str]:
    """Return the referenced table ID from a parent/child table container."""
    container = ref_elem.find(_ns(container_tag))
    if container is None:
        return None
    table = container.find(_ns(OBJECT_TABLE))
    return table.get("Ref") if table is not None else None


def _table_codes(
    ref_elem: ET.Element,
    tbl_id_to_code: Dict[str, str],
) -> tuple:
    """Resolve parent and child table references to physical table codes."""
    parent_id = _first_table_ref(ref_elem, "c:ParentTable")
    child_id = _first_table_ref(ref_elem, "c:ChildTable")
    parent_code = tbl_id_to_code.get(parent_id, parent_id or "UNKNOWN")
    child_code = tbl_id_to_code.get(child_id, child_id or "UNKNOWN")
    return parent_code, child_code


def _column_code(ref_id: Optional[str], id_map: Dict[str, ET.Element]) -> str:
    """Resolve a PowerDesigner column reference to its physical code."""
    if ref_id and ref_id in id_map:
        return _get_code(id_map[ref_id])
    return ref_id or ""


def _split_join_columns(
    join: ET.Element,
) -> tuple:
    """Return the first two column reference IDs from a ReferenceJoin."""
    refs = [
        obj.get("Ref")
        for obj in join.findall(f".//{_ns(OBJECT_COLUMN)}")
        if obj.get("Ref") is not None
    ]
    return (refs[0] if refs else None, refs[1] if len(refs) > 1 else None)


def _parse_reference_joins(
    ref_elem: ET.Element,
    id_map: Dict[str, ET.Element],
) -> List[Dict[str, str]]:
    """Parse all column joins in a foreign-key reference."""
    join_cols: List[Dict[str, str]] = []
    for join in ref_elem.iter(_ns("o:ReferenceJoin")):
        parent_ref, child_ref = _split_join_columns(join)
        join_cols.append({
            "parent_col": _column_code(parent_ref, id_map),
            "child_col": _column_code(child_ref, id_map),
        })
    return join_cols


def _parse_reference(
    ref_elem: ET.Element,
    id_map: Dict[str, ET.Element],
    tbl_id_to_code: Dict[str, str],
) -> Dict[str, Any]:
    """Parse a foreign-key Reference element."""
    parent_code, child_code = _table_codes(ref_elem, tbl_id_to_code)
    return {
        "name": _get_name(ref_elem),
        "code": _get_code(ref_elem),
        "parent_table": parent_code,
        "child_table": child_code,
        "join_columns": _parse_reference_joins(ref_elem, id_map),
    }


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def parse_pdm(filepath: str) -> Dict[str, Any]:
    """
    Parse a PowerDesigner .pdm file.

    Returns
    -------
    {
        "source_file": str,
        "model_name":  str,
        "tables": {
            "<PHYSICAL_TABLE_NAME>": {
                "code":     str,
                "name":     str,
                "columns":  [ {code, name, data_type, not_null, default} … ],
                "keys":     [ {name, code, is_pk, is_unique, columns:[code…]} … ],
                "indexes":  [ {name, code, is_unique, columns:[code…]} … ],
            }
        },
        "references": [
            {name, code, parent_table, child_table, join_columns:[{parent_col, child_col}]}
        ]
    }
    """
    try:
        tree = safe_parse(filepath)
        root = tree.getroot()
    except ET.ParseError as e:
        logger.error("XML parse error in %s: %s", filepath, e)
        return {"source_file": filepath, "error": str(e), "tables": {}, "references": []}

    id_map = _build_id_map(root)

    # Find the top-level Model element (works regardless of nesting depth)
    model_elem = root.find(f".//{_ns('o:Model')}")
    model_name = _get_name(model_elem) if model_elem is not None else ""

    # ── Tables ────────────────────────────────────────────────────────────────
    tables: Dict[str, Any] = {}
    tbl_id_to_code: Dict[str, str] = {}
    # Two tables can share a physical name in one PDM (different owners /
    # packages / schemas). Assigning straight into ``tables[code]`` overwrote
    # the first one, so the model silently lost a table AND every column in it
    # before the comparison even started — which shows up downstream as
    # "columns present in PowerDesigner are missing in erwin". Keep the first
    # definition, record the collision, and let the comparator report it.
    duplicate_tables: Dict[str, int] = {}

    for tbl in root.iter(_ns(OBJECT_TABLE)):
        if tbl.get("Ref"):
            continue
        parsed_tbl = _parse_table(tbl, id_map)
        code = parsed_tbl["code"].upper()
        if code in tables:
            duplicate_tables[code] = duplicate_tables.get(code, 1) + 1
            logger.warning(
                "Duplicate PowerDesigner table code '%s' in %s — keeping the "
                "first definition and reporting the collision", code, filepath)
            continue
        tables[code] = parsed_tbl
        tbl_id_to_code[parsed_tbl["id"]] = code

    # ── References (Foreign Keys) ─────────────────────────────────────────────
    references: List[Dict] = []
    for ref in root.iter(_ns("o:Reference")):
        if ref.get("Ref"):
            continue
        references.append(_parse_reference(ref, id_map, tbl_id_to_code))

    return {
        "source_file": filepath,
        "model_name":  model_name,
        "tables":      tables,
        "references":  references,
        "duplicate_tables": duplicate_tables,
    }