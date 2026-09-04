"""
PowerDesigner Conceptual Data Model (.cdm) Parser
-------------------------------------------------
Parses a SAP PowerDesigner CDM file into the canonical model in ``cdm_model``.

PowerDesigner XML uses three namespaces:

    o:   objects      o:Entity, o:EntityAttribute, o:Identifier, o:Relationship,
                      o:Inheritance, o:Association, o:Domain, o:BusinessRule
    a:   attributes   a:Name, a:Code, a:DataType, a:Mandatory, a:Comment
    c:   collections  c:Entities, c:Attributes, c:Identifiers, c:Relationships

Cross-references use the Id / Ref attribute pair: an element carrying ``Id`` is a
definition, an element carrying ``Ref`` is a pointer to one.  Every collection in
the file mixes the two, so definitions and pointers must be told apart before
anything else — that single distinction accounts for most naive-parser bugs.

Two further realities are handled deliberately:

  • **Namespaces are matched on local name.**  Hand-edited and re-exported CDM
    files routinely lose or rename the namespace prefixes; local-name matching
    keeps those files readable instead of silently yielding zero entities.

  • **Attributes may inherit their type from a Data Item or a Domain.**  In
    PowerDesigner an EntityAttribute frequently carries no DataType of its own
    and points at a shared o:DataItem or o:Domain instead.  Resolving that
    indirection is required, or a large share of a real model's attributes parse
    as untyped.
"""

import re
import logging
import xml.etree.ElementTree as ET  # nosec B405
from defusedxml.ElementTree import parse as safe_parse, iterparse as safe_iterparse
from typing import Any, Dict, List, Optional

from .cdm_model import (Attribute, BusinessRule, CDMModel, Domain, Entity,
                       Identifier, Inheritance, Relationship, RelationshipEnd)
from .cardinality import normalize_cardinality
from . import normalizers

logger = logging.getLogger(__name__)

TRUE_TOKENS = {"1", "true", "yes", "y", "t"}


# ─── LOW-LEVEL XML HELPERS ────────────────────────────────────────────────────

