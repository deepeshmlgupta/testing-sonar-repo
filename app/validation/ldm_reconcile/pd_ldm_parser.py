import logging
import re
import xml.etree.ElementTree as ET  # nosec B405
from defusedxml.ElementTree import parse as safe_parse, iterparse as safe_iterparse
from typing import Any, Dict, List, Optional, Tuple

from .ldm_model import (Attribute, BusinessRule, Domain, Entity,
                       Identifier, Inheritance, LDMModel, Relationship,
                       RelationshipEnd)
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
    return [child for child in elem if _local(child.tag) == local_name]


def _first_child(elem: ET.Element, local_name: str) -> Optional[ET.Element]:
    for child in elem:
        if _local(child.tag) == local_name:
            return child
    return None


def _descendants(elem: ET.Element, local_name: str) -> List[ET.Element]:
    return [node for node in elem.iter() if _local(node.tag) == local_name]


def _attr(elem: ET.Element, *names: str, default: str = "") -> str:
    """
    Value of the first matching ``a:<name>`` child element, falling back to an
    XML attribute of the same name.  Accepts several candidate field names so
    the same helper serves both the CDM-style ``Mandatory`` and the LDM-style
    ``LogicalAttribute.Mandatory`` without the caller needing to know which
    export produced the file.
    """
    for name in names:
        child = _first_child(elem, name)
        if child is not None and child.text and child.text.strip():
            return child.text.strip()
    for name in names:
        value = elem.get(name)
        if value and value.strip():
            return value.strip()
    return default


def _flag(elem: ET.Element, *names: str, default: bool = False) -> bool:
    raw = _attr(elem, *names)
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


# Destinations whose content is not visible body text and must be skipped
# entirely: font/color/style tables, document metadata, embedded binary
# objects (pictures), bookmarks, and field CODES (the URL/formula half of a
# hyperlink or cross-reference -- as opposed to \fldrslt, the field's
# visible RESULT text, which is deliberately NOT in this set and stays).
_RTF_SKIP_DESTINATIONS = {
    "fonttbl", "colortbl", "stylesheet", "info", "generator",
    "pict", "object", "objdata", "objclass", "result",
    "footnote", "header", "headerf", "headerl", "headerr",
    "footer", "footerf", "footerl", "footerr",
    "xe", "tc", "bkmkstart", "bkmkend",
    "listtable", "listoverridetable", "revtbl", "rsidtbl",
    "themedata", "colorschememapping", "datastore", "fldinst",
    "shp", "shpinst", "sp", "do", "dptxbxtext", "nonshppict",
}

_RTF_KNOWN_KEYWORDS = sorted(
    {"par", "line", "tab", "u"} | _RTF_SKIP_DESTINATIONS,
    key=len, reverse=True,   # longest first so no keyword shadows a longer one
)
# Recognised keyword, optionally followed by a signed numeric parameter and
# at most one delimiting space -- matched only when NOT immediately followed
# by another lowercase letter, so "\parSecond" (a missing delimiter some
# non-conformant generators produce) is never misread as the single unknown
# control word "parSecond", which would otherwise silently swallow "Second"
# along with it. Real conformant generators always delimit control words,
# but this guards the case where one doesn't.
_RTF_KNOWN_CONTROL_WORD = re.compile(
    r"\\(" + "|".join(_RTF_KNOWN_KEYWORDS) + r")(-?\d+)?( )?(?![a-z])"
)
_RTF_GENERIC_CONTROL_WORD = re.compile(r"\\([a-zA-Z]+)(-?\d+)?([ ])?")


class _RtfState:
    """Mutable cursor/output state for the RTF tokenizer walk."""
    __slots__ = ("text", "n", "i", "out", "skip_stack")

    def __init__(self, text: str):
        self.text = text
        self.n = len(text)
        self.i = 0
        self.out: list = []
        self.skip_stack: list = [False]   # top-level body is never skipped

    @property
    def skipping(self) -> bool:
        return any(self.skip_stack)

    def emit(self, value: str) -> None:
        if not self.skipping:
            self.out.append(value)


