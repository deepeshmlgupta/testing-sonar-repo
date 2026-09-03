"""
Conceptual Model Reconciliation Engine
--------------------------------------
Compares a parsed SAP PD CDM against a parsed erwin logical model and
produces a structured list of Findings plus per-object reconciliation records.

What makes conceptual reconciliation different from physical reconciliation:

  • **There are no columns to key on.**  A physical FK is identified by the
    columns it joins; a conceptual relationship is identified only by the pair of
    entities it connects.  Relationships are therefore matched in successive
    passes — name, then endpoints-plus-cardinality, then endpoints alone — so a
    relationship whose cardinality changed is reported as a *changed*
    relationship rather than as one deletion plus one addition.

  • **Names are the vocabulary, so renames must be visible.**  Entities and
    attributes are matched on code, then business name, then normalised name.
    Anything matched on a fallback key is reported, because a silent rename is
    exactly the kind of drift a conceptual reconciliation exists to catch.

  • **Meaning outranks storage.**  Cardinality, optionality, identifiers and
    inheritance carry the model's semantics and are graded CRITICAL.  Conceptual
    data types are advisory at this layer and are graded WARNING.

Finding fields
--------------
    category     ENTITY | ATTRIBUTE | IDENTIFIER | RELATIONSHIP | CARDINALITY | …
    severity     CRITICAL | WARNING | INFO
    object_type  ENTITY | ATTRIBUTE | RELATIONSHIP | INHERITANCE | DOMAIN | MODEL
    object_name  entity / relationship name the finding belongs to
    member       attribute or role name inside that object, when applicable
    message      human-readable description
    pd_value     value as modelled in SAP PD
    erwin_value  value as modelled in erwin
    remediation  the concrete fix a modeller should apply
"""

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import cardinality as card
from app.config.validation_config import CDM_CONFIG as config
from . import documentation
from . import normalizers
from .cdm_model import (Attribute, CDMModel, Entity, Identifier, Inheritance,
                       Relationship, RelationshipEnd)

logger = logging.getLogger(__name__)


# ─── SEVERITY POLICY ──────────────────────────────────────────────────────────
# Declarative default severity per finding category.  Every emission routes
# through _severity(), so config.SEVERITY_OVERRIDES can retune any category —
# including switching it off — without touching comparison logic.
SEVERITY_DEFAULTS: Dict[str, str] = {
    # Structure — loss here is loss of model content
    "ENTITY_MISSING":            "CRITICAL",
    "ENTITY_EXTRA":              "WARNING",
    "ATTRIBUTE_MISSING":         "CRITICAL",
    "ATTRIBUTE_EXTRA":           "WARNING",
    "PRIMARY_IDENTIFIER":        "CRITICAL",
    "ALTERNATE_IDENTIFIER":      "WARNING",
    "IDENTIFIER_VERIFIED":       "VERIFIED",  # matched identifier — context only, never scored
    "DOMAIN_VERIFIED":           "VERIFIED",  # matched domain — context only, never scored
    "INHERITANCE_VERIFIED":      "VERIFIED",  # matched hierarchy — context only, never scored
    "SHORTCUT":                  "VERIFIED",  # external reference — accounted for, never scored
    "INHERITED_IDENTITY":        "INFO",

    # Identity differences the two tools express in different places.  These are
    # separate categories precisely so PRIMARY_IDENTIFIER can stay CRITICAL: a
    # governance policy may accept automatic key migration while still refusing
    # to accept a key member that was genuinely lost.
    "IDENTIFYING_KEY_MIGRATION": "WARNING",   # erwin PK ⊃ SAP PD PK, extras all migrated
    "IDENTIFIER_UNDERSPECIFIED": "WARNING",   # SAP PD comment documents members it never built
    "ERWIN_INVERSION_ENTRY":     "INFO",      # tool-generated FK index, not a candidate key

    # Semantics — loss here silently changes what the model asserts
    "RELATIONSHIP_MISSING":      "CRITICAL",
    "RELATIONSHIP_EXTRA":        "WARNING",
    "CARDINALITY":               "CRITICAL",
    "OPTIONALITY":               "WARNING",
    "DEPENDENCY":                "WARNING",
    "ROLE_NAME":                 "INFO",
    "INHERITANCE_MISSING":       "CRITICAL",
    "INHERITANCE_EXTRA":         "WARNING",
    "INHERITANCE_STRUCTURE":     "CRITICAL",
    "INHERITANCE_CONSTRAINT":    "WARNING",
    "ASSOCIATION":               "WARNING",
    "MANDATORY":                 "WARNING",

    # Vocabulary
    "BUSINESS_NAME":             "WARNING",
    "DEFINITION":                "INFO",
    "DEFINITION_LOSS":           "WARNING",
    "DOMAIN":                    "WARNING",
    "DATA_ITEM":                 "INFO",      # PD-only object with a migration gap to note
    "DATA_ITEM_VERIFIED":        "VERIFIED",  # data item fully carried by its attributes
    "DATA_TYPE":                 "WARNING",
    "LENGTH_PRECISION":          "INFO",
    "ATTRIBUTE_ORDER":           "INFO",
    "FALLBACK_MATCH":            "INFO",

    # Governance
    "BUSINESS_RULE":             "WARNING",
    "SUBJECT_AREA":              "INFO",
    "MODEL_QUALITY":             "WARNING",
    "MODEL_METADATA":            "INFO",

    # Infrastructure
    "PARSE_ERROR":               "CRITICAL",
    "PARSE_WARNING":             "WARNING",
    "EXCEPTION":                 "CRITICAL",
}

MISSING = "—"


def _severity(category: str) -> str:
    """Effective severity for a category after applying config overrides."""
    return config.resolve_severity(category, SEVERITY_DEFAULTS.get(category, "WARNING"))


# ─── FINDING ──────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    category:    str
    severity:    str
    object_type: str = ""
    object_name: str = ""
    member:      str = ""
    message:     str = ""
    pd_value:    str = ""
    erwin_value: str = ""
    remediation: str = ""

    def as_dict(self) -> Dict[str, str]:
        return asdict(self)


# ─── RECONCILIATION RECORDS (for the matrix report sheets) ────────────────────

@dataclass
class EntityReconciliation:
    pd_name:        str = ""
    pd_code:        str = ""
    erwin_name:     str = ""
    erwin_code:     str = ""
    match_basis:    str = ""      # code | name | normalized name | UNMATCHED
    status:         str = ""      # MATCHED | MISSING_IN_ERWIN | EXTRA_IN_ERWIN
    pd_attributes:    int = 0
    erwin_attributes: int = 0
    attributes_matched: int = 0
    pd_primary_id:    str = ""
    erwin_primary_id: str = ""
    critical: int = 0
    warning:  int = 0
    info:     int = 0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RelationshipReconciliation:
    pd_name:      str = ""
    erwin_name:   str = ""
    entities:     str = ""
    pd_signature:    str = ""
    erwin_signature: str = ""
    pd_degree:    str = ""
    erwin_degree: str = ""
    match_basis:  str = ""
    status:       str = ""      # MATCHED | CARDINALITY_CHANGED | MISSING_IN_ERWIN | EXTRA_IN_ERWIN

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)




@dataclass
class DomainReconciliation:
    """One row of the DOMAINS census sheet — matched domains included."""
    pd_name:    str = ""
    pd_code:    str = ""
    pd_type:    str = ""       # rendered "A10 (10)" style
    erwin_name: str = ""
    erwin_type: str = ""
    match_basis: str = ""
    status:     str = ""       # MATCHED | TYPE_DIFFERS | MISSING_IN_ERWIN | EXTRA_IN_ERWIN

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class InheritanceReconciliation:
    """One row of the INHERITANCES census sheet — matched hierarchies included."""
    name:           str = ""
    pd_parent:      str = ""
    erwin_parent:   str = ""
    pd_children:    str = ""   # comma-joined subtype list
    erwin_children: str = ""
    pd_count:       int = 0
    erwin_count:    int = 0
    pd_constraints:    str = ""   # "Complete, Exclusive"
    erwin_constraints: str = ""
    match_basis:    str = ""
    status:         str = ""   # MATCHED | STRUCTURE_DIFFERS | CONSTRAINT_DIFFERS | MISSING_IN_ERWIN | EXTRA_IN_ERWIN

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─── VALIDATION RESULT ────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    pd_file:     str
    erwin_file:  str
    pd_model:    str = ""
    erwin_model: str = ""
    status:      str = "PASS"      # PASS | WARN | FAIL | ERROR
    findings:    List[Finding] = field(default_factory=list)

    # Comments→Notes / Definition→Definition mapping rows for the report's
    # DOCUMENTATION sheet.  Report-only: nothing here feeds the findings list,
    # the fidelity score or the PASS/WARN/FAIL status.
    documentation_rows: List = field(default_factory=list)

    # ── Object counters ──────────────────────────────────────────────────────
    entities_pd:      int = 0
    entities_erwin:   int = 0
    entities_matched: int = 0
    entities_missing_in_erwin: int = 0
    entities_extra_in_erwin:   int = 0

    attributes_pd:      int = 0
    attributes_erwin:   int = 0
    attributes_matched: int = 0
    attributes_missing_in_erwin: int = 0
    attributes_extra_in_erwin:   int = 0

    relationships_pd:      int = 0
    relationships_erwin:   int = 0
    relationships_matched: int = 0
    relationships_missing_in_erwin: int = 0
    relationships_extra_in_erwin:   int = 0

    identifiers_pd:    int = 0
    identifiers_erwin: int = 0
    inheritances_pd:    int = 0
    inheritances_erwin: int = 0
    domains_pd:    int = 0
    domains_erwin: int = 0
    data_items_pd: int = 0
    shortcuts_pd:  int = 0

    # ── Finding counters ─────────────────────────────────────────────────────
    critical_count: int = 0
    warning_count:  int = 0
    info_count:     int = 0

    fidelity_score:  float = 100.0
    needs_review:    bool = False

    entity_records:       List[EntityReconciliation] = field(default_factory=list)
    relationship_records: List[RelationshipReconciliation] = field(default_factory=list)
    domain_records:       List[DomainReconciliation] = field(default_factory=list)
    inheritance_records:  List[InheritanceReconciliation] = field(default_factory=list)

    # ── Mutators ─────────────────────────────────────────────────────────────
    def add(self, finding: Optional[Finding]) -> None:
        """Record a finding, ignoring categories switched off via config."""
        if finding is None or finding.severity == "IGNORE":
            return
        self.findings.append(finding)
        if finding.severity == "CRITICAL":
            self.critical_count += 1
        elif finding.severity == "WARNING":
            self.warning_count += 1
        elif finding.severity == "INFO":
            self.info_count += 1
        # Any other severity ("VERIFIED") is context-only: it appears
        # in the FINDINGS sheet as evidence of what migrated intact,
        # but is never weighted into fidelity or PASS/WARN/FAIL.

    def emit(self, category: str, severity: Optional[str] = None,
             **kwargs) -> Optional[Finding]:
        """
        Build a finding at the category's effective severity and record it.

        `severity` overrides the category default.  This is needed for findings
        that are pre-existing in BOTH models: they belong in the report as
        context but must not be weighted as migration defects.
        """
        effective = severity or _severity(category)
        if effective == "IGNORE" or _severity(category) == "IGNORE":
            return None
        finding = Finding(category=category, severity=effective, **kwargs)
        self.add(finding)
        return finding

    def by_severity(self, severity: str) -> List[Finding]:
        return [f for f in self.findings if f.severity == severity]

    def compute_score(self) -> None:
        """
        Migration fidelity: the share of comparable objects that survived the
        migration unaltered, weighted by how badly each difference distorts the
        model.  Reported as a percentage so it can be tracked release over release.
        """
        # A model that could not be validated scores zero: any other number would
        # imply a comparison that never happened, and would sort the worst case
        # into the middle of the report.
        if self.status == "ERROR":
            self.fidelity_score = 0.0
            self.needs_review   = True
            return

        # The denominator counts OBJECTS while the penalty counts FINDINGS, and a
        # single object can raise several findings (one relationship can trip
        # CARDINALITY + OPTIONALITY + ROLE_NAME).  The ratio could therefore
        # exceed 1 and clamp the score to 0, which destroys all resolution —
        # "slightly wrong" and "catastrophically wrong" both scored 0.00.
        # Each object is allowed to contribute at most FIDELITY_MAX_PENALTY_PER_OBJECT
        # so the ratio stays bounded and the score keeps its ordering.
        comparable = max(1, (self.entities_pd + self.attributes_pd +
                             self.relationships_pd + self.identifiers_pd +
                             self.inheritances_pd))
        weights = config.FIDELITY_WEIGHTS
        penalty = (self.critical_count * weights.get("CRITICAL", 1.0) +
                   self.warning_count  * weights.get("WARNING", 0.35) +
                   self.info_count     * weights.get("INFO", 0.05))
        cap = getattr(config, "FIDELITY_MAX_PENALTY_PER_OBJECT", 1.0)
        penalty = min(penalty, comparable * cap)
        self.fidelity_score = round(max(0.0, 100.0 * (1.0 - penalty / comparable)), 2)
        self.needs_review = self.fidelity_score < config.FIDELITY_REVIEW_THRESHOLD

    def compute_status(self) -> None:
        if self.status == "ERROR":
            return
        if self.critical_count > 0:
            self.status = "FAIL"
        elif self.warning_count > 0:
            self.status = "WARN"
        else:
            self.status = "PASS"

    def finalise(self) -> None:
        self.compute_score()
        self.compute_status()

    def as_dict(self) -> Dict[str, Any]:
        payload = {k: v for k, v in asdict(self).items()
                   if k not in ("findings", "entity_records", "relationship_records",
                              "domain_records", "inheritance_records")}
        payload["findings"] = [f.as_dict() for f in self.findings]
        return payload