def _local(tag: Any) -> str:
    """Local name of an element tag, namespace stripped."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _children(elem: ET.Element, local_name: str) -> List[ET.Element]:
    """Direct children whose local name matches (namespace-agnostic)."""
    return [child for child in list(elem) if _local(child.tag) == local_name]


def _first_child(elem: ET.Element, local_name: str) -> Optional[ET.Element]:
    for child in list(elem):
        if _local(child.tag) == local_name:
            return child
    return None


def _descendants(elem: ET.Element, local_name: str) -> List[ET.Element]:
    return [node for node in elem.iter() if _local(node.tag) == local_name]


def _attr(elem: ET.Element, name: str, default: str = "") -> str:
    """
    Value of an ``a:<name>`` child element, falling back to an XML attribute of
    the same name (older PowerDesigner releases write scalars either way).
    """
    child = _first_child(elem, name)
    if child is not None and child.text:
        return child.text.strip()
    value = elem.get(name)
    return value.strip() if value else default


def _flag(elem: ET.Element, name: str, default: bool = False) -> bool:
    raw = _attr(elem, name)
    if not raw:
        return default
    return raw.strip().lower() in TRUE_TOKENS


def _definitions(container: Optional[ET.Element], local_name: str) -> List[ET.Element]:
    """Child objects that are definitions (carry Id), not pointers (carry Ref)."""
    if container is None:
        return []
    return [child for child in _children(container, local_name)
            if child.get("Ref") is None]


def _ref_ids(container: Optional[ET.Element], local_name: str) -> List[str]:
    """Ref ids of pointer children inside a collection, in declaration order."""
    if container is None:
        return []
    return [child.get("Ref") for child in _descendants(container, local_name)
            if child.get("Ref")]


def _first_ref(elem: ET.Element, collection: str, object_name: str) -> str:
    """First Ref id found under ``elem/<collection>/<object_name>``."""
    container = _first_child(elem, collection)
    refs = _ref_ids(container, object_name)
    return refs[0] if refs else ""


def _description(elem: ET.Element) -> str:
    """
    Definition text.  PowerDesigner splits business meaning across Comment,
    Description and Annotation depending on how the modeller worked, and a
    migration can legitimately land it in any of them, so all are considered.
    """
    for field in ("Comment", "Description", "Annotation", "Definition"):
        value = _attr(elem, field)
        if value:
            return value
    return ""


# ── documentation mapping accessors (additive; _description is untouched) ────
# The Comments→Notes / Definition→Definition report needs to know WHICH SAP PD
# field a piece of text came from, which the first-match-wins _description()
# above deliberately hides.  These read one specific field each and are used
# only to populate the report-only doc_* attributes.

def _pd_comment(elem: ET.Element) -> str:
    """SAP PD <a:Comment> — the field that maps to an erwin Note."""
    return _strip_rtf_for_report(_attr(elem, "Comment"))


def _pd_definition(elem: ET.Element) -> str:
    """
    SAP PD <a:Description> — the Description sub-tab of the Definition tab.
    This is the field that maps to an erwin Definition.

    Annotation is NOT accepted as a fallback here.  The two sub-tabs land in
    different erwin fields (Description → Definition, Annotation → Extended
    Notes), so folding them together labelled an Annotation as a Description
    and then reported it lost, because erwin's Definition was legitimately
    empty.  ``_pd_annotation`` carries Annotation on its own mapping instead.
    """
    return _strip_rtf_for_report(_attr(elem, "Description"))


def _pd_annotation(elem: ET.Element) -> str:
    """SAP PD <a:Annotation> — the field that maps to erwin Extended Notes."""
    return _strip_rtf_for_report(_attr(elem, "Annotation"))


def _strip_rtf_for_report(value: str) -> str:
    """
    Reduce PowerDesigner rich text to readable plain text.

    PowerDesigner stores Description/Annotation through an RTF editor, so the
    raw value looks like ``{\\rtf1\\ansi\\ansicpg1252…}``.  Left as-is it makes
    the DOCUMENTATION sheet unreadable and guarantees a false MISMATCH against
    erwin's plain-text Definition.

    Scoped deliberately to the report-only doc_* fields: the existing
    ``_description()`` and every comparison that depends on it are untouched,
    so no validation result changes.
    """
    if not value or "\\rtf" not in value[:20]:
        return (value or "").strip()

    text = _strip_rtf_tables(value)

    text = re.sub(r"\\par[d]?\b", "\n", text)      # paragraph breaks
    text = re.sub(r"\\tab\b", "\t", text)
    text = re.sub(r"\\'([0-9a-fA-F]{2})",
                  lambda m: chr(int(m.group(1), 16)), text)   # \'e9 -> é
    text = re.sub(r"\\u(-?\d+)\??",
                  lambda m: chr(int(m.group(1)) % 65536), text)
    text = re.sub(r"\\[a-zA-Z]+-?\d*[ ]?", "", text)          # control words
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _rtf_group_end(text: str, index: int) -> int:
    """Position of the '}' closing the RTF group that opens at `index`."""
    depth, cursor = 0, index
    while cursor < len(text):
        if text[cursor] == "{":
            depth += 1
        elif text[cursor] == "}":
            depth -= 1
            if depth == 0:
                break
        cursor += 1
    return cursor


def _strip_rtf_tables(text: str) -> str:
    """Drop font/colour/stylesheet tables wholesale — they carry no prose."""
    for table in (r"\fonttbl", r"\colortbl", r"\stylesheet", r"\*\generator"):
        start = text.find(table)
        while start != -1:
            index = text.rfind("{", 0, start)
            if index == -1:
                break
            cursor = _rtf_group_end(text, index)
            text = text[:index] + text[cursor + 1:]
            start = text.find(table)
    return text


# ─── DOMAINS & DATA ITEMS ─────────────────────────────────────────────────────

def _parse_domain(elem: ET.Element) -> Domain:
    return Domain(
        oid        = elem.get("Id", ""),
        name       = _attr(elem, "Name"),
        code       = _attr(elem, "Code") or _attr(elem, "Name"),
        data_type  = _attr(elem, "DataType"),
        length     = _attr(elem, "Length"),
        precision  = _attr(elem, "Precision"),
        definition = _description(elem),
    )


def _parse_data_item(elem: ET.Element) -> Dict[str, str]:
    """
    A Data Item is PowerDesigner's reusable conceptual fact.  Attributes may
    borrow their name and type from one, so the type-bearing fields are kept.
    """
    return {
        "name":       _attr(elem, "Name"),
        "code":       _attr(elem, "Code") or _attr(elem, "Name"),
        "data_type":  _attr(elem, "DataType"),
        "length":     _attr(elem, "Length"),
        "precision":  _attr(elem, "Precision"),
        "definition": _description(elem),
    }


# ─── ATTRIBUTES ───────────────────────────────────────────────────────────────

def _resolve_attribute_domain(elem: ET.Element,
                              domains_by_oid: Dict[str, Domain],
                              data_type: str, length: str, precision: str) -> tuple:
    """Borrow the type from the attribute's Domain; returns (domain_name, data_type, length, precision)."""
    domain_name = ""
    domain_oid = _first_ref(elem, "Domain", "Domain") or _first_ref(elem, "Domain", "PhysicalDomain")
    if domain_oid and domain_oid in domains_by_oid:
        domain = domains_by_oid[domain_oid]
        domain_name = domain.name or domain.code
        if not data_type:
            data_type = domain.data_type
            length    = length    or domain.length
            precision = precision or domain.precision
    return domain_name, data_type, length, precision


