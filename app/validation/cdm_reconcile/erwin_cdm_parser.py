"""
erwin Data Modeler Logical / Conceptual XML Parser
-------------------------------------------------
Parses the XML export produced by:

    CA ERwin Data Modeler 9.x
    erwin Data Modeler 2019 / 2020 / 2021 / 2022+

    File → Save As → XML          (ERwin_Metamodel.DataModel)
    File → Export → XML           (same schema)

Object mapping onto the canonical conceptual model:

    Entity                          → Entity
    Attribute                       → Attribute
    Key_Group  (Key_Group_Type=PK)  → primary Identifier
    Key_Group  (Key_Group_Type=AK)  → alternate Identifier
    Key_Group  (Key_Group_Type=IE)  → ignored (physical access path, not semantics)
    Relationship                    → Relationship (+ cardinality, verb phrases)
    Subtype_Relationship            → Inheritance
    Domain                          → Domain
    Validation_Rule / Business_Rule → BusinessRule
    Subject_Area                    → subject-area membership

Two structural facts drive the design:

  • **erwin scalars live in either XML attributes or child elements** depending on
    version and export option.  Every read goes through ``_val``, which checks
    both, so one parser covers every release rather than one per release.

  • **erwin migrates parent keys into child entities; PowerDesigner CDM does not.**
    A conceptual model expresses the foreign key *as* the relationship, so those
    migrated attributes have no CDM counterpart by design.  They are tagged here
    and filtered per ``config.ERWIN_MIGRATED_KEY_HANDLING`` rather than silently
    inflating the difference count.
"""

import re
import logging
import xml.etree.ElementTree as ET  # nosec B405
from defusedxml.ElementTree import parse as safe_parse, iterparse as safe_iterparse
from typing import Any, Dict, List, Optional, Set, Tuple

from app.config.validation_config import CDM_CONFIG as config
from . import normalizers
from .cardinality import normalize_cardinality
from .cdm_model import (Attribute, BusinessRule, CDMModel, Domain, Entity,
                       Identifier, Inheritance, Relationship, RelationshipEnd)

logger = logging.getLogger(__name__)

TRUE_TOKENS  = {"1", "true", "yes", "y", "t", "on"}
FALSE_TOKENS = {"0", "false", "no", "n", "f", "off"}