def _rtf_handle_escaped_literal(state: _RtfState, nxt: str) -> None:
    """\\{  \\}  \\\\  -> literal brace/backslash."""
    state.emit(nxt)
    state.i += 2


def _rtf_handle_hex_escape(state: _RtfState) -> None:
    """\\'e9 -> decode one byte via cp1252 (RTF's default ANSI code page)."""
    hex_pair = state.text[state.i + 2:state.i + 4]
    state.i += 4
    if state.skipping:
        return
    try:
        state.out.append(bytes([int(hex_pair, 16)]).decode("cp1252", "ignore"))
    except ValueError:
        pass


def _rtf_handle_unicode(state: _RtfState, param: Optional[str]) -> None:
    """\\uN<fallback>: emit code point N, discard the one ANSI fallback char."""
    try:
        codepoint = int(param)
        if codepoint < 0:
            codepoint += 65536
        state.emit(chr(codepoint))
    except (ValueError, OverflowError):
        pass
    if state.i < state.n and state.text[state.i] not in "\\{}":
        state.i += 1


def _rtf_handle_control_word(state: _RtfState) -> bool:
    """
    Consume a control word at the cursor. Returns True when one was handled.
    """
    match = (_RTF_KNOWN_CONTROL_WORD.match(state.text, state.i)
             or _RTF_GENERIC_CONTROL_WORD.match(state.text, state.i))
    if not match:
        return False

    keyword = match.group(1)
    param = match.group(2)
    state.i = match.end()

    if keyword == "u" and param is not None:
        _rtf_handle_unicode(state, param)
    elif keyword in ("par", "line"):
        state.emit("\n")
    elif keyword == "tab":
        state.emit("\t")
    elif keyword in _RTF_SKIP_DESTINATIONS:
        state.skip_stack[-1] = True
    # Every other control word (\rtf1, \ansi, \f0, \fs20, \pard, ...) is
    # formatting noise: consumed, nothing emitted, nothing skipped.
    return True


def _rtf_handle_backslash(state: _RtfState) -> None:
    """Dispatch the token following a backslash at the cursor."""
    nxt = state.text[state.i + 1] if state.i + 1 < state.n else ""

    if nxt in ("{", "}", "\\"):
        _rtf_handle_escaped_literal(state, nxt)
        return

    if nxt == "'" and state.i + 3 < state.n:
        _rtf_handle_hex_escape(state)
        return

    # Extended-destination marker: skip an unrecognised destination group,
    # the safe default per the RTF spec so unknown destinations fail closed.
    if nxt == "*":
        state.skip_stack[-1] = True
        state.i += 2
        return

    if _rtf_handle_control_word(state):
        return

    # Lone backslash that matched no known pattern -- drop it.
    state.i += 1


def _rtf_tokenize(state: _RtfState) -> None:
    """Walk the RTF string once, filling state.out with visible text."""
    while state.i < state.n:
        ch = state.text[state.i]

        if ch == "{":
            state.skip_stack.append(False)
            state.i += 1
        elif ch == "}":
            if len(state.skip_stack) > 1:
                state.skip_stack.pop()
            state.i += 1
        elif ch == "\\":
            _rtf_handle_backslash(state)
        else:
            state.emit(ch)
            state.i += 1


