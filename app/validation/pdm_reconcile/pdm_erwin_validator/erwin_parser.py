"""
ERwin Data Modeler XML Parser  (dialect-robust)
-----------------------------------------------
Parses the XML export produced by every common erwin release and export option:

  1. "Flat" export  — root <ERwin_Metamodel.DataModel> / <ERwin_Data_Model>,
     no XML namespace, scalar values stored as XML *attributes*
     (e.g. <Entity Name="CUSTOMER" Physical_Name="CUSTOMER">).
     This is what File -> Save As -> XML produced on CA ERwin 9.x and the
     hand-written self-test fixture in test_data/sample_model.xml.

  2. "EMX / metamodel" export — root <erwin xmlns="http://www.erwin.com/dm">
     with the model body under a *different* default namespace
     (xmlns="http://www.erwin.com/dm/data"), scalar values stored inside a
     nested "<XxxProps>" wrapper as child *elements*, and object identity in
     lowercase 'id' / 'name' attributes.  This is what erwin 2019 / 2020 /
     2021 / 2022 (10.x) Save As / Export XML produces and is the format of the
     real models being validated.

The previous version of this parser only understood dialect (1): it called
``root.iter("Entity")`` which matches the *unqualified* tag name, so on a
namespaced EMX file it found ZERO entities — every table was then reported as
"missing in ERwin".  That is the root cause of the all-CRITICAL / 0-tables
report.

This rewrite is namespace-agnostic (it matches on the *local* tag name),
reads scalars from attributes, direct child elements, or a nested "<XxxProps>"
wrapper interchangeably (``_val``), and resolves numeric enum codes used by the
EMX dialect via small tables in config.py.  The public API and the returned
dict shape are unchanged, so comparator.py / report_generator.py need no edits.

Object mapping (physical model):

    Entity                              -> Table
    Attribute                           -> Column
    Key_Group  (Key_Group_Type = PK)    -> Primary Key
    Key_Group  (Key_Group_Type = AK)    -> Alternate (unique) Key
    Key_Group  (Key_Group_Type = IE)    -> non-unique index
    Key_Group  (Key_Group_Type = IF*)   -> foreign-key mirror index (erwin
                                            auto-creates one per relationship;
                                            skipped by default, see config)
    Relationship                        -> Foreign Key
"""

import xml.etree.ElementTree as ET  # nosec B405
from defusedxml.ElementTree import parse as safe_parse, iterparse as safe_iterparse
import logging
from typing import Dict, Any, List, Optional

try:
    from app.config.validation_config import PDM_CONFIG as config
except Exception:                       # pragma: no cover - config always present in app
    config = None

logger = logging.getLogger(__name__)


# ─── ENUM CODE TABLES (EMX dialect) ─────────────────────────────────────────────
# The EMX export stores enums as integers instead of English text.  Values were
# taken from the erwin metamodel and cross-checked against real models.  They can
# be overridden / extended from config.py without touching this module.

def _cfg(name: str, default):
    return getattr(config, name, default) if config is not None else default


# Attribute-level <Null_Option_Type>:  1 = NOT NULL,  0 = NULL.
_DEFAULT_ATTR_NULL_CODES = {"1": True, "0": False, "2": False}

# Strings that mean "value required" in the flat dialect's <Null_Option>.
_NOT_NULL_TOKENS = {"not null", "notnull", "nn", "mandatory", "required",
                    "no nulls", "1", "true", "yes", "y"}
_NULL_TOKENS = {"null", "nulls allowed", "optional", "0", "false", "no", "n"}


# ─── LOW-LEVEL XML HELPERS ──────────────────────────────────────────────────────

