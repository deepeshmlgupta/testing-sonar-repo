"""
Canonical Conceptual Model
--------------------------
Both parsers (PowerDesigner .cdm and erwin logical XML) emit objects from this
module.  The comparator therefore never sees a tool-specific structure, which is
what keeps the reconciliation logic tool-agnostic and testable.

Object mapping across the two tools:

    Canonical            PowerDesigner CDM        erwin (logical)
    ─────────────────────────────────────────────────────────────────────────
    Entity               o:Entity                 Entity
    Attribute            o:EntityAttribute        Attribute
    Identifier           o:Identifier             Key_Group (PK / AK)
    Relationship         o:Relationship           Relationship
    Association          o:Association            Entity (associative) / M:N rel
    Inheritance          o:Inheritance            Subtype_Relationship
    Domain               o:Domain                 Domain
    BusinessRule         o:BusinessRule           Validation_Rule / Business_Rule
    SubjectArea          o:Package                Subject_Area
"""

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ─── LEAF OBJECTS ─────────────────────────────────────────────────────────────

@dataclass
class Attribute:
    """A conceptual attribute — a business fact about an entity."""
    oid:          str = ""          # tool-internal id, used only for ref resolution
    name:         str = ""          # business name, e.g. "Customer Order Date"
    code:         str = ""          # technical code, e.g. "CUSTOMER_ORDER_DATE"
    data_type:    str = ""          # raw conceptual type as written by the tool
    length:       str = ""
    precision:    str = ""
    mandatory:    bool = False      # participates in existence constraint
    is_primary:   bool = False      # member of the primary identifier
    domain:       str = ""          # name of the assigned domain, if any
    data_item:    str = ""          # PowerDesigner data-item name (reuse tracking)
    definition:   str = ""          # Comment / Description / Definition text
    # ── documentation mapping (report-only, additive; see Entity) ────────────
    doc_comment:    str = ""
    doc_note:       str = ""
    doc_definition: str = ""
    doc_annotation:     str = ""
    doc_extended_notes: str = ""
    multiplicity: str = ""          # e.g. "0..1", "1..*" when modelled
    order:        int = 0           # declaration order within the entity
    is_migrated:  bool = False      # erwin role-migrated key (no CDM counterpart)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Identifier:
    """A primary or alternate identifier (candidate key at conceptual level)."""
    oid:        str = ""
    name:       str = ""
    code:       str = ""
    is_primary: bool = False
    attributes: List[str] = field(default_factory=list)   # attribute codes, in order

    # Free-text note the modeller attached to the identifier.  PowerDesigner
    # modellers routinely list the *intended* key composition here when the
    # structure itself was never completed, so it is real evidence about intent
    # and the comparator uses it to corroborate composition differences.
    comment:    str = ""

    # True when the tool created this key group as a physical access path (an
    # erwin inversion entry / FK index) rather than as a business uniqueness
    # rule.  A conceptual model cannot contain one, so it must never be compared
    # against a CDM alternate identifier — but it is still reported, as INFO.
    is_inversion_entry: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def documented_members(self) -> List[str]:
        """
        Attribute names the comment appears to enumerate.

        Modellers write one member per line, or separate them with commas,
        semicolons or plus signs.  Anything that is not a single bare token is
        prose, not a member list, and is discarded — so a genuine description
        never masquerades as evidence.
        """
        import re
        tokens = [t.strip() for t in re.split(r"[\n,;+/]+", self.comment or "")]
        return [t for t in tokens if t and re.fullmatch(r"[A-Za-z0-9_\- ]+", t)
                and " " not in t.strip()]


