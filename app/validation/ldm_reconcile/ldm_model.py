"""
Canonical Logical Data Model
-----------------------------
Both parsers (PowerDesigner .ldm and erwin logical XML export) emit objects
from this module.  The comparator therefore never sees a tool-specific
structure, which is what keeps the reconciliation logic tool-agnostic and
testable.

This module intentionally mirrors the shape of a Conceptual Data Model
(entities / attributes / identifiers / relationships / inheritance / domains)
because that is what the two supplied LDM files actually contain — verified by
direct inspection of SD_O2C_LDM_WC.ldm and SD_O2C_LDM_WC.xml, not assumed from
the CDM reference. A Logical Data Model additionally carries stronger
identifier and relationship-cardinality semantics than a CDM (identifiers are
always populated in a well-formed LDM, and relationships routinely encode
identifying vs. non-identifying dependency), which is why the comparator built
on top of this model applies LDM-appropriate severities rather than reusing
CDM's.

Object mapping across the two tools, as observed in the supplied files:

    Canonical            PowerDesigner LDM        erwin (logical export)
    ─────────────────────────────────────────────────────────────────────────
    Entity               o:Entity                 Entity
    Attribute            o:EntityAttribute        Attribute
    Identifier           o:Identifier             Key_Group (PK / AK)
    Relationship         o:Relationship           Relationship
    Inheritance          o:Inheritance            Subtype_Relationship
    Domain               o:Domain                 Domain
    BusinessRule         o:BusinessRule           Validation_Rule
    SubjectArea          o:Package                Subject_Area
"""

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ─── LEAF OBJECTS ─────────────────────────────────────────────────────────────

@dataclass
class Attribute:
    """A logical attribute — a business fact carried by an entity."""
    oid:          str = ""          # tool-internal id, used only for ref resolution
    name:         str = ""          # business name, e.g. "Customer Number"
    code:         str = ""          # technical code, e.g. "CUSTOMER_NUMBER"
    data_type:    str = ""          # raw logical type as written by the tool
    length:       str = ""
    precision:    str = ""
    mandatory:    bool = False      # participates in existence constraint
    is_primary:   bool = False      # member of the primary identifier
    domain:       str = ""          # name of the assigned domain, if any
    definition:   str = ""          # Comment / Description / Definition text
    # -- documentation mapping (report-only, additive; see Entity) -----------
    doc_comment:    str = ""
    doc_note:       str = ""
    doc_definition: str = ""
    doc_annotation:     str = ""
    doc_extended_notes: str = ""
    multiplicity: str = ""          # e.g. "0..1", "1..*" when modelled
    order:        int = 0           # declaration order within the entity
    is_migrated:  bool = False      # erwin role-migrated FK (no LDM-source counterpart)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Identifier:
    """A primary or alternate identifier (candidate key at logical level)."""
    oid:        str = ""
    name:       str = ""
    code:       str = ""
    is_primary: bool = False
    attributes: List[str] = field(default_factory=list)   # attribute codes, in order

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Entity:
    """A logical entity — a thing the business recognises and talks about."""
    oid:          str = ""
    name:         str = ""
    code:         str = ""
    definition:   str = ""
    # -- documentation mapping (report-only, additive) -----------------------
    # `definition` above keeps its first-match-wins behaviour so every existing
    # comparison result is unchanged. These three carry the SPECIFIC fields the
    # Comment->Note / Definition->Definition report needs:
    #   doc_comment    SAP PD <a:Comment>      / erwin <Comment>
    #   doc_note       (unused on the PD side) / erwin Note_List_Array text
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

    # ── convenience accessors ────────────────────────────────────────────────
    @property
    def primary_identifier(self) -> Optional[Identifier]:
        for ident in self.identifiers:
            if ident.is_primary:
                return ident
        return None

    @property
    def alternate_identifiers(self) -> List[Identifier]:
        return [i for i in self.identifiers if not i.is_primary]

    def attribute_by_code(self, code: str) -> Optional[Attribute]:
        for attr in self.attributes:
            if attr.code.upper() == code.upper():
                return attr
        return None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RelationshipEnd:
    """One end of a logical relationship."""
    entity:      str = ""       # entity code
    role:        str = ""       # verb phrase, e.g. "places"
    cardinality: str = ""       # canonical "0,1" | "1,1" | "0,n" | "1,n"
    mandatory:   bool = False
    dependent:   bool = False   # identifying / existence-dependent end

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Relationship:
    """A binary logical relationship between two entities."""
    oid:        str = ""
    name:       str = ""
    code:       str = ""
    definition: str = ""
    end1:       RelationshipEnd = field(default_factory=RelationshipEnd)
    end2:       RelationshipEnd = field(default_factory=RelationshipEnd)
    kind:       str = "RELATIONSHIP"   # RELATIONSHIP | ASSOCIATION
    identifying: bool = False          # parent key propagates into the child's identity

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
class LDMModel:
    """
    The complete parsed logical model.

    ``entities`` is keyed on the entity's code (upper-cased) so the comparator
    can do set arithmetic directly; the ordered ``entity_list`` preserves
    declaration order for reports.
    """
    source_file:   str = ""
    source_tool:   str = ""      # "PowerDesigner" | "erwin"
    model_name:    str = ""
    model_code:    str = ""
    model_type:    str = "Logical"
    entities:      Dict[str, Entity] = field(default_factory=dict)
    relationships: List[Relationship] = field(default_factory=list)
    inheritances:  List[Inheritance]  = field(default_factory=list)
    domains:       Dict[str, Domain]  = field(default_factory=dict)
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