def _resolve_attribute_data_item(elem: ET.Element,
                                 data_items_by_oid: Dict[str, Dict[str, str]],
                                 name: str, code: str, data_type: str,
                                 length: str, precision: str, definition: str) -> tuple:
    """
    Borrow name / code / type / definition from the attribute's Data Item;
    returns (data_item_name, name, code, data_type, length, precision, definition).
    """
    data_item_name = ""
    item_oid = _first_ref(elem, "DataItem", "DataItem")
    if item_oid and item_oid in data_items_by_oid:
        item = data_items_by_oid[item_oid]
        data_item_name = item["name"] or item["code"]
        # A CDM attribute frequently declares no name/code of its own and
        # relies entirely on its linked Data Item for both — the same
        # borrowing PowerDesigner already does for type/length/precision
        # below, so name/code must fall back the same way or the attribute
        # is emitted (and compared) as blank.
        if not name:
            name = item["name"]
        if not code:
            code = item["code"]
        if not data_type:
            data_type = item["data_type"]
            length    = length    or item["length"]
            precision = precision or item["precision"]
        if not definition:
            definition = item["definition"]
    return data_item_name, name, code, data_type, length, precision, definition


def _parse_attribute(elem: ET.Element,
                     order: int,
                     domains_by_oid: Dict[str, Domain],
                     data_items_by_oid: Dict[str, Dict[str, str]]) -> Attribute:
    name = _attr(elem, "Name")
    code = _attr(elem, "Code")

    data_type = _attr(elem, "DataType")
    length    = _attr(elem, "Length")
    precision = _attr(elem, "Precision")
    definition = _description(elem)

    # ── Resolve the type through Domain, then Data Item ───────────────────────
    domain_name, data_type, length, precision = _resolve_attribute_domain(
        elem, domains_by_oid, data_type, length, precision)

    (data_item_name, name, code,
     data_type, length, precision, definition) = _resolve_attribute_data_item(
        elem, data_items_by_oid, name, code, data_type, length, precision, definition)

    # Final fallback: whichever of name/code did resolve, use it for the other
    # if it's still empty (mirrors the entity/relationship-level convention
    # used elsewhere in this parser).
    name = name or code
    code = code or name

    return Attribute(
        oid          = elem.get("Id", ""),
        name         = name,
        code         = code,
        data_type    = data_type,
        length       = length,
        precision    = precision,
        mandatory    = _flag(elem, "Mandatory"),
        is_primary   = _flag(elem, "PrimaryIdentifier"),
        domain       = domain_name,
        data_item    = data_item_name,
        definition   = definition,
        # report-only; does not affect any comparison
        doc_comment    = _pd_comment(elem),
        doc_definition = _pd_definition(elem),
        doc_annotation = _pd_annotation(elem),
        multiplicity = _attr(elem, "Multiplicity"),
        order        = order,
    )


# ─── IDENTIFIERS ──────────────────────────────────────────────────────────────

def _parse_identifier(elem: ET.Element,
                      attr_code_by_oid: Dict[str, str]) -> Identifier:
    container = (_first_child(elem, "Identifier.Attributes")
                 or _first_child(elem, "Attributes"))
    member_oids = _ref_ids(container, "EntityAttribute")

    columns = [attr_code_by_oid.get(oid, oid) for oid in member_oids]

    return Identifier(
        oid        = elem.get("Id", ""),
        name       = _attr(elem, "Name"),
        code       = _attr(elem, "Code") or _attr(elem, "Name"),
        is_primary = False,          # set by the caller from c:PrimaryIdentifier
        attributes = [c for c in columns if c],
        # PowerDesigner modellers habitually list the intended key composition
        # in the identifier comment.  Where the structure was never completed
        # this is the only record of intent in the file, so it is preserved and
        # used to corroborate composition differences rather than discarded.
        comment    = _attr(elem, "Comment") or _attr(elem, "Description"),
    )


# ─── ENTITIES ─────────────────────────────────────────────────────────────────

def _parse_entity(elem: ET.Element,
                  subject_area: str,
                  domains_by_oid: Dict[str, Domain],
                  data_items_by_oid: Dict[str, Dict[str, str]]) -> Entity:
    name = _attr(elem, "Name")
    code = _attr(elem, "Code") or name

    entity = Entity(
        oid          = elem.get("Id", ""),
        name         = name,
        code         = code,
        definition   = _description(elem),
        subject_area = subject_area,
        # report-only; does not affect any comparison
        doc_comment    = _pd_comment(elem),
        doc_definition = _pd_definition(elem),
        doc_annotation = _pd_annotation(elem),
    )

    # ── Attributes ───────────────────────────────────────────────────────────
    attr_code_by_oid = _collect_entity_attributes(entity, elem, domains_by_oid, data_items_by_oid)

    # ── Identifiers ──────────────────────────────────────────────────────────
    identifiers_by_oid = _collect_entity_identifiers(entity, elem, attr_code_by_oid)
    _resolve_primary_identifier(entity, elem, identifiers_by_oid)
    _mark_primary_attributes(entity)

    return entity