# Null_Option spellings that mean "value required"
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
    Newer erwin 'EMX' XML exports (erwin 10.x+) wrap an object's scalar fields
    one level down inside a single '<XxxProps>' child — e.g. an <Entity> holds
    its Name / Physical_Name / Definition inside a nested <EntityProps>, not as
    direct children or attributes of <Entity> itself. Same pattern for
    Attribute → AttributeProps, Key_Group → Key_GroupProps, Relationship →
    RelationshipProps, Domain → DomainProps. Returns that wrapper if present.
    """
    for child in list(elem):
        if _local(child.tag).endswith("Props"):
            return child
    return None


def _attribute_value(elem: ET.Element, names: Tuple[str, ...]) -> str:
    """First non-empty XML attribute among `names`: exact case, then case-insensitive."""
    for name in names:
        raw = elem.get(name)
        if raw and raw.strip():
            return raw.strip()

    lower_attribs = {key.lower(): value for key, value in elem.attrib.items()}
    for name in names:
        raw = lower_attribs.get(name.lower())
        if raw and raw.strip():
            return raw.strip()
    return ""


def _child_text(elem: ET.Element, names: Tuple[str, ...]) -> str:
    """First non-empty text of a direct child element named in `names`."""
    for name in names:
        child = _first_child(elem, name)
        if child is not None and child.text and child.text.strip():
            return child.text.strip()
    return ""


def _val(elem: ET.Element, *names: str) -> str:
    """
    First non-empty value found among the given field names. Checked, in
    order: XML attributes (exact case), XML attributes (case-insensitive —
    newer erwin EMX exports use lowercase 'id'/'name' attributes), direct
    child elements, and finally children of a nested '<XxxProps>' wrapper
    (see ``_props_child``). erwin uses all of these conventions
    interchangeably across versions and export options.
    """
    value = _attribute_value(elem, names)
    if value:
        return value

    value = _child_text(elem, names)
    if value:
        return value

    props = _props_child(elem)
    if props is not None:
        return _child_text(props, names)

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
    """Business (logical) name."""
    return _val(elem, "Name", "Logical_Name", "Entity_Name", "Attribute_Name")


def _code(elem: ET.Element) -> str:
    """Technical name, falling back to the business name."""
    return (_val(elem, "Physical_Name", "Code", "Table_Name", "Column_Name")
            or _name(elem))


def _definition(elem: ET.Element) -> str:
    return _val(elem, "Definition", "Comment", "Note", "Description",
                "Business_Definition", "UDP_Definition")


# ── documentation mapping accessors (additive; _definition is untouched) ─────
# The framework's preprocessing stage (app/preprocessing/scripts/
# pd_comment_to_erwin_note.py) writes SAP PD Comments into erwin as
#     <Note_List_Array><Note_List index="0">…record…</Note_List></Note_List_Array>
# placed inside <EntityProps> / <AttributeProps>.  The record is 12 fields
# joined by the six literal characters  \#x1F  , with the note text at index 6
# and non-printable characters escaped as \#xNN.  _val() cannot see this: it
# looks for a child element named "Note", and the payload is a packed record
# rather than plain text.  These helpers read it properly so the report can
# show Comments → Notes as an actual mapping.

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


def _doc_containers(elem: ET.Element) -> List[ET.Element]:
    """The element itself plus its '<XxxProps>' wrapper, when it has one."""
    containers = [elem]
    props = _props_child(elem)
    if props is not None:
        containers.append(props)
    return containers


def _note_list_texts(container: ET.Element) -> List[str]:
    """Decoded, non-blank Note_List entries directly under `container`."""
    texts = []
    for child in container:
        if _local(child.tag) != _NOTE_ARRAY_TAG:
            continue
        for note in child:
            if _local(note.tag) != _NOTE_ITEM_TAG:
                continue
            decoded = _decode_note_text(note.text or "")
            if decoded.strip():
                texts.append(decoded.strip())
    return texts


def _note(elem: ET.Element) -> str:
    """
    All Note_List entries on this object, decoded and joined.  Returns "" when
    the object carries no notes (i.e. preprocessing has not been run, or the
    SAP PD Comment was empty).
    """
    texts = []
    for container in _doc_containers(elem):
        texts.extend(_note_list_texts(container))
    return "\n".join(texts)


def _erwin_definition_only(elem: ET.Element) -> str:
    """erwin's own Definition field, with no fallback to Comment/Note."""
    return _val(elem, "Definition")


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
    texts = []
    for container in _doc_containers(elem):
        texts.extend(_extended_note_texts(container))
    return "\n".join(texts)


def _extended_note_texts(container: ET.Element) -> List[str]:
    """Non-blank Extended_Notes comments directly under `container`."""
    texts = []
    for child in container:
        if _local(child.tag) != _EXT_NOTES_ARRAY_TAG:
            continue
        for note in child:
            if _local(note.tag) != _EXT_NOTES_ITEM_TAG:
                continue
            text = _val(note, "Comment")
            if text.strip():
                texts.append(text.strip())
    return texts


def _erwin_comment_only(elem: ET.Element) -> str:
    """erwin's own Comment field, with no fallback."""
    return _val(elem, "Comment")


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
    """
    Logical type first — this is a conceptual comparison, so erwin's logical
    datatype is the right side of the equation.  The physical datatype is used
    only as a last resort, when the logical one was never set.
    """
    return _val(elem,
                "Logical_Datatype", "Logical_Data_Type",
                "Datatype", "Data_Type",
                "Domain_Name", "Domain_Parent_Name",
                "Physical_Datatype", "Physical_Data_Type")


