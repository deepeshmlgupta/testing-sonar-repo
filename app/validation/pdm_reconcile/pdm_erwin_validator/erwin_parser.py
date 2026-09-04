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


def _non_empty(raw: Optional[str]) -> str:
    return raw.strip() if raw and raw.strip() else ""


def _attribute_value(elem: ET.Element, name: str) -> str:
    return _non_empty(elem.get(name))


def _case_insensitive_value(elem: ET.Element, name: str) -> str:
    lower = {key.lower(): value for key, value in elem.attrib.items()}
    return _non_empty(lower.get(name.lower()))


def _child_text(elem: ET.Element, name: str) -> str:
    child = _first_child(elem, name)
    return _non_empty(child.text if child is not None else None)


def _props_text(elem: ET.Element, name: str) -> str:
    props = _props_child(elem)
    return _child_text(props, name) if props is not None else ""


def _val(elem: ET.Element, *names: str) -> str:
    """
    First non-empty value found for the given field names, checked in order:
      1. XML attributes, exact case
      2. XML attributes, case-insensitive
      3. direct child element text
      4. child element text inside <XxxProps>
    """
    if elem is None:
        return ""

    for name in names:
        value = _attribute_value(elem, name)
        if value:
            return value

    for name in names:
        value = _case_insensitive_value(elem, name)
        if value:
            return value

    for name in names:
        value = _child_text(elem, name)
        if value:
            return value

    for name in names:
        value = _props_text(elem, name)
        if value:
            return value

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

def _parse_entity_columns(entity_elem: ET.Element,
                           entity_id: str,
                           migrated_sink: List[Dict]) -> tuple:
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

        _record_migration_info(parsed, entity_id, migrated_sink)
        columns.append({key: value for key, value in parsed.items()
                        if not key.startswith("_")})

    return columns, attr_id_to_code, attr_name_to_code


def _record_migration_info(parsed: Dict[str, Any], entity_id: str,
                           migrated_sink: List[Dict]) -> None:
    if not (parsed["_parent_rel_ref"] or parsed["_parent_attr_ref"]):
        return
    migrated_sink.append({
        "entity_id": entity_id,
        "code": parsed["code"],
        "parent_attr_ref": parsed["_parent_attr_ref"],
        "parent_rel_ref": parsed["_parent_rel_ref"],
    })


def _parse_entity_key_groups(entity_elem: ET.Element,
                              attr_id_to_code: Dict[str, str],
                              attr_name_to_code: Dict[str, str]) -> tuple:
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

        parsed_kg = _parse_key_group(
            kg, attr_id_to_code, attr_name_to_code, ignore_fk_idx)
        if parsed_kg is None:
            continue

        target, value = parsed_kg
        target.append(value)

    return keys, indexes


def _parse_key_group(kg: ET.Element,
                     attr_id_to_code: Dict[str, str],
                     attr_name_to_code: Dict[str, str],
                     ignore_fk_idx: bool) -> Optional[tuple]:
    is_pk, is_unique, kg_type, is_rel_index = _classify_key_group(kg)
    cols = _key_group_members(kg, attr_id_to_code, attr_name_to_code)
    name = _val(kg, "Name")
    code = _physical_name(kg) or name

    if is_pk or kg_type in ("AK", "ALTERNATE KEY", "ALTERNATE"):
        return {
            "name": name, "code": code,
            "is_pk": is_pk, "is_unique": is_unique,
            "kg_type": kg_type, "columns": cols,
        }

    if is_rel_index and ignore_fk_idx:
        return None

    return {
        "name": name, "code": code,
        "is_unique": is_unique, "kg_type": kg_type,
        "columns": cols,
    }


def _append_key_group(parsed_kg: Optional[Dict],
                      keys: List[Dict], indexes: List[Dict]) -> None:
    if parsed_kg is None:
        return
    if "is_pk" in parsed_kg:
        keys.append(parsed_kg)
    else:
        indexes.append(parsed_kg)


def _parse_entity_key_groups(entity_elem: ET.Element,
                              attr_id_to_code: Dict[str, str],
                              attr_name_to_code: Dict[str, str]) -> tuple:
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
        _append_key_group(
            _parse_key_group(kg, attr_id_to_code, attr_name_to_code,
                             ignore_fk_idx),
            keys, indexes)

    return keys, indexes


def _parse_entity(entity_elem: ET.Element,
                  migrated_sink: List[Dict]) -> Dict[str, Any]:
    entity_id = _oid(entity_elem)
    phys_name = (_physical_name(entity_elem)
                 or _val(entity_elem, "Name")).upper()

    columns, attr_id_to_code, attr_name_to_code = _parse_entity_columns(
        entity_elem, entity_id, migrated_sink)
    keys, indexes = _parse_entity_key_groups(
        entity_elem, attr_id_to_code, attr_name_to_code)

    return {
        "id": entity_id,
        "name": _val(entity_elem, "Name", "Logical_Name", "Entity_Name"),
        "code": phys_name,
        "owner": _val(entity_elem, "Owner", "Owner_Path", "Schema"),
        "columns": columns,
        "keys": keys,
        "indexes": indexes,
        "attr_id_map": attr_id_to_code,
    }


# ─── RELATIONSHIPS (foreign keys) ────────────────────────────────────────────────

def _resolve_join_column(ref: str, entity: Optional[Dict]) -> str:
    if entity:
        return entity["attr_id_map"].get(ref, ref)
    return ""


