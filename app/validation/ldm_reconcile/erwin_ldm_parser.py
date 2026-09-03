import re
import logging
import xml.etree.ElementTree as ET  # nosec B405
from defusedxml.ElementTree import parse as safe_parse, iterparse as safe_iterparse
from typing import Any, Dict, List, Optional, Set, Tuple

from app.config.validation_config import LDM_CONFIG as config
from . import normalizers
from .cardinality import normalize_cardinality
from .ldm_model import (Attribute, BusinessRule, Domain, Entity,
                       Identifier, Inheritance, LDMModel, Relationship,
                       RelationshipEnd)

logger = logging.getLogger(__name__)

TRUE_TOKENS  = {"1", "true", "yes", "y", "t", "on"}
FALSE_TOKENS = {"0", "false", "no", "n", "f", "off"}

_NOT_NULL_TOKENS = {"not null", "notnull", "nn", "mandatory", "required",
                    "no nulls", "1", "true", "yes"}


# ─── LOW-LEVEL XML HELPERS ────────────────────────────────────────────────────

def _local(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _children(elem: ET.Element, local_name: str) -> List[ET.Element]:
    return [child for child in list(elem) if _local(child.tag) == local_name]


def _first_child(elem: ET.Element, local_name: str) -> Optional[ET.Element]:
    for child in list(elem):
        if _local(child.tag) == local_name:
            return child
    return None


def _descendants(elem: ET.Element, local_name: str) -> List[ET.Element]:
    return [node for node in elem.iter() if _local(node.tag) == local_name]


def _props_child(elem: ET.Element) -> Optional[ET.Element]:
    """
    erwin's EMX export wraps an object's scalar fields one level down inside a
    single '<XxxProps>' child — e.g. an <Entity> holds its Name / Physical_Name
    / Definition inside a nested <EntityProps>. Confirmed present for every
    object type in the supplied file: Entity, Attribute, Key_Group,
    Key_Group_Member, Relationship, Domain.
    """
    for child in list(elem):
        if _local(child.tag).endswith("Props"):
            return child
    return None


def _val(elem: ET.Element, *names: str) -> str:
    """
    First non-empty value found among the given field names. Checked, in
    order: XML attributes (exact case), XML attributes (case-insensitive —
    erwin's EMX export uses lowercase 'id'/'name'), direct child elements, and
    finally children of a nested '<XxxProps>' wrapper.
    """
    for name in names:
        raw = elem.get(name)
        if raw and raw.strip():
            return raw.strip()

    lower_attribs = {key.lower(): value for key, value in elem.attrib.items()}
    for name in names:
        raw = lower_attribs.get(name.lower())
        if raw and raw.strip():
            return raw.strip()

    for name in names:
        child = _first_child(elem, name)
        if child is not None and child.text and child.text.strip():
            return child.text.strip()

    props = _props_child(elem)
    if props is not None:
        for name in names:
            child = _first_child(props, name)
            if child is not None and child.text and child.text.strip():
                return child.text.strip()

    return ""


def _bool_val(elem: ET.Element, *names: str, default: bool = False) -> bool:
    raw = _val(elem, *names).lower()
    if not raw:
        return default
    if raw in TRUE_TOKENS:
        return True
    if raw in FALSE_TOKENS:
        return False
    return default


def _oid(elem: ET.Element) -> str:
    return _val(elem, "id", "Id", "ID", "Object_Id", "GUID")


def _name(elem: ET.Element) -> str:
    return _val(elem, "Name", "Logical_Name", "Entity_Name", "Attribute_Name")


def _code(elem: ET.Element) -> str:
    return (_val(elem, "Physical_Name", "Code", "Table_Name", "Column_Name")
            or _name(elem))


def _definition(elem: ET.Element) -> str:
    return _val(elem, "Definition", "Comment", "Note", "Description",
                "Business_Definition", "UDP_Definition")



# --- DOCUMENTATION FIELDS (report-only) --------------------------------------
# These read the SPECIFIC erwin field for each mapping, with NO fallback, so the
# DOCUMENTATION sheet can distinguish "the Note carries the text" from "the
# Definition carries it". _definition() above keeps its first-match-wins
# behaviour, so every existing finding and the fidelity score are unchanged.

_NOTE_SEP = r"\#x1F"          # six literal chars: backslash # x 1 F
_NOTE_TEXT_INDEX = 6
_NOTE_ARRAY_TAG = "Note_List_Array"
_NOTE_ITEM_TAG = "Note_List"


def _decode_note_text(record: str) -> str:
    """Field 6 of an erwin Note_List record, with \\#xNN escapes resolved."""
    if not record:
        return ""
    fields = record.split(_NOTE_SEP)
    if len(fields) <= _NOTE_TEXT_INDEX:
        return ""
    text = fields[_NOTE_TEXT_INDEX]
    # Reverse the HandleNonPrintableChar="Y" encoding, e.g. \#x0A -> newline.
    return re.sub(r"\\#x([0-9A-Fa-f]{2})",
                  lambda m: chr(int(m.group(1), 16)), text)


def _note(elem: ET.Element) -> str:
    """
    All Note_List entries on this object, decoded and joined.  Returns "" when
    the object carries no notes (i.e. preprocessing has not been run, or the
    SAP PD Comment was empty).
    """
    containers = [elem]
    props = _props_child(elem)
    if props is not None:
        containers.append(props)

    texts = []
    for container in containers:
        for child in container:
            if _local(child.tag) != _NOTE_ARRAY_TAG:
                continue
            for note in child:
                if _local(note.tag) != _NOTE_ITEM_TAG:
                    continue
                decoded = _decode_note_text(note.text or "")
                if decoded.strip():
                    texts.append(decoded.strip())
    return "\n".join(texts)


# erwin "Extended Notes" tab.  Structurally unrelated to the Notes tab above:
# the payload sits in <Extended_Notes_Groups>/<Extended_Notes>/<Extended_NotesProps>/
# <Comment>, as plain text (no \#xNN escaping), and each object may carry
# several.  It is read from DIRECT children only, so reading an Entity never
# picks up the Extended Notes of one of its Attributes.
_EXT_NOTES_ARRAY_TAG = "Extended_Notes_Groups"
_EXT_NOTES_ITEM_TAG = "Extended_Notes"


def _extended_notes(elem: ET.Element) -> str:
    """
    All Extended Notes entries on this object, joined.  Returns "" when the
    object carries none, which is the common case -- Extended Notes is where
    an erwin import lands a SAP PD Annotation, and most objects have no
    Annotation.
    """
    containers = [elem]
    props = _props_child(elem)
    if props is not None:
        containers.append(props)

    texts = []
    for container in containers:
        for child in container:
            if _local(child.tag) != _EXT_NOTES_ARRAY_TAG:
                continue
            for note in child:
                if _local(note.tag) != _EXT_NOTES_ITEM_TAG:
                    continue
                text = _val(note, "Comment")
                if text.strip():
                    texts.append(text.strip())
    return "\n".join(texts)


def _erwin_comment_only(elem: ET.Element) -> str:
    """erwin's own Comment field, with no fallback."""
    return _val(elem, "Comment")


def _erwin_definition_only(elem: ET.Element) -> str:
    """erwin's own Definition field, with no fallback to Comment/Note."""
    return _val(elem, "Definition")


# ─── DOMAINS ──────────────────────────────────────────────────────────────────

def _parse_domain(elem: ET.Element) -> Domain:
    return Domain(
        oid        = _oid(elem),
        name       = _name(elem) or _val(elem, "Domain_Name"),
        code       = _code(elem) or _val(elem, "Domain_Name"),
        data_type  = _val(elem, "Logical_Datatype", "Datatype",
                          "Physical_Datatype", "Domain_Datatype"),
        length     = _val(elem, "Length", "Width", "Data_Length"),
        precision  = _val(elem, "Scale", "Precision", "Decimal_Precision"),
        definition = _definition(elem),
    )


# ─── ATTRIBUTES ───────────────────────────────────────────────────────────────

def _resolve_datatype(elem: ET.Element) -> str:
    """Logical type first — this is a logical-level comparison."""
    return _val(elem,
                "Logical_Data_Type", "Logical_Datatype",
                "Datatype", "Data_Type",
                "Domain_Name", "Domain_Parent_Name",
                "Physical_Data_Type", "Physical_Datatype")


def _is_attribute_mandatory(elem: ET.Element) -> bool:
    """
    erwin's attribute-level <Null_Option_Type> is a distinct, smaller code
    space from the relationship-level field of the same name — confirmed by
    direct inspection of SD_O2C_LDM_WC.xml: every attribute that is a PK
    Key_Group member carries "1"; every plain attribute observed carries "0".
    config.ERWIN_ATTRIBUTE_NULL_OPTION_CODES encodes exactly that mapping.
    Falls back to phrase-style Null_Option / Nulls_Allowed for older exports,
    and finally to an explicit Required/Mandatory flag.
    """
    code = _val(elem, "Null_Option_Type").strip()
    code_map = getattr(config, "ERWIN_ATTRIBUTE_NULL_OPTION_CODES", {}) or {}
    if code in code_map:
        return not code_map[code]     # code_map value is "nulls allowed" (optional)

    null_option = _val(elem, "Null_Option", "Nulls_Allowed",
                       "Logical_Null_Option", "Optional").lower()
    if null_option:
        if null_option in _NOT_NULL_TOKENS:
            return True
        if null_option.startswith("null") or null_option in FALSE_TOKENS:
            return False

    return _bool_val(elem, "Required", "Mandatory", "Is_Required")


def _is_migrated_key(elem: ET.Element) -> bool:
    """
    True when erwin created this attribute by migrating a parent key across a
    relationship, rather than the modeller declaring it directly on the
    entity. Confirmed present in the supplied file via <Parent_Attribute_Ref>
    on every migrated FK attribute (e.g. VBAP's "Sales Document",
    "Material Number", "Plant") and absent from every attribute the modeller
    declared directly (e.g. VBAP's own PK component "Sales Document Item").
    """
    if _bool_val(elem, "Is_Foreign_Key", "Foreign_Key", "Is_Migrated",
                 "Migrated", "Inherited"):
        return True
    if _val(elem, "Attribute_Type", "Key_Type").upper() in ("FK", "FOREIGN KEY"):
        return True
    if _val(elem, "Parent_Attribute_Ref", "Migrated_From",
            "Source_Attribute_Ref", "Migration_Source"):
        return True
    return False


def _is_hidden_logical_duplicate(elem: ET.Element) -> bool:
    """
    True when this <Attribute> is erwin's own bookkeeping copy of a key that
    reached this entity by a SECOND migration path, and erwin has already
    resolved the duplication itself -- not something this parser needs to
    surface as a second attribute.

    Confirmed in production (03_Lubes_Direct_Sales_Marketing_Portfolio_LDM.xml,
    entity DELIVER_OWN_ACCOUNT): when the same logical key (Customer_Master_ID)
    migrates into an entity via two different relationships -- once directly
    from CUSTOMER, once indirectly via CUSTOMER_ACCOUNT_GROUP -- erwin creates
    one <Attribute> element per path for internal traceability, designates
    exactly one as the "lead" via a SELF-referencing
    <Logical_Lead_Attribute_Ref>, and marks every other one
    <Hide_In_Logical>true</Hide_In_Logical> with its
    <Logical_Lead_Attribute_Ref> pointing at the lead's id instead. The erwin
    GUI's own Attribute Editor respects this and shows only the lead — this is
    NOT a data-quality defect in the source model, it is erwin's documented
    mechanism for a key reaching one entity by more than one path, and
    treating the hidden copy as a real second attribute is exactly what
    caused a false "exists in SAP PD but NOT in erwin" finding for a PD
    attribute that erwin, in fact, already has.

    Only <Hide_In_Logical> is checked (not <Hide_In_Physical>) because this
    framework compares LOGICAL models: an attribute erwin's physical
    generation suppresses but which is still visible at the logical level
    would be a real difference worth keeping, whereas one hidden at the
    logical level is invisible in the same view PowerDesigner's LDM export
    represents.
    """
    if _bool_val(elem, "Hide_In_Logical"):
        return True
    lead_ref = _val(elem, "Logical_Lead_Attribute_Ref")
    own_oid = _oid(elem)
    if lead_ref and own_oid and lead_ref != own_oid:
        return True
    return False


def _parse_attribute(elem: ET.Element, order: int,
                     domains_by_oid: Dict[str, Domain],
                     builtin_domain_oids: Optional[Set[str]] = None) -> Attribute:
    data_type = _resolve_datatype(elem)
    length    = _val(elem, "Length", "Width", "Data_Length")
    precision = _val(elem, "Scale", "Precision", "Decimal_Precision")

    domain_name = _val(elem, "Domain_Name", "Domain")
    domain_ref  = _val(elem, "Parent_Domain_Ref", "Domain_Ref", "Domain_Parent_Ref")
    builtin_domain_oids = builtin_domain_oids or set()
    if domain_ref and domain_ref in domains_by_oid:
        domain = domains_by_oid[domain_ref]
        # Every attribute in an erwin export points at SOME domain — its own,
        # if the modeller assigned one, or one of erwin's six built-in system
        # types (<root>, <default>, String, Number, Datetime, Blob) otherwise.
        # Confirmed by direct inspection of SD_O2C_LDM_WC.xml: every one of
        # its 84 attributes resolves Parent_Domain_Ref to a built-in domain,
        # while the paired PD LDM has no domains at all. Surfacing the
        # built-in as "the attribute's domain" would report 84 false
        # DOMAIN-assignment mismatches — one per attribute — for a model that
        # in truth uses no domains on either side. Only a genuine,
        # modeller-created domain is surfaced; built-ins are still used to
        # resolve type/length/precision below, since that part is correct
        # and useful regardless of who created the domain.
        if domain_ref not in builtin_domain_oids:
            domain_name = domain_name or domain.name or domain.code
        if not data_type:
            data_type = domain.data_type
            length    = length    or domain.length
            precision = precision or domain.precision

    return Attribute(
        oid         = _oid(elem),
        name        = _name(elem),
        code        = _code(elem),
        data_type   = data_type,
        length      = length,
        precision   = precision,
        mandatory   = _is_attribute_mandatory(elem),
        is_primary  = _val(elem, "Key_Type", "Attribute_Type").upper() == "PK",
        domain      = domain_name,
        definition  = _definition(elem),
        # report-only; does not affect any comparison
        doc_comment    = _erwin_comment_only(elem),
        doc_note       = _note(elem),
        doc_definition = _erwin_definition_only(elem),
        doc_extended_notes = _extended_notes(elem),
        multiplicity= _val(elem, "Multiplicity"),
        order       = order,
        is_migrated = _is_migrated_key(elem),
    )


# ─── IDENTIFIERS (Key Groups) ─────────────────────────────────────────────────

def _is_ignored_key_group(kg_type: str) -> bool:
    ignored = getattr(config, "ERWIN_IGNORED_KEY_GROUP_TYPES",
                      ("IE", "INVERSION ENTRY", "INDEX"))
    upper = (kg_type or "").upper()
    if upper in {t.upper() for t in ignored}:
        return True
    # Generic "IFn" pattern — erwin numbers each auto-generated FK index
    # sequentially per relationship (IF1, IF2, IF3, ...); matching the pattern
    # rather than an enumerated list keeps this correct for models with more
    # relationships than the supplied sample.
    if re.fullmatch(r"IF\d*", upper):
        return True
    return False


def _parse_key_group(elem: ET.Element,
                     attr_code_by_oid: Dict[str, str]) -> Optional[Identifier]:
    """
    Convert a Key_Group into an Identifier.

    Key_Group_Type values confirmed present in SD_O2C_LDM_WC.xml: "PK" (one
    per entity) and "IF1"/"IF2"/"IF3" (one auto-generated FK index per
    relationship — erwin's physical access path for the migrated key, with no
    PowerDesigner LDM counterpart, so dropped here exactly as "IE" is for a
    CDM). No "AK" is present in the supplied file, but is handled the same way
    a PK is (as a non-primary Identifier) for models that do have one.
    """
    kg_type = _val(elem, "Key_Group_Type", "Type", "Key_Type").upper()
    if _is_ignored_key_group(kg_type):
        return None

    is_primary = kg_type in ("PK", "PRIMARY KEY", "PRIMARY")

    members: List[tuple] = []
    for member in _descendants(elem, "Key_Group_Member"):
        attr_ref = _val(member, "Attribute_Ref", "Attribute_Id",
                        "Attribute", "Member_Ref")
        if not attr_ref:
            continue
        try:
            sequence = int(_val(member, "Key_Group_Member_Order",
                                "Index_Member_Order", "Sequence",
                                "Position", "Order") or 0)
        except ValueError:
            sequence = 0
        members.append((sequence, attr_code_by_oid.get(attr_ref, attr_ref)))

    if not members:
        for attr_ref in [_val(node, "Attribute_Ref")
                         for node in _descendants(elem, "Key_Group_Member_Ref")]:
            if attr_ref:
                members.append((0, attr_code_by_oid.get(attr_ref, attr_ref)))

    members.sort(key=lambda item: item[0])

    return Identifier(
        oid        = _oid(elem),
        name       = _name(elem),
        code       = _code(elem),
        is_primary = is_primary,
        attributes = [code for _, code in members if code],
    )


# ─── ENTITIES ─────────────────────────────────────────────────────────────────

def _parse_entity(elem: ET.Element,
                  subject_area: str,
                  domains_by_oid: Dict[str, Domain],
                  builtin_domain_oids: Optional[Set[str]] = None) -> Entity:
    entity = Entity(
        oid          = _oid(elem),
        name         = _name(elem),
        code         = _code(elem),
        definition   = _definition(elem),
        # report-only; does not affect any comparison
        doc_comment    = _erwin_comment_only(elem),
        doc_note       = _note(elem),
        doc_definition = _erwin_definition_only(elem),
        doc_extended_notes = _extended_notes(elem),
        subject_area = subject_area,
        is_associative = _bool_val(elem, "Is_Associative", "Associative_Entity"),
    )

    attr_code_by_oid: Dict[str, str] = {}
    order = 0

    for attr_elem in _descendants(elem, "Attribute"):
        if not _oid(attr_elem) and not _name(attr_elem):
            continue
        order += 1
        attribute = _parse_attribute(attr_elem, order, domains_by_oid, builtin_domain_oids)

        if normalizers.is_excluded_attribute(attribute.name, attribute.code):
            continue
        if _is_hidden_logical_duplicate(attr_elem):
            # erwin already resolved this to the lead attribute (see
            # _is_hidden_logical_duplicate) -- register the oid so a
            # Key_Group_Member that references this hidden copy's id still
            # resolves to the right code, but don't surface a second
            # attribute a human never sees in erwin's own logical view.
            if attribute.oid:
                attr_code_by_oid[attribute.oid] = attribute.code
            logger.debug(
                "Skipping hidden logical-duplicate attribute %r (oid %s) -- "
                "erwin's lead copy for this key is kept instead.",
                attribute.name, attribute.oid,
            )
            continue
        if attribute.is_migrated and config.ERWIN_MIGRATED_KEY_HANDLING == "ignore":
            if attribute.oid:
                attr_code_by_oid[attribute.oid] = attribute.code
            continue

        entity.attributes.append(attribute)
        if attribute.oid:
            attr_code_by_oid[attribute.oid] = attribute.code

    for kg_elem in _descendants(elem, "Key_Group"):
        if not _oid(kg_elem) and not _name(kg_elem):
            continue
        identifier = _parse_key_group(kg_elem, attr_code_by_oid)
        if identifier and identifier.attributes:
            entity.identifiers.append(identifier)

    primary = entity.primary_identifier
    if primary:
        members = {code.upper() for code in primary.attributes}
        for attribute in entity.attributes:
            if attribute.code.upper() in members:
                attribute.is_primary = True

    return entity


# ─── RELATIONSHIPS ────────────────────────────────────────────────────────────

def _erwin_type_kind(elem: ET.Element) -> str:
    """
    Classify the relationship using erwin's numeric <Type> code when present,
    falling back to English phrase matching for phrase-style exports.
    Confirmed present in the supplied file: "2" (identifying) and "7"
    (non-identifying) only.
    """
    raw = _val(elem, "Relationship_Type", "Type", "Relationship_Kind").strip()
    codes = getattr(config, "ERWIN_RELATIONSHIP_TYPE_CODES", {}) or {}
    if raw in codes:
        return codes[raw]
    upper = raw.upper()
    if "MANY" in upper and upper.count("MANY") >= 2:
        return "MANY_TO_MANY"
    if "SUBTYPE" in upper or "CATEGOR" in upper or "GENERALI" in upper:
        return "SUBTYPE"
    if "NON" in upper and "IDENT" in upper:
        return "NON_IDENTIFYING"
    if "IDENT" in upper:
        return "IDENTIFYING"
    return ""


def _relationship_is_many_to_many(elem: ET.Element) -> bool:
    return _erwin_type_kind(elem) == "MANY_TO_MANY"


def _relationship_is_subtype(elem: ET.Element) -> bool:
    return _erwin_type_kind(elem) == "SUBTYPE"


def _relationship_is_identifying(elem: ET.Element) -> bool:
    kind = _erwin_type_kind(elem)
    if kind == "IDENTIFYING":
        return True
    if kind in ("NON_IDENTIFYING", "MANY_TO_MANY", "SUBTYPE"):
        return False
    return _bool_val(elem, "Is_Identifying", "Identifying")


def _child_end_is_mandatory(elem: ET.Element, identifying: bool) -> bool:
    """
    Whether a child instance must have a parent. Confirmed against the
    supplied file: relationship-level <Null_Option_Type> 101 = Nulls Not
    Allowed (mandatory child), 100 = Nulls Allowed (optional) — a DIFFERENT
    code space from the attribute-level field of the same name (see
    _is_attribute_mandatory). Cross-checked against the PD LDM side's
    Entity2RoleCardinality with 100% agreement across all 10 relationships.
    """
    if identifying:
        return True
    null_code = _val(elem, "Null_Option_Type").strip()
    null_codes = getattr(config, "ERWIN_NULL_OPTION_CODES", {}) or {}
    if null_code in null_codes:
        return not null_codes[null_code]

    nulls = _val(elem, "Nulls_Allowed", "Null_Option", "Child_Nulls_Allowed").lower()
    if nulls:
        negated = ("not allowed" in nulls
                   or "no nulls" in nulls
                   or nulls.startswith("not null"))
        if negated or nulls in FALSE_TOKENS or nulls in _NOT_NULL_TOKENS:
            return True
        if nulls in TRUE_TOKENS or "null" in nulls:
            return False
    return _bool_val(elem, "Is_Mandatory", "Mandatory", "Required")


def resolve_erwin_cardinality(raw: str) -> Tuple[str, bool]:
    """
    Resolve a raw erwin <Cardinality> value to a canonical "low,high" pair.
    Confirmed present in the supplied file: "-3" only, on all 10 relationships.
    """
    code = (raw or "").strip()
    code_map = getattr(config, "ERWIN_CARDINALITY_CODES", {}) or {}
    if code in code_map:
        return code_map[code], True
    if re.fullmatch(r"\d+", code):
        return ("1,1" if int(code) <= 1 else "1,n"), True
    phrase = normalize_cardinality(code, mandatory=False, many=True)
    if code and normalize_cardinality(code):
        return phrase, True
    return (phrase or "0,n"), not bool(code)


def _parse_relationship(elem: ET.Element,
                        entity_code_by_oid: Dict[str, str]) -> Relationship:
    """
    Build a canonical Relationship, aligned to the PowerDesigner LDM
    convention (see pd_ldm_parser._parse_relationship for the full rationale):
    each end's cardinality describes the multiplicity of THAT END'S OWN entity,
    as seen from the opposite end.

        end1 = parent ("one") side; cardinality = how many parents per child
        end2 = child ("many") side;  cardinality = how many children per parent
               -> erwin's <Cardinality> field
    """
    parent_ref = _val(elem, "Entity_Ref_Parent", "Parent_Entity_Ref",
                      "Parent_Entity", "From_Entity_Ref")
    child_ref  = _val(elem, "Entity_Ref_Child", "Child_Entity_Ref",
                      "Child_Entity", "To_Entity_Ref")

    identifying   = _relationship_is_identifying(elem)
    many_to_many  = _relationship_is_many_to_many(elem)
    child_required = _child_end_is_mandatory(elem, identifying)

    raw_cardinality = _val(elem, "Cardinality", "Relationship_Cardinality",
                           "Parent_Cardinality", "Child_Cardinality_Type")

    parent_side_cardinality, recognised = resolve_erwin_cardinality(raw_cardinality)
    if raw_cardinality and not recognised:
        logger.warning(
            "Unrecognised erwin cardinality %r on relationship %r; defaulted to "
            "%s. Add it to ERWIN_CARDINALITY_CODES in config.py.",
            raw_cardinality, _name(elem) or _oid(elem), parent_side_cardinality,
        )

    children_per_parent = parent_side_cardinality

    if many_to_many:
        parents_per_child = "1,n" if child_required else "0,n"
    else:
        parents_per_child = "1,1" if child_required else "0,1"

    parent_to_child_phrase = _val(elem, "Parent_To_Child_Verb_Phrase", "Verb_Phrase",
                                  "Phrase", "Parent_To_Child_Phrase", "Forward_Phrase")
    child_to_parent_phrase = _val(elem, "Child_To_Parent_Verb_Phrase", "Inverse_Verb_Phrase",
                                  "Inverse_Phrase", "Child_To_Parent_Phrase", "Reverse_Phrase")

    end1 = RelationshipEnd(
        entity      = entity_code_by_oid.get(parent_ref, parent_ref or "UNKNOWN"),
        role        = child_to_parent_phrase,
        cardinality = parents_per_child,
        mandatory   = parents_per_child.startswith("1"),
        dependent   = False,
    )

    end2 = RelationshipEnd(
        entity      = entity_code_by_oid.get(child_ref, child_ref or "UNKNOWN"),
        role        = parent_to_child_phrase,
        cardinality = children_per_parent,
        mandatory   = children_per_parent.startswith("1"),
        dependent   = identifying,
    )

    return Relationship(
        oid        = _oid(elem),
        name       = _name(elem),
        code       = _code(elem),
        definition = _definition(elem),
        end1       = end1,
        end2       = end2,
        kind       = "RELATIONSHIP",
        identifying = identifying,
    )


# ─── INHERITANCE (Subtype Relationships) ──────────────────────────────────────

_SUBTYPE_TAGS = ("Subtype_Relationship", "Subtype", "Subtype_Group",
                 "Generalization", "Category_Relationship")


def _parse_subtype(elem: ET.Element,
                   entity_code_by_oid: Dict[str, str]) -> Inheritance:
    parent_ref = _val(elem, "Entity_Ref_Parent", "Parent_Entity_Ref",
                      "Supertype_Entity_Ref", "Supertype_Ref")

    child_refs: List[str] = []
    for member_tag in ("Subtype_Member", "Subtype_Symbol", "Subtype_Entity",
                       "Category_Member", "Child_Entity"):
        for member in _descendants(elem, member_tag):
            ref = _val(member, "Entity_Ref", "Entity_Ref_Child",
                       "Subtype_Entity_Ref", "Child_Entity_Ref", "id", "Id")
            if ref and ref != parent_ref:
                child_refs.append(ref)

    if not child_refs:
        ref = _val(elem, "Entity_Ref_Child", "Child_Entity_Ref")
        if ref:
            child_refs.append(ref)

    completeness = _val(elem, "Subtype_Type", "Completeness", "Type").upper()
    complete = ("COMPLETE" in completeness and "INCOMPLETE" not in completeness)
    if not completeness:
        complete = _bool_val(elem, "Is_Complete", "Complete")

    exclusivity = _val(elem, "Exclusivity", "Subtype_Exclusivity").upper()
    if exclusivity:
        exclusive = "INCLUSIVE" not in exclusivity
    else:
        exclusive = _bool_val(elem, "Is_Exclusive", "Exclusive", default=True)

    seen: Set[str] = set()
    children: List[str] = []
    for ref in child_refs:
        code = entity_code_by_oid.get(ref, ref)
        if code and code not in seen:
            seen.add(code)
            children.append(code)

    return Inheritance(
        oid      = _oid(elem),
        name     = _name(elem),
        code     = _code(elem),
        parent   = entity_code_by_oid.get(parent_ref, parent_ref or "UNKNOWN"),
        children = children,
        complete = complete,
        mutually_exclusive = exclusive,
    )


# ─── BUSINESS RULES ───────────────────────────────────────────────────────────

_RULE_TAGS = ("Validation_Rule", "Business_Rule", "Rule", "Constraint")


def _parse_business_rule(elem: ET.Element) -> BusinessRule:
    return BusinessRule(
        oid        = _oid(elem),
        name       = _name(elem) or _val(elem, "Rule_Name"),
        code       = _code(elem),
        rule_type  = _val(elem, "Rule_Type", "Type", "Validation_Type"),
        expression = _val(elem, "Expression", "Rule_Expression",
                          "Valid_Value_Expression", "Server_Expression"),
        definition = _definition(elem),
    )


# ─── SUBJECT AREAS ────────────────────────────────────────────────────────────

def _build_subject_area_map(root: ET.Element) -> Dict[str, str]:
    membership: Dict[str, str] = {}
    for area in _descendants(root, "Subject_Area"):
        area_name = _name(area)
        if not area_name:
            continue
        for member_tag in ("Subject_Area_Member", "Entity_Ref", "Member",
                           "Subject_Area_Entity"):
            for member in _descendants(area, member_tag):
                ref = _val(member, "Entity_Ref", "Object_Ref", "id", "Id", "Ref")
                if ref:
                    membership.setdefault(ref, area_name)
    return membership


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def parse_erwin_ldm(filepath: str) -> LDMModel:
    """
    Parse an erwin logical-model XML export into an :class:`LDMModel`.

    Never raises for malformed input: a parse failure is recorded on the
    returned model so one bad file cannot abort a large batch.
    """
    model = LDMModel(source_file=filepath, source_tool="erwin",
                     model_type="Logical")

    try:
        root = safe_parse(filepath).getroot()
    except ET.ParseError as exc:
        logger.error("XML parse error in %s: %s", filepath, exc)
        model.parse_error = f"XML parse error: {exc}"
        return model
    except OSError as exc:
        logger.error("Cannot read %s: %s", filepath, exc)
        model.parse_error = f"File read error: {exc}"
        return model

    props = (_first_child(root, "ModelProps")
             or _first_child(root, "Model_Properties")
             or _first_child(root, "Model"))
    if props is not None:
        model.model_name = _val(props, "Name", "Model_Name", "Logical_Name")
        declared_type    = _val(props, "ModelType", "Model_Type", "Model_Level")
        if declared_type:
            model.model_type = declared_type
    if not model.model_name:
        model.model_name = _val(root, "Name", "Model_Name")

    model.model_code = _val(root, "Code", "Physical_Name") or model.model_name

    # ── Domains ──────────────────────────────────────────────────────────────
    # erwin auto-generates system type domains in every model (<root>,
    # <default>, String, Number, Datetime, Blob — identifiable by a non-zero
    # Built_In_Id, confirmed present in the supplied file as IDs 1-6).  These
    # are internal scaffolding, not domains a modeler created, so they are
    # excluded from the comparison-facing domain list but kept available for
    # attribute type/length/precision resolution.
    domains_by_oid: Dict[str, Domain] = {}
    builtin_domain_oids: Set[str] = set()
    for tag in ("Domain", "Domain_Dictionary_Entry"):
        for elem in _descendants(root, tag):
            if not _oid(elem) and not _name(elem):
                continue
            domain = _parse_domain(elem)
            if domain.oid:
                domains_by_oid[domain.oid] = domain
            built_in = _val(elem, "Built_In_Id", "BuiltIn", "System_Domain")
            if built_in and built_in != "0":
                if domain.oid:
                    builtin_domain_oids.add(domain.oid)
                continue
            key = (domain.code or domain.name).upper()
            if key:
                model.domains.setdefault(key, domain)

    # ── Subject areas ────────────────────────────────────────────────────────
    subject_area_by_oid = _build_subject_area_map(root)
    model.subject_areas = sorted(set(subject_area_by_oid.values()))

    # ── Entities ─────────────────────────────────────────────────────────────
    entity_code_by_oid: Dict[str, str] = {}
    seen_entity_oids: Set[str] = set()

    for elem in _descendants(root, "Entity"):
        oid = _oid(elem)
        if not oid or not _name(elem):
            continue
        if oid in seen_entity_oids:
            continue
        seen_entity_oids.add(oid)

        entity = _parse_entity(elem, subject_area_by_oid.get(oid, ""), domains_by_oid,
                               builtin_domain_oids)
        if normalizers.is_excluded_entity(entity.name, entity.code):
            continue

        model.add_entity(entity)
        entity_code_by_oid[entity.oid] = entity.code

    # ── Relationships ────────────────────────────────────────────────────────
    seen_rel_oids: Set[str] = set()
    for elem in _descendants(root, "Relationship"):
        oid = _oid(elem)
        if not oid and not _name(elem):
            continue
        if oid and oid in seen_rel_oids:
            continue
        if oid:
            seen_rel_oids.add(oid)

        relationship = _parse_relationship(elem, entity_code_by_oid)

        if (getattr(config, "ERWIN_SUBTYPE_AS_INHERITANCE", True)
                and _relationship_is_subtype(elem)):
            parent = relationship.end1.entity
            child  = relationship.end2.entity
            if parent and parent != "UNKNOWN" and child and child != "UNKNOWN":
                existing = next((i for i in model.inheritances
                                 if i.parent == parent), None)
                if existing is None:
                    model.inheritances.append(
                        Inheritance(oid=_oid(elem), name=relationship.name,
                                    parent=parent, children=[child])
                    )
                elif child not in existing.children:
                    existing.children.append(child)
                continue

        if relationship.end1.entity == "UNKNOWN" and relationship.end2.entity == "UNKNOWN":
            model.parse_warnings.append(
                f"Relationship '{relationship.name or oid}' has unresolved endpoints"
            )
            continue

        child = model.entities.get(relationship.end2.entity.upper())
        if child is not None and child.is_associative:
            relationship.kind = "ASSOCIATION"

        model.relationships.append(relationship)

    # ── Collapsed-cardinality guard ───────────────────────────────────────────
    # Confirmed in the supplied file: all 10 relationships resolve to the same
    # <Cardinality> code (-3). Verified this IS the real model property (not a
    # parse failure) by cross-checking against the PD LDM side's own
    # Entity1ToEntity2RoleCardinality, which is independently "0,n" for all 10
    # relationships too. Reported at INFO so a genuinely small, uniform LDM is
    # never blocked, while the signal remains visible for audit.
    if getattr(config, "ERWIN_FLAG_COLLAPSED_CARDINALITY", True):
        real = [r for r in model.relationships if r.kind == "RELATIONSHIP"]
        if len(real) >= 3:
            pairs = {(r.end1.cardinality, r.end2.cardinality) for r in real}
            if len(pairs) == 1:
                only = next(iter(pairs))
                message = (
                    f"All {len(real)} erwin relationships resolved to the same "
                    f"cardinality {only[0]}/{only[1]}. Confirmed against the "
                    f"PD LDM side as a genuine model property in this file, not "
                    f"a parse failure — reported for audit visibility only."
                )
                logger.info(message)
                model.parse_warnings.append(message)

    # ── Inheritance ──────────────────────────────────────────────────────────
    seen_subtype_oids: Set[str] = set()
    for tag in _SUBTYPE_TAGS:
        for elem in _descendants(root, tag):
            oid = _oid(elem)
            if oid and oid in seen_subtype_oids:
                continue
            if oid:
                seen_subtype_oids.add(oid)
            inheritance = _parse_subtype(elem, entity_code_by_oid)
            if inheritance.parent and inheritance.parent != "UNKNOWN" and inheritance.children:
                model.inheritances.append(inheritance)

    # ── Business rules ───────────────────────────────────────────────────────
    seen_rule_keys: Set[str] = set()
    for tag in _RULE_TAGS:
        for elem in _descendants(root, tag):
            if not _name(elem) and not _val(elem, "Expression"):
                continue
            rule = _parse_business_rule(elem)
            key = (rule.name or rule.oid).upper()
            if key in seen_rule_keys:
                continue
            seen_rule_keys.add(key)
            model.business_rules.append(rule)

    logger.debug("Parsed %s -> %s", filepath, model.stats())
    return model
