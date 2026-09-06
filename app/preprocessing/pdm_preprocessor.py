import logging
import os
import shutil
import uuid
import xml.etree.ElementTree as ET  # nosec B405
from defusedxml.ElementTree import iterparse as safe_iterparse
from defusedxml.ElementTree import parse as safe_parse
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# The namespace erwin's EMX dialect uses for model-body elements.
_DATA_NS = "http://www.erwin.com/dm/data"  # NOSONAR
# Stable prefixes for the other erwin namespaces when they were declared as
# defaults in the source (only one default can survive re-serialisation).
_KNOWN_PREFIXES = {
    "http://www.erwin.com/dm": "EMX",           # NOSONAR
    "http://www.erwin.com/dm/EM2data": "EM2",   # NOSONAR
    "http://www.erwin.com/dm/metadata": "EMXMD",# NOSONAR
}


# ─── REPORT ───────────────────────────────────────────────────────────────────

@dataclass
class PreprocessReport:
    """What the remediation pass actually changed."""
    model_name: str = ""
    columns_added: int = 0
    pk_groups_created: int = 0
    pk_members_added: int = 0
    pks_fixed: int = 0                 # tables whose PK was re-linked
    fk_targets: int = 0                # FKs the validator reported missing
    fks_fixed: int = 0
    fk_unfixable: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.actions.append(message)
        logger.info("  [preprocess] %s", message)

    def summary(self) -> str:
        parts = [f"restored {self.columns_added} column(s)",
                 f"re-linked {self.pks_fixed} primary key(s)",
                 f"repaired {self.fks_fixed}/{self.fk_targets} foreign key(s)"]
        if self.fk_unfixable:
            parts.append(f"{len(self.fk_unfixable)} FK(s) unfixable from source")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return ", ".join(parts)


# ─── XML HELPERS (namespace-agnostic, mirroring the validator's parser) ───────