def _strip_rtf(text: str) -> str:
    """
    PowerDesigner's Comment/Description/Definition field is a rich-text
    editor internally -- even a one-line definition is stored wrapped in a
    full RTF envelope: '{\\rtf1\\ansi\\ansicpg1252\\deflang1033{\\fonttbl...}
    {\\colortbl...} ... actual text ... }'. Confirmed by inspecting
    BLM_Real_Estate_Lease_Administration.ldm directly.

    Left unstripped, this markup (a) makes the report's SAP PD Value column
    unreadable, and (b) artificially deflates DEFINITION similarity scores.

    This is a proper minimal RTF-to-text tokenizer, not a regex chain: it
    walks the string once, tracking group ("{"/"}") nesting on an explicit
    stack, and marks a group as "skip" (its text is not emitted) only when
    its own control word names a non-visible destination (font table, color
    table, embedded picture, bookmark, field CODE, ...) or is introduced by
    the RTF "\\*" extended-destination marker.  Tracking the stack directly
    -- rather than a one-level-deep regex, as an earlier version of this
    function did -- means a destination group nested two or more levels deep
    (list formatting, embedded objects, and field codes routinely nest this
    deep in real Word/PowerDesigner-authored RTF) can no longer cause the
    visible sentence sitting next to it to be swallowed along with it. That
    exact failure mode was found in production: a real definition collapsed
    to an empty string, which silently suppressed a DEFINITION_LOSS finding
    instead of surfacing the definition text. See FINDINGS.md for the case
    that exposed it and the mutation-style regression test that now guards
    against it recurring silently.
    """
    if not text or "{\\rtf" not in text[:12]:
        return text.strip() if text else ""

    original_len = len(text)
    state = _RtfState(text)
    _rtf_tokenize(state)

    result = "".join(state.out)
    result = re.sub(r"[ \t]+", " ", result)
    result = re.sub(r"\n\s*\n+", "\n", result)
    result = result.strip()

    # Safety net -- but note WHICH signal it keys on, because the obvious
    # choice is wrong. An empty result is NOT by itself evidence of failure:
    # a PowerDesigner description field that was opened and saved without
    # any text typed into it still stores a complete ~230-character RTF
    # envelope with no body text, and reducing that to "" is exactly right.
    # Confirmed in production on BLM_Real_Estate_Lease_Administration's
    # PYMNT_TYP attribute, whose empty-but-wrapped comment produced a false
    # DEFINITION_LOSS finding before this stripping existed. An earlier
    # version of this guard keyed on input LENGTH (">40 chars and empty ->
    # assume failure"), which mistook that legitimate case for a failure and
    # would have re-emitted the raw RTF and the false finding along with it.
    #
    # The signal that actually separates the two is brace balance: every
    # group the tokenizer opened should have been closed by the end of a
    # well-formed document. Unclosed groups mean the input was truncated or
    # malformed, so text may have been trapped inside a never-closed skip
    # destination -- that is the case worth warning about and falling back
    # for. Balanced braces with an empty result means the document genuinely
    # had no body text, and "" is the correct answer.
    unbalanced = len(state.skip_stack) > 1

    if unbalanced:
        logger.warning(
            "_strip_rtf() found %d unclosed RTF group(s) in a %d-character "
            "comment (malformed or truncated RTF). Falling back to the raw "
            "value so a real definition cannot be silently discarded; the "
            "report will show raw RTF for this object. Please report the "
            "source text so the tokenizer can be extended to cover it.",
            len(state.skip_stack) - 1, original_len,
        )
        return text.strip()

    if not result:
        # Legitimately empty rich-text field: log at DEBUG only, since this
        # is normal and expected, not a defect.
        logger.debug(
            "_strip_rtf(): %d-character RTF envelope contained no body text "
            "(empty description field) -- returning empty string.",
            original_len,
        )

    return result


def _pd_comment(elem: ET.Element) -> str:
    """SAP PD <a:Comment> - the field that maps to an erwin Note."""
    return _strip_rtf(_attr(elem, "Comment"))


def _pd_definition(elem: ET.Element) -> str:
    """
    SAP PD <a:Description> - the Description sub-tab of the Definition tab.
    This is the field that maps to an erwin Definition.

    Annotation is NOT accepted as a fallback here. The two sub-tabs land in
    different erwin fields (Description -> Definition, Annotation -> Extended
    Notes), so folding them together labelled an Annotation as a Description
    and then reported it lost, because erwin's Definition was legitimately
    empty. ``_pd_annotation`` carries Annotation on its own mapping instead.
    """
    return _strip_rtf(_attr(elem, "Description"))


def _pd_annotation(elem: ET.Element) -> str:
    """SAP PD <a:Annotation> - the field that maps to erwin Extended Notes."""
    return _strip_rtf(_attr(elem, "Annotation"))