def _is_mandatory(elem: ET.Element) -> bool:
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
    relationship, rather than the modeller declaring it on the entity.
    """
    if _bool_val(elem, "Is_Foreign_Key", "Foreign_Key", "Is_Migrated",
                 "Migrated", "Inherited"):
        return True
    if _val(elem, "Attribute_Type", "Key_Type").upper() in ("FK", "FOREIGN KEY"):
        return True
    # A migration source reference is the most reliable signal of all.
    if _val(elem, "Migrated_From", "Parent_Attribute_Ref",
            "Source_Attribute_Ref", "Migration_Source"):
        return True
    return False


def _parse_attribute(elem: ET.Element, order: int,
                     domains_by_oid: Dict[str, Domain]) -> Attribute:
    data_type = _resolve_datatype(elem)
    length    = _val(elem, "Length", "Width", "Data_Length")
    precision = _val(elem, "Scale", "Precision", "Decimal_Precision")

    domain_name = _val(elem, "Domain_Name", "Domain")
    domain_ref  = _val(elem, "Domain_Ref", "Domain_Parent_Ref")
    if domain_ref and domain_ref in domains_by_oid:
        domain = domains_by_oid[domain_ref]
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
        mandatory   = _is_mandatory(elem),
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

def _parse_key_group(elem: ET.Element,
                     attr_code_by_oid: Dict[str, str]) -> Optional[Identifier]:
    """
    Convert a Key_Group into an Identifier.

    Key_Group_Type:
        PK             → primary identifier
        AK             → alternate identifier
        IE / IF1 / IF2 → inversion entry: a physical access path with no
                         conceptual meaning.  Flagged rather than dropped, so
                         the report still accounts for every key group in the
                         export, but never compared against a CDM alternate
                         identifier (a CDM cannot contain one).

    Dialect note.  erwin's flat export writes the literal token "IE".  Its
    metamodel export numbers them instead — IF1, IF2, IF3 … one per foreign key
    on the entity — so a fixed token list cannot recognise them.  Both dialects
    are handled below, and any non-unique non-PK key group is treated as an
    access path regardless of what it is called: uniqueness is what makes a key
    group a candidate key, so a group that is explicitly not unique cannot be an
    alternate identifier under any naming convention.
    """
    kg_type = _val(elem, "Key_Group_Type", "Type", "Key_Type").upper().strip()
    is_primary = kg_type in ("PK", "PRIMARY KEY", "PRIMARY")

    is_inversion_entry = (
        kg_type in ("IE", "INVERSION ENTRY", "INDEX", "FK", "FOREIGN KEY")
        or (kg_type.startswith("IF") and kg_type[2:].isdigit())
        or (not is_primary
            and _val(elem, "Is_Unique", "Unique").strip().lower()
                in ("false", "0", "no", "n"))
    )

    # Members carry an explicit sequence; identifier order is part of its meaning.
    members: List[tuple] = []
    for member in _descendants(elem, "Key_Group_Member"):
        attr_ref = _val(member, "Attribute_Ref", "Attribute_Id",
                        "Attribute", "Member_Ref")
        if not attr_ref:
            continue
        try:
            sequence = int(_val(member, "Sequence", "Position", "Order") or 0)
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
        comment    = _val(elem, "Comment", "Definition", "Description"),
        is_inversion_entry = is_inversion_entry,
    )


# ─── ENTITIES ─────────────────────────────────────────────────────────────────

def _parse_entity(elem: ET.Element,
                  subject_area: str,
                  domains_by_oid: Dict[str, Domain]) -> Entity:
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

    # ── Attributes ───────────────────────────────────────────────────────────
    attr_code_by_oid = _collect_entity_attributes(entity, elem, domains_by_oid)

    # ── Identifiers ──────────────────────────────────────────────────────────
    _collect_entity_identifiers(entity, elem, attr_code_by_oid)
    _mark_primary_attributes(entity)

    return entity


def _register_attribute(entity: Entity, attribute: Attribute,
                        attr_code_by_oid: Dict[str, str]) -> None:
    """Place one parsed attribute on the entity per the migrated-key handling mode."""
    if attribute.is_migrated:
        # Remembered on the entity regardless of the handling mode: the
        # identifier comparison needs to know which key members arrived by
        # migration, and under "ignore" they never reach entity.attributes.
        entity.migrated_attributes.append(attribute)

    if attribute.is_migrated and config.ERWIN_MIGRATED_KEY_HANDLING == "ignore":
        # Still needed for identifier resolution, just not for comparison.
        if attribute.oid:
            attr_code_by_oid[attribute.oid] = attribute.code
        return

    entity.attributes.append(attribute)
    if attribute.oid:
        attr_code_by_oid[attribute.oid] = attribute.code


def _collect_entity_attributes(entity: Entity, elem: ET.Element,
                               domains_by_oid: Dict[str, Domain]) -> Dict[str, str]:
    """Parse the entity's attributes; returns the attribute oid → code map."""
    attr_code_by_oid: Dict[str, str] = {}
    order = 0

    for attr_elem in _descendants(elem, "Attribute"):
        if not _oid(attr_elem) and not _name(attr_elem):
            continue
        order += 1
        attribute = _parse_attribute(attr_elem, order, domains_by_oid)

        if normalizers.is_excluded_attribute(attribute.name, attribute.code):
            continue
        _register_attribute(entity, attribute, attr_code_by_oid)

    return attr_code_by_oid