def _collect_entity_attributes(entity: Entity, elem: ET.Element,
                               domains_by_oid: Dict[str, Domain],
                               data_items_by_oid: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """Parse the entity's attributes; returns the attribute oid → code map."""
    attr_container = _first_child(elem, "Attributes")
    attr_code_by_oid: Dict[str, str] = {}

    for index, attr_elem in enumerate(_definitions(attr_container, "EntityAttribute"), start=1):
        attribute = _parse_attribute(attr_elem, index, domains_by_oid, data_items_by_oid)
        if normalizers.is_excluded_attribute(attribute.name, attribute.code):
            continue
        entity.attributes.append(attribute)
        if attribute.oid:
            attr_code_by_oid[attribute.oid] = attribute.code
    return attr_code_by_oid


def _collect_entity_identifiers(entity: Entity, elem: ET.Element,
                                attr_code_by_oid: Dict[str, str]) -> Dict[str, Identifier]:
    """Parse the entity's identifiers; returns the identifier oid → Identifier map."""
    ident_container = _first_child(elem, "Identifiers")
    identifiers_by_oid: Dict[str, Identifier] = {}

    for ident_elem in _definitions(ident_container, "Identifier"):
        identifier = _parse_identifier(ident_elem, attr_code_by_oid)
        entity.identifiers.append(identifier)
        if identifier.oid:
            identifiers_by_oid[identifier.oid] = identifier
    return identifiers_by_oid


def _flag_primary_by_attributes(entity: Entity) -> None:
    """Fall back to the PrimaryIdentifier flag carried on the attributes."""
    primary_attrs = {a.code.upper() for a in entity.attributes if a.is_primary}
    if primary_attrs:
        for identifier in entity.identifiers:
            if {c.upper() for c in identifier.attributes} == primary_attrs:
                identifier.is_primary = True
                break


def _resolve_primary_identifier(entity: Entity, elem: ET.Element,
                                identifiers_by_oid: Dict[str, Identifier]) -> None:
    # The primary identifier is a pointer, not a flag on the identifier itself.
    primary_oid = _first_ref(elem, "PrimaryIdentifier", "Identifier")
    if primary_oid and primary_oid in identifiers_by_oid:
        identifiers_by_oid[primary_oid].is_primary = True
    elif entity.identifiers:
        _flag_primary_by_attributes(entity)


def _mark_primary_attributes(entity: Entity) -> None:
    """Keep the attribute-level primary flag consistent with the identifier."""
    primary = entity.primary_identifier
    if primary:
        members = {c.upper() for c in primary.attributes}
        for attribute in entity.attributes:
            if attribute.code.upper() in members:
                attribute.is_primary = True


# ─── RELATIONSHIPS ────────────────────────────────────────────────────────────

def _parse_relationship(elem: ET.Element,
                        entity_code_by_oid: Dict[str, str]) -> Relationship:
    """
    Build a canonical Relationship from an o:Relationship element.

    PowerDesigner's convention: ``Entity1ToEntity2RoleCardinality`` is stored on
    end 1 and states how many *end 2* instances relate to one end 1 instance.
    The canonical model preserves that convention exactly so the erwin parser
    can align to it.
    """
    oid1 = _first_ref(elem, "Object1", "Entity")
    oid2 = _first_ref(elem, "Object2", "Entity")

    end1 = RelationshipEnd(
        entity      = entity_code_by_oid.get(oid1, oid1 or "UNKNOWN"),
        role        = _attr(elem, "Entity1ToEntity2Role"),
        cardinality = normalize_cardinality(
            _attr(elem, "Entity1ToEntity2RoleCardinality"),
            mandatory = _flag(elem, "Entity1ToEntity2RoleMandatory"),
        ),
        mandatory   = _flag(elem, "Entity1ToEntity2RoleMandatory"),
        dependent   = _flag(elem, "Entity1ToEntity2RoleDependent"),
    )

    end2 = RelationshipEnd(
        entity      = entity_code_by_oid.get(oid2, oid2 or "UNKNOWN"),
        role        = _attr(elem, "Entity2ToEntity1Role"),
        cardinality = normalize_cardinality(
            _attr(elem, "Entity2ToEntity1RoleCardinality"),
            mandatory = _flag(elem, "Entity2ToEntity1RoleMandatory"),
        ),
        mandatory   = _flag(elem, "Entity2ToEntity1RoleMandatory"),
        dependent   = _flag(elem, "Entity2ToEntity1RoleDependent"),
    )

    # When cardinality was never stated, derive the lower bound from the
    # mandatory flags rather than leaving the relationship undescribed.
    if not end1.cardinality:
        end1.cardinality = normalize_cardinality("", mandatory=end1.mandatory, many=True)
    if not end2.cardinality:
        end2.cardinality = normalize_cardinality("", mandatory=end2.mandatory, many=False)

    return Relationship(
        oid        = elem.get("Id", ""),
        name       = _attr(elem, "Name"),
        code       = _attr(elem, "Code") or _attr(elem, "Name"),
        definition = _description(elem),
        end1       = end1,
        end2       = end2,
        kind       = "RELATIONSHIP",
    )


# ─── ASSOCIATIONS ─────────────────────────────────────────────────────────────

def _parse_association_links(root: ET.Element,
                             entity_code_by_oid: Dict[str, str],
                             association_code_by_oid: Dict[str, str]) -> List[Relationship]:
    """
    An Association in PowerDesigner is a first-class object joined to its
    participating entities by o:AssociationLink elements.  Each link becomes an
    ASSOCIATION-kind relationship, which is exactly how erwin models the same
    construct (associative entity plus two relationships) — so the two tools
    reconcile without special-casing either.
    """
    links: List[Relationship] = []

    for link in _descendants(root, "AssociationLink"):
        if link.get("Ref") is not None:
            continue

        entity_oid      = _first_ref(link, "Object1", "Entity")
        association_oid = _first_ref(link, "Object2", "Association")

        # Some exports invert the two ends.
        if not entity_oid:
            entity_oid = _first_ref(link, "Object2", "Entity")
        if not association_oid:
            association_oid = _first_ref(link, "Object1", "Association")

        entity_code      = entity_code_by_oid.get(entity_oid, entity_oid or "UNKNOWN")
        association_code = association_code_by_oid.get(association_oid,
                                                       association_oid or "UNKNOWN")

        raw_cardinality = _attr(link, "Cardinality") or _attr(link, "RoleCardinality")

        links.append(Relationship(
            oid        = link.get("Id", ""),
            name       = _attr(link, "Name"),
            code       = _attr(link, "Code") or _attr(link, "Name"),
            definition = _description(link),
            end1       = RelationshipEnd(
                entity      = entity_code,
                role        = _attr(link, "Role"),
                cardinality = normalize_cardinality(raw_cardinality) or "0,n",
                mandatory   = _flag(link, "Mandatory"),
                dependent   = _flag(link, "Dependent"),
            ),
            end2       = RelationshipEnd(
                entity      = association_code,
                role        = "",
                cardinality = "1,1",
            ),
            kind       = "ASSOCIATION",
        ))

    return links


# ─── INHERITANCES ─────────────────────────────────────────────────────────────

def _parse_inheritance(elem: ET.Element,
                       entity_code_by_oid: Dict[str, str]) -> Inheritance:
    parent_oid = (_first_ref(elem, "ParentEntity", "Entity")
                  or _first_ref(elem, "Object1", "Entity"))

    # Subtypes hang off o:InheritanceLink children, each pointing at one entity.
    child_oids: List[str] = []
    children_container = _first_child(elem, "Children")
    link_scope = children_container if children_container is not None else elem

    for link in _descendants(link_scope, "InheritanceLink"):
        oid = (_first_ref(link, "Object2", "Entity")
               or _first_ref(link, "ChildEntity", "Entity")
               or _first_ref(link, "Object1", "Entity"))
        if oid and oid != parent_oid:
            child_oids.append(oid)

    # Fall back to any direct entity pointers when links are absent.
    if not child_oids and children_container is not None:
        child_oids = [oid for oid in _ref_ids(children_container, "Entity")
                      if oid != parent_oid]

    return Inheritance(
        oid      = elem.get("Id", ""),
        name     = _attr(elem, "Name"),
        code     = _attr(elem, "Code") or _attr(elem, "Name"),
        parent   = entity_code_by_oid.get(parent_oid, parent_oid or "UNKNOWN"),
        children = [entity_code_by_oid.get(oid, oid) for oid in child_oids],
        complete = _flag(elem, "Complete"),
        mutually_exclusive = _flag(elem, "MutuallyExclusive", default=True),
        generate_parent    = _flag(elem, "GenerateParent", default=True),
    )


# ─── BUSINESS RULES ───────────────────────────────────────────────────────────

def _parse_business_rule(elem: ET.Element) -> BusinessRule:
    return BusinessRule(
        oid        = elem.get("Id", ""),
        name       = _attr(elem, "Name"),
        code       = _attr(elem, "Code") or _attr(elem, "Name"),
        rule_type  = _attr(elem, "RuleType") or _attr(elem, "Type"),
        expression = (_attr(elem, "ServerExpression")
                      or _attr(elem, "ClientExpression")
                      or _attr(elem, "Expression")),
        definition = _description(elem),
    )


# ─── SCOPE WALKER ─────────────────────────────────────────────────────────────

def _walk_scope(scope: ET.Element,
                subject_area: str,
                model: CDMModel,
                domains_by_oid: Dict[str, Domain],
                data_items_by_oid: Dict[str, Dict[str, str]],
                entity_elements: List[tuple]) -> None:
    """
    Collect entity elements from a model or package scope, recursing into nested
    packages so subject-area membership is preserved rather than flattened.
    """
    entity_container = _first_child(scope, "Entities")
    for entity_elem in _definitions(entity_container, "Entity"):
        entity_elements.append((entity_elem, subject_area))

    # Associations become associative entities in the canonical model.
    association_container = _first_child(scope, "Associations")
    for assoc_elem in _definitions(association_container, "Association"):
        entity_elements.append((assoc_elem, subject_area))

    package_container = _first_child(scope, "Packages")
    for package_elem in _definitions(package_container, "Package"):
        package_name = _attr(package_elem, "Name") or _attr(package_elem, "Code")
        if package_name and package_name not in model.subject_areas:
            model.subject_areas.append(package_name)
        _walk_scope(package_elem, package_name, model,
                    domains_by_oid, data_items_by_oid, entity_elements)


# ─── PUBLIC API ───────────────────────────────────────────────────────────────


# PowerDesigner class GUIDs a shortcut can point at.  Only the ones seen in
# real estates are named; anything else renders as "Object" (still reported).
_SHORTCUT_TARGET_CLASSES = {
    "186C8AC3-D3DC-11D3-881C-00508B03C75C": "Model",
    "CD2F5C71-2CA9-4B74-9BBA-50786D1D88EF": "Category",
    "EBE946CF-7218-4FAC-B85E-4AB922930494": "Term",
}

# A model file contains <o:Shortcut> elements in three different collections,
# and only ONE of them is what PowerDesigner shows in Model Properties →
# "List of Shortcuts":
#
#   c:ExternalObjects          ← THE list of shortcuts (what the modeller made)
#   c:TermObjects              glossary terminology links, created automatically
#                              when a glossary is attached; PD does not list
#                              these as shortcuts and neither do we
#   c:ExtendedModelDefinitions the .xem extension reference (e.g. "BIM-core")
#
# Counting all three is what made a model with an EMPTY List of Shortcuts
# report dozens of them.  The census therefore reads c:ExternalObjects only,
# so the report's count always equals the number of rows the modeller sees.
_SHORTCUT_COLLECTION = "ExternalObjects"


def _parse_shortcuts(model_elem: ET.Element, model) -> None:
    """
    Collect PowerDesigner's "List of Shortcuts" — references to objects OWNED
    BY ANOTHER MODEL (a glossary category, an entity of a shared model).

    A model with no shortcuts yields an empty list, and the report then says
    nothing about shortcuts at all — which is the correct answer for it.
    """
    container = _first_child(model_elem, _SHORTCUT_COLLECTION)
    if container is None:
        return

    claimed, fallback_model = _shortcut_target_models(model_elem)

    for elem in container:
        if elem.tag.rsplit("}", 1)[-1] != "Shortcut":
            continue
        if elem.get("Ref") is not None or not elem.get("Id"):
            continue
        model.shortcuts.append(_shortcut_record(elem, claimed, fallback_model))


def _shortcut_target_models(model_elem: ET.Element) -> tuple:
    """
    Owner model per shortcut, as (claimed, fallback_model).

    PD resolves the "Target Model" column through the repository, which a
    single file cannot do.  Two things are available here: shortcuts a
    <o:TargetModel> block claims explicitly, and — failing that — the one
    attached model that is not the .xem extension, which is the owner
    whenever a model attaches a single shared model (the usual case).
    """
    claimed: Dict[str, str] = {}
    attached: List[str] = []
    for tm in _descendants(model_elem, "TargetModel"):
        if tm.get("Ref") is not None or not tm.get("Id"):
            continue
        tm_name = _attr(tm, "Name") or _attr(tm, "Code")
        if not _attr(tm, "TargetModelURL").lower().endswith(".xem"):
            attached.append(tm_name)
        for ref in _descendants(tm, "Shortcut"):
            oid = ref.get("Ref")
            if oid:
                claimed[oid] = tm_name
    fallback_model = attached[0] if len(attached) == 1 else ""
    return claimed, fallback_model


def _shortcut_record(elem: ET.Element, claimed: Dict[str, str],
                     fallback_model: str) -> Dict[str, str]:
    class_id = _attr(elem, "TargetClassID").upper()
    return {
        "name": _attr(elem, "Name"),
        "code": _attr(elem, "Code") or _attr(elem, "Name"),
        "type": _SHORTCUT_TARGET_CLASSES.get(class_id, "Object"),
        "target_model": claimed.get(elem.get("Id", ""), fallback_model),
        "target_package": _attr(elem, "TargetPackagePath"),
        "target_stereotype": _attr(elem, "TargetStereotype"),
    }


def parse_cdm(filepath: str) -> CDMModel:
    """
    Parse a PowerDesigner .cdm file into a :class:`CDMModel`.

    Never raises for malformed input: a parse failure is recorded on the returned
    model so a single bad file cannot abort a 1500-model batch.
    """
    model = CDMModel(source_file=filepath, source_tool="PowerDesigner",
                     model_type="Conceptual")

    root = _read_root(model, filepath)
    if root is None:
        return model

    # ── Model header ─────────────────────────────────────────────────────────
    model_elem = _model_element(root)
    _parse_model_header(model, model_elem, root)

    # ── Domains and Data Items (resolved before attributes need them) ────────
    domains_by_oid    = _parse_domains(model, model_elem)
    data_items_by_oid = _parse_data_items(model, model_elem)

    # ── Shortcuts (references to objects owned by other models) ──────────
    _parse_shortcuts(model_elem, model)

    # ── Entities (including packages / subject areas) ────────────────────────
    entity_elements = _collect_entity_elements(root, model_elem, model,
                                               domains_by_oid, data_items_by_oid)
    entity_code_by_oid, association_code_by_oid = _parse_entities(
        model, entity_elements, domains_by_oid, data_items_by_oid)

    # Associations may also be referenced as entities by relationships.
    entity_code_by_oid.update(association_code_by_oid)

    # ── Relationships ────────────────────────────────────────────────────────
    _parse_relationships(model, model_elem, entity_code_by_oid, association_code_by_oid)

    # ── Inheritances ─────────────────────────────────────────────────────────
    _parse_inheritances(model, model_elem, entity_code_by_oid)

    # ── Business rules ───────────────────────────────────────────────────────
    _parse_business_rules(model, model_elem)

    logger.debug("Parsed %s → %s", filepath, model.stats())
    return model


def _read_root(model: CDMModel, filepath: str) -> Optional[ET.Element]:
    """Root element of the file, or None with the failure recorded on `model`."""
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


def _is_definition(elem: ET.Element) -> bool:
    """True for an <o:Xxx Id="…"> definition, False for a <o:Xxx Ref="…"/> pointer."""
    return elem.get("Ref") is None and bool(elem.get("Id"))


def _model_element(root: ET.Element) -> ET.Element:
    """The <o:Model> definition, falling back to the document root."""
    model_elements = [node for node in _descendants(root, "Model")
                      if node.get("Ref") is None and node.get("Id")]
    return model_elements[0] if model_elements else root


def _parse_model_header(model: CDMModel, model_elem: ET.Element, root: ET.Element) -> None:
    model.model_name = _attr(model_elem, "Name") or _attr(root, "Name")
    model.model_code = _attr(model_elem, "Code") or model.model_name
    declared_type = _attr(model_elem, "ModelType")
    if declared_type:
        model.model_type = declared_type


def _parse_domains(model: CDMModel, model_elem: ET.Element) -> Dict[str, Domain]:
    """Parse every domain onto `model`; returns the domain oid → Domain map."""
    domains_by_oid: Dict[str, Domain] = {}
    for local_name in ("Domain", "PhysicalDomain"):
        for elem in _descendants(model_elem, local_name):
            if not _is_definition(elem):
                continue
            domain = _parse_domain(elem)
            domains_by_oid[domain.oid] = domain
            key = (domain.code or domain.name).upper()
            if key:
                model.domains[key] = domain
    return domains_by_oid


def _parse_data_items(model: CDMModel, model_elem: ET.Element) -> Dict[str, Dict[str, str]]:
    """Parse every data item; returns the data-item oid → record map."""
    data_items_by_oid: Dict[str, Dict[str, str]] = {}
    for elem in _descendants(model_elem, "DataItem"):
        if not _is_definition(elem):
            continue
        data_items_by_oid[elem.get("Id", "")] = _parse_data_item(elem)
    # Kept on the model so the comparator can account for PD's
    # "List of Data Items" one-for-one in the FINDINGS sheet.
    model.data_items = dict(data_items_by_oid)
    return data_items_by_oid


def _fallback_entity_elements(root: ET.Element) -> List[tuple]:
    """Safety net: a non-standard nesting must not cost us the whole model."""
    entity_elements: List[tuple] = []
    seen_ids = set()
    for elem in _descendants(root, "Entity"):
        if not _is_definition(elem):
            continue
        if elem.get("Id") in seen_ids:
            continue
        seen_ids.add(elem.get("Id"))
        entity_elements.append((elem, ""))
    return entity_elements


def _collect_entity_elements(root: ET.Element, model_elem: ET.Element, model: CDMModel,
                             domains_by_oid: Dict[str, Domain],
                             data_items_by_oid: Dict[str, Dict[str, str]]) -> List[tuple]:
    """Every (entity element, subject area) pair, walking packages first."""
    entity_elements: List[tuple] = []
    _walk_scope(model_elem, "", model, domains_by_oid, data_items_by_oid, entity_elements)

    if not entity_elements:
        entity_elements = _fallback_entity_elements(root)
        if entity_elements:
            model.parse_warnings.append(
                "Entities found outside the expected c:Entities collection — "
                "file may be a non-standard export."
            )
    return entity_elements


def _register_entity_code(entity: Entity, is_association: bool,
                          entity_code_by_oid: Dict[str, str],
                          association_code_by_oid: Dict[str, str]) -> None:
    if entity.oid:
        if is_association:
            association_code_by_oid[entity.oid] = entity.code
        else:
            entity_code_by_oid[entity.oid] = entity.code


def _parse_entities(model: CDMModel, entity_elements: List[tuple],
                    domains_by_oid: Dict[str, Domain],
                    data_items_by_oid: Dict[str, Dict[str, str]]) -> tuple:
    """
    Parse every collected entity onto `model`; returns
    (entity_code_by_oid, association_code_by_oid).

    A real PowerDesigner CDM can list the same physical entity under more
    than one scope — e.g. once in the model-level Entities collection and
    again inside a package's own Entities collection — as two full <o:Entity
    Id="oXXX"> definitions sharing the same Id, not a Ref pointer (those are
    already filtered out by _definitions()). Left unguarded, that produces
    two separate Entity objects for one physical entity, which add_entity()
    then treats as a genuine code collision (storing the second as
    "EMPLOYEE#2") — a false duplicate that gets independently validated and
    reported twice. Keep only the first occurrence of each Id.
    """
    entity_code_by_oid: Dict[str, str] = {}
    association_code_by_oid: Dict[str, str] = {}

    seen_entity_ids: set = set()
    for entity_elem, subject_area in entity_elements:
        elem_id = entity_elem.get("Id")
        if elem_id:
            if elem_id in seen_entity_ids:
                continue
            seen_entity_ids.add(elem_id)

        is_association = _local(entity_elem.tag) == "Association"
        entity = _parse_entity(entity_elem, subject_area,
                               domains_by_oid, data_items_by_oid)
        entity.is_associative = is_association

        if normalizers.is_excluded_entity(entity.name, entity.code):
            continue

        model.add_entity(entity)
        _register_entity_code(entity, is_association,
                              entity_code_by_oid, association_code_by_oid)

    return entity_code_by_oid, association_code_by_oid


def _parse_relationships(model: CDMModel, model_elem: ET.Element,
                         entity_code_by_oid: Dict[str, str],
                         association_code_by_oid: Dict[str, str]) -> None:
    for elem in _descendants(model_elem, "Relationship"):
        if not _is_definition(elem):
            continue
        model.relationships.append(_parse_relationship(elem, entity_code_by_oid))

    model.relationships.extend(
        _parse_association_links(model_elem, entity_code_by_oid, association_code_by_oid)
    )


def _index_inheritance_children(model_elem: ET.Element) -> Dict[str, List[str]]:
    """
    Subtype entity oids per inheritance oid.

    PowerDesigner does NOT nest the subtypes inside <o:Inheritance>.  The
    parent sits in <c:ParentEntity>, while each child lives in a SEPARATE
    sibling <o:InheritanceLink> whose <c:Object1> points back at the
    inheritance and whose <c:Object2> points at the subtype entity.  Indexing
    those links model-wide is the only way to recover the children; looking
    for them as descendants finds nothing and silently drops every hierarchy.
    """
    children_by_inheritance: Dict[str, List[str]] = {}
    for link in _descendants(model_elem, "InheritanceLink"):
        if link.get("Ref") is not None:
            continue
        owner = _first_ref(link, "Object1", "Inheritance")
        child = _first_ref(link, "Object2", "Entity")
        if owner and child:
            children_by_inheritance.setdefault(owner, []).append(child)
    return children_by_inheritance


def _parse_inheritances(model: CDMModel, model_elem: ET.Element,
                        entity_code_by_oid: Dict[str, str]) -> None:
    children_by_inheritance = _index_inheritance_children(model_elem)

    for elem in _descendants(model_elem, "Inheritance"):
        if not _is_definition(elem):
            continue
        inheritance = _parse_inheritance(elem, entity_code_by_oid)
        if not inheritance.children:
            linked = children_by_inheritance.get(elem.get("Id", ""), [])
            inheritance.children = [entity_code_by_oid.get(oid, oid) for oid in linked
                                    if entity_code_by_oid.get(oid, oid) != inheritance.parent]
        if inheritance.parent and inheritance.children:
            model.inheritances.append(inheritance)


def _parse_business_rules(model: CDMModel, model_elem: ET.Element) -> None:
    for elem in _descendants(model_elem, "BusinessRule"):
        if not _is_definition(elem):
            continue
        model.business_rules.append(_parse_business_rule(elem))