def _local(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _ns_of(tag: str) -> str:
    """'{uri}Entity' -> '{uri}' (empty string when un-namespaced)."""
    if isinstance(tag, str) and tag.startswith("{"):
        return tag.split("}", 1)[0] + "}"
    return ""


def _first_child(elem: ET.Element, name: str) -> Optional[ET.Element]:
    for c in elem:
        if _local(c.tag) == name:
            return c
    return None


def _props_child(elem: ET.Element) -> Optional[ET.Element]:
    for c in elem:
        if _local(c.tag).endswith("Props"):
            return c
    return None


def _prop_text(elem: ET.Element, name: str) -> str:
    """Value of <name> inside the element's <XxxProps> wrapper (or '')."""
    props = _props_child(elem)
    if props is None:
        return ""
    child = _first_child(props, name)
    if child is not None and child.text:
        return child.text.strip()
    return ""


def _set_prop(elem: ET.Element, name: str, value: str) -> None:
    """Set (create if needed) <name> inside the element's props wrapper."""
    props = _props_child(elem)
    if props is None:
        props = ET.SubElement(elem, f"{_ns_of(elem.tag)}{_local(elem.tag)}Props")
    child = _first_child(props, name)
    if child is None:
        child = ET.SubElement(props, f"{_ns_of(props.tag)}{name}")
    child.text = value


def _del_prop(elem: ET.Element, name: str) -> None:
    props = _props_child(elem)
    if props is None:
        return
    child = _first_child(props, name)
    if child is not None:
        props.remove(child)


def _new_id() -> str:
    """A fresh object id in erwin's EMX format."""
    return "{" + str(uuid.uuid4()).upper() + "}+00000000"


def _key(name: str) -> str:
    return (name or "").strip().upper()


# ─── DOCUMENT INDEX ───────────────────────────────────────────────────────────

class _ErwinDoc:
    """Index of an EMX erwin XML document, built once and kept current."""

    def __init__(self, root: ET.Element):
        self.root = root
        self.entities: Dict[str, ET.Element] = {}        # PHYS_NAME -> Entity
        self.entity_by_id: Dict[str, ET.Element] = {}
        self.rels: List[ET.Element] = []
        self.all_ids: set = set()
        for elem in root.iter():
            oid = elem.get("id")
            if oid:
                self.all_ids.add(oid)
            name = _local(elem.tag)
            if name == "Entity" and oid:
                phys = _key(_prop_text(elem, "Physical_Name") or elem.get("name", ""))
                if phys and phys not in self.entities:
                    self.entities[phys] = elem
                    self.entity_by_id[oid] = elem
            elif name == "Relationship" and oid:
                self.rels.append(elem)

    def entity_phys(self, elem: ET.Element) -> str:
        return _key(_prop_text(elem, "Physical_Name") or elem.get("name", ""))

    def attributes(self, entity: ET.Element) -> List[ET.Element]:
        return [a for a in entity.iter()
                if _local(a.tag) == "Attribute" and a.get("id")]

    def attr_by_code(self, entity: ET.Element, code: str) -> Optional[ET.Element]:
        code = _key(code)
        for a in self.attributes(entity):
            phys = _key(_prop_text(a, "Physical_Name") or a.get("name", ""))
            if phys == code:
                return a
        return None

    def key_groups(self, entity: ET.Element) -> List[ET.Element]:
        return [k for k in entity.iter()
                if _local(k.tag) == "Key_Group" and k.get("id")]


# ─── 1. MISSING COLUMNS ───────────────────────────────────────────────────────

def _template_attribute(doc: _ErwinDoc, entity: ET.Element) -> Optional[ET.Element]:
    """An existing Attribute to clone (same entity preferred, any entity else)."""
    attrs = doc.attributes(entity)
    if attrs:
        return attrs[0]
    for other in doc.entities.values():
        attrs = doc.attributes(other)
        if attrs:
            return attrs[0]
    return None


def _attribute_container(entity: ET.Element) -> ET.Element:
    grp = _first_child(entity, "Attribute_Groups")
    if grp is None:
        grp = ET.SubElement(entity, f"{_ns_of(entity.tag)}Attribute_Groups")
    return grp


def _add_column(doc: _ErwinDoc, report: PreprocessReport, model_name: str,
                entity: ET.Element, pd_col: Dict[str, Any]) -> Optional[ET.Element]:
    template = _template_attribute(doc, entity)
    if template is None:
        report.errors.append(
            f"No attribute template available to restore column "
            f"'{pd_col.get('code')}' in '{doc.entity_phys(entity)}'")
        return None

    new = deepcopy(template)
    new_id = _new_id()
    new.set("id", new_id)
    new.set("name", pd_col.get("name") or pd_col.get("code", ""))

    tbl_phys = doc.entity_phys(entity)
    order = len(doc.attributes(entity)) + 1

    _set_prop(new, "Name", pd_col.get("name") or pd_col.get("code", ""))
    _set_prop(new, "Long_Id", new_id)
    if model_name:
        _set_prop(new, "Owner_Path", f"{model_name}.{tbl_phys}")
    _set_prop(new, "Physical_Name", _key(pd_col.get("code", "")))
    dtype = pd_col.get("data_type") or ""
    if dtype:
        _set_prop(new, "Physical_Data_Type", dtype)
        _set_prop(new, "Logical_Data_Type", dtype)
    _set_prop(new, "Null_Option_Type", "1" if pd_col.get("not_null") else "0")
    _set_prop(new, "Physical_Order", str(order))
    if pd_col.get("comment"):
        _set_prop(new, "Comment", pd_col["comment"])
    if pd_col.get("default"):
        _set_prop(new, "Default_Value", pd_col["default"])
    else:
        _del_prop(new, "Default_Value")
    _set_prop(new, "Master_Attribute_Ref", new_id)
    _set_prop(new, "User_Formatted_Name", pd_col.get("name") or pd_col.get("code", ""))
    _set_prop(new, "User_Formatted_Physical_Name", _key(pd_col.get("code", "")))
    # The clone must not inherit the template's migration/domain pointers.
    for stale in ("Parent_Attribute_Ref", "Parent_Relationship_Ref",
                  "Parent_Domain_Ref", "Definition", "Long_Id_Header"):
        if stale in ("Parent_Attribute_Ref", "Parent_Relationship_Ref"):
            _del_prop(new, stale)

    _attribute_container(entity).append(new)
    doc.all_ids.add(new_id)
    report.columns_added += 1
    report.note(f"restored column {tbl_phys}.{_key(pd_col.get('code',''))} "
                f"({dtype or 'no type'}, "
                f"{'NOT NULL' if pd_col.get('not_null') else 'NULL'})")
    return new


# ─── 2. PRIMARY KEYS ──────────────────────────────────────────────────────────

def _pd_pk_columns(pd_table: Dict[str, Any]) -> List[str]:
    for k in pd_table.get("keys", []):
        if k.get("is_pk"):
            return [_key(c) for c in k.get("columns", [])]
    return []


def _template_kgm(doc: _ErwinDoc) -> Optional[ET.Element]:
    for elem in doc.root.iter():
        if _local(elem.tag) == "Key_Group_Member" and elem.get("id"):
            return elem
    return None


def _template_pk_group(doc: _ErwinDoc) -> Optional[ET.Element]:
    for entity in doc.entities.values():
        for kg in doc.key_groups(entity):
            if _prop_text(kg, "Key_Group_Type").upper() == "PK":
                return kg
    return None


def _kg_member_codes(doc: _ErwinDoc, entity: ET.Element,
                     kg: ET.Element) -> List[str]:
    """Physical column codes currently linked by a key group's members."""
    id_to_code = {}
    for a in doc.attributes(entity):
        id_to_code[a.get("id")] = _key(_prop_text(a, "Physical_Name")
                                       or a.get("name", ""))
    codes = []
    for m in kg.iter():
        if _local(m.tag) != "Key_Group_Member":
            continue
        ref = _prop_text(m, "Attribute_Ref")
        code = id_to_code.get(ref) or _key(_prop_text(m, "Physical_Name")
                                           or m.get("name", ""))
        if code and ref in id_to_code:
            codes.append(code)
    return codes


def _fix_primary_key(doc: _ErwinDoc, report: PreprocessReport, model_name: str,
                       entity: ET.Element, pd_table: Dict[str, Any]) -> bool:
    pd_pk = _pd_pk_columns(pd_table)
    if not pd_pk:
        return False
    tbl_phys = doc.entity_phys(entity)

    pk_group = _get_or_create_pk_group(doc, report, model_name, entity, tbl_phys)
    if pk_group is None:
        return False

    existing = set(_kg_member_codes(doc, entity, pk_group))
    missing = [c for c in pd_pk if c not in existing]
    if not missing:
        return False

    return _add_missing_pk_members(doc, report, model_name, entity, tbl_phys, pk_group, missing)

def _get_or_create_pk_group(doc: _ErwinDoc, report: PreprocessReport, model_name: str, entity: ET.Element, tbl_phys: str) -> ET.Element:
    for kg in doc.key_groups(entity):
        if _prop_text(kg, "Key_Group_Type").upper() == "PK":
            return kg

    template = _template_pk_group(doc)
    if template is None:
        report.errors.append(f"No PK Key_Group template for '{tbl_phys}'")
        return None

    pk_group = deepcopy(template)
    for holder in list(pk_group):
        if _local(holder.tag) == "Key_Group_Member_Groups":
            pk_group.remove(holder)

    kg_id = _new_id()
    pk_group.set("id", kg_id)
    pk_group.set("name", "Identifier_1")
    _set_prop(pk_group, "Name", "Identifier_1")
    _set_prop(pk_group, "Long_Id", kg_id)
    if model_name:
        _set_prop(pk_group, "Owner_Path", f"{model_name}.{tbl_phys}")
    _set_prop(pk_group, "Key_Group_Type", "PK")
    _set_prop(pk_group, "Physical_Name", f"{tbl_phys}_PK")
    _set_prop(pk_group, "Is_Unique", "true")

    holder = _first_child(entity, "Key_Group_Groups")
    if holder is None:
        holder = ET.SubElement(entity, f"{_ns_of(entity.tag)}Key_Group_Groups")
    holder.append(pk_group)
    doc.all_ids.add(kg_id)
    report.pk_groups_created += 1
    report.note(f"created PK Key_Group for {tbl_phys}")
    return pk_group

def _add_missing_pk_members(doc: _ErwinDoc, report: PreprocessReport, model_name: str, entity: ET.Element, tbl_phys: str, pk_group: ET.Element, missing: list) -> bool:
    kgm_template = _template_kgm(doc)
    member_holder = _first_child(pk_group, "Key_Group_Member_Groups")
    if member_holder is None:
        member_holder = ET.SubElement(pk_group, f"{_ns_of(pk_group.tag)}Key_Group_Member_Groups")

    added = 0
    order_base = len(list(member_holder))
    for i, code in enumerate(missing, start=1):
        attr = doc.attr_by_code(entity, code)
        if attr is None:
            report.errors.append(f"PK column '{code}' not found in erwin entity '{tbl_phys}'")
            continue

        member = deepcopy(kgm_template) if kgm_template is not None else ET.Element(f"{_ns_of(pk_group.tag)}Key_Group_Member")
        m_id = _new_id()
        logical = attr.get("name") or code
        member.set("id", m_id)
        member.set("name", logical)
        _set_prop(member, "Name", logical)
        _set_prop(member, "Long_Id", m_id)
        if model_name:
            _set_prop(member, "Owner_Path", f"{model_name}.{tbl_phys}.{pk_group.get('name', 'Identifier_1')}")
        _set_prop(member, "Attribute_Ref", attr.get("id"))
        _set_prop(member, "Physical_Name", code)
        _set_prop(member, "Key_Group_Sort_Order", "ASC")
        _set_prop(member, "Key_Group_Member_Order", str(order_base + i))
        _set_prop(member, "Index_Member_Order", str(order_base + i))
        _set_prop(member, "User_Formatted_Name", logical)
        _set_prop(member, "User_Formatted_Physical_Name", code)
        member_holder.append(member)
        doc.all_ids.add(m_id)
        added += 1
        report.pk_members_added += 1
        report.note(f"re-linked PK member {tbl_phys}.{code}")

    return added > 0

def _fk_signature(ref: Dict) -> str:
    """Must mirror the validator comparator's _fk_signature exactly."""
    parent = _key(ref.get("parent_table", ""))
    child = _key(ref.get("child_table", ""))
    joins = tuple(sorted(
        (_key(j.get("parent_col", "")), _key(j.get("child_col", "")))
        for j in ref.get("join_columns", [])))
    return f"{parent}→{child}:{joins}"


def _rel_endpoints(doc: _ErwinDoc, rel: ET.Element) -> Tuple[str, str]:
    parent = doc.entity_by_id.get(_prop_text(rel, "Parent_Entity_Ref"))
    child = doc.entity_by_id.get(_prop_text(rel, "Child_Entity_Ref"))
    return (doc.entity_phys(parent) if parent is not None else "",
            doc.entity_phys(child) if child is not None else "")


def _fix_foreign_keys(doc: _ErwinDoc, report: PreprocessReport,
                      pd_model: Dict[str, Any], result) -> None:
    missing_sigs = {f.pd_value for f in getattr(result, "findings", [])
                    if f.category == "FOREIGN_KEY" and f.erwin_value == "—"}
    if not missing_sigs:
        return

    pd_refs = [r for r in pd_model.get("references", [])
               if _fk_signature(r) in missing_sigs]
    report.fk_targets = len(pd_refs)

    rels_by_pair: Dict[Tuple[str, str], List[ET.Element]] = {}
    for rel in doc.rels:
        rels_by_pair.setdefault(_rel_endpoints(doc, rel), []).append(rel)
    used_rel_ids: set = set()

    for ref in pd_refs:
        _repair_single_fk(doc, report, ref, rels_by_pair, used_rel_ids)

def _repair_single_fk(doc: _ErwinDoc, report: PreprocessReport, ref: Dict[str, Any], rels_by_pair: Dict[Tuple[str, str], List[ET.Element]], used_rel_ids: set) -> None:
    name = ref.get("name") or ref.get("code") or "?"
    parent_phys = _key(ref.get("parent_table", ""))
    child_phys = _key(ref.get("child_table", ""))
    joins = [j for j in ref.get("join_columns", [])]

    if not _validate_fk_joins(report, name, joins, parent_phys, child_phys):
        return

    parent_entity = doc.entities.get(parent_phys)
    child_entity = doc.entities.get(child_phys)
    if parent_entity is None or child_entity is None:
        report.fk_unfixable.append(name)
        report.note(f"FK '{name}': entity missing in erwin ({parent_phys}->{child_phys}); left in report")
        return

    rel = _find_matching_relationship(name, parent_phys, child_phys, rels_by_pair, used_rel_ids)
    if rel is None:
        report.fk_unfixable.append(name)
        report.note(f"FK '{name}': no erwin Relationship between {parent_phys} and {child_phys}; left in report")
        return

    _wire_fk_joins(doc, report, name, joins, parent_entity, child_entity, rel, parent_phys, child_phys, used_rel_ids)

def _validate_fk_joins(report: PreprocessReport, name: str, joins: list, parent_phys: str, child_phys: str) -> bool:
    if not joins or any(not j.get("parent_col") or not j.get("child_col") for j in joins):
        report.fk_unfixable.append(name)
        report.note(f"FK '{name}' ({parent_phys}->{child_phys}) cannot be restored: the PD reference has no complete column join")
        return False
    return True

def _find_matching_relationship(name: str, parent_phys: str, child_phys: str, rels_by_pair: dict, used_rel_ids: set) -> ET.Element:
    candidates = [r for r in rels_by_pair.get((parent_phys, child_phys), []) if r.get("id") not in used_rel_ids]
    rel = next((cand for cand in candidates if _key(cand.get("name", "")) == _key(name)), None)
    if rel is None and candidates:
        rel = candidates[0]
    return rel

def _wire_fk_joins(doc: _ErwinDoc, report: PreprocessReport, name: str, joins: list, parent_entity: ET.Element, child_entity: ET.Element, rel: ET.Element, parent_phys: str, child_phys: str, used_rel_ids: set) -> None:
    rel_id = rel.get("id")
    wired = 0
    for j in joins:
        parent_attr = doc.attr_by_code(parent_entity, j["parent_col"])
        child_attr = doc.attr_by_code(child_entity, j["child_col"])
        if parent_attr is None or child_attr is None:
            report.errors.append(f"FK '{name}': join column missing ({parent_phys}.{j['parent_col']} -> {child_phys}.{j['child_col']})")
            continue
        
        current = _prop_text(child_attr, "Parent_Attribute_Ref")
        if current and current in doc.all_ids:
            current_rel = _prop_text(child_attr, "Parent_Relationship_Ref")
            if current_rel and current_rel in doc.all_ids:
                wired += 1
                continue
        _set_prop(child_attr, "Parent_Relationship_Ref", rel_id)
        _set_prop(child_attr, "Parent_Attribute_Ref", parent_attr.get("id"))
        wired += 1

    if wired == len(joins):
        used_rel_ids.add(rel_id)
        report.fks_fixed += 1
        report.note(f"repaired FK '{name}' ({parent_phys}->{child_phys}) via relationship {rel.get('name', rel_id)}")
    else:
        report.fk_unfixable.append(name)

def _register_namespaces(source_xml: str) -> None:
    """
    Re-register the document's own namespace prefixes so the rewritten file
    keeps them.  Only one default namespace can survive; the erwin data
    namespace (the bulk of the document) gets it, the others get their
    conventional erwin prefixes.
    """
    declared: Dict[str, str] = {}
    try:
        for _, (prefix, uri) in safe_iterparse(source_xml, events=("start-ns",)):
            declared.setdefault(uri, prefix)
    except ET.ParseError:
        return

    # The DATA namespace must serialise as the DEFAULT namespace, whatever
    # prefix the source happened to declare for it: the model body (Entity,
    # EntityProps, Note_List...) lives there, and erwin's own exports write it
    # unprefixed. Tools that scan the file as text — the provenance audit's
    # content inventory among them — match on '<EntityProps>', so a prefixed
    # re-serialisation made every remediated file read as empty.
    used_prefixes = set()
    assignments: Dict[str, str] = {}
    if _DATA_NS in declared:
        assignments[_DATA_NS] = ""
    synthetic = 0
    for uri, prefix in declared.items():
        if uri in assignments:
            continue
        candidate = prefix or _KNOWN_PREFIXES.get(uri) or ""
        if not candidate or candidate in used_prefixes:
            candidate = _KNOWN_PREFIXES.get(uri) or ""
        while not candidate or candidate in used_prefixes:
            candidate = f"ns{synthetic}"
            synthetic += 1
        assignments[uri] = candidate
        used_prefixes.add(candidate)
    for uri, prefix in assignments.items():
        ET.register_namespace(prefix, uri)


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def preprocess_model(pd_model: Dict[str, Any],
                     result,
                     source_xml: str,
                     target_xml: str,
                     source_erwin: str = "",
                     target_erwin: str = "",
                     validator_config=None,
                     model_name: str = "") -> PreprocessReport:
    """
    Remediate one erwin XML export against its PowerDesigner source.

    ``validator_config`` is accepted for call-site compatibility. The
    remediation pass is driven entirely by the validator's findings on
    ``result``, so it takes no configuration of its own.
    """
    report = PreprocessReport(model_name=model_name)

    if validator_config is not None:
        logger.debug("validator_config supplied but not consumed by the "
                     "remediation pass for %s", model_name or target_xml)

    _register_namespaces(source_xml)
    tree = safe_parse(source_xml)
    doc = _ErwinDoc(tree.getroot())

    erwin_model_name = ""
    for elem in tree.getroot().iter():
        if _local(elem.tag) == "Model" and elem.get("id"):
            erwin_model_name = elem.get("name") or _prop_text(elem, "Name")
            break
    owner_model = erwin_model_name or model_name

    pd_tables = {_key(k): v for k, v in pd_model.get("tables", {}).items()}

    _restore_missing_columns(doc, report, owner_model, result, pd_tables)
    _relink_primary_keys(doc, report, owner_model, result, pd_tables)
    _fix_foreign_keys(doc, report, pd_model, result)

    os.makedirs(os.path.dirname(os.path.abspath(target_xml)), exist_ok=True)
    tree.write(target_xml, encoding="utf-8", xml_declaration=True)
    report.note(f"remediated XML written to {target_xml}")

    if source_erwin and target_erwin and os.path.exists(source_erwin):
        os.makedirs(os.path.dirname(os.path.abspath(target_erwin)), exist_ok=True)
        shutil.copy2(source_erwin, target_erwin)
        if not rebuild_erwin_binary(target_xml, target_erwin):
            report.note(".erwin binary carried forward unchanged (erwin COM not available here); regenerate it from the remediated XML on a Windows machine with erwin Data Modeler installed")

    return report

def _restore_missing_columns(doc: _ErwinDoc, report: PreprocessReport, owner_model: str, result, pd_tables: dict) -> None:
    for f in getattr(result, "findings", []):
        if f.category != "COLUMN" or f.erwin_value != "—":
            continue
        tbl, col = _key(f.table), _key(f.column)
        entity = doc.entities.get(tbl)
        pd_table = pd_tables.get(tbl)
        if entity is None or pd_table is None:
            report.errors.append(f"cannot restore {tbl}.{col}: table not matched")
            continue
        if doc.attr_by_code(entity, col) is not None:
            continue
        pd_col = next((c for c in pd_table.get("columns", []) if _key(c.get("code", "")) == col), None)
        if pd_col is None:
            report.errors.append(f"cannot restore {tbl}.{col}: not in PD model")
            continue
        _add_column(doc, report, owner_model, entity, pd_col)

def _relink_primary_keys(doc: _ErwinDoc, report: PreprocessReport, owner_model: str, result, pd_tables: dict) -> None:
    pk_tables = {_key(f.table) for f in getattr(result, "findings", []) if f.category == "PRIMARY_KEY" and f.erwin_value == "(none)"}
    pk_tables |= {_key(f.table) for f in getattr(result, "findings", []) if f.category == "COLUMN" and f.erwin_value == "—"}
    for tbl in sorted(pk_tables):
        entity = doc.entities.get(tbl)
        pd_table = pd_tables.get(tbl)
        if entity is None or pd_table is None:
            continue
        if _fix_primary_key(doc, report, owner_model, entity, pd_table):
            report.pks_fixed += 1

def rebuild_erwin_binary(xml_path: str, erwin_path: str) -> bool:
    """
    Regenerate the .erwin binary from a (remediated) XML export via erwin's
    COM API.  ``.erwin`` is a proprietary binary format (GDMM magic bytes), so
    this is only possible on Windows with erwin Data Modeler installed.
    Returns True on success; logs and returns False everywhere else.
    """
    try:
        # pywin32, Windows only
        import pythoncom  # noqa: F401
        import win32com.client
    except ImportError:
        logger.info("pywin32/erwin not available; skipping .erwin rebuild for %s",
                    erwin_path)
        return False
    try:
        import pythoncom
        pythoncom.CoInitialize()
        app = win32com.client.Dispatch("erwin9.SCAPI")
        units = app.PersistenceUnits
        unit = units.Add(os.path.abspath(xml_path))
        unit.Save(os.path.abspath(erwin_path), "OVF=ERWIN")
        pythoncom.CoUninitialize()
        logger.info("Rebuilt %s from %s via erwin SCAPI", erwin_path, xml_path)
        return True
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("erwin SCAPI rebuild failed for %s: %s (continuing "
                       "with the carried-forward binary)", erwin_path, exc)
        return False


# """
# PDM Preprocessor (remediation engine)
# =====================================
# Repairs an erwin XML export so it matches the PowerDesigner PDM it was imported
# from, using the validator's own findings as the work list.  PowerDesigner is
# the source of truth.  One missing attribute normally causes three separate
# findings, so all three are fixed together:

#   1. **Missing columns** — restored into the erwin entity with the PD data
#      type, nullability and default.
#   2. **Empty primary keys** — ``Key_Group_Member`` entries re-linked to the
#      (possibly just-restored) attributes; a PK ``Key_Group`` is created when
#      erwin has none.
#   3. **Broken foreign-key joins** — restored child columns are marked as
#      migrated (``Parent_Relationship_Ref`` / ``Parent_Attribute_Ref``) so the
#      erwin dialect's FK-join resolution finds them again, and children whose
#      parent pointer no longer resolves are repointed.  A pointer that already
#      resolves is never rewritten.

# What it deliberately does NOT do: invent data to raise a score.  A PD
# reference with no child column bound, or an abstract PD type realised
# physically by erwin, is left in the report — if those remain, the model does
# not reach 100% and is not promoted.  That is the gate working, not failing.

# ``.erwin`` files are binary (GDMM magic), writable only through erwin's COM
# API on Windows.  Remediation is therefore applied to the ``.xml`` standard
# export; ``rebuild_erwin_binary()`` regenerates the binary when erwin is
# available and logs + continues when it is not.

# The output XML is re-serialised with the document's own namespaces, so it
# stays valid for both the validator (which is namespace-agnostic) and erwin.
# """

# import logging
# import os
# import shutil
# import uuid
# import xml.etree.ElementTree as ET  # nosec B405
# from defusedxml.ElementTree import iterparse as safe_iterparse
# from defusedxml.ElementTree import parse as safe_parse
# from copy import deepcopy
# from dataclasses import dataclass, field
# from typing import Any, Dict, List, Optional, Tuple

# logger = logging.getLogger(__name__)

# # The namespace erwin's EMX dialect uses for model-body elements.
# _DATA_NS = "http://www.erwin.com/dm/data"  # NOSONAR
# # Stable prefixes for the other erwin namespaces when they were declared as
# # defaults in the source (only one default can survive re-serialisation).
# _KNOWN_PREFIXES = {
#     "http://www.erwin.com/dm": "EMX",           # NOSONAR
#     "http://www.erwin.com/dm/EM2data": "EM2",   # NOSONAR
#     "http://www.erwin.com/dm/metadata": "EMXMD",# NOSONAR
# }


# # ─── REPORT ───────────────────────────────────────────────────────────────────

# @dataclass
# class PreprocessReport:
#     """What the remediation pass actually changed."""
#     model_name: str = ""
#     columns_added: int = 0
#     pk_groups_created: int = 0
#     pk_members_added: int = 0
#     pks_fixed: int = 0                 # tables whose PK was re-linked
#     fk_targets: int = 0                # FKs the validator reported missing
#     fks_fixed: int = 0
#     fk_unfixable: List[str] = field(default_factory=list)
#     actions: List[str] = field(default_factory=list)
#     errors: List[str] = field(default_factory=list)

#     def note(self, message: str) -> None:
#         self.actions.append(message)
#         logger.info("  [preprocess] %s", message)

#     def summary(self) -> str:
#         parts = [f"restored {self.columns_added} column(s)",
#                  f"re-linked {self.pks_fixed} primary key(s)",
#                  f"repaired {self.fks_fixed}/{self.fk_targets} foreign key(s)"]
#         if self.fk_unfixable:
#             parts.append(f"{len(self.fk_unfixable)} FK(s) unfixable from source")
#         if self.errors:
#             parts.append(f"{len(self.errors)} error(s)")
#         return ", ".join(parts)


# # ─── XML HELPERS (namespace-agnostic, mirroring the validator's parser) ───────

# def _local(tag: Any) -> str:
#     if not isinstance(tag, str):
#         return ""
#     return tag.rsplit("}", 1)[-1]


# def _ns_of(tag: str) -> str:
#     """'{uri}Entity' -> '{uri}' (empty string when un-namespaced)."""
#     if isinstance(tag, str) and tag.startswith("{"):
#         return tag.split("}", 1)[0] + "}"
#     return ""


# def _first_child(elem: ET.Element, name: str) -> Optional[ET.Element]:
#     for c in list(elem):
#         if _local(c.tag) == name:
#             return c
#     return None


# def _props_child(elem: ET.Element) -> Optional[ET.Element]:
#     for c in list(elem):
#         if _local(c.tag).endswith("Props"):
#             return c
#     return None


# def _prop_text(elem: ET.Element, name: str) -> str:
#     """Value of <name> inside the element's <XxxProps> wrapper (or '')."""
#     props = _props_child(elem)
#     if props is None:
#         return ""
#     child = _first_child(props, name)
#     if child is not None and child.text:
#         return child.text.strip()
#     return ""


# def _set_prop(elem: ET.Element, name: str, value: str) -> None:
#     """Set (create if needed) <name> inside the element's props wrapper."""
#     props = _props_child(elem)
#     if props is None:
#         props = ET.SubElement(elem, f"{_ns_of(elem.tag)}{_local(elem.tag)}Props")
#     child = _first_child(props, name)
#     if child is None:
#         child = ET.SubElement(props, f"{_ns_of(props.tag)}{name}")
#     child.text = value


# def _del_prop(elem: ET.Element, name: str) -> None:
#     props = _props_child(elem)
#     if props is None:
#         return
#     child = _first_child(props, name)
#     if child is not None:
#         props.remove(child)


# def _new_id() -> str:
#     """A fresh object id in erwin's EMX format."""
#     return "{" + str(uuid.uuid4()).upper() + "}+00000000"


# def _key(name: str) -> str:
#     return (name or "").strip().upper()


# # ─── DOCUMENT INDEX ───────────────────────────────────────────────────────────

# class _ErwinDoc:
#     """Index of an EMX erwin XML document, built once and kept current."""

#     def __init__(self, root: ET.Element):
#         self.root = root
#         self.entities: Dict[str, ET.Element] = {}        # PHYS_NAME -> Entity
#         self.entity_by_id: Dict[str, ET.Element] = {}
#         self.rels: List[ET.Element] = []
#         self.all_ids: set = set()
#         for elem in root.iter():
#             oid = elem.get("id")
#             if oid:
#                 self.all_ids.add(oid)
#             name = _local(elem.tag)
#             if name == "Entity" and oid:
#                 phys = _key(_prop_text(elem, "Physical_Name") or elem.get("name", ""))
#                 if phys and phys not in self.entities:
#                     self.entities[phys] = elem
#                     self.entity_by_id[oid] = elem
#             elif name == "Relationship" and oid:
#                 self.rels.append(elem)

#     def entity_phys(self, elem: ET.Element) -> str:
#         return _key(_prop_text(elem, "Physical_Name") or elem.get("name", ""))

#     def attributes(self, entity: ET.Element) -> List[ET.Element]:
#         return [a for a in entity.iter()
#                 if _local(a.tag) == "Attribute" and a.get("id")]

#     def attr_by_code(self, entity: ET.Element, code: str) -> Optional[ET.Element]:
#         code = _key(code)
#         for a in self.attributes(entity):
#             phys = _key(_prop_text(a, "Physical_Name") or a.get("name", ""))
#             if phys == code:
#                 return a
#         return None

#     def key_groups(self, entity: ET.Element) -> List[ET.Element]:
#         return [k for k in entity.iter()
#                 if _local(k.tag) == "Key_Group" and k.get("id")]


# # ─── 1. MISSING COLUMNS ───────────────────────────────────────────────────────

# def _template_attribute(doc: _ErwinDoc, entity: ET.Element) -> Optional[ET.Element]:
#     """An existing Attribute to clone (same entity preferred, any entity else)."""
#     attrs = doc.attributes(entity)
#     if attrs:
#         return attrs[0]
#     for other in doc.entities.values():
#         attrs = doc.attributes(other)
#         if attrs:
#             return attrs[0]
#     return None


# def _attribute_container(entity: ET.Element) -> ET.Element:
#     grp = _first_child(entity, "Attribute_Groups")
#     if grp is None:
#         grp = ET.SubElement(entity, f"{_ns_of(entity.tag)}Attribute_Groups")
#     return grp


# def _add_column(doc: _ErwinDoc, report: PreprocessReport, model_name: str,
#                 entity: ET.Element, pd_col: Dict[str, Any]) -> Optional[ET.Element]:
#     template = _template_attribute(doc, entity)
#     if template is None:
#         report.errors.append(
#             f"No attribute template available to restore column "
#             f"'{pd_col.get('code')}' in '{doc.entity_phys(entity)}'")
#         return None

#     new = deepcopy(template)
#     new_id = _new_id()
#     new.set("id", new_id)
#     new.set("name", pd_col.get("name") or pd_col.get("code", ""))

#     tbl_phys = doc.entity_phys(entity)
#     order = len(doc.attributes(entity)) + 1

#     _set_prop(new, "Name", pd_col.get("name") or pd_col.get("code", ""))
#     _set_prop(new, "Long_Id", new_id)
#     if model_name:
#         _set_prop(new, "Owner_Path", f"{model_name}.{tbl_phys}")
#     _set_prop(new, "Physical_Name", _key(pd_col.get("code", "")))
#     dtype = pd_col.get("data_type") or ""
#     if dtype:
#         _set_prop(new, "Physical_Data_Type", dtype)
#         _set_prop(new, "Logical_Data_Type", dtype)
#     _set_prop(new, "Null_Option_Type", "1" if pd_col.get("not_null") else "0")
#     _set_prop(new, "Physical_Order", str(order))
#     if pd_col.get("comment"):
#         _set_prop(new, "Comment", pd_col["comment"])
#     if pd_col.get("default"):
#         _set_prop(new, "Default_Value", pd_col["default"])
#     else:
#         _del_prop(new, "Default_Value")
#     _set_prop(new, "Master_Attribute_Ref", new_id)
#     _set_prop(new, "User_Formatted_Name", pd_col.get("name") or pd_col.get("code", ""))
#     _set_prop(new, "User_Formatted_Physical_Name", _key(pd_col.get("code", "")))
#     # The clone must not inherit the template's migration/domain pointers.
#     for stale in ("Parent_Attribute_Ref", "Parent_Relationship_Ref",
#                   "Parent_Domain_Ref", "Definition", "Long_Id_Header"):
#         if stale in ("Parent_Attribute_Ref", "Parent_Relationship_Ref"):
#             _del_prop(new, stale)

#     _attribute_container(entity).append(new)
#     doc.all_ids.add(new_id)
#     report.columns_added += 1
#     report.note(f"restored column {tbl_phys}.{_key(pd_col.get('code',''))} "
#                 f"({dtype or 'no type'}, "
#                 f"{'NOT NULL' if pd_col.get('not_null') else 'NULL'})")
#     return new


# # ─── 2. PRIMARY KEYS ──────────────────────────────────────────────────────────

# def _pd_pk_columns(pd_table: Dict[str, Any]) -> List[str]:
#     for k in pd_table.get("keys", []):
#         if k.get("is_pk"):
#             return [_key(c) for c in k.get("columns", [])]
#     return []


# def _template_kgm(doc: _ErwinDoc) -> Optional[ET.Element]:
#     for elem in doc.root.iter():
#         if _local(elem.tag) == "Key_Group_Member" and elem.get("id"):
#             return elem
#     return None


# def _template_pk_group(doc: _ErwinDoc) -> Optional[ET.Element]:
#     for entity in doc.entities.values():
#         for kg in doc.key_groups(entity):
#             if _prop_text(kg, "Key_Group_Type").upper() == "PK":
#                 return kg
#     return None


# def _kg_member_codes(doc: _ErwinDoc, entity: ET.Element,
#                      kg: ET.Element) -> List[str]:
#     """Physical column codes currently linked by a key group's members."""
#     id_to_code = {}
#     for a in doc.attributes(entity):
#         id_to_code[a.get("id")] = _key(_prop_text(a, "Physical_Name")
#                                        or a.get("name", ""))
#     codes = []
#     for m in kg.iter():
#         if _local(m.tag) != "Key_Group_Member":
#             continue
#         ref = _prop_text(m, "Attribute_Ref")
#         code = id_to_code.get(ref) or _key(_prop_text(m, "Physical_Name")
#                                            or m.get("name", ""))
#         if code and ref in id_to_code:
#             codes.append(code)
#     return codes


# def _fix_primary_key(doc: _ErwinDoc, report: PreprocessReport, model_name: str,
#                        entity: ET.Element, pd_table: Dict[str, Any]) -> bool:
#     pd_pk = _pd_pk_columns(pd_table)
#     if not pd_pk:
#         return False
#     tbl_phys = doc.entity_phys(entity)

#     pk_group = _get_or_create_pk_group(doc, report, model_name, entity, tbl_phys)
#     if pk_group is None:
#         return False

#     existing = set(_kg_member_codes(doc, entity, pk_group))
#     missing = [c for c in pd_pk if c not in existing]
#     if not missing:
#         return False

#     return _add_missing_pk_members(doc, report, model_name, entity, tbl_phys, pk_group, missing)

# def _get_or_create_pk_group(doc: _ErwinDoc, report: PreprocessReport, model_name: str, entity: ET.Element, tbl_phys: str) -> ET.Element:
#     for kg in doc.key_groups(entity):
#         if _prop_text(kg, "Key_Group_Type").upper() == "PK":
#             return kg

#     template = _template_pk_group(doc)
#     if template is None:
#         report.errors.append(f"No PK Key_Group template for '{tbl_phys}'")
#         return None

#     pk_group = deepcopy(template)
#     for holder in list(pk_group):
#         if _local(holder.tag) == "Key_Group_Member_Groups":
#             pk_group.remove(holder)

#     kg_id = _new_id()
#     pk_group.set("id", kg_id)
#     pk_group.set("name", "Identifier_1")
#     _set_prop(pk_group, "Name", "Identifier_1")
#     _set_prop(pk_group, "Long_Id", kg_id)
#     if model_name:
#         _set_prop(pk_group, "Owner_Path", f"{model_name}.{tbl_phys}")
#     _set_prop(pk_group, "Key_Group_Type", "PK")
#     _set_prop(pk_group, "Physical_Name", f"{tbl_phys}_PK")
#     _set_prop(pk_group, "Is_Unique", "true")

#     holder = _first_child(entity, "Key_Group_Groups")
#     if holder is None:
#         holder = ET.SubElement(entity, f"{_ns_of(entity.tag)}Key_Group_Groups")
#     holder.append(pk_group)
#     doc.all_ids.add(kg_id)
#     report.pk_groups_created += 1
#     report.note(f"created PK Key_Group for {tbl_phys}")
#     return pk_group

# def _add_missing_pk_members(doc: _ErwinDoc, report: PreprocessReport, model_name: str, entity: ET.Element, tbl_phys: str, pk_group: ET.Element, missing: list) -> bool:
#     kgm_template = _template_kgm(doc)
#     member_holder = _first_child(pk_group, "Key_Group_Member_Groups")
#     if member_holder is None:
#         member_holder = ET.SubElement(pk_group, f"{_ns_of(pk_group.tag)}Key_Group_Member_Groups")

#     added = 0
#     order_base = len(list(member_holder))
#     for i, code in enumerate(missing, start=1):
#         attr = doc.attr_by_code(entity, code)
#         if attr is None:
#             report.errors.append(f"PK column '{code}' not found in erwin entity '{tbl_phys}'")
#             continue

#         member = deepcopy(kgm_template) if kgm_template is not None else ET.Element(f"{_ns_of(pk_group.tag)}Key_Group_Member")
#         m_id = _new_id()
#         logical = attr.get("name") or code
#         member.set("id", m_id)
#         member.set("name", logical)
#         _set_prop(member, "Name", logical)
#         _set_prop(member, "Long_Id", m_id)
#         if model_name:
#             _set_prop(member, "Owner_Path", f"{model_name}.{tbl_phys}.{pk_group.get('name', 'Identifier_1')}")
#         _set_prop(member, "Attribute_Ref", attr.get("id"))
#         _set_prop(member, "Physical_Name", code)
#         _set_prop(member, "Key_Group_Sort_Order", "ASC")
#         _set_prop(member, "Key_Group_Member_Order", str(order_base + i))
#         _set_prop(member, "Index_Member_Order", str(order_base + i))
#         _set_prop(member, "User_Formatted_Name", logical)
#         _set_prop(member, "User_Formatted_Physical_Name", code)
#         member_holder.append(member)
#         doc.all_ids.add(m_id)
#         added += 1
#         report.pk_members_added += 1
#         report.note(f"re-linked PK member {tbl_phys}.{code}")

#     return added > 0

# def _fk_signature(ref: Dict) -> str:
#     """Must mirror the validator comparator's _fk_signature exactly."""
#     parent = _key(ref.get("parent_table", ""))
#     child = _key(ref.get("child_table", ""))
#     joins = tuple(sorted(
#         (_key(j.get("parent_col", "")), _key(j.get("child_col", "")))
#         for j in ref.get("join_columns", [])))
#     return f"{parent}→{child}:{joins}"


# def _rel_endpoints(doc: _ErwinDoc, rel: ET.Element) -> Tuple[str, str]:
#     parent = doc.entity_by_id.get(_prop_text(rel, "Parent_Entity_Ref"))
#     child = doc.entity_by_id.get(_prop_text(rel, "Child_Entity_Ref"))
#     return (doc.entity_phys(parent) if parent is not None else "",
#             doc.entity_phys(child) if child is not None else "")


# def _fix_foreign_keys(doc: _ErwinDoc, report: PreprocessReport,
#                       pd_model: Dict[str, Any], result) -> None:
#     missing_sigs = {f.pd_value for f in getattr(result, "findings", [])
#                     if f.category == "FOREIGN_KEY" and f.erwin_value == "—"}
#     if not missing_sigs:
#         return

#     pd_refs = [r for r in pd_model.get("references", [])
#                if _fk_signature(r) in missing_sigs]
#     report.fk_targets = len(pd_refs)

#     rels_by_pair: Dict[Tuple[str, str], List[ET.Element]] = {}
#     for rel in doc.rels:
#         rels_by_pair.setdefault(_rel_endpoints(doc, rel), []).append(rel)
#     used_rel_ids: set = set()

#     for ref in pd_refs:
#         _repair_single_fk(doc, report, ref, rels_by_pair, used_rel_ids)

# def _repair_single_fk(doc: _ErwinDoc, report: PreprocessReport, ref: Dict[str, Any], rels_by_pair: Dict[Tuple[str, str], List[ET.Element]], used_rel_ids: set) -> None:
#     name = ref.get("name") or ref.get("code") or "?"
#     parent_phys = _key(ref.get("parent_table", ""))
#     child_phys = _key(ref.get("child_table", ""))
#     joins = [j for j in ref.get("join_columns", [])]

#     if not _validate_fk_joins(report, name, joins, parent_phys, child_phys):
#         return

#     parent_entity = doc.entities.get(parent_phys)
#     child_entity = doc.entities.get(child_phys)
#     if parent_entity is None or child_entity is None:
#         report.fk_unfixable.append(name)
#         report.note(f"FK '{name}': entity missing in erwin ({parent_phys}->{child_phys}); left in report")
#         return

#     rel = _find_matching_relationship(name, parent_phys, child_phys, rels_by_pair, used_rel_ids)
#     if rel is None:
#         report.fk_unfixable.append(name)
#         report.note(f"FK '{name}': no erwin Relationship between {parent_phys} and {child_phys}; left in report")
#         return

#     _wire_fk_joins(doc, report, name, joins, parent_entity, child_entity, rel, parent_phys, child_phys, used_rel_ids)

# def _validate_fk_joins(report: PreprocessReport, name: str, joins: list, parent_phys: str, child_phys: str) -> bool:
#     if not joins or any(not j.get("parent_col") or not j.get("child_col") for j in joins):
#         report.fk_unfixable.append(name)
#         report.note(f"FK '{name}' ({parent_phys}->{child_phys}) cannot be restored: the PD reference has no complete column join")
#         return False
#     return True

# def _find_matching_relationship(name: str, parent_phys: str, child_phys: str, rels_by_pair: dict, used_rel_ids: set) -> ET.Element:
#     candidates = [r for r in rels_by_pair.get((parent_phys, child_phys), []) if r.get("id") not in used_rel_ids]
#     rel = next((cand for cand in candidates if _key(cand.get("name", "")) == _key(name)), None)
#     if rel is None and candidates:
#         rel = candidates[0]
#     return rel

# def _wire_fk_joins(doc: _ErwinDoc, report: PreprocessReport, name: str, joins: list, parent_entity: ET.Element, child_entity: ET.Element, rel: ET.Element, parent_phys: str, child_phys: str, used_rel_ids: set) -> None:
#     rel_id = rel.get("id")
#     wired = 0
#     for j in joins:
#         parent_attr = doc.attr_by_code(parent_entity, j["parent_col"])
#         child_attr = doc.attr_by_code(child_entity, j["child_col"])
#         if parent_attr is None or child_attr is None:
#             report.errors.append(f"FK '{name}': join column missing ({parent_phys}.{j['parent_col']} -> {child_phys}.{j['child_col']})")
#             continue
        
#         current = _prop_text(child_attr, "Parent_Attribute_Ref")
#         if current and current in doc.all_ids:
#             current_rel = _prop_text(child_attr, "Parent_Relationship_Ref")
#             if current_rel and current_rel in doc.all_ids:
#                 wired += 1
#                 continue
#         _set_prop(child_attr, "Parent_Relationship_Ref", rel_id)
#         _set_prop(child_attr, "Parent_Attribute_Ref", parent_attr.get("id"))
#         wired += 1

#     if wired == len(joins):
#         used_rel_ids.add(rel_id)
#         report.fks_fixed += 1
#         report.note(f"repaired FK '{name}' ({parent_phys}->{child_phys}) via relationship {rel.get('name', rel_id)}")
#     else:
#         report.fk_unfixable.append(name)

# def _register_namespaces(source_xml: str) -> None:
#     """
#     Re-register the document's own namespace prefixes so the rewritten file
#     keeps them.  Only one default namespace can survive; the erwin data
#     namespace (the bulk of the document) gets it, the others get their
#     conventional erwin prefixes.
#     """
#     declared: Dict[str, str] = {}
#     try:
#         for _, (prefix, uri) in safe_iterparse(source_xml, events=("start-ns",)):
#             declared.setdefault(uri, prefix)
#     except ET.ParseError:
#         return

#     # The DATA namespace must serialise as the DEFAULT namespace, whatever
#     # prefix the source happened to declare for it: the model body (Entity,
#     # EntityProps, Note_List...) lives there, and erwin's own exports write it
#     # unprefixed. Tools that scan the file as text — the provenance audit's
#     # content inventory among them — match on '<EntityProps>', so a prefixed
#     # re-serialisation made every remediated file read as empty.
#     used_prefixes = set()
#     assignments: Dict[str, str] = {}
#     if _DATA_NS in declared:
#         assignments[_DATA_NS] = ""
#     synthetic = 0
#     for uri, prefix in declared.items():
#         if uri in assignments:
#             continue
#         candidate = prefix or _KNOWN_PREFIXES.get(uri) or ""
#         if not candidate or candidate in used_prefixes:
#             candidate = _KNOWN_PREFIXES.get(uri) or ""
#         while not candidate or candidate in used_prefixes:
#             candidate = f"ns{synthetic}"
#             synthetic += 1
#         assignments[uri] = candidate
#         used_prefixes.add(candidate)
#     for uri, prefix in assignments.items():
#         ET.register_namespace(prefix, uri)


# # ─── PUBLIC API ───────────────────────────────────────────────────────────────

# def preprocess_model(pd_model: Dict[str, Any],
#                      result,
#                      source_xml: str,
#                      target_xml: str,
#                      source_erwin: str = "",
#                      target_erwin: str = "",
#                      validator_config=None,
#                      model_name: str = "") -> PreprocessReport:
#     """
#     Remediate one erwin XML export against its PowerDesigner source.
#     """
#     report = PreprocessReport(model_name=model_name)

#     _register_namespaces(source_xml)
#     tree = safe_parse(source_xml)
#     doc = _ErwinDoc(tree.getroot())

#     erwin_model_name = ""
#     for elem in tree.getroot().iter():
#         if _local(elem.tag) == "Model" and elem.get("id"):
#             erwin_model_name = elem.get("name") or _prop_text(elem, "Name")
#             break
#     owner_model = erwin_model_name or model_name

#     pd_tables = {_key(k): v for k, v in pd_model.get("tables", {}).items()}

#     _restore_missing_columns(doc, report, owner_model, result, pd_tables)
#     _relink_primary_keys(doc, report, owner_model, result, pd_tables)
#     _fix_foreign_keys(doc, report, pd_model, result)

#     os.makedirs(os.path.dirname(os.path.abspath(target_xml)), exist_ok=True)
#     tree.write(target_xml, encoding="utf-8", xml_declaration=True)
#     report.note(f"remediated XML written to {target_xml}")

#     if source_erwin and target_erwin and os.path.exists(source_erwin):
#         os.makedirs(os.path.dirname(os.path.abspath(target_erwin)), exist_ok=True)
#         shutil.copy2(source_erwin, target_erwin)
#         if not rebuild_erwin_binary(target_xml, target_erwin):
#             report.note(".erwin binary carried forward unchanged (erwin COM not available here); regenerate it from the remediated XML on a Windows machine with erwin Data Modeler installed")

#     return report

# def _restore_missing_columns(doc: _ErwinDoc, report: PreprocessReport, owner_model: str, result, pd_tables: dict) -> None:
#     for f in getattr(result, "findings", []):
#         if f.category != "COLUMN" or f.erwin_value != "—":
#             continue
#         tbl, col = _key(f.table), _key(f.column)
#         entity = doc.entities.get(tbl)
#         pd_table = pd_tables.get(tbl)
#         if entity is None or pd_table is None:
#             report.errors.append(f"cannot restore {tbl}.{col}: table not matched")
#             continue
#         if doc.attr_by_code(entity, col) is not None:
#             continue
#         pd_col = next((c for c in pd_table.get("columns", []) if _key(c.get("code", "")) == col), None)
#         if pd_col is None:
#             report.errors.append(f"cannot restore {tbl}.{col}: not in PD model")
#             continue
#         _add_column(doc, report, owner_model, entity, pd_col)

# def _relink_primary_keys(doc: _ErwinDoc, report: PreprocessReport, owner_model: str, result, pd_tables: dict) -> None:
#     pk_tables = {_key(f.table) for f in getattr(result, "findings", []) if f.category == "PRIMARY_KEY" and f.erwin_value == "(none)"}
#     pk_tables |= {_key(f.table) for f in getattr(result, "findings", []) if f.category == "COLUMN" and f.erwin_value == "—"}
#     for tbl in sorted(pk_tables):
#         entity = doc.entities.get(tbl)
#         pd_table = pd_tables.get(tbl)
#         if entity is None or pd_table is None:
#             continue
#         if _fix_primary_key(doc, report, owner_model, entity, pd_table):
#             report.pks_fixed += 1

# def rebuild_erwin_binary(xml_path: str, erwin_path: str) -> bool:
#     """
#     Regenerate the .erwin binary from a (remediated) XML export via erwin's
#     COM API.  ``.erwin`` is a proprietary binary format (GDMM magic bytes), so
#     this is only possible on Windows with erwin Data Modeler installed.
#     Returns True on success; logs and returns False everywhere else.
#     """
#     try:
#         import pythoncom            # noqa: F401  (pywin32, Windows only)
#         import win32com.client
#     except ImportError:
#         logger.info("pywin32/erwin not available; skipping .erwin rebuild for %s",
#                     erwin_path)
#         return False
#     try:
#         import pythoncom
#         pythoncom.CoInitialize()
#         app = win32com.client.Dispatch("erwin9.SCAPI")
#         units = app.PersistenceUnits
#         unit = units.Add(os.path.abspath(xml_path))
#         unit.Save(os.path.abspath(erwin_path), "OVF=ERWIN")
#         pythoncom.CoUninitialize()
#         logger.info("Rebuilt %s from %s via erwin SCAPI", erwin_path, xml_path)
#         return True
#     except Exception as exc:                                   # noqa: BLE001
#         logger.warning("erwin SCAPI rebuild failed for %s: %s (continuing "
#                        "with the carried-forward binary)", erwin_path, exc)
#         return False