def _collect_entity_identifiers(entity: Entity, elem: ET.Element,
                                attr_code_by_oid: Dict[str, str]) -> None:
    for kg_elem in _descendants(elem, "Key_Group"):
        if not _oid(kg_elem) and not _name(kg_elem):
            continue
        identifier = _parse_key_group(kg_elem, attr_code_by_oid)
        if identifier and identifier.attributes:
            entity.identifiers.append(identifier)


def _mark_primary_attributes(entity: Entity) -> None:
    primary = entity.primary_identifier
    if primary:
        members = {code.upper() for code in primary.attributes}
        for attribute in entity.attributes:
            if attribute.code.upper() in members:
                attribute.is_primary = True


# ─── RELATIONSHIPS ────────────────────────────────────────────────────────────

def _erwin_type_kind(elem: ET.Element) -> str:
    """
    Classify the relationship using erwin's numeric <Type> code when present,
    falling back to English phrase matching for phrase-style exports.

    Returns one of IDENTIFYING, NON_IDENTIFYING, MANY_TO_MANY, SUBTYPE, or "".
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
    Whether a child instance must have a parent.  erwin expresses this through
    Nulls_Allowed on the relationship; identifying relationships are mandatory
    by definition, because the parent key is part of the child's identity.
    """
    if identifying:
        return True
    # erwin's metamodel export names this Null_Option_Type and stores a numeric
    # code (101 = Nulls Not Allowed, 100 = Nulls Allowed).  Neither the field
    # name nor the value form was recognised before, so every child end fell
    # through to the identifying-based default and read as optional.
    null_code = _val(elem, "Null_Option_Type").strip()
    null_codes = getattr(config, "ERWIN_NULL_OPTION_CODES", {}) or {}
    if null_code in null_codes:
        return not null_codes[null_code]      # nulls allowed -> optional

    nulls = _val(elem, "Nulls_Allowed", "Null_Option", "Child_Nulls_Allowed").lower()
    if nulls:
        # Test the NEGATED spellings first.  erwin's Null Option renders as the
        # display string "Nulls Not Allowed", which begins with "null" — so a
        # leading-"null" test placed first reads a mandatory end as optional.
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

    Single source of truth shared by the parser and diagnose_cardinality.py so
    the diagnostic can never disagree with what the parser actually does.

    Returns (cardinality, recognised).  When recognised is False the caller is
    looking at a value this build does not understand and the returned value is
    the 0,n default rather than a reading of the data.
    """
    code = (raw or "").strip()
    code_map = getattr(config, "ERWIN_CARDINALITY_CODES", {}) or {}
    if code in code_map:
        return code_map[code], True
    if re.fullmatch(r"\d+", code):
        # A positive value is the n of erwin's "Exactly n".
        return ("1,1" if int(code) <= 1 else "1,n"), True
    phrase = normalize_cardinality(code, mandatory=False, many=True)
    if code and normalize_cardinality(code):
        return phrase, True
    return (phrase or "0,n"), not bool(code)


def _parse_relationship(elem: ET.Element,
                        entity_code_by_oid: Dict[str, str]) -> Relationship:
    """
    Build a canonical Relationship, aligned to the PowerDesigner convention:

    End convention — MUST match pd_cdm_parser, which follows PowerDesigner:
    each end's cardinality describes the multiplicity of THAT END'S OWN entity,
    as seen from the opposite end.  PowerDesigner stores
    Entity1ToEntity2RoleCardinality = "how many Entity1 per one Entity2".

        end1 = parent ("one") side; cardinality = how many parents per child
               → 1,1 when the child end is mandatory, else 0,1
        end2 = child ("many") side;  cardinality = how many children per parent
               → erwin's <Cardinality> field

    erwin's own field naming is the mirror image of this (its <Cardinality>
    counts children, i.e. describes the OTHER end), so the two values are
    assigned to the ends that own them rather than to the ends erwin names.
    Getting this backwards keeps both degrees at 1:N yet reports every
    relationship as changed, because entity↔cardinality pairing differs
    between the two parsers.
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

    # erwin's metamodel export stores this as an integer code, not a phrase.
    # Negative codes select an option; a positive value is the n of "Exactly n".
    parent_side_cardinality, recognised = resolve_erwin_cardinality(raw_cardinality)
    if raw_cardinality and not recognised:
        logger.warning(
            "Unrecognised erwin cardinality %r on relationship %r; defaulted to "
            "%s. Add it to ERWIN_CARDINALITY_CODES in config.py.",
            raw_cardinality, _name(elem) or _oid(elem), parent_side_cardinality,
        )

    # How many CHILDREN exist per parent — erwin's <Cardinality>.  This
    # describes the child entity, so it belongs on the child end.
    children_per_parent = parent_side_cardinality

    # How many PARENTS exist per child — derived from the child end's
    # nullability.  This describes the parent entity, so it belongs on the
    # parent end.  An M:N relationship has no single parent per child.
    if many_to_many:
        parents_per_child = "1,n" if child_required else "0,n"
    else:
        parents_per_child = "1,1" if child_required else "0,1"

    # PowerDesigner's Entity1ToEntity2Role sits on the end whose entity plays
    # that role.  Empirically it corresponds to erwin's parent-to-child verb
    # phrase (PD: 151 forward / 4 reverse; erwin: 150 forward / 5 reverse), and
    # PD's Entity1 is erwin's CHILD — so the parent-to-child phrase belongs on
    # the child end.
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
    )