@dataclass
class Entity:
    """A conceptual entity — a thing the business recognises and talks about."""
    oid:          str = ""
    name:         str = ""
    code:         str = ""
    definition:   str = ""
    # ── documentation mapping (report-only, additive) ────────────────────────
    # `definition` above keeps its original first-match-wins behaviour so every
    # existing comparison result is unchanged.  These two carry the SPECIFIC
    # fields the Comments→Notes / Definition→Definition report needs:
    #   doc_comment    SAP PD <a:Comment>          / erwin <Comment>
    #   doc_note       (unused on the PD side)     / erwin Note_List_Array text
    #   doc_definition     SAP PD <a:Description>  / erwin <Definition>
    #   doc_annotation     SAP PD <a:Annotation>   / (unused on the erwin side)
    #   doc_extended_notes (unused on the PD side) / erwin Extended_Notes text
    doc_comment:    str = ""
    doc_note:       str = ""
    doc_definition: str = ""
    doc_annotation:     str = ""
    doc_extended_notes: str = ""
    subject_area: str = ""          # package / subject area membership
    is_associative: bool = False    # resolves a many-to-many relationship
    attributes:   List[Attribute]  = field(default_factory=list)
    identifiers:  List[Identifier] = field(default_factory=list)

    # Every attribute the tool migrated into this entity across a relationship.
    # Recorded separately from `attributes` because
    # ERWIN_MIGRATED_KEY_HANDLING="ignore" deliberately keeps migrated
    # attributes out of the comparison list — yet the comparator still needs to
    # know which key members were migrated in order to tell erwin's automatic
    # key migration apart from a genuine change of entity identity.  The whole
    # Attribute is kept, not just the code, because an identifier comment may
    # name a member by its business name while the key group lists its code.
    migrated_attributes: List[Attribute] = field(default_factory=list)

    # ── convenience accessors ────────────────────────────────────────────────
    @property
    def primary_identifier(self) -> Optional[Identifier]:
        for ident in self.identifiers:
            if ident.is_primary:
                return ident
        return None

    @property
    def alternate_identifiers(self) -> List[Identifier]:
        """Business uniqueness rules only — inversion entries are not keys."""
        return [i for i in self.identifiers
                if not i.is_primary and not i.is_inversion_entry]

    @property
    def inversion_entries(self) -> List[Identifier]:
        """Tool-generated access paths (erwin IE / IF1 / IF2 … key groups)."""
        return [i for i in self.identifiers if i.is_inversion_entry]

    @property
    def all_attributes(self) -> List[Attribute]:
        """Declared attributes plus any the tool migrated in and excluded."""
        return list(self.attributes) + list(self.migrated_attributes)

    @property
    def attribute_aliases(self) -> List[List[str]]:
        """
        For every attribute, the set of names it is known by: [code, name].

        The two tools disagree about which name they store where — PowerDesigner
        keeps Name and Code, erwin keeps name and Physical_Name — and a key group
        lists its members by code while a modeller's comment usually names them
        by business name.  Callers resolve across the pair so those can be
        matched, which also means a name/code typo becomes visible rather than
        fatal.  Normalisation is left to the caller.
        """
        return [[a for a in (attr.code, attr.name) if a]
                for attr in self.all_attributes]

    def attribute_by_code(self, code: str) -> Optional[Attribute]:
        for attr in self.attributes:
            if attr.code.upper() == code.upper():
                return attr
        return None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RelationshipEnd:
    """One end of a conceptual relationship."""
    entity:      str = ""       # entity code
    role:        str = ""       # verb phrase, e.g. "places"
    cardinality: str = ""       # canonical "0,1" | "1,1" | "0,n" | "1,n"
    mandatory:   bool = False
    dependent:   bool = False   # identifying / existence-dependent end

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Relationship:
    """A binary conceptual relationship between two entities."""
    oid:        str = ""
    name:       str = ""
    code:       str = ""
    definition: str = ""
    end1:       RelationshipEnd = field(default_factory=RelationshipEnd)
    end2:       RelationshipEnd = field(default_factory=RelationshipEnd)
    kind:       str = "RELATIONSHIP"   # RELATIONSHIP | ASSOCIATION

    @property
    def entities(self) -> tuple:
        return (self.end1.entity, self.end2.entity)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Inheritance:
    """A generalisation: one supertype, one or more subtypes."""
    oid:        str = ""
    name:       str = ""
    code:       str = ""
    parent:     str = ""                                  # supertype entity code
    children:   List[str] = field(default_factory=list)   # subtype entity codes
    complete:   bool = False        # every parent instance is some subtype
    mutually_exclusive: bool = True # an instance belongs to at most one subtype
    generate_parent:    bool = True # PowerDesigner generation hint

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Domain:
    """A reusable semantic type shared by many attributes."""
    oid:        str = ""
    name:       str = ""
    code:       str = ""
    data_type:  str = ""
    length:     str = ""
    precision:  str = ""
    definition: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BusinessRule:
    """A named business constraint attached to the model."""
    oid:        str = ""
    name:       str = ""
    code:       str = ""
    rule_type:  str = ""      # Constraint | Definition | Fact | Formula | Requirement
    expression: str = ""
    definition: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─── MODEL CONTAINER ──────────────────────────────────────────────────────────