def _description(elem: ET.Element) -> str:
    """
    Definition text.  PowerDesigner splits business meaning across Comment,
    Description and Annotation depending on how the modeller worked, and
    stores it as RTF (see ``_strip_rtf``).
    """
    for field_name in ("Comment", "Description", "Annotation", "Definition"):
        value = _attr(elem, field_name)
        if value:
            return _strip_rtf(value)
    return ""


# ─── DOMAINS ──────────────────────────────────────────────────────────────────

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


# ─── ATTRIBUTES ───────────────────────────────────────────────────────────────

def _parse_attribute(elem: ET.Element,
                     order: int,
                     domains_by_oid: Dict[str, Domain]) -> Attribute:
    name = _attr(elem, "Name")
    code = _attr(elem, "Code")

    data_type = _attr(elem, "DataType")
    length    = _attr(elem, "Length")
    precision = _attr(elem, "Precision")
    definition = _description(elem)

    domain_name = ""
    domain_oid = _first_ref(elem, "Domain", "Domain")
    if domain_oid and domain_oid in domains_by_oid:
        domain = domains_by_oid[domain_oid]
        domain_name = domain.name or domain.code
        if not data_type:
            data_type = domain.data_type
            length    = length    or domain.length
            precision = precision or domain.precision

    name = name or code
    code = code or name

    # LDM writes the mandatory flag as <a:LogicalAttribute.Mandatory> — a
    # different element name from a CDM's <a:Mandatory>.  Both are accepted so
    # a hand-edited or downgraded export still parses correctly.
    mandatory = _flag(elem, "LogicalAttribute.Mandatory", "Mandatory")

    return Attribute(
        oid          = elem.get("Id", ""),
        name         = name,
        code         = code,
        data_type    = data_type,
        length       = length,
        precision    = precision,
        mandatory    = mandatory,
        is_primary   = _flag(elem, "PrimaryIdentifier"),
        domain       = domain_name,
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
    )


# ─── ENTITIES ─────────────────────────────────────────────────────────────────

def _parse_entity_attributes(elem: ET.Element, entity: Entity,
                             domains_by_oid: Dict[str, Domain]) -> Dict[str, str]:
    """Parse and append attributes; return an oid→code map for identifiers."""
    attr_container = _first_child(elem, "Attributes")
    attr_code_by_oid: Dict[str, str] = {}

    for index, attr_elem in enumerate(_definitions(attr_container, "EntityAttribute"), start=1):
        attribute = _parse_attribute(attr_elem, index, domains_by_oid)
        if normalizers.is_excluded_attribute(attribute.name, attribute.code):
            continue
        entity.attributes.append(attribute)
        if attribute.oid:
            attr_code_by_oid[attribute.oid] = attribute.code
    return attr_code_by_oid


def _parse_entity_identifiers(elem: ET.Element, entity: Entity,
                              attr_code_by_oid: Dict[str, str]) -> Dict[str, Identifier]:
    ident_container = _first_child(elem, "Identifiers")
    identifiers_by_oid: Dict[str, Identifier] = {}

    for ident_elem in _definitions(ident_container, "Identifier"):
        identifier = _parse_identifier(ident_elem, attr_code_by_oid)
        entity.identifiers.append(identifier)
        if identifier.oid:
            identifiers_by_oid[identifier.oid] = identifier
    return identifiers_by_oid


def _resolve_primary_identifier(elem: ET.Element, entity: Entity,
                                identifiers_by_oid: Dict[str, Identifier]) -> None:
    # The primary identifier is a pointer (c:PrimaryIdentifier), not a flag on
    # the identifier itself — confirmed present on every entity in the file.
    primary_oid = _first_ref(elem, "PrimaryIdentifier", "Identifier")
    if primary_oid and primary_oid in identifiers_by_oid:
        identifiers_by_oid[primary_oid].is_primary = True
    elif entity.identifiers:
        _infer_primary_from_attributes(entity)