# ─── GENERIC MULTI-PASS MATCHER ───────────────────────────────────────────────

KeyFunc = Tuple[str, Callable[[Any], str]]


@dataclass
class MatchOutcome:
    pairs: List[Tuple[Any, Any, str]] = field(default_factory=list)
    unmatched_left:  List[Any] = field(default_factory=list)
    unmatched_right: List[Any] = field(default_factory=list)


def match_objects(left: Sequence[Any],
                  right: Sequence[Any],
                  key_funcs: Sequence[KeyFunc]) -> MatchOutcome:
    """
    Match two collections using an ordered list of key functions.

    Each pass considers only the items still unmatched, and pairs them only when
    the key is *unique on both sides*.  Ambiguous keys are deliberately left for
    a later pass rather than guessed at — a wrong pairing produces two false
    findings and hides a real one, which is worse than an honest "unmatched".
    """
    remaining_left  = list(left)
    remaining_right = list(right)
    outcome = MatchOutcome()

    for basis, key_func in key_funcs:
        if not remaining_left or not remaining_right:
            break

        left_index:  Dict[str, List[Any]] = {}
        right_index: Dict[str, List[Any]] = {}

        for item in remaining_left:
            key = key_func(item)
            if key:
                left_index.setdefault(key, []).append(item)
        for item in remaining_right:
            key = key_func(item)
            if key:
                right_index.setdefault(key, []).append(item)

        matched_left, matched_right = [], []
        for key, left_items in left_index.items():
            right_items = right_index.get(key)
            if not right_items:
                continue
            if len(left_items) == 1 and len(right_items) == 1:
                outcome.pairs.append((left_items[0], right_items[0], basis))
                matched_left.append(left_items[0])
                matched_right.append(right_items[0])

        remaining_left  = [i for i in remaining_left  if i not in matched_left]
        remaining_right = [i for i in remaining_right if i not in matched_right]

    outcome.unmatched_left  = remaining_left
    outcome.unmatched_right = remaining_right
    return outcome


def _named_key_funcs(mode: str) -> List[KeyFunc]:
    """Key-function ladder for entities and attributes, driven by config."""
    by_code = ("code", lambda o: normalizers.compare_key(o.code))
    by_name = ("name", lambda o: normalizers.compare_key(o.name))
    by_norm_name = ("normalized name", lambda o: normalizers.normalize_name(o.name))
    by_norm_code = ("normalized code", lambda o: normalizers.normalize_name(o.code))
    by_cross = ("name↔code", lambda o: normalizers.normalize_name(o.code or o.name))

    if mode == "code":
        return [by_code]
    if mode == "name":
        return [by_name]
    if mode == "normalized":
        return [by_norm_name, by_norm_code]
    return [by_code, by_name, by_norm_name, by_norm_code, by_cross]


# ─── ATTRIBUTE COMPARISON ─────────────────────────────────────────────────────

def _compare_attribute_pair(result: ValidationResult,
                            entity_label: str,
                            pd_attr: Attribute,
                            erwin_attr: Attribute,
                            match_basis: str) -> None:
    """Compare every conceptual property of one matched attribute pair."""
    label = pd_attr.code or pd_attr.name

    if match_basis != "code" and config.REPORT_FALLBACK_MATCHES:
        result.emit(
            "FALLBACK_MATCH", object_type="ATTRIBUTE",
            object_name=entity_label, member=label,
            message=f"Attribute matched on {match_basis}, not on code — possible rename",
            pd_value=f"{pd_attr.name} / {pd_attr.code}",
            erwin_value=f"{erwin_attr.name} / {erwin_attr.code}",
            remediation="Confirm the rename was intentional and update the "
                        "business glossary, or restore the original code.",
        )

    if config.CHECK_BUSINESS_NAMES and not normalizers.names_equivalent(
            pd_attr.name, erwin_attr.name):
        result.emit(
            "BUSINESS_NAME", object_type="ATTRIBUTE",
            object_name=entity_label, member=label,
            message="Attribute business name differs",
            pd_value=pd_attr.name or "(none)",
            erwin_value=erwin_attr.name or "(none)",
            remediation="Align the logical name in erwin with the SAP PD business name.",
        )

    if config.CHECK_DATA_TYPES and not normalizers.types_match(
            pd_attr.data_type, erwin_attr.data_type):
        result.emit(
            "DATA_TYPE", object_type="ATTRIBUTE",
            object_name=entity_label, member=label,
            message=f"Conceptual data type differs "
                    f"(compared at '{config.TYPE_COMPARISON_MODE}' strictness)",
            pd_value=normalizers.describe_type(pd_attr.data_type,
                                               pd_attr.length, pd_attr.precision),
            erwin_value=normalizers.describe_type(erwin_attr.data_type,
                                                  erwin_attr.length, erwin_attr.precision),
            remediation="Reassign the erwin logical datatype or domain so both "
                        "sides express the same conceptual type family.",
        )

    if config.CHECK_LENGTH_PRECISION and not normalizers.dimensions_match(
            pd_attr.data_type, pd_attr.length, pd_attr.precision,
            erwin_attr.data_type, erwin_attr.length, erwin_attr.precision):
        result.emit(
            "LENGTH_PRECISION", object_type="ATTRIBUTE",
            object_name=entity_label, member=label,
            message="Declared length or precision differs",
            pd_value=f"len={pd_attr.length or '-'}, prec={pd_attr.precision or '-'}",
            erwin_value=f"len={erwin_attr.length or '-'}, prec={erwin_attr.precision or '-'}",
            remediation="Match the declared size, or clear it on both sides if "
                        "size is not a conceptual concern.",
        )

    if config.CHECK_MANDATORY_ATTRS and pd_attr.mandatory != erwin_attr.mandatory:
        result.emit(
            "MANDATORY", object_type="ATTRIBUTE",
            object_name=entity_label, member=label,
            message="Attribute mandatory flag differs — the existence rule changed",
            pd_value="Mandatory" if pd_attr.mandatory else "Optional",
            erwin_value="Mandatory" if erwin_attr.mandatory else "Optional",
            remediation="Set Null_Option in erwin to match the SAP PD Mandatory flag.",
        )

    if config.CHECK_DOMAINS:
        pd_domain    = (pd_attr.domain or "").strip()
        erwin_domain = (erwin_attr.domain or "").strip()
        if pd_domain or erwin_domain:
            if not normalizers.names_equivalent(pd_domain, erwin_domain):
                result.emit(
                    "DOMAIN", object_type="ATTRIBUTE",
                    object_name=entity_label, member=label,
                    message="Domain assignment differs",
                    pd_value=pd_domain or "(no domain)",
                    erwin_value=erwin_domain or "(no domain)",
                    remediation="Reattach the attribute to the equivalent erwin "
                                "domain so shared semantics stay shared.",
                )

    if config.CHECK_DEFINITIONS:
        _compare_definition(result, "ATTRIBUTE", entity_label, label,
                            pd_attr.definition, erwin_attr.definition)

    if config.CHECK_ATTRIBUTE_ORDER and pd_attr.order != erwin_attr.order:
        result.emit(
            "ATTRIBUTE_ORDER", object_type="ATTRIBUTE",
            object_name=entity_label, member=label,
            message="Attribute declaration order differs",
            pd_value=str(pd_attr.order), erwin_value=str(erwin_attr.order),
            remediation="Reorder attributes in erwin if presentation order is governed.",
        )