# ─── INHERITANCE (Subtype Relationships) ──────────────────────────────────────

_SUBTYPE_TAGS = ("Subtype_Relationship", "Subtype", "Subtype_Group",
                 "Generalization", "Category_Relationship")


def _subtype_child_refs(elem: ET.Element, parent_ref: str) -> List[str]:
    """Entity references of every subtype member, in declaration order."""
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
    return child_refs


def _subtype_is_complete(elem: ET.Element) -> bool:
    # erwin describes completeness as Complete / Incomplete, either as a flag
    # or as free text.
    completeness = _val(elem, "Subtype_Type", "Completeness", "Type").upper()
    complete = ("COMPLETE" in completeness and "INCOMPLETE" not in completeness)
    if not completeness:
        complete = _bool_val(elem, "Is_Complete", "Complete")
    return complete


def _subtype_is_exclusive(elem: ET.Element) -> bool:
    # erwin describes exclusivity as Exclusive / Inclusive, either as a flag or
    # as free text.
    exclusivity = _val(elem, "Exclusivity", "Subtype_Exclusivity").upper()
    if exclusivity:
        return "INCLUSIVE" not in exclusivity
    return _bool_val(elem, "Is_Exclusive", "Exclusive", default=True)


def _unique_child_codes(child_refs: List[str],
                        entity_code_by_oid: Dict[str, str]) -> List[str]:
    """Resolve refs to entity codes, de-duplicated while preserving declaration order."""
    seen: Set[str] = set()
    children: List[str] = []
    for ref in child_refs:
        code = entity_code_by_oid.get(ref, ref)
        if code and code not in seen:
            seen.add(code)
            children.append(code)
    return children


def _parse_subtype(elem: ET.Element,
                   entity_code_by_oid: Dict[str, str]) -> Inheritance:
    parent_ref = _val(elem, "Entity_Ref_Parent", "Parent_Entity_Ref",
                      "Supertype_Entity_Ref", "Supertype_Ref")

    child_refs = _subtype_child_refs(elem, parent_ref)
    complete   = _subtype_is_complete(elem)
    exclusive  = _subtype_is_exclusive(elem)
    children   = _unique_child_codes(child_refs, entity_code_by_oid)

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
    """Map entity oid → subject-area name using Subject_Area membership lists."""
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