def _infer_primary_from_attributes(entity: Entity) -> None:
    primary_attrs = {a.code.upper() for a in entity.attributes if a.is_primary}
    if not primary_attrs:
        return
    for identifier in entity.identifiers:
        if {c.upper() for c in identifier.attributes} == primary_attrs:
            identifier.is_primary = True
            break


def _mark_primary_attributes(entity: Entity) -> None:
    primary = entity.primary_identifier
    if not primary:
        return
    members = {c.upper() for c in primary.attributes}
    for attribute in entity.attributes:
        if attribute.code.upper() in members:
            attribute.is_primary = True


def _parse_entity(elem: ET.Element,
                  subject_area: str,
                  domains_by_oid: Dict[str, Domain]) -> Entity:
    name = _attr(elem, "Name")
    code = _attr(elem, "Code") or name

    entity = Entity(
        oid          = elem.get("Id", ""),
        name         = name,
        code         = code,
        definition   = _description(elem),
        # report-only; does not affect any comparison
        doc_comment    = _pd_comment(elem),
        doc_definition = _pd_definition(elem),
        doc_annotation = _pd_annotation(elem),
        subject_area = subject_area,
    )

    attr_code_by_oid = _parse_entity_attributes(elem, entity, domains_by_oid)
    identifiers_by_oid = _parse_entity_identifiers(elem, entity, attr_code_by_oid)
    _resolve_primary_identifier(elem, entity, identifiers_by_oid)
    _mark_primary_attributes(entity)

    return entity


# ─── RELATIONSHIPS ────────────────────────────────────────────────────────────

def _build_relationship_end(elem: ET.Element, entity_code_by_oid: Dict[str, str],
                            oid: str, role_attr: str, card_attr: str,
                            mand_attr: str, default_many: bool) -> RelationshipEnd:
    mandatory = _flag(elem, mand_attr)
    end = RelationshipEnd(
        entity      = entity_code_by_oid.get(oid, oid or "UNKNOWN"),
        role        = _attr(elem, role_attr),
        cardinality = normalize_cardinality(_attr(elem, card_attr), mandatory=mandatory),
        mandatory   = mandatory,
        dependent   = False,
    )
    if not end.cardinality:
        end.cardinality = normalize_cardinality("", mandatory=mandatory, many=default_many)
    return end


def _resolve_parent_child_oids(elem: ET.Element, oid1: str, oid2: str,
                               identifier_owner_by_oid: Dict[str, str]) -> Tuple[str, str]:
    """Return (parent_entity_oid, child_entity_oid) or ('', '') when unknown."""
    parent_ident_oid = _first_ref(elem, "ParentIdentifier", "Identifier")
    parent_entity_oid = identifier_owner_by_oid.get(parent_ident_oid, "")
    if parent_entity_oid == oid1:
        return parent_entity_oid, oid2
    if parent_entity_oid == oid2:
        return parent_entity_oid, oid1
    return parent_entity_oid, ""


def _pk_codes_overlap(entities_by_oid: Dict[str, Entity],
                      parent_entity_oid: str, child_entity_oid: str) -> bool:
    """True when the child's PK shares any attribute code with the parent's PK."""
    parent_entity = entities_by_oid.get(parent_entity_oid)
    child_entity  = entities_by_oid.get(child_entity_oid)
    parent_pk = parent_entity.primary_identifier if parent_entity else None
    child_pk  = child_entity.primary_identifier if child_entity else None
    if not (parent_pk and child_pk):
        return False
    parent_codes = {c.upper() for c in parent_pk.attributes}
    child_codes  = {c.upper() for c in child_pk.attributes}
    return bool(parent_codes & child_codes)