def _compare_definition(result: ValidationResult,
                        object_type: str,
                        object_name: str,
                        member: str,
                        pd_text: str,
                        erwin_text: str) -> None:
    """
    Definitions are the payload of a conceptual model.  Losing one is a real
    defect; rewording one is worth an INFO note and nothing more.
    """
    pd_text    = (pd_text or "").strip()
    erwin_text = (erwin_text or "").strip()

    if not pd_text and not erwin_text:
        return

    if config.FLAG_DEFINITION_LOSS and pd_text and not erwin_text:
        result.emit(
            "DEFINITION_LOSS", object_type=object_type,
            object_name=object_name, member=member,
            message="Definition present in SAP PD but absent in erwin",
            pd_value=normalizers.truncate(pd_text),
            erwin_value=MISSING,
            remediation="Copy the business definition into the erwin object so "
                        "the glossary survives the migration.",
        )
        return

    if config.FLAG_DEFINITION_LOSS and erwin_text and not pd_text:
        result.emit(
            "DEFINITION", object_type=object_type,
            object_name=object_name, member=member,
            message="Definition present in erwin but absent in SAP PD",
            pd_value=MISSING,
            erwin_value=normalizers.truncate(erwin_text),
            remediation="Back-port the definition to SAP PD, or accept "
                        "erwin as the new system of record.",
        )
        return

    if not normalizers.definitions_match(pd_text, erwin_text):
        similarity = normalizers.definition_similarity(pd_text, erwin_text)
        result.emit(
            "DEFINITION", object_type=object_type,
            object_name=object_name, member=member,
            message=f"Definition text differs (similarity {similarity:.0%})",
            pd_value=normalizers.truncate(pd_text),
            erwin_value=normalizers.truncate(erwin_text),
            remediation="Reconcile the wording and nominate one system as the "
                        "authoritative glossary.",
        )


def _compare_attributes(result: ValidationResult,
                        entity_label: str,
                        pd_entity: Entity,
                        erwin_entity: Entity) -> int:
    """Compare an entity's attributes; returns the matched count."""
    outcome = match_objects(pd_entity.attributes, erwin_entity.attributes,
                            _named_key_funcs(config.ATTRIBUTE_MATCH_KEY))

    for pd_attr, erwin_attr, basis in outcome.pairs:
        _compare_attribute_pair(result, entity_label, pd_attr, erwin_attr, basis)

    for pd_attr in outcome.unmatched_left:
        result.attributes_missing_in_erwin += 1
        result.emit(
            "ATTRIBUTE_MISSING", object_type="ATTRIBUTE",
            object_name=entity_label, member=pd_attr.code or pd_attr.name,
            message=f"Attribute '{pd_attr.name or pd_attr.code}' exists in SAP PD "
                    f"but NOT in erwin",
            pd_value=normalizers.describe_type(pd_attr.data_type,
                                               pd_attr.length, pd_attr.precision),
            erwin_value=MISSING,
            remediation=f"Add the attribute to erwin entity '{entity_label}'.",
        )

    for erwin_attr in outcome.unmatched_right:
        result.attributes_extra_in_erwin += 1
        if erwin_attr.is_migrated and config.ERWIN_MIGRATED_KEY_HANDLING == "info":
            result.emit(
                "ATTRIBUTE_EXTRA" if config.ERWIN_MIGRATED_KEY_HANDLING == "strict"
                else "FALLBACK_MATCH",
                object_type="ATTRIBUTE",
                object_name=entity_label, member=erwin_attr.code or erwin_attr.name,
                message="erwin role-migrated key attribute — expected, since SAP PD "
                        "expresses the foreign key as a relationship",
                pd_value=MISSING,
                erwin_value=erwin_attr.code or erwin_attr.name,
                remediation="No action required; informational only.",
            )
            continue
        result.emit(
            "ATTRIBUTE_EXTRA", object_type="ATTRIBUTE",
            object_name=entity_label, member=erwin_attr.code or erwin_attr.name,
            message=f"Attribute '{erwin_attr.name or erwin_attr.code}' exists in erwin "
                    f"but NOT in SAP PD",
            pd_value=MISSING,
            erwin_value=normalizers.describe_type(erwin_attr.data_type,
                                                  erwin_attr.length, erwin_attr.precision),
            remediation="Remove it from erwin, or add it to SAP PD if the business "
                        "genuinely needs the fact.",
        )

    result.attributes_matched += len(outcome.pairs)
    return len(outcome.pairs)


# ─── IDENTIFIER COMPARISON ────────────────────────────────────────────────────

def _identifier_members(identifier: Optional[Identifier]) -> List[str]:
    if identifier is None:
        return []
    return [normalizers.normalize_name(code) for code in identifier.attributes]


def _format_members(identifier: Optional[Identifier]) -> str:
    if identifier is None:
        return "(none)"
    if not identifier.attributes:
        return f"{identifier.name or '(unnamed)'} → (no members)"
    return f"{identifier.name or '(unnamed)'} → {', '.join(identifier.attributes)}"


def _migrated_members(entity: Entity, members: Sequence[str]) -> Set[str]:
    """
    Of `members` (already normalised), those the tool migrated in across a
    relationship rather than the modeller declaring on the entity.
    """
    migrated: Set[str] = set()
    for attr in entity.migrated_attributes:
        migrated.add(normalizers.normalize_name(attr.code or attr.name))
        migrated.add(normalizers.normalize_name(attr.name or attr.code))
    for attr in entity.attributes:
        if attr.is_migrated:
            migrated.add(normalizers.normalize_name(attr.code or attr.name))
            migrated.add(normalizers.normalize_name(attr.name or attr.code))
    migrated.discard("")
    return {m for m in members if m in migrated}


def _documented_members(*identifiers: Optional[Identifier]) -> Set[str]:
    """Normalised member names the identifiers' own comments enumerate."""
    documented: Set[str] = set()
    for identifier in identifiers:
        if identifier is None:
            continue
        documented |= {normalizers.normalize_name(token)
                       for token in identifier.documented_members}
    documented.discard("")
    return documented


def _is_documented(member: str, entity: Entity, documented: Set[str]) -> bool:
    """
    True when `documented` names this key member under ANY of its aliases.

    A comment written by a modeller names members by business name; a key group
    lists them by code.  Where the two differ — including where one of them
    carries a typo — matching on the code alone would miss the evidence.
    """
    if member in documented:
        return True
    for alias_group in entity.attribute_aliases:
        tokens = {normalizers.normalize_name(alias) for alias in alias_group}
        if member in tokens and tokens & documented:
            return True
    return False


def _identity_is_inherited(erwin_entity: Entity,
                           identifier: Optional[Identifier]) -> bool:
    """
    True when every member of an erwin identifier is a role-migrated key rather
    than an attribute the modeller declared on the entity.

    This is the signature of *inherited identity*: a subtype identified by its
    supertype's key, or an associative entity identified by the entities it
    joins.  A conceptual model expresses both structurally — through the
    inheritance or the association — and so carries no identifier of its own.
    Reporting that as a lost primary identifier would be wrong: nothing was lost,
    the two tools simply record the same fact in different places.
    """
    if identifier is None or not identifier.attributes:
        return False
    declared = {attr.code.upper() for attr in erwin_entity.attributes
                if not attr.is_migrated}
    return all(code.upper() not in declared for code in identifier.attributes)


def _emit_primary_identifier_difference(result: ValidationResult,
                                        entity_label: str,
                                        erwin_entity: Entity,
                                        pd_primary: Identifier,
                                        erwin_primary: Identifier,
                                        pd_members: Sequence[str],
                                        erwin_members: Sequence[str],
                                        pd_label: str,
                                        erwin_label: str) -> None:
    """
    Classify a primary-identifier composition difference.

    Three outcomes are possible, and telling them apart is the whole point:

    1. erwin's key is a SUPERSET of SAP PD's and every extra member arrived by
       key migration.  Nothing was lost.  erwin migrated the parent key into the
       child because the relationship is identifying there, which is what erwin
       does automatically and what a CDM cannot express: PowerDesigner only
       materialises a foreign key when the relationship is marked Dependent.
       Reporting that as changed identity blames the target tool for a
       difference the source tool created, and the stock remediation ("align
       erwin with SAP PD") would destroy the only complete key in the estate.
       Emitted as IDENTIFYING_KEY_MIGRATION.

    2. The same, AND SAP PD's own identifier comment already enumerates the
       members it never implemented.  The two models then agree on intent and
       differ only in whether that intent was built — a source-model
       completeness defect, not a migration defect.  Emitted as
       IDENTIFIER_UNDERSPECIFIED, which carries the modeller's own words as
       evidence so the variance can be accepted on record rather than waved off.

    3. Anything else — a member erwin dropped, or one a modeller added by hand.
       Entity identity really did change.  Stays PRIMARY_IDENTIFIER / CRITICAL.

    Nothing is suppressed in any branch: every difference still produces exactly
    one finding, with its own category, severity and remediation.
    """
    pd_set    = set(pd_members)
    erwin_set = set(erwin_members)
    lost      = pd_set - erwin_set          # in SAP PD, absent from erwin
    added     = erwin_set - pd_set          # in erwin, absent from SAP PD

    migrated   = _migrated_members(erwin_entity, added)
    documented = _documented_members(pd_primary, erwin_primary)

    is_key_migration = (config.RECOGNISE_ERWIN_KEY_MIGRATION
                        and not lost and added and added <= migrated)
    all_documented = bool(added) and all(
        _is_documented(member, erwin_entity, documented) for member in added)

    if is_key_migration and all_documented:
        result.emit(
            "IDENTIFIER_UNDERSPECIFIED", object_type="IDENTIFIER",
            object_name=entity_label, member=pd_primary.name or "",
            message="SAP PD's identifier comment documents members that were "
                    "never added to the identifier; erwin materialised them by "
                    "key migration — the models agree on intent",
            pd_value=f"{pd_label}  [comment documents: "
                     f"{', '.join(sorted(pd_primary.documented_members))}]",
            erwin_value=erwin_label,
            remediation="erwin holds the complete identity — do NOT strip its PK "
                        "key group. Raise a SAP PD backlog item to mark the "
                        "parent relationship Dependent so the identifier matches "
                        "its own documented composition.",
        )
        return

    if is_key_migration:
        result.emit(
            "IDENTIFYING_KEY_MIGRATION", object_type="IDENTIFIER",
            object_name=entity_label, member=pd_primary.name or "",
            message="erwin extended the primary identifier with parent keys "
                    "migrated across an identifying relationship; SAP PD does "
                    "not express the dependency",
            pd_value=pd_label, erwin_value=erwin_label,
            remediation="Known tool difference — erwin holds the richer identity. "
                        "Confirm with the data owner whether the child is truly "
                        "existence-dependent; if so, mark the SAP PD relationship "
                        "Dependent rather than removing members from erwin.",
        )
        return

    detail = []
    if lost:
        detail.append(f"absent from erwin: {', '.join(sorted(lost))}")
    if added:
        detail.append(f"added in erwin: {', '.join(sorted(added))}")

    result.emit(
        "PRIMARY_IDENTIFIER", object_type="IDENTIFIER", object_name=entity_label,
        member=pd_primary.name or "",
        message="Primary identifier composition differs — entity identity has "
                "changed" + (f" ({'; '.join(detail)})" if detail else ""),
        pd_value=pd_label, erwin_value=erwin_label,
        remediation="Align the PK key-group members in erwin with the SAP PD "
                    "primary identifier."
                    + (" Members present in SAP PD are missing from erwin, so "
                       "this is genuine identity loss." if lost else ""),
    )