def parse_erwin(filepath: str) -> CDMModel:
    """
    Parse an erwin logical-model XML export into a :class:`CDMModel`.

    Never raises for malformed input: a parse failure is recorded on the returned
    model so one bad file cannot abort a large batch.
    """
    model = CDMModel(source_file=filepath, source_tool="erwin",
                     model_type="Logical")

    root = _read_root(model, filepath)
    if root is None:
        return model

    # ── Model header ─────────────────────────────────────────────────────────
    _parse_model_header(model, root)

    # ── Domains ──────────────────────────────────────────────────────────────
    domains_by_oid = _parse_domains(model, root)

    # ── Subject areas ────────────────────────────────────────────────────────
    subject_area_by_oid = _build_subject_area_map(root)
    model.subject_areas = sorted(set(subject_area_by_oid.values()))

    # ── Entities ─────────────────────────────────────────────────────────────
    entity_code_by_oid = _parse_entities(model, root, subject_area_by_oid, domains_by_oid)

    # ── Relationships ────────────────────────────────────────────────────────
    _parse_relationships(model, root, entity_code_by_oid)

    # ── Collapsed-cardinality guard ───────────────────────────────────────────
    _flag_collapsed_cardinality(model)

    # ── Inheritance ──────────────────────────────────────────────────────────
    _parse_inheritances(model, root, entity_code_by_oid)

    # ── Business rules ───────────────────────────────────────────────────────
    _parse_business_rules(model, root)

    logger.debug("Parsed %s → %s", filepath, model.stats())
    return model


def _read_root(model: CDMModel, filepath: str) -> Optional[ET.Element]:
    """Root element of the export, or None with the failure recorded on `model`."""
    try:
        return safe_parse(filepath).getroot()
    except ET.ParseError as exc:
        logger.error("XML parse error in %s: %s", filepath, exc)
        model.parse_error = f"XML parse error: {exc}"
        return None
    except OSError as exc:
        logger.error("Cannot read %s: %s", filepath, exc)
        model.parse_error = f"File read error: {exc}"
        return None


def _tagged_descendants(root: ET.Element, tags: Tuple[str, ...]) -> List[ET.Element]:
    """Descendants of `root` carrying any of `tags`, grouped by tag in the order given."""
    return [elem for tag in tags for elem in _descendants(root, tag)]


def _parse_model_header(model: CDMModel, root: ET.Element) -> None:
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


def _register_domain(model: CDMModel, elem: ET.Element,
                     domains_by_oid: Dict[str, Domain]) -> None:
    domain = _parse_domain(elem)
    if domain.oid:
        domains_by_oid[domain.oid] = domain
    built_in = _val(elem, "Built_In_Id", "BuiltIn", "System_Domain")
    if built_in and built_in != "0":
        return
    key = (domain.code or domain.name).upper()
    if key:
        model.domains.setdefault(key, domain)


def _parse_domains(model: CDMModel, root: ET.Element) -> Dict[str, Domain]:
    """
    Parse every domain; returns the oid → Domain map.

    erwin auto-generates a handful of built-in system type domains in every
    model (<root>, <default>, String, Number, Datetime, Blob — identifiable
    by a non-zero Built_In_Id). These are internal scaffolding, not domains
    a modeler created, so comparing them against the CDM produces false
    "domain missing" findings. Excluded from the comparison-facing domain
    list — but kept in domains_by_oid, since a real attribute may
    legitimately be typed against one and still needs it for data-type/
    length/precision resolution.
    """
    domains_by_oid: Dict[str, Domain] = {}
    for elem in _tagged_descendants(root, ("Domain", "Domain_Dictionary_Entry")):
        if not _oid(elem) and not _name(elem):
            continue
        _register_domain(model, elem, domains_by_oid)
    return domains_by_oid


def _parse_entities(model: CDMModel, root: ET.Element,
                    subject_area_by_oid: Dict[str, str],
                    domains_by_oid: Dict[str, Domain]) -> Dict[str, str]:
    """Parse every entity onto `model`; returns the entity oid → code map."""
    entity_code_by_oid: Dict[str, str] = {}
    seen_entity_oids: Set[str] = set()

    for elem in _descendants(root, "Entity"):
        oid = _oid(elem)
        # Skip pointer-style <Entity Ref="..."/> nodes and repeats.
        if not oid or not _name(elem):
            continue
        if oid in seen_entity_oids:
            continue
        seen_entity_oids.add(oid)

        entity = _parse_entity(elem, subject_area_by_oid.get(oid, ""), domains_by_oid)
        if normalizers.is_excluded_entity(entity.name, entity.code):
            continue

        model.add_entity(entity)
        entity_code_by_oid[entity.oid] = entity.code

    return entity_code_by_oid