def _apply_identifying(elem: ET.Element, oid1: str, oid2: str,
                       end1: RelationshipEnd, end2: RelationshipEnd,
                       entities_by_oid: Dict[str, Entity],
                       identifier_owner_by_oid: Dict[str, str]) -> bool:
    """
    Decide whether the relationship is identifying and set the dependent flag on
    the child end.  Returns the identifying verdict.
    """
    parent_entity_oid, child_entity_oid = _resolve_parent_child_oids(
        elem, oid1, oid2, identifier_owner_by_oid)
    if not (parent_entity_oid and child_entity_oid):
        return False

    identifying = _pk_codes_overlap(entities_by_oid, parent_entity_oid, child_entity_oid)
    if child_entity_oid == oid1:
        end1.dependent = identifying
    else:
        end2.dependent = identifying
    return identifying


def _parse_relationship(elem: ET.Element,
                        entity_code_by_oid: Dict[str, str],
                        entities_by_oid: Dict[str, Entity],
                        identifier_owner_by_oid: Dict[str, str]) -> Relationship:
    """
    Build a canonical Relationship from an o:Relationship element.

    Convention (confirmed against SD_O2C_LDM_WC.ldm):
    ``Entity1ToEntity2RoleCardinality`` is stored on end 1 and states how many
    *end 2* instances relate to one end 1 instance.  The canonical model
    preserves that convention exactly so the erwin parser can align to it.

    Identifying-ness (verified against the real file, not assumed):
    ``c:ParentIdentifier`` is present on EVERY relationship in this LDM — it
    names which identifier of the "one" side the join uses, not whether the
    relationship is identifying. The actual, verified signal is whether the
    CHILD entity's own primary identifier contains attribute(s) inherited
    from the PARENT entity's primary identifier (i.e. the foreign key was
    migrated into the child's own identity, not just into its attribute
    list). Cross-checked against the erwin side's Relationship <Type> for
    every one of the 10 relationships in this model with 100% agreement:
    exactly the 3 relationships where this condition holds are erwin Type=2
    (IDENTIFYING); the other 7 are erwin Type=7 (NON_IDENTIFYING).
    """
    oid1 = _first_ref(elem, "Object1", "Entity")
    oid2 = _first_ref(elem, "Object2", "Entity")

    end1 = _build_relationship_end(
        elem, entity_code_by_oid, oid1, "Entity1ToEntity2Role",
        "Entity1ToEntity2RoleCardinality", "Entity1ToEntity2RoleMandatory",
        default_many=True)
    end2 = _build_relationship_end(
        elem, entity_code_by_oid, oid2, "Entity2ToEntity1Role",
        "Entity2ToEntity1RoleCardinality", "Entity2ToEntity1RoleMandatory",
        default_many=False)

    identifying = _apply_identifying(
        elem, oid1, oid2, end1, end2, entities_by_oid, identifier_owner_by_oid)

    return Relationship(
        oid         = elem.get("Id", ""),
        name        = _attr(elem, "Name"),
        code        = _attr(elem, "Code") or _attr(elem, "Name"),
        definition  = _description(elem),
        end1        = end1,
        end2        = end2,
        kind        = "RELATIONSHIP",
        identifying = identifying,
    )


# ─── INHERITANCES ─────────────────────────────────────────────────────────────

def _parse_inheritance(elem: ET.Element,
                       entity_code_by_oid: Dict[str, str]) -> Inheritance:
    parent_oid = (_first_ref(elem, "ParentEntity", "Entity")
                  or _first_ref(elem, "Object1", "Entity"))

    child_oids: List[str] = []
    children_container = _first_child(elem, "Children")
    link_scope = children_container if children_container is not None else elem

    for link in _descendants(link_scope, "InheritanceLink"):
        oid = (_first_ref(link, "Object2", "Entity")
               or _first_ref(link, "ChildEntity", "Entity")
               or _first_ref(link, "Object1", "Entity"))
        if oid and oid != parent_oid:
            child_oids.append(oid)

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
                model: LDMModel,
                domains_by_oid: Dict[str, Domain],
                entity_elements: List[tuple]) -> None:
    """
    Collect entity elements from a model or package scope, recursing into
    nested packages so subject-area membership is preserved.
    """
    entity_container = _first_child(scope, "Entities")
    for entity_elem in _definitions(entity_container, "Entity"):
        entity_elements.append((entity_elem, subject_area))

    package_container = _first_child(scope, "Packages")
    for package_elem in _definitions(package_container, "Package"):
        package_name = _attr(package_elem, "Name") or _attr(package_elem, "Code")
        if package_name and package_name not in model.subject_areas:
            model.subject_areas.append(package_name)
        _walk_scope(package_elem, package_name, model, domains_by_oid, entity_elements)


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