def _compare_identifiers(result: ValidationResult,
                         entity_label: str,
                         pd_entity: Entity,
                         erwin_entity: Entity,
                         pd_subtypes: Set[str]) -> Tuple[str, str]:
    """Compare primary and alternate identifiers; returns (pd_pi, erwin_pi) labels."""
    pd_primary    = pd_entity.primary_identifier
    erwin_primary = erwin_entity.primary_identifier

    pd_label    = _format_members(pd_primary)
    erwin_label = _format_members(erwin_primary)

    if config.CHECK_PRIMARY_IDENTIFIERS:
        if pd_primary is None and erwin_primary is None:
            if config.CHECK_MODEL_QUALITY:
                # Absent on BOTH sides: the migration preserved the model
                # faithfully, so this is source-model quality, not fidelity.
                # Emitted as INFO so it cannot dominate the score.
                result.emit(
                    "MODEL_QUALITY", object_type="IDENTIFIER", object_name=entity_label,
                    message="Entity has no primary identifier in either model",
                    pd_value="(none)", erwin_value="(none)",
                    severity="INFO",
                    remediation="Pre-existing in SAP PD, not caused by the "
                                "migration. Define a primary identifier if the "
                                "business requires unique referencing.",
                )
        elif pd_primary is None:
            is_subtype     = normalizers.normalize_name(entity_label) in pd_subtypes
            is_associative = pd_entity.is_associative or erwin_entity.is_associative
            if _identity_is_inherited(erwin_entity, erwin_primary) and \
                    (is_subtype or is_associative):
                origin = "supertype" if is_subtype else "associated entities"
                result.emit(
                    "INHERITED_IDENTITY", object_type="IDENTIFIER", object_name=entity_label,
                    message=f"erwin materialises an identifier from migrated keys; "
                            f"SAP PD inherits identity from its {origin} — "
                            f"equivalent, no action needed",
                    pd_value="(inherited)", erwin_value=erwin_label,
                    remediation="No action required; the two tools record the same "
                                "identity in different places.",
                )
            else:
                result.emit(
                    "PRIMARY_IDENTIFIER", object_type="IDENTIFIER", object_name=entity_label,
                    message="Primary identifier exists in erwin but not in SAP PD",
                    pd_value="(none)", erwin_value=erwin_label,
                    remediation="Add the primary identifier to SAP PD so both models "
                                "agree on entity identity.",
                )
        elif erwin_primary is None:
            result.emit(
                "PRIMARY_IDENTIFIER", object_type="IDENTIFIER", object_name=entity_label,
                message="Primary identifier LOST in migration — present in SAP PD, "
                        "absent in erwin",
                pd_value=pd_label, erwin_value="(none)",
                remediation="Create the corresponding PK key group on the erwin entity.",
            )
        else:
            pd_members    = _identifier_members(pd_primary)
            erwin_members = _identifier_members(erwin_primary)
            if set(pd_members) != set(erwin_members):
                _emit_primary_identifier_difference(
                    result, entity_label, erwin_entity,
                    pd_primary, erwin_primary,
                    pd_members, erwin_members, pd_label, erwin_label,
                )
            elif pd_members != erwin_members:
                result.emit(
                    "ALTERNATE_IDENTIFIER", object_type="IDENTIFIER", object_name=entity_label,
                    message="Primary identifier member order differs",
                    pd_value=pd_label, erwin_value=erwin_label,
                    remediation="Reorder the key-group members if identifier order "
                                "is governed.",
                )
            else:
                result.emit(
                    "IDENTIFIER_VERIFIED", object_type="IDENTIFIER",
                    object_name=entity_label,
                    member=pd_primary.name or "(primary identifier)",
                    message=f"Primary identifier migrated intact — "
                            f"{len(pd_members)} member(s) match in name and order",
                    pd_value=pd_label, erwin_value=erwin_label,
                    remediation="No action required.",
                )

    if config.CHECK_ALTERNATE_IDENTIFIERS:
        pd_alternates    = pd_entity.alternate_identifiers
        erwin_alternates = erwin_entity.alternate_identifiers

        def signature(identifier: Identifier) -> str:
            return "|".join(sorted(_identifier_members(identifier)))

        pd_by_signature    = {signature(i): i for i in pd_alternates}
        erwin_by_signature = {signature(i): i for i in erwin_alternates}

        for sig in sorted(set(pd_by_signature) & set(erwin_by_signature)):
            identifier = pd_by_signature[sig]
            result.emit(
                "IDENTIFIER_VERIFIED", object_type="IDENTIFIER",
                object_name=entity_label, member=identifier.name,
                message=f"Alternate identifier '{identifier.name or sig}' "
                        f"migrated intact",
                pd_value=_format_members(identifier),
                erwin_value=_format_members(erwin_by_signature[sig]),
                remediation="No action required.",
            )

        for sig in set(pd_by_signature) - set(erwin_by_signature):
            identifier = pd_by_signature[sig]
            result.emit(
                "ALTERNATE_IDENTIFIER", object_type="IDENTIFIER", object_name=entity_label,
                member=identifier.name,
                message=f"Alternate identifier '{identifier.name or sig}' missing in erwin",
                pd_value=_format_members(identifier), erwin_value=MISSING,
                remediation="Create the matching AK key group in erwin to preserve "
                            "the uniqueness rule.",
            )

        for sig in set(erwin_by_signature) - set(pd_by_signature):
            identifier = erwin_by_signature[sig]
            result.emit(
                "ALTERNATE_IDENTIFIER", object_type="IDENTIFIER", object_name=entity_label,
                member=identifier.name,
                message=f"Alternate identifier '{identifier.name or sig}' exists only in erwin",
                pd_value=MISSING, erwin_value=_format_members(identifier),
                remediation="Add the uniqueness rule to SAP PD, or remove it from erwin.",
            )

    # ── erwin inversion entries ──────────────────────────────────────────────
    # erwin creates a non-unique index for every foreign key it migrates, named
    # XIF1…, XIF2… and typed IF1, IF2 … in the metamodel export.  They are
    # physical access paths: a conceptual model has no counterpart and needs
    # none, so comparing them against CDM alternate identifiers reports a
    # uniqueness rule that neither tool ever asserted.  They are still listed —
    # every key group in the export is accounted for — but as INFO, and only
    # uniqueness rule that neither tool ever asserted.  They are still reported
    # one-for-one — every key group in the export keeps its own row, so the
    # report remains a complete inventory — but as INFO, with the tool behaviour
    # named in the message instead of a remediation nobody can action.
    if config.REPORT_ERWIN_INVERSION_ENTRIES:
        for entry in erwin_entity.inversion_entries:
            result.emit(
                "ERWIN_INVERSION_ENTRY", object_type="IDENTIFIER",
                object_name=entity_label,
                member=entry.name or entry.code,
                message=f"'{entry.name or entry.code}' is an erwin auto-generated "
                        f"foreign-key index (inversion entry), not a business "
                        f"uniqueness rule",
                pd_value=MISSING,
                erwin_value=_format_members(entry),
                remediation="No action required. erwin creates one of these for "
                            "every migrated foreign key; SAP PD has no equivalent "
                            "object at conceptual level.",
            )

    return pd_label, erwin_label


# ─── ENTITY COMPARISON ────────────────────────────────────────────────────────