def _local(tag: Any) -> str:
    """Strip any XML namespace: '{uri}Entity' -> 'Entity'."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _children(elem: ET.Element, name: str) -> List[ET.Element]:
    return [c for c in list(elem) if _local(c.tag) == name]


def _first_child(elem: ET.Element, name: str) -> Optional[ET.Element]:
    for c in list(elem):
        if _local(c.tag) == name:
            return c
    return None


def _descendants(elem: ET.Element, name: str) -> List[ET.Element]:
    return [n for n in elem.iter() if _local(n.tag) == name]


def _props_child(elem: ET.Element) -> Optional[ET.Element]:
    """
    EMX exports wrap an object's scalar fields one level down inside a single
    '<XxxProps>' child (EntityProps, AttributeProps, Key_GroupProps,
    RelationshipProps, ...).  Return that wrapper if present.
    """
    for c in list(elem):
        if _local(c.tag).endswith("Props"):
            return c
    return None


def _val(elem: ET.Element, *names: str) -> str:
    """
    First non-empty value found for the given field names, checked in order:
      1. XML attributes, exact case          (flat dialect: Physical_Name="...")
      2. XML attributes, case-insensitive     (EMX identity: id="..." name="...")
      3. direct child element text
      4. child element text inside <XxxProps>  (EMX scalars)
    erwin uses all four conventions interchangeably across versions.
    """
    if elem is None:
        return ""

    for n in names:
        raw = elem.get(n)
        if raw and raw.strip():
            return raw.strip()

    lower = {k.lower(): v for k, v in elem.attrib.items()}
    for n in names:
        raw = lower.get(n.lower())
        if raw and raw.strip():
            return raw.strip()

    for n in names:
        c = _first_child(elem, n)
        if c is not None and c.text and c.text.strip():
            return c.text.strip()

    props = _props_child(elem)
    if props is not None:
        for n in names:
            c = _first_child(props, n)
            if c is not None and c.text and c.text.strip():
                return c.text.strip()

    return ""


def _oid(elem: ET.Element) -> str:
    return _val(elem, "id", "Id", "ID", "Object_Id", "Long_Id", "GUID")


def _is_ref_node(elem: ET.Element) -> bool:
    """A pointer/cross-reference node (e.g. <Entity Ref='...'/>), not a definition."""
    return bool(elem.get("Ref")) and not _oid(elem)


# ─── SCALAR RESOLVERS ───────────────────────────────────────────────────────────

def _physical_name(elem: ET.Element) -> str:
    return _val(elem, "Physical_Name", "Code", "Table_Name", "Column_Name")


def _resolve_datatype(elem: ET.Element) -> str:
    """
    Physical model -> physical datatype wins.  Fall back to logical / domain so a
    partially-populated attribute still yields something comparable.
    """
    return _val(elem,
                "Physical_Datatype", "Physical_Data_Type",
                "Datatype", "Data_Type",
                "Logical_Datatype", "Logical_Data_Type",
                "Domain_Name", "Domain_Parent_Name")


def _is_not_null(elem: ET.Element) -> bool:
    # EMX numeric code first.
    null_codes = _cfg("ERWIN_ATTR_NULL_OPTION_CODES", _DEFAULT_ATTR_NULL_CODES)
    code = _val(elem, "Null_Option_Type")
    if code and code in null_codes:
        return bool(null_codes[code])

    # Flat-dialect string.
    raw = _val(elem, "Null_Option", "Nulls_Allowed", "Optional").lower()
    if raw:
        if any(tok == raw or raw.startswith("not null") for tok in _NOT_NULL_TOKENS):
            return True
        if raw in _NULL_TOKENS or raw.startswith("null"):
            return False
    return False


# ─── ATTRIBUTES (columns) ───────────────────────────────────────────────────────

def _parse_attribute(attr_elem: ET.Element) -> Dict[str, Any]:
    code = (_physical_name(attr_elem)
            or _val(attr_elem, "Name")).upper()
    return {
        "id":        _oid(attr_elem),
        "name":      _val(attr_elem, "Name", "Logical_Name", "Attribute_Name"),
        "code":      code,
        "data_type": _resolve_datatype(attr_elem),
        "not_null":  _is_not_null(attr_elem),
        "default":   _val(attr_elem, "Default_Value", "Default", "Server_Default"),
        # migration bookkeeping (used for FK join resolution in EMX)
        "_parent_attr_ref": _val(attr_elem, "Parent_Attribute_Ref",
                                 "Master_Attribute_Ref", "Migrated_From"),
        "_parent_rel_ref":  _val(attr_elem, "Parent_Relationship_Ref"),
    }


# ─── KEY GROUPS (PK / AK / indexes) ─────────────────────────────────────────────

def _key_group_members(kg_elem: ET.Element,
                       attr_id_to_code: Dict[str, str],
                       attr_name_to_code: Dict[str, str]) -> List[str]:
    """
    Resolve a key group's member columns to physical column codes, in order.

    Members are located by (in priority order):
      * <Attribute_Ref> child / attribute            -> attribute id
      * the member's own 'name'                       -> attribute logical name
      * <Attribute_Ref>/<Attribute_Id> on the member  (flat dialect)
    Order is taken from an explicit sequence field when present, else document
    order.
    """
    ordered: List[tuple] = []
    seq_fallback = 0

    for member in _descendants(kg_elem, "Key_Group_Member"):
        seq_fallback += 1
        attr_ref = _val(member, "Attribute_Ref", "Attribute_Id", "Attribute",
                        "Member_Ref")
        member_name = _val(member, "Name", "name")

        code = ""
        if attr_ref and attr_ref in attr_id_to_code:
            code = attr_id_to_code[attr_ref]
        elif member_name and member_name.upper() in attr_name_to_code:
            code = attr_name_to_code[member_name.upper()]
        elif attr_ref:
            code = attr_ref
        elif member_name:
            code = member_name.upper()

        if not code:
            continue

        seq_raw = _val(member, "Key_Group_Member_Order", "Sequence",
                       "Position", "Order", "Index_Member_Order")
        try:
            seq = int(seq_raw)
        except (TypeError, ValueError):
            seq = seq_fallback
        ordered.append((seq, code))

    ordered.sort(key=lambda x: x[0])
    return [c for _, c in ordered]


def _classify_key_group(kg_elem: ET.Element):
    """Return (is_pk, is_unique, kg_type, is_relationship_index)."""
    kg_type = _val(kg_elem, "Key_Group_Type", "Type", "Key_Type").upper()
    is_pk = kg_type in ("PK", "PRIMARY KEY", "PRIMARY")
    is_ak = kg_type in ("AK", "ALTERNATE KEY", "ALTERNATE")
    is_unique = is_pk or is_ak or _val(kg_elem, "Is_Unique").lower() in (
        "1", "true", "yes", "y")
    is_rel_index = bool(_val(kg_elem, "Relationship_Ref"))
    return is_pk, is_unique, kg_type, is_rel_index


# ─── ENTITY (table) ─────────────────────────────────────────────────────────────

def _parse_entity(entity_elem: ET.Element,
                  migrated_sink: List[Dict]) -> Dict[str, Any]:
    entity_id = _oid(entity_elem)
    phys_name = (_physical_name(entity_elem)
                 or _val(entity_elem, "Name")).upper()

    # ── Columns ────────────────────────────────────────────────────────────────
    columns: List[Dict] = []
    attr_id_to_code: Dict[str, str] = {}
    attr_name_to_code: Dict[str, str] = {}
    seen_attr: set = set()

    for attr in _descendants(entity_elem, "Attribute"):
        if _is_ref_node(attr):
            continue
        aid = _oid(attr)
        if aid and aid in seen_attr:
            continue
        parsed = _parse_attribute(attr)
        if not parsed["code"]:
            continue
        if aid:
            seen_attr.add(aid)
            attr_id_to_code[aid] = parsed["code"]
        if parsed["name"]:
            attr_name_to_code[parsed["name"].upper()] = parsed["code"]

        # record migration info for FK-join resolution
        if parsed["_parent_rel_ref"] or parsed["_parent_attr_ref"]:
            migrated_sink.append({
                "entity_id":       entity_id,
                "code":            parsed["code"],
                "parent_attr_ref": parsed["_parent_attr_ref"],
                "parent_rel_ref":  parsed["_parent_rel_ref"],
            })

        columns.append({k: v for k, v in parsed.items()
                        if not k.startswith("_")})

    # ── Key groups -> keys + indexes ─────────────────────────────────────────────
    keys: List[Dict] = []
    indexes: List[Dict] = []
    ignore_fk_idx = _cfg("ERWIN_IGNORE_FK_INDEXES", True)
    seen_kg: set = set()

    for kg in _descendants(entity_elem, "Key_Group"):
        if _is_ref_node(kg):
            continue
        kid = _oid(kg)
        if kid and kid in seen_kg:
            continue
        if kid:
            seen_kg.add(kid)

        is_pk, is_unique, kg_type, is_rel_index = _classify_key_group(kg)
        cols = _key_group_members(kg, attr_id_to_code, attr_name_to_code)
        name = _val(kg, "Name")
        code = _physical_name(kg) or name

        if is_pk or kg_type in ("AK", "ALTERNATE KEY", "ALTERNATE"):
            keys.append({
                "name": name, "code": code,
                "is_pk": is_pk, "is_unique": is_unique,
                "kg_type": kg_type, "columns": cols,
            })
        else:
            # IE = real inversion index; IF* = erwin's auto FK mirror index.
            if is_rel_index and ignore_fk_idx:
                continue
            indexes.append({
                "name": name, "code": code,
                "is_unique": is_unique, "kg_type": kg_type,
                "columns": cols,
            })

    return {
        "id":          entity_id,
        "name":        _val(entity_elem, "Name", "Logical_Name", "Entity_Name"),
        "code":        phys_name,
        "owner":       _val(entity_elem, "Owner", "Owner_Path", "Schema"),
        "columns":     columns,
        "keys":        keys,
        "indexes":     indexes,
        "attr_id_map": attr_id_to_code,
    }


# ─── RELATIONSHIPS (foreign keys) ────────────────────────────────────────────────

def _parse_relationship(rel_elem: ET.Element,
                        entity_id_to_code: Dict[str, str],
                        all_entities: Dict[str, Any],
                        attr_id_to_code_global: Dict[str, str],
                        migrated: List[Dict]) -> Dict[str, Any]:
    rel_id = _oid(rel_elem)
    parent_ref = _val(rel_elem, "Entity_Ref_Parent", "Parent_Entity_Ref",
                      "Parent_Entity", "From_Entity_Ref")
    child_ref = _val(rel_elem, "Entity_Ref_Child", "Child_Entity_Ref",
                     "Child_Entity", "To_Entity_Ref")
    parent_code = entity_id_to_code.get(parent_ref, parent_ref or "UNKNOWN")
    child_code = entity_id_to_code.get(child_ref, child_ref or "UNKNOWN")

    join_columns: List[Dict] = []

    # (a) flat dialect: explicit RI_Constraint children carry both ends.
    for ri in _descendants(rel_elem, "RI_Constraint"):
        p_ref = _val(ri, "Parent_Attribute_Ref", "Parent_Attribute")
        c_ref = _val(ri, "Child_Attribute_Ref", "Child_Attribute")
        p_col = ""
        c_col = ""
        pe = all_entities.get(parent_code)
        if pe:
            p_col = pe["attr_id_map"].get(p_ref, p_ref)
        ce = all_entities.get(child_code)
        if ce:
            c_col = ce["attr_id_map"].get(c_ref, c_ref)
        join_columns.append({"parent_col": p_col, "child_col": c_col})

    # (b) EMX dialect: join columns are the child's migrated attributes that
    #     point back at this relationship.
    if not join_columns and rel_id:
        for m in migrated:
            if m["parent_rel_ref"] and m["parent_rel_ref"] == rel_id:
                parent_col = attr_id_to_code_global.get(
                    m["parent_attr_ref"], "")
                join_columns.append({
                    "parent_col": parent_col,
                    "child_col":  m["code"],
                })

    return {
        "name":         _val(rel_elem, "Name"),
        "code":         _physical_name(rel_elem) or _val(rel_elem, "Name"),
        "parent_table": parent_code,
        "child_table":  child_code,
        "join_columns": join_columns,
    }


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def parse_erwin(filepath: str) -> Dict[str, Any]:
    """
    Parse an ERwin XML export file (any supported dialect).

    Returns
    -------
    {
        "source_file": str,
        "model_name":  str,
        "tables": {
            "<PHYSICAL_TABLE_NAME>": {
                "code":    str,
                "name":    str,
                "owner":   str,
                "columns": [ {id, name, code, data_type, not_null, default} ... ],
                "keys":    [ {name, code, is_pk, is_unique, kg_type, columns:[code]} ... ],
                "indexes": [ {name, code, is_unique, kg_type, columns:[code]} ... ],
                "attr_id_map": {id: code},
            }
        },
        "references": [
            {name, code, parent_table, child_table,
             join_columns:[{parent_col, child_col}]}
        ]
    }
    On a parse failure an "error" key is added (comparator turns it into a
    single ERROR finding) — one bad file never aborts a batch.
    """
    try:
        root = safe_parse(filepath).getroot()
    except ET.ParseError as e:
        logger.error("XML parse error in %s: %s", filepath, e)
        return {"source_file": filepath, "error": str(e),
                "tables": {}, "references": []}
    except OSError as e:
        logger.error("Cannot read %s: %s", filepath, e)
        return {"source_file": filepath, "error": str(e),
                "tables": {}, "references": []}

    # ── Model name (dialect-agnostic) ───────────────────────────────────────────
    model_name = ""
    model_elem = None
    for cand in _descendants(root, "Model"):
        if _oid(cand) or _val(cand, "Name", "Model_Name"):
            model_elem = cand
            break
    if model_elem is not None:
        model_name = _val(model_elem, "Name", "Model_Name", "Logical_Name")
    if not model_name:
        props = _first_child(root, "ModelProps") or _first_child(root, "Model_Properties")
        if props is not None:
            model_name = _val(props, "Name", "Model_Name")
    if not model_name:
        model_name = _val(root, "Name", "Model_Name")

    # ── Entities (tables) ────────────────────────────────────────────────────────
    tables: Dict[str, Any] = {}
    entity_id_to_code: Dict[str, str] = {}
    attr_id_to_code_global: Dict[str, str] = {}
    migrated: List[Dict] = []
    seen_entities: set = set()
    # Entities that collide on physical name are dropped below. Record them so
    # the comparator can report the loss instead of it being a log line nobody
    # reads (the table totals silently stopped adding up).
    duplicate_tables: Dict[str, int] = {}

    for entity in _descendants(root, "Entity"):
        if _is_ref_node(entity):
            continue
        eid = _oid(entity)
        if eid and eid in seen_entities:
            continue
        # A definition node must have identity or a usable name.
        if not eid and not _val(entity, "Name", "Physical_Name"):
            continue
        if eid:
            seen_entities.add(eid)

        parsed = _parse_entity(entity, migrated)
        code = parsed["code"].upper()
        if not code:
            continue
        if code in tables:
            duplicate_tables[code] = duplicate_tables.get(code, 1) + 1
            logger.warning("Duplicate ERwin table code '%s' in %s — keeping first",
                           code, filepath)
            continue
        tables[code] = parsed
        if parsed["id"]:
            entity_id_to_code[parsed["id"]] = code
        attr_id_to_code_global.update(parsed["attr_id_map"])

    # ── Relationships (foreign keys) ─────────────────────────────────────────────
    references: List[Dict] = []
    seen_rels: set = set()
    for rel in _descendants(root, "Relationship"):
        if _is_ref_node(rel):
            continue
        rid = _oid(rel)
        if rid and rid in seen_rels:
            continue
        if not rid and not _val(rel, "Name"):
            continue
        if rid:
            seen_rels.add(rid)
        references.append(_parse_relationship(
            rel, entity_id_to_code, tables, attr_id_to_code_global, migrated))

    logger.info("Parsed ERwin %s: %d tables, %d references",
                filepath, len(tables), len(references))

    return {
        "source_file": filepath,
        "model_name":  model_name,
        "tables":      tables,
        "references":  references,
        "duplicate_tables": duplicate_tables,
    }