def _collect_shortcut_target_models(model_elem: ET.Element) -> Tuple[Dict[str, str], str]:
    """
    Resolve shortcut owner models.  Returns (claimed_by_oid, fallback_model).

    PD resolves the "Target Model" column through the repository, which a
    single file cannot do.  Two things are available here: shortcuts a
    <o:TargetModel> block claims explicitly, and — failing that — the one
    attached model that is not the .xem extension, which is the owner whenever
    a model attaches a single shared model (the usual case).
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


def _shortcut_row(elem: ET.Element, claimed: Dict[str, str],
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

    claimed, fallback_model = _collect_shortcut_target_models(model_elem)

    for elem in container:
        if elem.tag.rsplit("}", 1)[-1] != "Shortcut":
            continue
        if elem.get("Ref") is not None or not elem.get("Id"):
            continue
        model.shortcuts.append(_shortcut_row(elem, claimed, fallback_model))


def _parse_ldm_header(root: ET.Element, model_elem: ET.Element, model: LDMModel) -> None:
    model.model_name = _attr(model_elem, "Name") or _attr(root, "Name")
    model.model_code = _attr(model_elem, "Code") or model.model_name
    declared_type = _attr(model_elem, "ModelType")
    if declared_type:
        model.model_type = declared_type


def _parse_ldm_domains(model_elem: ET.Element, model: LDMModel,
                       domains_by_oid: Dict[str, Domain]) -> None:
    for elem in _descendants(model_elem, "Domain"):
        if elem.get("Ref") is not None or not elem.get("Id"):
            continue
        domain = _parse_domain(elem)
        domains_by_oid[domain.oid] = domain
        key = (domain.code or domain.name).upper()
        if key:
            model.domains[key] = domain


def _collect_entity_elements(root: ET.Element, model_elem: ET.Element,
                             model: LDMModel,
                             domains_by_oid: Dict[str, Domain]) -> List[tuple]:
    """Gather (entity_elem, subject_area) pairs, with a non-standard fallback."""
    entity_elements: List[tuple] = []
    _walk_scope(model_elem, "", model, domains_by_oid, entity_elements)

    if entity_elements:
        return entity_elements

    seen_ids = set()
    for elem in _descendants(root, "Entity"):
        if elem.get("Ref") is not None or not elem.get("Id"):
            continue
        if elem.get("Id") in seen_ids:
            continue
        seen_ids.add(elem.get("Id"))
        entity_elements.append((elem, ""))
    if entity_elements:
        model.parse_warnings.append(
            "Entities found outside the expected c:Entities collection — "
            "file may be a non-standard export."
        )
    return entity_elements


def _register_entity_lookups(entity: Entity,
                             entity_code_by_oid: Dict[str, str],
                             entities_by_oid: Dict[str, Entity],
                             identifier_owner_by_oid: Dict[str, str]) -> None:
    if not entity.oid:
        return
    entity_code_by_oid[entity.oid] = entity.code
    entities_by_oid[entity.oid] = entity
    for identifier in entity.identifiers:
        if identifier.oid:
            identifier_owner_by_oid[identifier.oid] = entity.oid


def _parse_ldm_entities(entity_elements: List[tuple], model: LDMModel,
                        domains_by_oid: Dict[str, Domain],
                        entity_code_by_oid: Dict[str, str],
                        entities_by_oid: Dict[str, Entity],
                        identifier_owner_by_oid: Dict[str, str]) -> None:
    seen_entity_ids: set = set()
    for entity_elem, subject_area in entity_elements:
        elem_id = entity_elem.get("Id")
        if elem_id:
            if elem_id in seen_entity_ids:
                continue
            seen_entity_ids.add(elem_id)

        entity = _parse_entity(entity_elem, subject_area, domains_by_oid)
        if normalizers.is_excluded_entity(entity.name, entity.code):
            continue

        model.add_entity(entity)
        _register_entity_lookups(entity, entity_code_by_oid,
                                 entities_by_oid, identifier_owner_by_oid)


def _parse_ldm_relationships(model_elem: ET.Element, model: LDMModel,
                             entity_code_by_oid: Dict[str, str],
                             entities_by_oid: Dict[str, Entity],
                             identifier_owner_by_oid: Dict[str, str]) -> None:
    for elem in _descendants(model_elem, "Relationship"):
        if elem.get("Ref") is not None or not elem.get("Id"):
            continue
        model.relationships.append(
            _parse_relationship(elem, entity_code_by_oid, entities_by_oid,
                               identifier_owner_by_oid)
        )


def _collect_inheritance_children(model_elem: ET.Element) -> Dict[str, List[str]]:
    children_by_inheritance: Dict[str, List[str]] = {}
    for link in _descendants(model_elem, "InheritanceLink"):
        if link.get("Ref") is not None:
            continue
        owner = _first_ref(link, "Object1", "Inheritance")
        child = _first_ref(link, "Object2", "Entity")
        if owner and child:
            children_by_inheritance.setdefault(owner, []).append(child)
    return children_by_inheritance


def _parse_ldm_inheritances(model_elem: ET.Element, model: LDMModel,
                            entity_code_by_oid: Dict[str, str]) -> None:
    children_by_inheritance = _collect_inheritance_children(model_elem)

    for elem in _descendants(model_elem, "Inheritance"):
        if elem.get("Ref") is not None or not elem.get("Id"):
            continue
        inheritance = _parse_inheritance(elem, entity_code_by_oid)
        if not inheritance.children:
            linked = children_by_inheritance.get(elem.get("Id", ""), [])
            inheritance.children = [entity_code_by_oid.get(oid, oid) for oid in linked
                                    if entity_code_by_oid.get(oid, oid) != inheritance.parent]
        if inheritance.parent and inheritance.children:
            model.inheritances.append(inheritance)


def _parse_ldm_business_rules(model_elem: ET.Element, model: LDMModel) -> None:
    for elem in _descendants(model_elem, "BusinessRule"):
        if elem.get("Ref") is not None or not elem.get("Id"):
            continue
        model.business_rules.append(_parse_business_rule(elem))


def parse_ldm(filepath: str) -> LDMModel:
    """
    Parse a PowerDesigner .ldm file into an :class:`LDMModel`.

    Never raises for malformed input: a parse failure is recorded on the
    returned model so a single bad file cannot abort a large batch.
    """
    model = LDMModel(source_file=filepath, source_tool="PowerDesigner",
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

    model_elements = [node for node in _descendants(root, "Model")
                      if node.get("Ref") is None and node.get("Id")]
    model_elem = model_elements[0] if model_elements else root

    _parse_ldm_header(root, model_elem, model)

    domains_by_oid: Dict[str, Domain] = {}
    _parse_ldm_domains(model_elem, model, domains_by_oid)

    _parse_shortcuts(model_elem, model)

    entity_elements = _collect_entity_elements(root, model_elem, model, domains_by_oid)

    entity_code_by_oid: Dict[str, str] = {}
    entities_by_oid: Dict[str, Entity] = {}
    identifier_owner_by_oid: Dict[str, str] = {}
    _parse_ldm_entities(entity_elements, model, domains_by_oid,
                        entity_code_by_oid, entities_by_oid, identifier_owner_by_oid)

    _parse_ldm_relationships(model_elem, model, entity_code_by_oid,
                             entities_by_oid, identifier_owner_by_oid)
    _parse_ldm_inheritances(model_elem, model, entity_code_by_oid)
    _parse_ldm_business_rules(model_elem, model)

    logger.debug("Parsed %s -> %s", filepath, model.stats())
    return model