def _compare_entities(result: ValidationResult,
                      pd_model: CDMModel,
                      erwin_model: CDMModel) -> Dict[str, str]:
    """
    Match and compare all entities.
    Returns a map of pd_entity_code → erwin_entity_code for relationship rewriting.
    """
    pd_entities    = pd_model.entity_list
    erwin_entities = erwin_model.entity_list

    result.entities_pd    = len(pd_entities)
    result.entities_erwin = len(erwin_entities)
    result.attributes_pd    = pd_model.attribute_count
    result.attributes_erwin = erwin_model.attribute_count
    result.identifiers_pd    = pd_model.identifier_count
    result.identifiers_erwin = erwin_model.identifier_count

    # Subtypes inherit their identity from the supertype and legitimately carry
    # no identifier of their own — needed before identifiers are compared.
    pd_subtypes: Set[str] = {
        normalizers.normalize_name(child)
        for tree in pd_model.inheritances for child in tree.children
    }

    outcome = match_objects(pd_entities, erwin_entities,
                            _named_key_funcs(config.ENTITY_MATCH_KEY))

    entity_alias: Dict[str, str] = {}

    for pd_entity, erwin_entity, basis in outcome.pairs:
        label = _entity_label(pd_entity)
        entity_alias[normalizers.normalize_name(pd_entity.code or pd_entity.name)] = \
            erwin_entity.code or erwin_entity.name

        before = (result.critical_count, result.warning_count, result.info_count)

        if basis != "code" and config.REPORT_FALLBACK_MATCHES:
            result.emit(
                "FALLBACK_MATCH", object_type="ENTITY", object_name=label,
                message=f"Entity matched on {basis}, not on code — possible rename",
                pd_value=f"{pd_entity.name} / {pd_entity.code}",
                erwin_value=f"{erwin_entity.name} / {erwin_entity.code}",
                remediation="Confirm the rename was intentional and record it in "
                            "the migration mapping.",
            )

        if config.CHECK_BUSINESS_NAMES and not normalizers.names_equivalent(
                pd_entity.name, erwin_entity.name):
            result.emit(
                "BUSINESS_NAME", object_type="ENTITY", object_name=label,
                message="Entity business name differs",
                pd_value=pd_entity.name or "(none)",
                erwin_value=erwin_entity.name or "(none)",
                remediation="Align the erwin logical name with the SAP PD business name.",
            )

        if config.CHECK_DEFINITIONS:
            _compare_definition(result, "ENTITY", label, "",
                                pd_entity.definition, erwin_entity.definition)

        # Only meaningful when both models actually organise into subject areas;
        # one side using them and the other not is a tooling choice, not drift.
        compare_subject_areas = bool(config.CHECK_SUBJECT_AREAS
                                     and pd_model.subject_areas
                                     and erwin_model.subject_areas)
        if compare_subject_areas:
            pd_area    = (pd_entity.subject_area or "").strip()
            erwin_area = (erwin_entity.subject_area or "").strip()
            if (pd_area or erwin_area) and not normalizers.names_equivalent(
                    pd_area, erwin_area):
                result.emit(
                    "SUBJECT_AREA", object_type="ENTITY", object_name=label,
                    message="Subject-area / package membership differs",
                    pd_value=pd_area or "(none)", erwin_value=erwin_area or "(none)",
                    remediation="Reassign the entity to the equivalent erwin subject area.",
                )

        matched_attributes = 0
        if config.CHECK_ATTRIBUTES:
            matched_attributes = _compare_attributes(result, label, pd_entity, erwin_entity)

        pd_primary_label, erwin_primary_label = ("", "")
        if config.CHECK_PRIMARY_IDENTIFIERS or config.CHECK_ALTERNATE_IDENTIFIERS:
            pd_primary_label, erwin_primary_label = _compare_identifiers(
                result, label, pd_entity, erwin_entity, pd_subtypes)

        after = (result.critical_count, result.warning_count, result.info_count)

        result.entity_records.append(EntityReconciliation(
            pd_name=pd_entity.name, pd_code=pd_entity.code,
            erwin_name=erwin_entity.name, erwin_code=erwin_entity.code,
            match_basis=basis, status="MATCHED",
            pd_attributes=len(pd_entity.attributes),
            erwin_attributes=len(erwin_entity.attributes),
            attributes_matched=matched_attributes,
            pd_primary_id=pd_primary_label, erwin_primary_id=erwin_primary_label,
            critical=after[0] - before[0],
            warning=after[1] - before[1],
            info=after[2] - before[2],
        ))

    if config.CHECK_ENTITIES:
        for pd_entity in outcome.unmatched_left:
            label = _entity_label(pd_entity)
            result.entities_missing_in_erwin += 1
            result.emit(
                "ENTITY_MISSING", object_type="ENTITY", object_name=label,
                message=f"Entity '{pd_entity.name or label}' exists in SAP PD but "
                        f"NOT in erwin",
                pd_value=f"{len(pd_entity.attributes)} attributes, "
                         f"{len(pd_entity.identifiers)} identifiers",
                erwin_value=MISSING,
                remediation="Create the entity in erwin, or record a documented "
                            "decommission decision.",
            )
            result.entity_records.append(EntityReconciliation(
                pd_name=pd_entity.name, pd_code=pd_entity.code,
                match_basis="UNMATCHED", status="MISSING_IN_ERWIN",
                pd_attributes=len(pd_entity.attributes),
                pd_primary_id=_format_members(pd_entity.primary_identifier),
                critical=1,
            ))

        for erwin_entity in outcome.unmatched_right:
            label = _entity_label(erwin_entity)
            result.entities_extra_in_erwin += 1
            result.emit(
                "ENTITY_EXTRA", object_type="ENTITY", object_name=label,
                message=f"Entity '{erwin_entity.name or label}' exists in erwin but "
                        f"NOT in SAP PD",
                pd_value=MISSING,
                erwin_value=f"{len(erwin_entity.attributes)} attributes, "
                            f"{len(erwin_entity.identifiers)} identifiers",
                remediation="Add the entity to SAP PD if it is a real business "
                            "concept, otherwise remove it from erwin.",
            )
            result.entity_records.append(EntityReconciliation(
                erwin_name=erwin_entity.name, erwin_code=erwin_entity.code,
                match_basis="UNMATCHED", status="EXTRA_IN_ERWIN",
                erwin_attributes=len(erwin_entity.attributes),
                erwin_primary_id=_format_members(erwin_entity.primary_identifier),
                warning=1,
            ))

    result.entities_matched = len(outcome.pairs)
    return entity_alias


# ─── RELATIONSHIP COMPARISON ──────────────────────────────────────────────────

def _translate(rel: Relationship, entity_alias: Dict[str, str]) -> Relationship:
    """
    Rewrite a PD relationship's endpoints into their matched erwin entity names,
    so that a relationship between two *renamed* entities still reconciles.
    """
    def rename(code: str) -> str:
        return entity_alias.get(normalizers.normalize_name(code), code)

    def rebind(end: RelationshipEnd) -> RelationshipEnd:
        return RelationshipEnd(
            entity=rename(end.entity), role=end.role,
            cardinality=end.cardinality, mandatory=end.mandatory,
            dependent=end.dependent,
        )

    return Relationship(
        oid=rel.oid, name=rel.name, code=rel.code,
        definition=rel.definition, kind=rel.kind,
        end1=rebind(rel.end1), end2=rebind(rel.end2),
    )


def _compare_relationship_ends(result: ValidationResult,
                               label: str,
                               pd_rel: Relationship,
                               erwin_rel: Relationship) -> None:
    """
    Compare the two ends of a matched relationship pair, aligning them first so
    that a relationship serialised end-for-end is not reported as a defect.
    """
    # A SAP PD AssociationLink records a role and cardinality only on the
    # entity side; the association side carries nothing.  erwin's associative
    # entity, by contrast, always has an inverse phrase and an identifying
    # dependency.  Treat a silent PD end as "not modelled", not as a difference.
    is_association = "ASSOCIATION" in (pd_rel.kind, erwin_rel.kind)

    aligned = card.ends_aligned(pd_rel, erwin_rel)
    pairs = ([(pd_rel.end1, erwin_rel.end1, "end 1"),
              (pd_rel.end2, erwin_rel.end2, "end 2")] if aligned else
             [(pd_rel.end1, erwin_rel.end2, "end 1"),
              (pd_rel.end2, erwin_rel.end1, "end 2")])

    for pd_end, erwin_end, position in pairs:
        if config.CHECK_OPTIONALITY:
            pd_mandatory    = card.is_mandatory(pd_end.cardinality)
            erwin_mandatory = card.is_mandatory(erwin_end.cardinality)
            if pd_mandatory != erwin_mandatory:
                result.emit(
                    "OPTIONALITY", object_type="RELATIONSHIP", object_name=label,
                    member=f"{position} ({pd_end.entity})",
                    message="Participation changed between mandatory and optional",
                    pd_value="Mandatory" if pd_mandatory else "Optional",
                    erwin_value="Mandatory" if erwin_mandatory else "Optional",
                    remediation="Adjust Nulls_Allowed / relationship type in erwin "
                                "to restore the participation rule.",
                )

        if config.CHECK_DEPENDENCY and pd_end.dependent != erwin_end.dependent \
                and not (is_association and not pd_end.dependent):
            result.emit(
                "DEPENDENCY", object_type="RELATIONSHIP", object_name=label,
                member=f"{position} ({pd_end.entity})",
                message="Existence dependency (identifying relationship) differs",
                pd_value="Dependent" if pd_end.dependent else "Independent",
                erwin_value="Dependent" if erwin_end.dependent else "Independent",
                # Direction matters.  When erwin is the dependent side it has
                # migrated the parent key into the child's identifier, so the
                # richer model is erwin's and flattening it would lose identity;
                # when SAP PD is the dependent side erwin genuinely dropped the
                # dependency.  A single blanket instruction is wrong in one of
                # the two cases, so each gets its own.
                remediation=(
                    "erwin treats the child as existence-dependent and has "
                    "migrated the parent key into its identifier. Confirm with "
                    "the data owner, then mark the SAP PD relationship Dependent "
                    "— do not flatten erwin, which would remove key members."
                    if erwin_end.dependent and not pd_end.dependent else
                    "SAP PD treats the child as existence-dependent but erwin "
                    "does not, so the child no longer inherits the parent key. "
                    "Make the erwin relationship identifying."
                ),
            )

        if config.CHECK_ROLE_NAMES:
            pd_role    = (pd_end.role or "").strip()
            erwin_role = (erwin_end.role or "").strip()
            if (pd_role or erwin_role) and not normalizers.names_equivalent(
                    pd_role, erwin_role) and not (is_association and not pd_role):
                result.emit(
                    "ROLE_NAME", object_type="RELATIONSHIP", object_name=label,
                    member=f"{position} ({pd_end.entity})",
                    message="Role / verb phrase differs",
                    pd_value=pd_role or "(none)", erwin_value=erwin_role or "(none)",
                    remediation="Restore the verb phrase so the relationship still "
                                "reads as a business sentence.",
                )


def _entity_label(entity: "Entity") -> str:
    """
    An entity's Name and Code are usually the same thing spelled two ways, but
    a real CDM occasionally has two genuinely different entities that share
    one Code — e.g. a copy-pasted entity whose Code was never updated, so
    PowerDesigner kept both on the diagram by auto-suffixing the Name instead
    (seen in practice: "EMPLOYEE" and "Employee_2", same Code, different
    Name). Labelling every finding with Code alone then shows two distinct
    entities as identical "EMPLOYEE" rows, which reads as a duplicate-finding
    bug rather than the real data-quality issue it is. Combine both whenever
    they actually differ, so each entity is uniquely identifiable in the
    report; keep the simple single-value label when they agree.

    A SAP-sourced Name is also commonly written as "Business Name(CODE)" —
    the Code already spelled out at the end of the Name. That is not a
    collision to flag; appending "(CODE)" again would just be noise on every
    single entity. Recognise that pattern and leave those alone.
    """
    name, code = entity.name, entity.code
    if not code or not name:
        return code or name
    if normalizers.normalize_name(code) == normalizers.normalize_name(name):
        return code
    if re.search(r"\(\s*" + re.escape(code) + r"\s*\)\s*$", name, re.IGNORECASE):
        return code
    return f"{name} ({code})"


def _relationship_label(rel: "Relationship") -> str:
    """
    A relationship's Name/Code often isn't something a reviewer can locate on
    the diagram — some SAP-sourced exports carry a raw internal object code
    (e.g. "A000910636") in that field, or PowerDesigner's auto-naming has set
    it to a bare verb phrase (e.g. "contains") that many relationships share.
    Always pair whatever Name/Code exists with the two entities it connects,
    so the label alone is enough to find the right relationship on the
    diagram — and fall back to the entity pair alone when Name/Code is blank.
    """
    pair = f"{rel.end1.entity} \u2194 {rel.end2.entity}"
    given = rel.name or rel.code
    return f"{given} ({pair})" if given else pair