def _parse_flat_join_columns(rel_elem: ET.Element,
                             parent_code: str,
                             child_code: str,
                             all_entities: Dict[str, Any]) -> List[Dict]:
    join_columns: List[Dict] = []
    parent_entity = all_entities.get(parent_code)
    child_entity = all_entities.get(child_code)

    for ri in _descendants(rel_elem, "RI_Constraint"):
        p_ref = _val(ri, "Parent_Attribute_Ref", "Parent_Attribute")
        c_ref = _val(ri, "Child_Attribute_Ref", "Child_Attribute")
        join_columns.append({
            "parent_col": _resolve_join_column(p_ref, parent_entity),
            "child_col": _resolve_join_column(c_ref, child_entity),
        })
    return join_columns


def _parse_migrated_join_columns(rel_id: str, migrated: List[Dict],
                                 attr_id_to_code_global: Dict[str, str]
                                 ) -> List[Dict]:
    join_columns: List[Dict] = []
    if not rel_id:
        return join_columns

    for item in migrated:
        if item["parent_rel_ref"] != rel_id:
            continue
        join_columns.append({
            "parent_col": attr_id_to_code_global.get(item["parent_attr_ref"], ""),
            "child_col": item["code"],
        })
    return join_columns


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

    join_columns = _parse_flat_join_columns(
        rel_elem, parent_code, child_code, all_entities)
    if not join_columns:
        join_columns = _parse_migrated_join_columns(
            rel_id, migrated, attr_id_to_code_global)

    return {
        "name": _val(rel_elem, "Name"),
        "code": _physical_name(rel_elem) or _val(rel_elem, "Name"),
        "parent_table": parent_code,
        "child_table": child_code,
        "join_columns": join_columns,
    }


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def _parse_model_name(root: ET.Element) -> str:
    for candidate in _descendants(root, "Model"):
        if _oid(candidate) or _val(candidate, "Name", "Model_Name"):
            return _val(candidate, "Name", "Model_Name", "Logical_Name")

    props = (_first_child(root, "ModelProps")
             or _first_child(root, "Model_Properties"))
    if props is not None:
        model_name = _val(props, "Name", "Model_Name")
        if model_name:
            return model_name

    return _val(root, "Name", "Model_Name")


def _parse_entities(root: ET.Element, filepath: str) -> tuple:
    tables: Dict[str, Any] = {}
    entity_id_to_code: Dict[str, str] = {}
    attr_id_to_code_global: Dict[str, str] = {}
    migrated: List[Dict] = []
    seen_entities: set = set()
    duplicate_tables: Dict[str, int] = {}

    for entity in _descendants(root, "Entity"):
        if _is_ref_node(entity):
            continue
        eid = _oid(entity)
        if eid and eid in seen_entities:
            continue
        if not eid and not _val(entity, "Name", "Physical_Name"):
            continue
        if eid:
            seen_entities.add(eid)

        parsed = _parse_entity(entity, migrated)
        code = parsed["code"].upper()
        if not code:
            continue
        if _store_entity(
                tables, entity_id_to_code, attr_id_to_code_global,
                duplicate_tables, parsed, code, filepath):
            continue

    return (tables, entity_id_to_code, attr_id_to_code_global,
            migrated, duplicate_tables)


def _store_entity(tables: Dict[str, Any],
                  entity_id_to_code: Dict[str, str],
                  attr_id_to_code_global: Dict[str, str],
                  duplicate_tables: Dict[str, int],
                  parsed: Dict[str, Any],
                  code: str,
                  filepath: str) -> bool:
    if code in tables:
        duplicate_tables[code] = duplicate_tables.get(code, 1) + 1
        logger.warning(
            "Duplicate ERwin table code '%s' in %s — keeping first",
            code, filepath)
        return False

    tables[code] = parsed
    if parsed["id"]:
        entity_id_to_code[parsed["id"]] = code
    attr_id_to_code_global.update(parsed["attr_id_map"])
    return True


def _parse_relationships(root: ET.Element,
                         entity_id_to_code: Dict[str, str],
                         tables: Dict[str, Any],
                         attr_id_to_code_global: Dict[str, str],
                         migrated: List[Dict]) -> List[Dict]:
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
            rel, entity_id_to_code, tables,
            attr_id_to_code_global, migrated))

    return references


def _parse_xml_root(filepath: str) -> tuple:
    try:
        return safe_parse(filepath).getroot(), ""
    except ET.ParseError as exc:
        logger.error("XML parse error in %s: %s", filepath, exc)
        return None, str(exc)
    except OSError as exc:
        logger.error("Cannot read %s: %s", filepath, exc)
        return None, str(exc)


def parse_erwin(filepath: str) -> Dict[str, Any]:
    """
    Parse an ERwin XML export file (any supported dialect).

    Returns the same public dictionary shape as the original parser.  On a
    parse failure an "error" key is returned so the comparator can report a
    single ERROR finding without aborting a batch.
    """
    root, error = _parse_xml_root(filepath)
    if root is None:
        return {"source_file": filepath, "error": error,
                "tables": {}, "references": []}

    model_name = _parse_model_name(root)
    (tables, entity_id_to_code, attr_id_to_code_global,
     migrated, duplicate_tables) = _parse_entities(root, filepath)
    references = _parse_relationships(
        root, entity_id_to_code, tables,
        attr_id_to_code_global, migrated)

    logger.info("Parsed ERwin %s: %d tables, %d references",
                filepath, len(tables), len(references))

    return {
        "source_file": filepath,
        "model_name": model_name,
        "tables": tables,
        "references": references,
        "duplicate_tables": duplicate_tables,
    }