@dataclass
class CDMModel:
    """
    The complete parsed conceptual model.

    ``entities`` is keyed on the entity's code (upper-cased) so the comparator can
    do set arithmetic directly; the ordered ``entity_list`` preserves declaration
    order for reports.
    """
    source_file:   str = ""
    source_tool:   str = ""      # "PowerDesigner" | "erwin"
    model_name:    str = ""
    model_code:    str = ""
    model_type:    str = ""      # Conceptual | Logical
    entities:      Dict[str, Entity] = field(default_factory=dict)
    relationships: List[Relationship] = field(default_factory=list)
    inheritances:  List[Inheritance]  = field(default_factory=list)
    domains:       Dict[str, Domain]  = field(default_factory=dict)
    # PowerDesigner Data Items keyed by tool OID (PD allows duplicate
    # names, so the OID is the only safe key).  erwin never fills this.
    data_items:    Dict[str, Dict[str, str]] = field(default_factory=dict)
    # PowerDesigner shortcuts — references to objects owned by other
    # models (glossary terms/categories, shared entities).  PD-only.
    shortcuts:     List[Dict[str, str]] = field(default_factory=list)
    business_rules: List[BusinessRule] = field(default_factory=list)
    subject_areas: List[str] = field(default_factory=list)
    parse_error:   str = ""
    parse_warnings: List[str] = field(default_factory=list)

    # ── convenience accessors ────────────────────────────────────────────────
    @property
    def entity_list(self) -> List[Entity]:
        return list(self.entities.values())

    @property
    def attribute_count(self) -> int:
        return sum(len(e.attributes) for e in self.entities.values())

    @property
    def identifier_count(self) -> int:
        return sum(len(e.identifiers) for e in self.entities.values())

    @property
    def associations(self) -> List[Relationship]:
        return [r for r in self.relationships if r.kind == "ASSOCIATION"]

    def entity_by_oid(self, oid: str) -> Optional[Entity]:
        for ent in self.entities.values():
            if ent.oid == oid:
                return ent
        return None

    def add_entity(self, entity: Entity) -> None:
        """
        Register an entity under its code, de-duplicating collisions so a model
        with two entities sharing one code never silently loses one of them.
        """
        key = (entity.code or entity.name).strip().upper()
        if not key:
            key = f"UNNAMED_{len(self.entities) + 1}"
        if key in self.entities:
            suffix = 2
            while f"{key}#{suffix}" in self.entities:
                suffix += 1
            self.parse_warnings.append(
                f"Duplicate entity code '{key}' — stored as '{key}#{suffix}'"
            )
            key = f"{key}#{suffix}"
        self.entities[key] = entity

    def stats(self) -> Dict[str, int]:
        return {
            "entities":       len(self.entities),
            "attributes":     self.attribute_count,
            "identifiers":    self.identifier_count,
            "relationships":  len([r for r in self.relationships
                                   if r.kind == "RELATIONSHIP"]),
            "associations":   len(self.associations),
            "inheritances":   len(self.inheritances),
            "domains":        len(self.domains),
            "shortcuts":      len(self.shortcuts),
            "data_items":     len(self.data_items),
            "business_rules": len(self.business_rules),
        }

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source_file":  self.source_file,
            "source_tool":  self.source_tool,
            "model_name":   self.model_name,
            "model_type":   self.model_type,
            "parse_error":  self.parse_error,
            "stats":        self.stats(),
            "entities":     {k: v.as_dict() for k, v in self.entities.items()},
            "relationships": [r.as_dict() for r in self.relationships],
            "inheritances":  [i.as_dict() for i in self.inheritances],
            "domains":       {k: v.as_dict() for k, v in self.domains.items()},
            "business_rules": [b.as_dict() for b in self.business_rules],
        }