def _compare_relationships(result: ValidationResult,
                           pd_model: CDMModel,
                           erwin_model: CDMModel,
                           entity_alias: Dict[str, str]) -> None:
    """
    Reconcile relationships in three passes of decreasing confidence:

        1. by name              — the migration preserved the relationship name
        2. by endpoints + cardinality — same entities, same rule
        3. by endpoints only    — same entities, and the cardinality is the defect

    Only after all three does anything count as missing or extra, which is what
    lets a cardinality change be reported as one CRITICAL change rather than as
    a spurious delete/add pair.
    """
    pd_relationships = [_translate(r, entity_alias) for r in pd_model.relationships]
    erwin_relationships = list(erwin_model.relationships)

    result.relationships_pd    = len(pd_relationships)
    result.relationships_erwin = len(erwin_relationships)

    outcome = match_objects(
        pd_relationships, erwin_relationships,
        [
            ("name",                  card.name_signature),
            ("name+endpoints",        card.name_endpoint_signature),
            ("endpoints+cardinality", card.full_signature),
            ("endpoints",             card.endpoint_signature),
        ],
    )

    for pd_rel, erwin_rel, basis in outcome.pairs:
        label = _relationship_label(pd_rel)
        pd_degree    = card.degree(pd_rel.end1.cardinality, pd_rel.end2.cardinality)
        erwin_degree = card.degree(erwin_rel.end1.cardinality, erwin_rel.end2.cardinality)
        if not card.ends_aligned(pd_rel, erwin_rel):
            erwin_degree = card.invert_degree(erwin_degree)

        status = "MATCHED"

        # Endpoints must actually agree before any end-level check is meaningful.
        pd_endpoints    = card.endpoint_signature(pd_rel)
        erwin_endpoints = card.endpoint_signature(erwin_rel)
        if pd_endpoints != erwin_endpoints:
            status = "ENDPOINTS_CHANGED"
            result.emit(
                "RELATIONSHIP_MISSING", object_type="RELATIONSHIP", object_name=label,
                message="Relationship connects different entities in the two models",
                pd_value=card.describe_relationship(pd_rel),
                erwin_value=card.describe_relationship(erwin_rel),
                remediation="Reconnect the erwin relationship to the entities named "
                            "in SAP PD.",
            )
        else:
            if config.CHECK_CARDINALITY and pd_degree != erwin_degree:
                status = "CARDINALITY_CHANGED"
                result.emit(
                    "CARDINALITY", object_type="RELATIONSHIP", object_name=label,
                    message=f"Cardinality changed from {pd_degree} to {erwin_degree} — "
                            f"the model now asserts a different business rule",
                    pd_value=card.describe_relationship(pd_rel),
                    erwin_value=card.describe_relationship(erwin_rel),
                    remediation="Correct the erwin cardinality; a degree change alters "
                                "what the business is allowed to record.",
                )
            _compare_relationship_ends(result, label, pd_rel, erwin_rel)

        if config.CHECK_ASSOCIATIONS and pd_rel.kind != erwin_rel.kind:
            result.emit(
                "ASSOCIATION", object_type="RELATIONSHIP", object_name=label,
                message="Relationship is modelled as an association on one side only",
                pd_value=pd_rel.kind, erwin_value=erwin_rel.kind,
                remediation="Model the many-to-many consistently — either as an "
                            "association or as an associative entity on both sides.",
            )

        if config.CHECK_DEFINITIONS:
            _compare_definition(result, "RELATIONSHIP", label, "",
                                pd_rel.definition, erwin_rel.definition)

        result.relationship_records.append(RelationshipReconciliation(
            pd_name=pd_rel.name, erwin_name=erwin_rel.name,
            entities=f"{pd_rel.end1.entity} ↔ {pd_rel.end2.entity}",
            pd_signature=card.describe_relationship(pd_rel),
            erwin_signature=card.describe_relationship(erwin_rel),
            pd_degree=pd_degree, erwin_degree=erwin_degree,
            match_basis=basis, status=status,
        ))

    if config.CHECK_RELATIONSHIPS:
        for pd_rel in outcome.unmatched_left:
            label = _relationship_label(pd_rel)
            result.relationships_missing_in_erwin += 1
            result.emit(
                "RELATIONSHIP_MISSING", object_type="RELATIONSHIP", object_name=label,
                message="Relationship exists in SAP PD but NOT in erwin — a business "
                        "rule has been dropped",
                pd_value=card.describe_relationship(pd_rel), erwin_value=MISSING,
                remediation="Recreate the relationship in erwin with the same "
                            "cardinality and participation.",
            )
            result.relationship_records.append(RelationshipReconciliation(
                pd_name=pd_rel.name,
                entities=f"{pd_rel.end1.entity} ↔ {pd_rel.end2.entity}",
                pd_signature=card.describe_relationship(pd_rel),
                pd_degree=card.degree(pd_rel.end1.cardinality, pd_rel.end2.cardinality),
                match_basis="UNMATCHED", status="MISSING_IN_ERWIN",
            ))

        for erwin_rel in outcome.unmatched_right:
            label = _relationship_label(erwin_rel)
            result.relationships_extra_in_erwin += 1
            result.emit(
                "RELATIONSHIP_EXTRA", object_type="RELATIONSHIP", object_name=label,
                message="Relationship exists in erwin but NOT in SAP PD",
                pd_value=MISSING, erwin_value=card.describe_relationship(erwin_rel),
                remediation="Add the relationship to SAP PD if the rule is real, "
                            "otherwise remove it from erwin.",
            )
            result.relationship_records.append(RelationshipReconciliation(
                erwin_name=erwin_rel.name,
                entities=f"{erwin_rel.end1.entity} ↔ {erwin_rel.end2.entity}",
                erwin_signature=card.describe_relationship(erwin_rel),
                erwin_degree=card.degree(erwin_rel.end1.cardinality,
                                         erwin_rel.end2.cardinality),
                match_basis="UNMATCHED", status="EXTRA_IN_ERWIN",
            ))

    result.relationships_matched = len(outcome.pairs)


# ─── INHERITANCE COMPARISON ───────────────────────────────────────────────────

def _compare_inheritances(result: ValidationResult,
                          pd_model: CDMModel,
                          erwin_model: CDMModel,
                          entity_alias: Dict[str, str]) -> None:
    """
    Generalisation hierarchies are the most frequently lost SAP PD construct, because
    erwin and SAP PD disagree about how much of a subtype tree is a single
    object.  Trees are therefore matched on their supertype first, then on name.
    """
    def rename(code: str) -> str:
        return entity_alias.get(normalizers.normalize_name(code), code)

    pd_trees = [Inheritance(
        oid=i.oid, name=i.name, code=i.code,
        parent=rename(i.parent), children=[rename(c) for c in i.children],
        complete=i.complete, mutually_exclusive=i.mutually_exclusive,
        generate_parent=i.generate_parent,
    ) for i in pd_model.inheritances]

    erwin_trees = list(erwin_model.inheritances)

    result.inheritances_pd    = len(pd_trees)
    result.inheritances_erwin = len(erwin_trees)

    outcome = match_objects(
        pd_trees, erwin_trees,
        [
            ("supertype", lambda t: normalizers.normalize_name(t.parent)),
            ("name",      lambda t: normalizers.normalize_name(t.name or t.code)),
        ],
    )

    for pd_tree, erwin_tree, basis in outcome.pairs:
        label = pd_tree.name or f"{pd_tree.parent} hierarchy"

        pd_children    = {normalizers.normalize_name(c) for c in pd_tree.children}
        erwin_children = {normalizers.normalize_name(c) for c in erwin_tree.children}

        missing = pd_children - erwin_children
        extra   = erwin_children - pd_children

        # Census row: matched hierarchies are recorded too, so the report can
        # show every generalisation the way PowerDesigner's List of
        # Inheritances does, not only the broken ones.
        _pd_cons = ("Complete" if pd_tree.complete else "Incomplete") + ", " + \
                   ("Exclusive" if pd_tree.mutually_exclusive else "Overlapping")
        _er_cons = ("Complete" if erwin_tree.complete else "Incomplete") + ", " + \
                   ("Exclusive" if erwin_tree.mutually_exclusive else "Overlapping")
        if missing or extra:
            _inh_status = "STRUCTURE_DIFFERS"
        elif _pd_cons != _er_cons:
            _inh_status = "CONSTRAINT_DIFFERS"
        else:
            _inh_status = "MATCHED"
        result.inheritance_records.append(InheritanceReconciliation(
            name=label, pd_parent=pd_tree.parent, erwin_parent=erwin_tree.parent,
            pd_children=", ".join(sorted(pd_tree.children)),
            erwin_children=", ".join(sorted(erwin_tree.children)),
            pd_count=len(pd_tree.children), erwin_count=len(erwin_tree.children),
            pd_constraints=_pd_cons, erwin_constraints=_er_cons,
            match_basis=basis, status=_inh_status,
        ))

        if _inh_status == "MATCHED":
            result.emit(
                "INHERITANCE_VERIFIED", object_type="INHERITANCE",
                object_name=label,
                member=f"{len(pd_tree.children)} subtype(s)",
                message=f"Generalisation of '{pd_tree.parent}' migrated "
                        f"intact — same subtypes and constraints ({_pd_cons})",
                pd_value=", ".join(sorted(pd_tree.children)),
                erwin_value=", ".join(sorted(erwin_tree.children)),
                remediation="No action required.",
            )

        if missing:
            result.emit(
                "INHERITANCE_STRUCTURE", object_type="INHERITANCE", object_name=label,
                message=f"Subtype(s) missing from the erwin hierarchy of "
                        f"'{pd_tree.parent}'",
                pd_value=", ".join(sorted(pd_tree.children)),
                erwin_value=", ".join(sorted(erwin_tree.children)) or MISSING,
                remediation="Attach the missing subtypes to the erwin subtype "
                            "relationship.",
            )
        if extra:
            result.emit(
                "INHERITANCE_EXTRA", object_type="INHERITANCE", object_name=label,
                message=f"Extra subtype(s) in the erwin hierarchy of '{pd_tree.parent}'",
                pd_value=", ".join(sorted(pd_tree.children)) or MISSING,
                erwin_value=", ".join(sorted(erwin_tree.children)),
                remediation="Remove the extra subtypes from erwin, or add them to "
                            "SAP PD.",
            )

        if pd_tree.complete != erwin_tree.complete:
            result.emit(
                "INHERITANCE_CONSTRAINT", object_type="INHERITANCE", object_name=label,
                message="Completeness constraint differs (is every supertype "
                        "instance also a subtype?)",
                pd_value="Complete" if pd_tree.complete else "Incomplete",
                erwin_value="Complete" if erwin_tree.complete else "Incomplete",
                remediation="Set the erwin subtype relationship to Complete / "
                            "Incomplete to match SAP PD.",
            )

        if pd_tree.mutually_exclusive != erwin_tree.mutually_exclusive:
            result.emit(
                "INHERITANCE_CONSTRAINT", object_type="INHERITANCE", object_name=label,
                message="Exclusivity constraint differs (may an instance be more "
                        "than one subtype?)",
                pd_value="Exclusive" if pd_tree.mutually_exclusive else "Overlapping",
                erwin_value="Exclusive" if erwin_tree.mutually_exclusive else "Overlapping",
                remediation="Set the erwin subtype relationship to Exclusive / "
                            "Inclusive to match SAP PD.",
            )

        if basis != "supertype" and config.REPORT_FALLBACK_MATCHES:
            result.emit(
                "FALLBACK_MATCH", object_type="INHERITANCE", object_name=label,
                message=f"Hierarchy matched on {basis}, not on supertype",
                pd_value=pd_tree.parent, erwin_value=erwin_tree.parent,
                remediation="Verify the supertype was intentionally changed.",
            )

    for pd_tree in outcome.unmatched_left:
        label = pd_tree.name or f"{pd_tree.parent} hierarchy"
        result.inheritance_records.append(InheritanceReconciliation(
            name=label, pd_parent=pd_tree.parent,
            pd_children=", ".join(sorted(pd_tree.children)),
            pd_count=len(pd_tree.children),
            pd_constraints=("Complete" if pd_tree.complete else "Incomplete") + ", " +
                           ("Exclusive" if pd_tree.mutually_exclusive else "Overlapping"),
            match_basis="UNMATCHED", status="MISSING_IN_ERWIN",
        ))
        result.emit(
            "INHERITANCE_MISSING", object_type="INHERITANCE", object_name=label,
            message=f"Generalisation of '{pd_tree.parent}' exists in SAP PD but "
                    f"NOT in erwin",
            pd_value=f"{pd_tree.parent} → {', '.join(pd_tree.children)}",
            erwin_value=MISSING,
            remediation="Recreate the subtype relationship in erwin; a flattened "
                        "hierarchy loses the classification rule.",
        )

    for erwin_tree in outcome.unmatched_right:
        label = erwin_tree.name or f"{erwin_tree.parent} hierarchy"
        result.inheritance_records.append(InheritanceReconciliation(
            name=label, erwin_parent=erwin_tree.parent,
            erwin_children=", ".join(sorted(erwin_tree.children)),
            erwin_count=len(erwin_tree.children),
            erwin_constraints=("Complete" if erwin_tree.complete else "Incomplete") + ", " +
                              ("Exclusive" if erwin_tree.mutually_exclusive else "Overlapping"),
            match_basis="UNMATCHED", status="EXTRA_IN_ERWIN",
        ))
        result.emit(
            "INHERITANCE_EXTRA", object_type="INHERITANCE", object_name=label,
            message=f"Generalisation of '{erwin_tree.parent}' exists in erwin but "
                    f"NOT in SAP PD",
            pd_value=MISSING,
            erwin_value=f"{erwin_tree.parent} → {', '.join(erwin_tree.children)}",
            remediation="Add the hierarchy to SAP PD, or remove it from erwin.",
        )