def _absorb_subtype_relationship(model: CDMModel, elem: ET.Element,
                                 relationship: Relationship) -> bool:
    """
    erwin encodes a subtype/generalisation as a <Relationship> with
    Type=9 (no verb phrases, no PowerDesigner counterpart).  Left in the
    relationship list, each one is reported as EXTRA_IN_ERWIN against a
    CDM that models it as an inheritance.

    Returns True when the relationship was recorded as an inheritance and must
    not be added to the relationship list.
    """
    if not (getattr(config, "ERWIN_SUBTYPE_AS_INHERITANCE", True)
            and _relationship_is_subtype(elem)):
        return False
    parent = relationship.end1.entity
    child  = relationship.end2.entity
    if not (parent and parent != "UNKNOWN" and child and child != "UNKNOWN"):
        return False
    existing = next((i for i in model.inheritances
                     if i.parent == parent), None)
    if existing is None:
        model.inheritances.append(
            Inheritance(oid=_oid(elem), name=relationship.name,
                        parent=parent, children=[child])
        )
    elif child not in existing.children:
        existing.children.append(child)
    return True


def _register_relationship(model: CDMModel, relationship: Relationship, oid: str) -> None:
    if relationship.end1.entity == "UNKNOWN" and relationship.end2.entity == "UNKNOWN":
        model.parse_warnings.append(
            f"Relationship '{relationship.name or oid}' has unresolved endpoints"
        )
        return

    # An associative entity plus its two relationships is erwin's rendering
    # of a many-to-many association; tag it so it reconciles with a
    # PowerDesigner Association rather than looking like a stray relationship.
    child = model.entities.get(relationship.end2.entity.upper())
    if child is not None and child.is_associative:
        relationship.kind = "ASSOCIATION"

    model.relationships.append(relationship)


def _parse_relationships(model: CDMModel, root: ET.Element,
                         entity_code_by_oid: Dict[str, str]) -> None:
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

        if _absorb_subtype_relationship(model, elem, relationship):
            continue

        _register_relationship(model, relationship, oid)


def _flag_collapsed_cardinality(model: CDMModel) -> None:
    """
    A misread cardinality produces a *valid* value, so nothing downstream can
    tell it apart from a real one.  The one detectable signature is loss of
    variance: every relationship in the model resolving to the same pair.
    Real models vary, so a constant means the source value never landed.
    """
    if not getattr(config, "ERWIN_FLAG_COLLAPSED_CARDINALITY", True):
        return
    real = [r for r in model.relationships if r.kind == "RELATIONSHIP"]
    if len(real) < 3:
        return
    pairs = {(r.end1.cardinality, r.end2.cardinality) for r in real}
    if len(pairs) != 1:
        return
    only = next(iter(pairs))
    message = (
        f"All {len(real)} erwin relationships resolved to the same "
        f"cardinality {only[0]}/{only[1]} — suspected cardinality "
        f"parse failure, not a real model property. Run "
        f"diagnose_cardinality.py against this XML to see the raw values."
    )
    logger.warning(message)
    model.parse_warnings.append(message)


def _parse_inheritances(model: CDMModel, root: ET.Element,
                        entity_code_by_oid: Dict[str, str]) -> None:
    seen_subtype_oids: Set[str] = set()
    for elem in _tagged_descendants(root, _SUBTYPE_TAGS):
        oid = _oid(elem)
        if oid and oid in seen_subtype_oids:
            continue
        if oid:
            seen_subtype_oids.add(oid)
        inheritance = _parse_subtype(elem, entity_code_by_oid)
        if inheritance.parent and inheritance.parent != "UNKNOWN" and inheritance.children:
            model.inheritances.append(inheritance)


def _parse_business_rules(model: CDMModel, root: ET.Element) -> None:
    seen_rule_keys: Set[str] = set()
    for elem in _tagged_descendants(root, _RULE_TAGS):
        if not _name(elem) and not _val(elem, "Expression"):
            continue
        rule = _parse_business_rule(elem)
        key = (rule.name or rule.oid).upper()
        if key in seen_rule_keys:
            continue
        seen_rule_keys.add(key)
        model.business_rules.append(rule)