# ─── DATA ITEM CENSUS ─────────────────────────────────────────────────────────

def _compare_data_items(result: ValidationResult,
                        pd_model: CDMModel,
                        erwin_model: CDMModel,
                        entity_alias: Dict[str, str]) -> None:
    """
    Account for every PowerDesigner Data Item — the CDM's reusable conceptual
    fact, PD's own "List of Data Items".

    erwin has no data-item object: each one crosses the migration through the
    entity attribute(s) that borrow its name and type.  So the census walks the
    usages.  A data item whose every borrowing attribute arrived in erwin with
    a matching type is VERIFIED (context only, never scored); an unused item or
    one with a usage gap is reported with the gap named.  Exactly one finding
    per data item, under object type DATA_ITEM, so the FINDINGS sheet accounts
    for PD's list one-for-one.
    """
    data_items = getattr(pd_model, "data_items", None) or {}
    if not data_items:
        return

    for item in data_items.values():
        item_label = item.get("name") or item.get("code") or "(unnamed)"
        pd_type = normalizers.describe_type(item.get("data_type", ""),
                                            item.get("length", ""),
                                            item.get("precision", ""))
        used, gaps, erwin_types = 0, [], []
        for pd_entity in pd_model.entity_list:
            for attr in pd_entity.attributes:
                if (attr.data_item or "").strip().upper() != \
                        item_label.strip().upper():
                    continue
                used += 1
                usage = f"{pd_entity.code or pd_entity.name}." \
                        f"{attr.code or attr.name}"
                erwin_code = (entity_alias.get(normalizers.normalize_name(
                    pd_entity.code or pd_entity.name)) or "").strip().upper()
                erwin_entity = erwin_model.entities.get(erwin_code)
                erwin_attr = None
                if erwin_entity is not None:
                    wanted = {normalizers.normalize_name(attr.code or attr.name),
                              normalizers.normalize_name(attr.name or attr.code)}
                    for candidate in erwin_entity.attributes:
                        if normalizers.normalize_name(
                                candidate.code or candidate.name) in wanted or \
                           normalizers.normalize_name(
                                candidate.name or candidate.code) in wanted:
                            erwin_attr = candidate
                            break
                if erwin_attr is None:
                    gaps.append(f"{usage} not found in erwin")
                    continue
                erwin_type = normalizers.describe_type(
                    erwin_attr.data_type, erwin_attr.length, erwin_attr.precision)
                if erwin_type:
                    erwin_types.append(erwin_type)
                pd_raw = item.get("data_type", "") or attr.data_type
                if pd_raw and erwin_attr.data_type and \
                        not normalizers.types_match(pd_raw, erwin_attr.data_type):
                    gaps.append(f"{usage} type differs "
                                f"({pd_type or pd_raw} → {erwin_type or '(untyped)'})")

        erwin_value = ", ".join(sorted(set(erwin_types))) \
            or "(realised through attributes)"
        if used == 0:
            result.emit(
                "DATA_ITEM", object_type="DATA_ITEM", object_name=item_label,
                message="Data item is defined in SAP PD but not used by any "
                        "attribute — nothing carries it into erwin",
                pd_value=pd_type or "(untyped)",
                erwin_value="(not migrated — unused)",
                remediation="Pre-existing in SAP PD. Attach the data item to an "
                            "attribute or retire it; an unused data item does "
                            "not migrate because erwin has no data-item object.",
            )
        elif gaps:
            result.emit(
                "DATA_ITEM", object_type="DATA_ITEM", object_name=item_label,
                member=f"used by {used} attribute(s)",
                message="Data item did not fully carry over: " + "; ".join(gaps),
                pd_value=pd_type or "(untyped)", erwin_value=erwin_value,
                remediation="Fix the attribute-level differences listed — the "
                            "data item itself has no erwin counterpart to edit.",
            )
        else:
            result.emit(
                "DATA_ITEM_VERIFIED", object_type="DATA_ITEM",
                object_name=item_label, member=f"used by {used} attribute(s)",
                message=f"Data item fully migrated — all {used} attribute(s) "
                        f"using it are present in erwin with a matching type",
                pd_value=pd_type or "(untyped)", erwin_value=erwin_value,
                remediation="No action required.",
            )


# ─── MODEL-LEVEL COMPARISONS ──────────────────────────────────────────────────

def _compare_domains(result: ValidationResult,
                     pd_model: CDMModel,
                     erwin_model: CDMModel) -> None:
    outcome = match_objects(
        list(pd_model.domains.values()), list(erwin_model.domains.values()),
        [("code", lambda d: normalizers.compare_key(d.code)),
         ("name", lambda d: normalizers.compare_key(d.name)),
         ("normalized name", lambda d: normalizers.normalize_name(d.name or d.code))],
    )

    for pd_domain, erwin_domain, _basis in outcome.pairs:
        _types_ok = normalizers.types_match(pd_domain.data_type, erwin_domain.data_type)
        result.domain_records.append(DomainReconciliation(
            pd_name=pd_domain.name, pd_code=pd_domain.code,
            pd_type=normalizers.describe_type(pd_domain.data_type,
                                              pd_domain.length, pd_domain.precision),
            erwin_name=erwin_domain.name or erwin_domain.code,
            erwin_type=normalizers.describe_type(erwin_domain.data_type,
                                                 erwin_domain.length,
                                                 erwin_domain.precision),
            match_basis=_basis,
            status="MATCHED" if _types_ok else "TYPE_DIFFERS",
        ))
        if _types_ok:
            result.emit(
                "DOMAIN_VERIFIED", object_type="DOMAIN",
                object_name=pd_domain.name or pd_domain.code,
                message=f"Domain migrated intact — matched in erwin "
                        f"(by {_basis})",
                pd_value=normalizers.describe_type(
                    pd_domain.data_type, pd_domain.length,
                    pd_domain.precision) or "(untyped)",
                erwin_value=normalizers.describe_type(
                    erwin_domain.data_type, erwin_domain.length,
                    erwin_domain.precision) or "(no type in erwin export)",
                remediation="No action required.",
            )
        if not _types_ok:
            result.emit(
                "DOMAIN", object_type="DOMAIN",
                object_name=pd_domain.name or pd_domain.code,
                message="Domain conceptual type differs",
                pd_value=normalizers.describe_type(pd_domain.data_type,
                                                   pd_domain.length, pd_domain.precision),
                erwin_value=normalizers.describe_type(erwin_domain.data_type,
                                                      erwin_domain.length,
                                                      erwin_domain.precision),
                remediation="Align the erwin domain definition; every attribute "
                            "using it inherits the discrepancy.",
            )
        if config.CHECK_DEFINITIONS:
            _compare_definition(result, "DOMAIN",
                                pd_domain.name or pd_domain.code, "",
                                pd_domain.definition, erwin_domain.definition)

    for pd_domain in outcome.unmatched_left:
        result.domain_records.append(DomainReconciliation(
            pd_name=pd_domain.name, pd_code=pd_domain.code,
            pd_type=normalizers.describe_type(pd_domain.data_type,
                                              pd_domain.length, pd_domain.precision),
            match_basis="UNMATCHED", status="MISSING_IN_ERWIN",
        ))
        result.emit(
            "DOMAIN", object_type="DOMAIN", object_name=pd_domain.name or pd_domain.code,
            message="Domain exists in SAP PD but NOT in erwin",
            pd_value=normalizers.describe_type(pd_domain.data_type,
                                               pd_domain.length, pd_domain.precision),
            erwin_value=MISSING,
            remediation="Create the domain in erwin so shared semantics remain "
                        "centrally governed.",
        )

    for erwin_domain in outcome.unmatched_right:
        result.domain_records.append(DomainReconciliation(
            erwin_name=erwin_domain.name or erwin_domain.code,
            erwin_type=normalizers.describe_type(erwin_domain.data_type,
                                                 erwin_domain.length,
                                                 erwin_domain.precision),
            match_basis="UNMATCHED", status="EXTRA_IN_ERWIN",
        ))
        result.emit(
            "DOMAIN", object_type="DOMAIN",
            object_name=erwin_domain.name or erwin_domain.code,
            message="Domain exists in erwin but NOT in SAP PD",
            pd_value=MISSING,
            erwin_value=normalizers.describe_type(erwin_domain.data_type,
                                                  erwin_domain.length,
                                                  erwin_domain.precision),
            remediation="Register the domain in SAP PD, or retire it in erwin.",
        )


def _compare_business_rules(result: ValidationResult,
                            pd_model: CDMModel,
                            erwin_model: CDMModel) -> None:
    outcome = match_objects(
        pd_model.business_rules, erwin_model.business_rules,
        [("name",            lambda r: normalizers.compare_key(r.name)),
         ("normalized name", lambda r: normalizers.normalize_name(r.name or r.code))],
    )

    for pd_rule, erwin_rule, _basis in outcome.pairs:
        pd_expression    = normalizers.normalize_definition(pd_rule.expression)
        erwin_expression = normalizers.normalize_definition(erwin_rule.expression)
        if pd_expression and erwin_expression and pd_expression != erwin_expression:
            result.emit(
                "BUSINESS_RULE", object_type="BUSINESS_RULE", object_name=pd_rule.name,
                message="Business rule expression differs",
                pd_value=normalizers.truncate(pd_rule.expression),
                erwin_value=normalizers.truncate(erwin_rule.expression),
                remediation="Reconcile the rule expression; a constraint that "
                            "changed silently is a governance breach.",
            )

    for pd_rule in outcome.unmatched_left:
        result.emit(
            "BUSINESS_RULE", object_type="BUSINESS_RULE",
            object_name=pd_rule.name or pd_rule.code,
            message="Business rule exists in SAP PD but NOT in erwin",
            pd_value=normalizers.truncate(pd_rule.expression or pd_rule.definition),
            erwin_value=MISSING,
            remediation="Recreate the rule in erwin as a validation rule or "
                        "documented constraint.",
        )

    for erwin_rule in outcome.unmatched_right:
        result.emit(
            "BUSINESS_RULE", object_type="BUSINESS_RULE",
            object_name=erwin_rule.name or erwin_rule.code,
            message="Business rule exists in erwin but NOT in SAP PD",
            pd_value=MISSING,
            erwin_value=normalizers.truncate(erwin_rule.expression or erwin_rule.definition),
            remediation="Add the rule to SAP PD, or remove it from erwin.",
        )


def _check_model_quality(result: ValidationResult,
                         pd_model: CDMModel,
                         erwin_model: CDMModel) -> None:
    """
    Quality rules that apply to the migrated model in its own right, independent
    of the comparison.  A structurally valid but semantically hollow model can
    reconcile perfectly and still be unusable.
    """
    connected: set = set()
    for rel in erwin_model.relationships:
        connected.add(normalizers.normalize_name(rel.end1.entity))
        connected.add(normalizers.normalize_name(rel.end2.entity))
    for tree in erwin_model.inheritances:
        connected.add(normalizers.normalize_name(tree.parent))
        for child in tree.children:
            connected.add(normalizers.normalize_name(child))

    for entity in erwin_model.entity_list:
        label = _entity_label(entity)

        # Connectivity is keyed by the plain code/name that relationship ends
        # actually store (see the `connected` set above) — not by the
        # disambiguated display label, which can include extra text (e.g. a
        # "Customer(KNA1)" business name plus its own "(KNA1)" code suffix)
        # that would never match and would wrongly flag every entity as an
        # orphan.
        if normalizers.normalize_name(entity.code or entity.name) not in connected:
            result.emit(
                "MODEL_QUALITY", object_type="ENTITY", object_name=label,
                message="Entity participates in no relationship or hierarchy in erwin "
                        "— possible orphan from the migration",
                pd_value="", erwin_value="0 relationships",
                remediation="Reconnect the entity, or confirm it is genuinely "
                            "standalone reference data.",
            )

        # Only a migration LOSS is worth reporting: SAP PD had attributes and
        # erwin does not.  When neither side has any, the emptiness is a property
        # of the source model, not something the migration did — emitting it per
        # entity produced 190 warnings that buried the real findings and drove
        # the fidelity score to zero.
        if not entity.attributes:
            pd_counterpart = pd_model.entities.get((entity.code or "").upper())
            if pd_counterpart is not None and pd_counterpart.attributes:
                result.emit(
                    "MODEL_QUALITY", object_type="ENTITY", object_name=label,
                    message="Entity lost all attributes in the migration",
                    pd_value=f"{len(pd_counterpart.attributes)} attributes",
                    erwin_value="0 attributes",
                    remediation="Re-migrate the entity's attributes; they are "
                                "present in SAP PD but absent in erwin.",
                )

    if pd_model.model_name and erwin_model.model_name and \
            not normalizers.names_equivalent(pd_model.model_name, erwin_model.model_name):
        result.emit(
            "MODEL_METADATA", object_type="MODEL", object_name="(model)",
            message="Model names differ between the two tools",
            pd_value=pd_model.model_name, erwin_value=erwin_model.model_name,
            remediation="Align the model names so the pair is traceable in the "
                        "migration inventory.",
        )



# ─── SHORTCUT CENSUS ──────────────────────────────────────────────────────────

def _compare_shortcuts(result: ValidationResult, pd_model) -> None:
    """
    Account for PD's "List of Shortcuts", one FINDINGS row per shortcut under
    object type SHORTCUT.  A shortcut is a reference to an object OWNED BY
    ANOTHER MODEL (a glossary term or category, an entity of a shared model),
    and erwin's XML export has no shortcut object — so nothing is expected to
    migrate, and the rows are context (VERIFIED severity, never scored).
    """
    for shortcut in getattr(pd_model, "shortcuts", None) or []:
        label  = shortcut.get("name") or shortcut.get("code") or "(unnamed)"
        kind   = shortcut.get("type") or "Object"
        target = shortcut.get("target_model") or "another model"
        where  = shortcut.get("target_package") or ""
        result.emit(
            "SHORTCUT", object_type="SHORTCUT", object_name=label,
            member=kind,
            message=f"Shortcut to {kind} '{label}' owned by model '{target}' — "
                    f"an external SAP PD reference; erwin's export has no "
                    f"shortcut object, so nothing is expected to migrate",
            pd_value=f"{kind} in '{target}'" + (f" ({where})" if where else ""),
            erwin_value="(not applicable — erwin has no shortcut object)",
            remediation="No action required. The referenced object lives in "
                        "its own model; migrating that model carries it across.",
        )


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def compare(pd_model: CDMModel, erwin_model: CDMModel) -> ValidationResult:
    """
    Reconcile a SAP PD CDM against an erwin logical model.

    Always returns a ValidationResult; parse failures and unexpected exceptions
    are reported as findings rather than raised, so one bad model cannot abort a
    batch of 1500.
    """
    result = ValidationResult(
        pd_file     = pd_model.source_file,
        erwin_file  = erwin_model.source_file,
        pd_model    = pd_model.model_name,
        erwin_model = erwin_model.model_name,
    )

    # ── Parse-failure guards ─────────────────────────────────────────────────
    if pd_model.parse_error:
        result.status = "ERROR"
        result.add(Finding("PARSE_ERROR", "CRITICAL", object_type="MODEL",
                           message=f"SAP PD CDM parse error: {pd_model.parse_error}",
                           remediation="Re-export the .cdm file from SAP PD "
                                       "and confirm it is well-formed XML."))
        result.compute_score()
        return result

    if erwin_model.parse_error:
        result.status = "ERROR"
        result.add(Finding("PARSE_ERROR", "CRITICAL", object_type="MODEL",
                           message=f"erwin parse error: {erwin_model.parse_error}",
                           remediation="Re-export the model from erwin as XML."))
        result.compute_score()
        return result

    # An empty model on either side is a *validation* failure, not a comparison
    # result.  Reporting every entity as individually missing would bury the real
    # problem — usually the wrong file, a physical export, or a truncated save —
    # under hundreds of identical findings.
    if not pd_model.entities or not erwin_model.entities:
        result.status = "ERROR"
        if not pd_model.entities and not erwin_model.entities:
            message = ("Neither file yielded any entities — the exports are "
                       "probably not conceptual or logical models")
        elif not erwin_model.entities:
            message = (f"The erwin export contains no entities while SAP PD "
                       f"contains {len(pd_model.entities)} — nothing could be "
                       f"reconciled")
        else:
            message = (f"The SAP PD CDM contains no entities while erwin "
                       f"contains {len(erwin_model.entities)} — nothing could be "
                       f"reconciled")
        result.entities_pd    = len(pd_model.entities)
        result.entities_erwin = len(erwin_model.entities)
        result.add(Finding("PARSE_ERROR", "CRITICAL", object_type="MODEL",
                           message=message,
                           pd_value=f"{len(pd_model.entities)} entities",
                           erwin_value=f"{len(erwin_model.entities)} entities",
                           remediation="Confirm the file is a conceptual or logical "
                                       "model export for the intended subject area, "
                                       "and that the export completed."))
        result.compute_score()
        return result

    for warning in list(pd_model.parse_warnings) + list(erwin_model.parse_warnings):
        result.emit("PARSE_WARNING", object_type="MODEL", object_name="(model)",
                    message=warning,
                    remediation="Review the export; the parser fell back to a "
                                "tolerant strategy.")

    # ── Reconciliation ───────────────────────────────────────────────────────
    entity_alias = _compare_entities(result, pd_model, erwin_model)

    # Object counts for the SUMMARY sheet — census, not comparison.
    result.domains_pd    = len(pd_model.domains)
    result.domains_erwin = len(erwin_model.domains)
    result.data_items_pd = len(getattr(pd_model, "data_items", None) or {})
    result.shortcuts_pd  = len(getattr(pd_model, "shortcuts", None) or [])

    if config.CHECK_RELATIONSHIPS or config.CHECK_CARDINALITY:
        _compare_relationships(result, pd_model, erwin_model, entity_alias)

    if config.CHECK_INHERITANCE:
        _compare_inheritances(result, pd_model, erwin_model, entity_alias)

    if config.CHECK_DOMAINS:
        _compare_domains(result, pd_model, erwin_model)

    if getattr(config, "CHECK_DATA_ITEMS", True):
        _compare_data_items(result, pd_model, erwin_model, entity_alias)

    if getattr(config, "CHECK_SHORTCUTS", True):
        _compare_shortcuts(result, pd_model)

    if config.CHECK_BUSINESS_RULES:
        _compare_business_rules(result, pd_model, erwin_model)

    if config.CHECK_MODEL_QUALITY:
        _check_model_quality(result, pd_model, erwin_model)

    # ── Documentation mapping (report-only) ──────────────────────────────────
    # Built after the reconciliation above so it cannot influence any finding.
    # Failure here must never invalidate an otherwise good validation result.
    try:
        result.documentation_rows = documentation.build_rows(
            pd_model, erwin_model, entity_alias)
    except Exception as exc:                                   # pragma: no cover
        logger.warning("Documentation mapping unavailable for %s: %s",
                       result.pd_file, exc)
        result.documentation_rows = []

    result.finalise()
    return result
