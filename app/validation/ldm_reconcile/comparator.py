import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from app.validation import udp_fidelity          # UDP integration
from . import cardinality as card
from app.config.validation_config import LDM_CONFIG as config
from . import documentation
from . import normalizers
from .ldm_model import (Attribute, LDMModel, Entity, Identifier, Inheritance,
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

# Literals that recur across many findings/match-passes. Centralised here so
# SonarQube's duplicated-literal check passes and so a wording change only
# needs to happen in one place.
NONE_VALUE = "(none)"
NORMALIZED_NAME_BASIS = "normalized name"
NO_ACTION_REQUIRED = "No action required."


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

    # Comment->Note / Definition->Definition mapping rows for the report's
    # DOCUMENTATION sheet. Report-only: nothing here feeds the findings list,
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
    # erwin role-migrated FK attributes that have no PD attribute counterpart
    # because SAP PD expresses that same foreign key as a RELATIONSHIP instead.
    # Tracked separately so they are not double-counted as attribute
    # differences when the relationship itself already reconciles cleanly.
    attributes_via_relationship: int = 0

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
    shortcuts_pd:  int = 0
    # erwin has no shortcut object, so this is 0 for every erwin export —
    # reported as a pair with the SAP PD count so the SUMMARY shows the gap
    # explicitly instead of leaving the erwin side unstated.
    shortcuts_erwin: int = 0

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


def _index_by_key(items: Sequence[Any],
                  key_func: Callable[[Any], str]) -> Dict[str, List[Any]]:
    """Group items by their (truthy) key."""
    index: Dict[str, List[Any]] = {}
    for item in items:
        key = key_func(item)
        if key:
            index.setdefault(key, []).append(item)
    return index


def _match_one_pass(remaining_left: List[Any], remaining_right: List[Any],
                    key_func: Callable[[Any], str], basis: str,
                    outcome: MatchOutcome) -> Tuple[List[Any], List[Any]]:
    """
    Run a single matching pass and return the still-unmatched left/right lists.

    Pairs are made only when the key is *unique on both sides*; ambiguous keys
    are left for a later pass rather than guessed at.
    """
    left_index  = _index_by_key(remaining_left, key_func)
    right_index = _index_by_key(remaining_right, key_func)

    matched_left, matched_right = [], []
    for key, left_items in left_index.items():
        right_items = right_index.get(key)
        if right_items and len(left_items) == 1 and len(right_items) == 1:
            outcome.pairs.append((left_items[0], right_items[0], basis))
            matched_left.append(left_items[0])
            matched_right.append(right_items[0])

    new_left  = [i for i in remaining_left  if i not in matched_left]
    new_right = [i for i in remaining_right if i not in matched_right]
    return new_left, new_right


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
        remaining_left, remaining_right = _match_one_pass(
            remaining_left, remaining_right, key_func, basis, outcome)

    outcome.unmatched_left  = remaining_left
    outcome.unmatched_right = remaining_right
    return outcome


def _named_key_funcs(mode: str) -> List[KeyFunc]:
    """Key-function ladder for entities and attributes, driven by config."""
    by_code = ("code", lambda o: normalizers.compare_key(o.code))
    by_name = ("name", lambda o: normalizers.compare_key(o.name))
    by_norm_name = (NORMALIZED_NAME_BASIS, lambda o: normalizers.normalize_name(o.name))
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

def _check_attribute_fallback_match(result: ValidationResult, entity_label: str,
                                    label: str, pd_attr: Attribute,
                                    erwin_attr: Attribute, match_basis: str) -> None:
    if match_basis == "code" or not config.REPORT_FALLBACK_MATCHES:
        return
    result.emit(
        "FALLBACK_MATCH", object_type="ATTRIBUTE",
        object_name=entity_label, member=label,
        message=f"Attribute matched on {match_basis}, not on code — possible rename",
        pd_value=f"{pd_attr.name} / {pd_attr.code}",
        erwin_value=f"{erwin_attr.name} / {erwin_attr.code}",
        remediation="Confirm the rename was intentional and update the "
                    "business glossary, or restore the original code.",
    )


def _check_attribute_business_name(result: ValidationResult, entity_label: str,
                                   label: str, pd_attr: Attribute,
                                   erwin_attr: Attribute) -> None:
    if not config.CHECK_BUSINESS_NAMES:
        return
    if normalizers.names_equivalent(pd_attr.name, erwin_attr.name):
        return
    result.emit(
        "BUSINESS_NAME", object_type="ATTRIBUTE",
        object_name=entity_label, member=label,
        message="Attribute business name differs",
        pd_value=pd_attr.name or NONE_VALUE,
        erwin_value=erwin_attr.name or NONE_VALUE,
        remediation="Align the logical name in erwin with the SAP PD business name.",
    )


def _check_attribute_data_type(result: ValidationResult, entity_label: str,
                               label: str, pd_attr: Attribute,
                               erwin_attr: Attribute) -> None:
    if not config.CHECK_DATA_TYPES:
        return
    if normalizers.types_match(pd_attr.data_type, erwin_attr.data_type):
        return
    # Report the basis that ACTUALLY decided this (approved matrix vs.
    # the TYPE_COMPARISON_MODE fallback) rather than always naming the
    # fallback mode, since the two can now disagree on which one fired.
    matrix_key = normalizers.pd_type_in_approved_matrix(pd_attr.data_type)
    basis = ("approved PD→erwin type matrix" if matrix_key
             else f"'{config.TYPE_COMPARISON_MODE}' strictness")
    result.emit(
        "DATA_TYPE", object_type="ATTRIBUTE",
        object_name=entity_label, member=label,
        message=f"Logical data type differs (compared at {basis})",
        pd_value=normalizers.describe_type(pd_attr.data_type,
                                           pd_attr.length, pd_attr.precision),
        erwin_value=normalizers.describe_type(erwin_attr.data_type,
                                              erwin_attr.length, erwin_attr.precision),
        remediation="Reassign the erwin logical datatype or domain so both "
                    "sides express the same logical type family.",
    )


def _check_attribute_length_precision(result: ValidationResult, entity_label: str,
                                      label: str, pd_attr: Attribute,
                                      erwin_attr: Attribute) -> None:
    # LENGTH_PRECISION is only meaningful when the base TYPES already agree --
    # e.g. DECIMAL(10,2) vs DECIMAL(12,4), where the type is right and only the
    # declared size drifted. When the types themselves differ (VARCHAR(2) vs
    # CHAR(18)), the DATA_TYPE finding above already reports that exact
    # discrepancy including both sizes, so raising LENGTH_PRECISION as well
    # reported one difference twice -- which is what produced the apparent
    # duplicate rows for CSTMR_NM, CTY_NM, VT_CD and others.
    if not config.CHECK_LENGTH_PRECISION:
        return
    if not normalizers.types_match(pd_attr.data_type, erwin_attr.data_type):
        return
    if normalizers.dimensions_match(
            pd_attr.data_type, pd_attr.length, pd_attr.precision,
            erwin_attr.data_type, erwin_attr.length, erwin_attr.precision):
        return
    # Show the EFFECTIVE dimensions. PowerDesigner sometimes stores the
    # size in separate Length/Precision fields and sometimes glues it into
    # the type string ("varchar2", "VBIN64000"); erwin almost always glues
    # it ("CHAR(18)"). Reading only the separate fields printed
    # "len=-, prec=-" on both sides for a real 2-vs-18 difference, giving
    # the reviewer no usable evidence.
    _, pd_glued_len, pd_glued_prec = normalizers.split_type(pd_attr.data_type)
    _, er_glued_len, er_glued_prec = normalizers.split_type(erwin_attr.data_type)
    pd_len  = (pd_attr.length    or pd_glued_len).strip()  or "-"
    pd_prec = (pd_attr.precision or pd_glued_prec).strip() or "-"
    er_len  = (erwin_attr.length    or er_glued_len).strip()  or "-"
    er_prec = (erwin_attr.precision or er_glued_prec).strip() or "-"
    result.emit(
        "LENGTH_PRECISION", object_type="ATTRIBUTE",
        object_name=entity_label, member=label,
        message="Declared length or precision differs",
        pd_value=f"len={pd_len}, prec={pd_prec}",
        erwin_value=f"len={er_len}, prec={er_prec}",
        remediation="Match the declared size, or clear it on both sides if "
                    "size is not a logical-modelling concern.",
    )


def _check_attribute_mandatory(result: ValidationResult, entity_label: str,
                               label: str, pd_attr: Attribute,
                               erwin_attr: Attribute) -> None:
    if not config.CHECK_MANDATORY_ATTRS or pd_attr.mandatory == erwin_attr.mandatory:
        return
    result.emit(
        "MANDATORY", object_type="ATTRIBUTE",
        object_name=entity_label, member=label,
        message="Attribute mandatory flag differs — the existence rule changed",
        pd_value="Mandatory" if pd_attr.mandatory else "Optional",
        erwin_value="Mandatory" if erwin_attr.mandatory else "Optional",
        remediation="Set Null_Option in erwin to match the SAP PD Mandatory flag.",
    )


def _check_attribute_domain(result: ValidationResult, entity_label: str,
                            label: str, pd_attr: Attribute,
                            erwin_attr: Attribute) -> None:
    if not config.CHECK_DOMAINS:
        return
    pd_domain    = (pd_attr.domain or "").strip()
    erwin_domain = (erwin_attr.domain or "").strip()
    if not (pd_domain or erwin_domain):
        return
    if normalizers.names_equivalent(pd_domain, erwin_domain):
        return
    result.emit(
        "DOMAIN", object_type="ATTRIBUTE",
        object_name=entity_label, member=label,
        message="Domain assignment differs",
        pd_value=pd_domain or "(no domain)",
        erwin_value=erwin_domain or "(no domain)",
        remediation="Reattach the attribute to the equivalent erwin "
                    "domain so shared semantics stay shared.",
    )


def _check_attribute_order(result: ValidationResult, entity_label: str,
                           label: str, pd_attr: Attribute,
                           erwin_attr: Attribute) -> None:
    if not config.CHECK_ATTRIBUTE_ORDER or pd_attr.order == erwin_attr.order:
        return
    result.emit(
        "ATTRIBUTE_ORDER", object_type="ATTRIBUTE",
        object_name=entity_label, member=label,
        message="Attribute declaration order differs",
        pd_value=str(pd_attr.order), erwin_value=str(erwin_attr.order),
        remediation="Reorder attributes in erwin if presentation order is governed.",
    )


def _compare_attribute_pair(result: ValidationResult,
                            entity_label: str,
                            pd_attr: Attribute,
                            erwin_attr: Attribute,
                            match_basis: str) -> None:
    """Compare every logical property of one matched attribute pair.

    Each property is checked by its own single-purpose helper (guard clause +
    at most one finding) so this function is a flat, low-complexity dispatch
    list rather than a wall of nested conditionals.
    """
    label = pd_attr.code or pd_attr.name

    _check_attribute_fallback_match(result, entity_label, label, pd_attr, erwin_attr, match_basis)
    _check_attribute_business_name(result, entity_label, label, pd_attr, erwin_attr)
    _check_attribute_data_type(result, entity_label, label, pd_attr, erwin_attr)
    _check_attribute_length_precision(result, entity_label, label, pd_attr, erwin_attr)
    _check_attribute_mandatory(result, entity_label, label, pd_attr, erwin_attr)
    _check_attribute_domain(result, entity_label, label, pd_attr, erwin_attr)

    if config.CHECK_DEFINITIONS:
        _compare_definition(result, "ATTRIBUTE", entity_label, label,
                            pd_attr.definition, erwin_attr.definition)

    _check_attribute_order(result, entity_label, label, pd_attr, erwin_attr)


def _compare_definition(result: ValidationResult,
                        object_type: str,
                        object_name: str,
                        member: str,
                        pd_text: str,
                        erwin_text: str) -> None:
    """
    Definitions are the payload of a logical model.  Losing one is a real
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


def _emit_missing_attribute(result: ValidationResult, entity_label: str,
                            pd_attr: Attribute) -> None:
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


def _is_relationship_migrated_key(erwin_attr: Attribute) -> bool:
    """
    True when an erwin role-migrated key has no PD attribute counterpart
    because SAP PD expresses that same foreign key as a RELATIONSHIP instead.
    """
    return bool(erwin_attr.is_migrated
                and config.ERWIN_MIGRATED_KEY_HANDLING in ("info", "relationship"))


def _emit_extra_or_relationship_attribute(result: ValidationResult, entity_label: str,
                                          erwin_attr: Attribute) -> None:
    # An erwin role-migrated key with no PD attribute counterpart is NOT an
    # extra attribute: SAP PD expresses that same foreign key as a
    # RELATIONSHIP, and erwin materialises it as an attribute. Counting it
    # here as well would double-count one modelling fact that the
    # relationship comparison already reconciles, which is exactly what
    # made a clean model report 91 erwin attributes against 90 in PD.
    #
    # NOTE the ordering: attributes_extra_in_erwin must only be
    # incremented for genuinely extra attributes. The previous version
    # incremented it BEFORE this check, so even the "info" path inflated
    # the extra count and the report's erwin attribute total.
    if _is_relationship_migrated_key(erwin_attr):
        result.attributes_via_relationship += 1
        result.emit(
            "FALLBACK_MATCH",
            object_type="ATTRIBUTE",
            object_name=entity_label, member=erwin_attr.code or erwin_attr.name,
            message="erwin role-migrated key attribute — reconciled by the "
                    "corresponding SAP PD relationship, which expresses the "
                    "same foreign key without a separate attribute object",
            pd_value=MISSING,
            erwin_value=erwin_attr.code or erwin_attr.name,
            remediation="No action required; informational only.",
        )
        return

    result.attributes_extra_in_erwin += 1
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
        _emit_missing_attribute(result, entity_label, pd_attr)

    for erwin_attr in outcome.unmatched_right:
        _emit_extra_or_relationship_attribute(result, entity_label, erwin_attr)

    result.attributes_matched += len(outcome.pairs)
    return len(outcome.pairs)


# ─── IDENTIFIER COMPARISON ────────────────────────────────────────────────────

def _identifier_members(identifier: Optional[Identifier]) -> List[str]:
    if identifier is None:
        return []
    return [normalizers.normalize_name(code) for code in identifier.attributes]


def _format_members(identifier: Optional[Identifier]) -> str:
    if identifier is None:
        return NONE_VALUE
    if not identifier.attributes:
        return f"{identifier.name or '(unnamed)'} → (no members)"
    return f"{identifier.name or '(unnamed)'} → {', '.join(identifier.attributes)}"


def _identity_is_inherited(erwin_entity: Entity,
                           identifier: Optional[Identifier]) -> bool:
    """
    True when every member of an erwin identifier is a role-migrated key rather
    than an attribute the modeller declared on the entity.

    This is the signature of *inherited identity*: a subtype identified by its
    supertype's key, or an associative entity identified by the entities it
    joins.  A logical model expresses both structurally — through the
    inheritance or the association — and so carries no identifier of its own.
    Reporting that as a lost primary identifier would be wrong: nothing was lost,
    the two tools simply record the same fact in different places.
    """
    if identifier is None or not identifier.attributes:
        return False
    declared = {attr.code.upper() for attr in erwin_entity.attributes
                if not attr.is_migrated}
    return all(code.upper() not in declared for code in identifier.attributes)


def _emit_primary_absent_both(result: ValidationResult, entity_label: str) -> None:
    if not config.CHECK_MODEL_QUALITY:
        return
    # Absent on BOTH sides: the migration preserved the model faithfully, so
    # this is source-model quality, not fidelity. Emitted as INFO so it
    # cannot dominate the score.
    result.emit(
        "MODEL_QUALITY", object_type="IDENTIFIER", object_name=entity_label,
        message="Entity has no primary identifier in either model",
        pd_value=NONE_VALUE, erwin_value=NONE_VALUE,
        severity="INFO",
        remediation="Pre-existing in SAP PD, not caused by the "
                    "migration. Define a primary identifier if the "
                    "business requires unique referencing.",
    )


def _emit_primary_missing_in_pd(result: ValidationResult, entity_label: str,
                                pd_entity: Entity, erwin_entity: Entity,
                                pd_subtypes: Set[str],
                                erwin_primary: Optional[Identifier],
                                erwin_label: str) -> None:
    is_subtype     = normalizers.normalize_name(entity_label) in pd_subtypes
    is_associative = pd_entity.is_associative or erwin_entity.is_associative
    if _identity_is_inherited(erwin_entity, erwin_primary) and (is_subtype or is_associative):
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
        return
    result.emit(
        "PRIMARY_IDENTIFIER", object_type="IDENTIFIER", object_name=entity_label,
        message="Primary identifier exists in erwin but not in SAP PD",
        pd_value=NONE_VALUE, erwin_value=erwin_label,
        remediation="Add the primary identifier to SAP PD so both models "
                    "agree on entity identity.",
    )


def _emit_primary_missing_in_erwin(result: ValidationResult, entity_label: str,
                                   pd_label: str) -> None:
    result.emit(
        "PRIMARY_IDENTIFIER", object_type="IDENTIFIER", object_name=entity_label,
        message="Primary identifier LOST in migration — present in SAP PD, "
                "absent in erwin",
        pd_value=pd_label, erwin_value=NONE_VALUE,
        remediation="Create the corresponding PK key group on the erwin entity.",
    )


def _emit_primary_present_both(result: ValidationResult, entity_label: str,
                               pd_primary: Identifier, erwin_primary: Identifier,
                               pd_label: str, erwin_label: str) -> None:
    pd_members    = _identifier_members(pd_primary)
    erwin_members = _identifier_members(erwin_primary)

    if set(pd_members) != set(erwin_members):
        result.emit(
            "PRIMARY_IDENTIFIER", object_type="IDENTIFIER", object_name=entity_label,
            message="Primary identifier composition differs — entity identity "
                    "has changed",
            pd_value=pd_label, erwin_value=erwin_label,
            remediation="Align the PK key-group members in erwin with the "
                        "SAP PD primary identifier.",
        )
        return

    if pd_members != erwin_members:
        result.emit(
            "ALTERNATE_IDENTIFIER", object_type="IDENTIFIER", object_name=entity_label,
            message="Primary identifier member order differs",
            pd_value=pd_label, erwin_value=erwin_label,
            remediation="Reorder the key-group members if identifier order "
                        "is governed.",
        )
        return

    result.emit(
        "IDENTIFIER_VERIFIED", object_type="IDENTIFIER",
        object_name=entity_label,
        member=pd_primary.name or "(primary identifier)",
        message=f"Primary identifier migrated intact — "
                f"{len(pd_members)} member(s) match in name and order",
        pd_value=pd_label, erwin_value=erwin_label,
        remediation=NO_ACTION_REQUIRED,
    )


def _compare_primary_identifier(result: ValidationResult, entity_label: str,
                                pd_entity: Entity, erwin_entity: Entity,
                                pd_subtypes: Set[str],
                                pd_primary: Optional[Identifier],
                                erwin_primary: Optional[Identifier],
                                pd_label: str, erwin_label: str) -> None:
    """Dispatch to the one case that applies; each case is a single helper."""
    if pd_primary is None and erwin_primary is None:
        _emit_primary_absent_both(result, entity_label)
        return
    if pd_primary is None:
        _emit_primary_missing_in_pd(result, entity_label, pd_entity, erwin_entity,
                                    pd_subtypes, erwin_primary, erwin_label)
        return
    if erwin_primary is None:
        _emit_primary_missing_in_erwin(result, entity_label, pd_label)
        return
    _emit_primary_present_both(result, entity_label, pd_primary, erwin_primary,
                               pd_label, erwin_label)


def _compare_alternate_identifiers(result: ValidationResult, entity_label: str,
                                   pd_entity: Entity, erwin_entity: Entity) -> None:
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
            remediation=NO_ACTION_REQUIRED,
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
        _compare_primary_identifier(result, entity_label, pd_entity, erwin_entity,
                                    pd_subtypes, pd_primary, erwin_primary,
                                    pd_label, erwin_label)

    if config.CHECK_ALTERNATE_IDENTIFIERS:
        _compare_alternate_identifiers(result, entity_label, pd_entity, erwin_entity)

    return pd_label, erwin_label


# ─── ENTITY COMPARISON ────────────────────────────────────────────────────────

def _check_entity_fallback_match(result: ValidationResult, label: str,
                                 pd_entity: Entity, erwin_entity: Entity,
                                 basis: str) -> None:
    if basis == "code" or not config.REPORT_FALLBACK_MATCHES:
        return
    result.emit(
        "FALLBACK_MATCH", object_type="ENTITY", object_name=label,
        message=f"Entity matched on {basis}, not on code — possible rename",
        pd_value=f"{pd_entity.name} / {pd_entity.code}",
        erwin_value=f"{erwin_entity.name} / {erwin_entity.code}",
        remediation="Confirm the rename was intentional and record it in "
                    "the migration mapping.",
    )


def _check_entity_business_name(result: ValidationResult, label: str,
                                pd_entity: Entity, erwin_entity: Entity) -> None:
    if not config.CHECK_BUSINESS_NAMES:
        return
    if normalizers.names_equivalent(pd_entity.name, erwin_entity.name):
        return
    result.emit(
        "BUSINESS_NAME", object_type="ENTITY", object_name=label,
        message="Entity business name differs",
        pd_value=pd_entity.name or NONE_VALUE,
        erwin_value=erwin_entity.name or NONE_VALUE,
        remediation="Align the erwin logical name with the SAP PD business name.",
    )


def _check_entity_subject_area(result: ValidationResult, label: str,
                               pd_entity: Entity, erwin_entity: Entity,
                               compare_subject_areas: bool) -> None:
    # Only meaningful when both models actually organise into subject areas;
    # one side using them and the other not is a tooling choice, not drift.
    if not compare_subject_areas:
        return
    pd_area    = (pd_entity.subject_area or "").strip()
    erwin_area = (erwin_entity.subject_area or "").strip()
    if not (pd_area or erwin_area):
        return
    if normalizers.names_equivalent(pd_area, erwin_area):
        return
    result.emit(
        "SUBJECT_AREA", object_type="ENTITY", object_name=label,
        message="Subject-area / package membership differs",
        pd_value=pd_area or NONE_VALUE, erwin_value=erwin_area or NONE_VALUE,
        remediation="Reassign the entity to the equivalent erwin subject area.",
    )


def _compare_entity_pair(result: ValidationResult, entity_alias: Dict[str, str],
                         pd_entity: Entity, erwin_entity: Entity, basis: str,
                         pd_subtypes: Set[str], compare_subject_areas: bool) -> None:
    label = _entity_label(pd_entity)
    entity_alias[normalizers.normalize_name(pd_entity.code or pd_entity.name)] = \
        erwin_entity.code or erwin_entity.name

    before = (result.critical_count, result.warning_count, result.info_count)

    _check_entity_fallback_match(result, label, pd_entity, erwin_entity, basis)
    _check_entity_business_name(result, label, pd_entity, erwin_entity)

    if config.CHECK_DEFINITIONS:
        _compare_definition(result, "ENTITY", label, "",
                            pd_entity.definition, erwin_entity.definition)

    _check_entity_subject_area(result, label, pd_entity, erwin_entity, compare_subject_areas)

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


def _emit_missing_entity(result: ValidationResult, pd_entity: Entity) -> None:
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


def _emit_extra_entity(result: ValidationResult, erwin_entity: Entity) -> None:
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


def _compare_entities(result: ValidationResult,
                      pd_model: LDMModel,
                      erwin_model: LDMModel) -> Dict[str, str]:
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

    compare_subject_areas = bool(config.CHECK_SUBJECT_AREAS
                                 and pd_model.subject_areas
                                 and erwin_model.subject_areas)

    outcome = match_objects(pd_entities, erwin_entities,
                            _named_key_funcs(config.ENTITY_MATCH_KEY))

    entity_alias: Dict[str, str] = {}

    for pd_entity, erwin_entity, basis in outcome.pairs:
        _compare_entity_pair(result, entity_alias, pd_entity, erwin_entity, basis,
                             pd_subtypes, compare_subject_areas)

    if config.CHECK_ENTITIES:
        for pd_entity in outcome.unmatched_left:
            _emit_missing_entity(result, pd_entity)

        for erwin_entity in outcome.unmatched_right:
            _emit_extra_entity(result, erwin_entity)

    result.entities_matched = len(outcome.pairs)

    # Exclude erwin's role-migrated FK attributes that PD expresses as a
    # relationship instead. They are real objects in erwin, but they are
    # reconciled by the relationship comparison, not the attribute
    # comparison — leaving them in this total is what made a clean model
    # report a 91-vs-90 attribute discrepancy with no corresponding finding.
    # attributes_via_relationship is reported separately so the raw erwin
    # object count is never lost.
    if result.attributes_via_relationship:
        result.attributes_erwin -= result.attributes_via_relationship

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


def _check_end_optionality(result: ValidationResult, label: str, position: str,
                           pd_end: RelationshipEnd, erwin_end: RelationshipEnd) -> None:
    if not config.CHECK_OPTIONALITY:
        return
    pd_mandatory    = card.is_mandatory(pd_end.cardinality)
    erwin_mandatory = card.is_mandatory(erwin_end.cardinality)
    if pd_mandatory == erwin_mandatory:
        return
    result.emit(
        "OPTIONALITY", object_type="RELATIONSHIP", object_name=label,
        member=f"{position} ({pd_end.entity})",
        message="Participation changed between mandatory and optional",
        pd_value="Mandatory" if pd_mandatory else "Optional",
        erwin_value="Mandatory" if erwin_mandatory else "Optional",
        remediation="Adjust Nulls_Allowed / relationship type in erwin "
                    "to restore the participation rule.",
    )


def _check_end_dependency(result: ValidationResult, label: str, position: str,
                          pd_end: RelationshipEnd, erwin_end: RelationshipEnd,
                          is_association: bool) -> None:
    if not config.CHECK_DEPENDENCY or pd_end.dependent == erwin_end.dependent:
        return
    if is_association and not pd_end.dependent:
        return
    result.emit(
        "DEPENDENCY", object_type="RELATIONSHIP", object_name=label,
        member=f"{position} ({pd_end.entity})",
        message="Existence dependency (identifying relationship) differs",
        pd_value="Dependent" if pd_end.dependent else "Independent",
        erwin_value="Dependent" if erwin_end.dependent else "Independent",
        remediation="Switch the erwin relationship between identifying and "
                    "non-identifying to match SAP PD.",
    )


def _check_end_role_name(result: ValidationResult, label: str, position: str,
                         pd_end: RelationshipEnd, erwin_end: RelationshipEnd,
                         is_association: bool) -> None:
    if not config.CHECK_ROLE_NAMES:
        return
    pd_role    = (pd_end.role or "").strip()
    erwin_role = (erwin_end.role or "").strip()
    if not (pd_role or erwin_role):
        return
    if normalizers.names_equivalent(pd_role, erwin_role):
        return
    if is_association and not pd_role:
        return
    result.emit(
        "ROLE_NAME", object_type="RELATIONSHIP", object_name=label,
        member=f"{position} ({pd_end.entity})",
        message="Role / verb phrase differs",
        pd_value=pd_role or NONE_VALUE, erwin_value=erwin_role or NONE_VALUE,
        remediation="Restore the verb phrase so the relationship still "
                    "reads as a business sentence.",
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
        _check_end_optionality(result, label, position, pd_end, erwin_end)
        _check_end_dependency(result, label, position, pd_end, erwin_end, is_association)
        _check_end_role_name(result, label, position, pd_end, erwin_end, is_association)


def _entity_label(entity: "Entity") -> str:
    """
    An entity's Name and Code are usually the same thing spelled two ways, but
    a real LDM occasionally has two genuinely different entities that share
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


def _check_relationship_cardinality(result: ValidationResult, label: str,
                                    pd_rel: Relationship, erwin_rel: Relationship,
                                    pd_degree: str, erwin_degree: str) -> str:
    """Emits a CARDINALITY finding if needed; returns the resulting status."""
    if not (config.CHECK_CARDINALITY and pd_degree != erwin_degree):
        return "MATCHED"
    result.emit(
        "CARDINALITY", object_type="RELATIONSHIP", object_name=label,
        message=f"Cardinality changed from {pd_degree} to {erwin_degree} — "
                f"the model now asserts a different business rule",
        pd_value=card.describe_relationship(pd_rel),
        erwin_value=card.describe_relationship(erwin_rel),
        remediation="Correct the erwin cardinality; a degree change alters "
                    "what the business is allowed to record.",
    )
    return "CARDINALITY_CHANGED"


def _check_relationship_association_kind(result: ValidationResult, label: str,
                                         pd_rel: Relationship,
                                         erwin_rel: Relationship) -> None:
    if not config.CHECK_ASSOCIATIONS or pd_rel.kind == erwin_rel.kind:
        return
    result.emit(
        "ASSOCIATION", object_type="RELATIONSHIP", object_name=label,
        message="Relationship is modelled as an association on one side only",
        pd_value=pd_rel.kind, erwin_value=erwin_rel.kind,
        remediation="Model the many-to-many consistently — either as an "
                    "association or as an associative entity on both sides.",
    )


def _compare_relationship_pair(result: ValidationResult, pd_rel: Relationship,
                               erwin_rel: Relationship, basis: str) -> None:
    label = _relationship_label(pd_rel)
    pd_degree    = card.degree(pd_rel.end1.cardinality, pd_rel.end2.cardinality)
    erwin_degree = card.degree(erwin_rel.end1.cardinality, erwin_rel.end2.cardinality)
    if not card.ends_aligned(pd_rel, erwin_rel):
        erwin_degree = card.invert_degree(erwin_degree)

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
        status = _check_relationship_cardinality(result, label, pd_rel, erwin_rel,
                                                  pd_degree, erwin_degree)
        _compare_relationship_ends(result, label, pd_rel, erwin_rel)

    _check_relationship_association_kind(result, label, pd_rel, erwin_rel)

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


def _emit_missing_relationship(result: ValidationResult, pd_rel: Relationship) -> None:
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


def _emit_extra_relationship(result: ValidationResult, erwin_rel: Relationship) -> None:
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


def _compare_relationships(result: ValidationResult,
                           pd_model: LDMModel,
                           erwin_model: LDMModel,
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
        _compare_relationship_pair(result, pd_rel, erwin_rel, basis)

    if config.CHECK_RELATIONSHIPS:
        for pd_rel in outcome.unmatched_left:
            _emit_missing_relationship(result, pd_rel)

        for erwin_rel in outcome.unmatched_right:
            _emit_extra_relationship(result, erwin_rel)

    result.relationships_matched = len(outcome.pairs)


# ─── INHERITANCE COMPARISON ───────────────────────────────────────────────────

def _constraint_label(complete: bool, mutually_exclusive: bool) -> str:
    completeness = "Complete" if complete else "Incomplete"
    exclusivity  = "Exclusive" if mutually_exclusive else "Overlapping"
    return f"{completeness}, {exclusivity}"


def _check_inheritance_subtype_diff(result: ValidationResult, label: str,
                                    pd_tree: Inheritance, erwin_tree: Inheritance,
                                    missing: Set[str], extra: Set[str]) -> None:
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


def _check_inheritance_completeness(result: ValidationResult, label: str,
                                    pd_tree: Inheritance, erwin_tree: Inheritance) -> None:
    if pd_tree.complete == erwin_tree.complete:
        return
    result.emit(
        "INHERITANCE_CONSTRAINT", object_type="INHERITANCE", object_name=label,
        message="Completeness constraint differs (is every supertype "
                "instance also a subtype?)",
        pd_value="Complete" if pd_tree.complete else "Incomplete",
        erwin_value="Complete" if erwin_tree.complete else "Incomplete",
        remediation="Set the erwin subtype relationship to Complete / "
                    "Incomplete to match SAP PD.",
    )


def _check_inheritance_exclusivity(result: ValidationResult, label: str,
                                   pd_tree: Inheritance, erwin_tree: Inheritance) -> None:
    if pd_tree.mutually_exclusive == erwin_tree.mutually_exclusive:
        return
    result.emit(
        "INHERITANCE_CONSTRAINT", object_type="INHERITANCE", object_name=label,
        message="Exclusivity constraint differs (may an instance be more "
                "than one subtype?)",
        pd_value="Exclusive" if pd_tree.mutually_exclusive else "Overlapping",
        erwin_value="Exclusive" if erwin_tree.mutually_exclusive else "Overlapping",
        remediation="Set the erwin subtype relationship to Exclusive / "
                    "Inclusive to match SAP PD.",
    )


def _check_inheritance_fallback_match(result: ValidationResult, label: str,
                                      pd_tree: Inheritance, erwin_tree: Inheritance,
                                      basis: str) -> None:
    if basis == "supertype" or not config.REPORT_FALLBACK_MATCHES:
        return
    result.emit(
        "FALLBACK_MATCH", object_type="INHERITANCE", object_name=label,
        message=f"Hierarchy matched on {basis}, not on supertype",
        pd_value=pd_tree.parent, erwin_value=erwin_tree.parent,
        remediation="Verify the supertype was intentionally changed.",
    )


def _compare_inheritance_pair(result: ValidationResult, pd_tree: Inheritance,
                              erwin_tree: Inheritance, basis: str) -> None:
    label = pd_tree.name or f"{pd_tree.parent} hierarchy"

    pd_children    = {normalizers.normalize_name(c) for c in pd_tree.children}
    erwin_children = {normalizers.normalize_name(c) for c in erwin_tree.children}
    missing = pd_children - erwin_children
    extra   = erwin_children - pd_children

    # Census row: matched hierarchies are recorded too, so the report can
    # show every generalisation the way PowerDesigner's List of
    # Inheritances does, not only the broken ones.
    pd_cons    = _constraint_label(pd_tree.complete, pd_tree.mutually_exclusive)
    erwin_cons = _constraint_label(erwin_tree.complete, erwin_tree.mutually_exclusive)

    if missing or extra:
        inh_status = "STRUCTURE_DIFFERS"
    elif pd_cons != erwin_cons:
        inh_status = "CONSTRAINT_DIFFERS"
    else:
        inh_status = "MATCHED"

    result.inheritance_records.append(InheritanceReconciliation(
        name=label, pd_parent=pd_tree.parent, erwin_parent=erwin_tree.parent,
        pd_children=", ".join(sorted(pd_tree.children)),
        erwin_children=", ".join(sorted(erwin_tree.children)),
        pd_count=len(pd_tree.children), erwin_count=len(erwin_tree.children),
        pd_constraints=pd_cons, erwin_constraints=erwin_cons,
        match_basis=basis, status=inh_status,
    ))

    if inh_status == "MATCHED":
        result.emit(
            "INHERITANCE_VERIFIED", object_type="INHERITANCE",
            object_name=label,
            member=f"{len(pd_tree.children)} subtype(s)",
            message=f"Generalisation of '{pd_tree.parent}' migrated "
                    f"intact — same subtypes and constraints ({pd_cons})",
            pd_value=", ".join(sorted(pd_tree.children)),
            erwin_value=", ".join(sorted(erwin_tree.children)),
            remediation=NO_ACTION_REQUIRED,
        )

    _check_inheritance_subtype_diff(result, label, pd_tree, erwin_tree, missing, extra)
    _check_inheritance_completeness(result, label, pd_tree, erwin_tree)
    _check_inheritance_exclusivity(result, label, pd_tree, erwin_tree)
    _check_inheritance_fallback_match(result, label, pd_tree, erwin_tree, basis)


def _emit_missing_inheritance(result: ValidationResult, pd_tree: Inheritance) -> None:
    label = pd_tree.name or f"{pd_tree.parent} hierarchy"
    result.inheritance_records.append(InheritanceReconciliation(
        name=label, pd_parent=pd_tree.parent,
        pd_children=", ".join(sorted(pd_tree.children)),
        pd_count=len(pd_tree.children),
        pd_constraints=_constraint_label(pd_tree.complete, pd_tree.mutually_exclusive),
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


def _emit_extra_inheritance(result: ValidationResult, erwin_tree: Inheritance) -> None:
    label = erwin_tree.name or f"{erwin_tree.parent} hierarchy"
    result.inheritance_records.append(InheritanceReconciliation(
        name=label, erwin_parent=erwin_tree.parent,
        erwin_children=", ".join(sorted(erwin_tree.children)),
        erwin_count=len(erwin_tree.children),
        erwin_constraints=_constraint_label(erwin_tree.complete, erwin_tree.mutually_exclusive),
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


def _compare_inheritances(result: ValidationResult,
                          pd_model: LDMModel,
                          erwin_model: LDMModel,
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
        _compare_inheritance_pair(result, pd_tree, erwin_tree, basis)

    for pd_tree in outcome.unmatched_left:
        _emit_missing_inheritance(result, pd_tree)

    for erwin_tree in outcome.unmatched_right:
        _emit_extra_inheritance(result, erwin_tree)


# ─── MODEL-LEVEL COMPARISONS ──────────────────────────────────────────────────

def _compare_domain_pair(result: ValidationResult, pd_domain, erwin_domain, basis: str) -> None:
    types_ok = normalizers.types_match(pd_domain.data_type, erwin_domain.data_type)
    result.domain_records.append(DomainReconciliation(
        pd_name=pd_domain.name, pd_code=pd_domain.code,
        pd_type=normalizers.describe_type(pd_domain.data_type,
                                          pd_domain.length, pd_domain.precision),
        erwin_name=erwin_domain.name or erwin_domain.code,
        erwin_type=normalizers.describe_type(erwin_domain.data_type,
                                             erwin_domain.length,
                                             erwin_domain.precision),
        match_basis=basis,
        status="MATCHED" if types_ok else "TYPE_DIFFERS",
    ))

    if types_ok:
        result.emit(
            "DOMAIN_VERIFIED", object_type="DOMAIN",
            object_name=pd_domain.name or pd_domain.code,
            message=f"Domain migrated intact — matched in erwin "
                    f"(by {basis})",
            pd_value=normalizers.describe_type(
                pd_domain.data_type, pd_domain.length,
                pd_domain.precision) or "(untyped)",
            erwin_value=normalizers.describe_type(
                erwin_domain.data_type, erwin_domain.length,
                erwin_domain.precision) or "(no type in erwin export)",
            remediation=NO_ACTION_REQUIRED,
        )
    else:
        result.emit(
            "DOMAIN", object_type="DOMAIN",
            object_name=pd_domain.name or pd_domain.code,
            message="Domain logical type differs",
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


def _emit_missing_domain(result: ValidationResult, pd_domain) -> None:
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


def _emit_extra_domain(result: ValidationResult, erwin_domain) -> None:
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


def _compare_domains(result: ValidationResult,
                     pd_model: LDMModel,
                     erwin_model: LDMModel) -> None:
    outcome = match_objects(
        list(pd_model.domains.values()), list(erwin_model.domains.values()),
        [("code", lambda d: normalizers.compare_key(d.code)),
         ("name", lambda d: normalizers.compare_key(d.name)),
         (NORMALIZED_NAME_BASIS, lambda d: normalizers.normalize_name(d.name or d.code))],
    )

    for pd_domain, erwin_domain, basis in outcome.pairs:
        _compare_domain_pair(result, pd_domain, erwin_domain, basis)

    for pd_domain in outcome.unmatched_left:
        _emit_missing_domain(result, pd_domain)

    for erwin_domain in outcome.unmatched_right:
        _emit_extra_domain(result, erwin_domain)


def _check_business_rule_expression(result: ValidationResult, pd_rule, erwin_rule) -> None:
    pd_expression    = normalizers.normalize_definition(pd_rule.expression)
    erwin_expression = normalizers.normalize_definition(erwin_rule.expression)
    if not (pd_expression and erwin_expression and pd_expression != erwin_expression):
        return
    result.emit(
        "BUSINESS_RULE", object_type="BUSINESS_RULE", object_name=pd_rule.name,
        message="Business rule expression differs",
        pd_value=normalizers.truncate(pd_rule.expression),
        erwin_value=normalizers.truncate(erwin_rule.expression),
        remediation="Reconcile the rule expression; a constraint that "
                    "changed silently is a governance breach.",
    )


def _emit_missing_business_rule(result: ValidationResult, pd_rule) -> None:
    result.emit(
        "BUSINESS_RULE", object_type="BUSINESS_RULE",
        object_name=pd_rule.name or pd_rule.code,
        message="Business rule exists in SAP PD but NOT in erwin",
        pd_value=normalizers.truncate(pd_rule.expression or pd_rule.definition),
        erwin_value=MISSING,
        remediation="Recreate the rule in erwin as a validation rule or "
                    "documented constraint.",
    )


def _emit_extra_business_rule(result: ValidationResult, erwin_rule) -> None:
    result.emit(
        "BUSINESS_RULE", object_type="BUSINESS_RULE",
        object_name=erwin_rule.name or erwin_rule.code,
        message="Business rule exists in erwin but NOT in SAP PD",
        pd_value=MISSING,
        erwin_value=normalizers.truncate(erwin_rule.expression or erwin_rule.definition),
        remediation="Add the rule to SAP PD, or remove it from erwin.",
    )


def _compare_business_rules(result: ValidationResult,
                            pd_model: LDMModel,
                            erwin_model: LDMModel) -> None:
    outcome = match_objects(
        pd_model.business_rules, erwin_model.business_rules,
        [("name",            lambda r: normalizers.compare_key(r.name)),
         (NORMALIZED_NAME_BASIS, lambda r: normalizers.normalize_name(r.name or r.code))],
    )

    for pd_rule, erwin_rule, _basis in outcome.pairs:
        _check_business_rule_expression(result, pd_rule, erwin_rule)

    for pd_rule in outcome.unmatched_left:
        _emit_missing_business_rule(result, pd_rule)

    for erwin_rule in outcome.unmatched_right:
        _emit_extra_business_rule(result, erwin_rule)


def _connected_entity_names(erwin_model: LDMModel) -> Set[str]:
    """Normalised names of every entity that participates in a relationship or
    hierarchy in the erwin model."""
    connected: Set[str] = set()
    for rel in erwin_model.relationships:
        connected.add(normalizers.normalize_name(rel.end1.entity))
        connected.add(normalizers.normalize_name(rel.end2.entity))
    for tree in erwin_model.inheritances:
        connected.add(normalizers.normalize_name(tree.parent))
        for child in tree.children:
            connected.add(normalizers.normalize_name(child))
    return connected


def _check_entity_orphan(result: ValidationResult, entity: Entity, label: str,
                         connected: Set[str]) -> None:
    # Connectivity is keyed by the plain code/name that relationship ends
    # actually store (see the `connected` set above) — not by the
    # disambiguated display label, which can include extra text (e.g. a
    # "Customer(KNA1)" business name plus its own "(KNA1)" code suffix)
    # that would never match and would wrongly flag every entity as an
    # orphan.
    if normalizers.normalize_name(entity.code or entity.name) in connected:
        return
    result.emit(
        "MODEL_QUALITY", object_type="ENTITY", object_name=label,
        message="Entity participates in no relationship or hierarchy in erwin "
                "— possible orphan from the migration",
        pd_value="", erwin_value="0 relationships",
        remediation="Reconnect the entity, or confirm it is genuinely "
                    "standalone reference data.",
    )


def _check_entity_attribute_loss(result: ValidationResult, entity: Entity,
                                 label: str, pd_model: LDMModel) -> None:
    # Only a migration LOSS is worth reporting: SAP PD had attributes and
    # erwin does not.  When neither side has any, the emptiness is a property
    # of the source model, not something the migration did — emitting it per
    # entity produced 190 warnings that buried the real findings and drove
    # the fidelity score to zero.
    if entity.attributes:
        return
    pd_counterpart = pd_model.entities.get((entity.code or "").upper())
    if pd_counterpart is None or not pd_counterpart.attributes:
        return
    result.emit(
        "MODEL_QUALITY", object_type="ENTITY", object_name=label,
        message="Entity lost all attributes in the migration",
        pd_value=f"{len(pd_counterpart.attributes)} attributes",
        erwin_value="0 attributes",
        remediation="Re-migrate the entity's attributes; they are "
                    "present in SAP PD but absent in erwin.",
    )


def _check_model_name_match(result: ValidationResult, pd_model: LDMModel,
                            erwin_model: LDMModel) -> None:
    if not (pd_model.model_name and erwin_model.model_name):
        return
    if normalizers.names_equivalent(pd_model.model_name, erwin_model.model_name):
        return
    result.emit(
        "MODEL_METADATA", object_type="MODEL", object_name="(model)",
        message="Model names differ between the two tools",
        pd_value=pd_model.model_name, erwin_value=erwin_model.model_name,
        remediation="Align the model names so the pair is traceable in the "
                    "migration inventory.",
    )


def _check_model_quality(result: ValidationResult,
                         pd_model: LDMModel,
                         erwin_model: LDMModel) -> None:
    """
    Quality rules that apply to the migrated model in its own right, independent
    of the comparison.  A structurally valid but semantically hollow model can
    reconcile perfectly and still be unusable.
    """
    connected = _connected_entity_names(erwin_model)

    for entity in erwin_model.entity_list:
        label = _entity_label(entity)
        _check_entity_orphan(result, entity, label, connected)
        _check_entity_attribute_loss(result, entity, label, pd_model)

    _check_model_name_match(result, pd_model, erwin_model)



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

def _guard_parse_errors(result: ValidationResult,
                        pd_model: LDMModel, erwin_model: LDMModel) -> bool:
    """Emit a fatal PARSE_ERROR finding if either side failed to parse.

    Returns True when a guard fired and the caller should stop.
    """
    if pd_model.parse_error:
        result.status = "ERROR"
        result.add(Finding("PARSE_ERROR", "CRITICAL", object_type="MODEL",
                           message=f"SAP PD LDM parse error: {pd_model.parse_error}",
                           remediation="Re-export the .cdm file from SAP PD "
                                       "and confirm it is well-formed XML."))
        result.compute_score()
        return True

    if erwin_model.parse_error:
        result.status = "ERROR"
        result.add(Finding("PARSE_ERROR", "CRITICAL", object_type="MODEL",
                           message=f"erwin parse error: {erwin_model.parse_error}",
                           remediation="Re-export the model from erwin as XML."))
        result.compute_score()
        return True

    return False


def _empty_model_message(pd_model: LDMModel, erwin_model: LDMModel) -> str:
    if not pd_model.entities and not erwin_model.entities:
        return ("Neither file yielded any entities — the exports are "
                "probably not logical models")
    if not erwin_model.entities:
        return (f"The erwin export contains no entities while SAP PD "
                f"contains {len(pd_model.entities)} — nothing could be "
                f"reconciled")
    return (f"The SAP PD LDM contains no entities while erwin "
            f"contains {len(erwin_model.entities)} — nothing could be "
            f"reconciled")


def _guard_empty_models(result: ValidationResult,
                        pd_model: LDMModel, erwin_model: LDMModel) -> bool:
    """Emit a fatal finding when either model has no entities.

    An empty model on either side is a *validation* failure, not a comparison
    result.  Reporting every entity as individually missing would bury the real
    problem — usually the wrong file, a physical export, or a truncated save —
    under hundreds of identical findings.

    Returns True when the guard fired and the caller should stop.
    """
    if pd_model.entities and erwin_model.entities:
        return False

    result.status = "ERROR"
    result.entities_pd    = len(pd_model.entities)
    result.entities_erwin = len(erwin_model.entities)
    result.add(Finding("PARSE_ERROR", "CRITICAL", object_type="MODEL",
                       message=_empty_model_message(pd_model, erwin_model),
                       pd_value=f"{len(pd_model.entities)} entities",
                       erwin_value=f"{len(erwin_model.entities)} entities",
                       remediation="Confirm the file is a logical "
                                   "model export for the intended subject area, "
                                   "and that the export completed."))
    result.compute_score()
    return True


def _run_reconciliation(result: ValidationResult,
                        pd_model: LDMModel, erwin_model: LDMModel) -> None:
    """Run every config-gated comparison pass over a pair of parsed models."""
    entity_alias = _compare_entities(result, pd_model, erwin_model)

    # Object counts for the SUMMARY sheet — census, not comparison.
    result.domains_pd    = len(pd_model.domains)
    result.domains_erwin = len(erwin_model.domains)
    result.shortcuts_pd  = len(getattr(pd_model, "shortcuts", None) or [])
    result.shortcuts_erwin = len(getattr(erwin_model, "shortcuts", None) or [])

    if config.CHECK_RELATIONSHIPS or config.CHECK_CARDINALITY:
        _compare_relationships(result, pd_model, erwin_model, entity_alias)

    if config.CHECK_INHERITANCE:
        _compare_inheritances(result, pd_model, erwin_model, entity_alias)

    if config.CHECK_DOMAINS:
        _compare_domains(result, pd_model, erwin_model)

    if getattr(config, "CHECK_SHORTCUTS", True):
        _compare_shortcuts(result, pd_model)

    if config.CHECK_BUSINESS_RULES:
        _compare_business_rules(result, pd_model, erwin_model)

    if config.CHECK_MODEL_QUALITY:
        _check_model_quality(result, pd_model, erwin_model)

    # -- Documentation mapping (report-only) --------------------------------
    # Built after the reconciliation above so it cannot influence any finding.
    # Failure here must never invalidate an otherwise good validation result.
    try:
        result.documentation_rows = documentation.build_rows(pd_model, erwin_model)
    except Exception as exc:                                   # pragma: no cover
        logger.warning("Documentation mapping unavailable for %s: %s",
                       getattr(result, "pd_file", "?"), exc)
        result.documentation_rows = []


def compare(pd_model: LDMModel, erwin_model: LDMModel) -> ValidationResult:
    """
    Reconcile a SAP PD LDM against an erwin logical model.

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

    if _guard_parse_errors(result, pd_model, erwin_model):
        return result

    if _guard_empty_models(result, pd_model, erwin_model):
        return result

    for warning in list(pd_model.parse_warnings) + list(erwin_model.parse_warnings):
        result.emit("PARSE_WARNING", object_type="MODEL", object_name="(model)",
                    message=warning,
                    remediation="Review the export; the parser fell back to a "
                                "tolerant strategy.")

    _run_reconciliation(result, pd_model, erwin_model)

    result.finalise()

    # ─── UDP FIDELITY ─────────────────────────────────────────────────────────
    # Added by the UDP integration. Scores SAP PD Extended Attributes against
    # the UDPs the erwin export carries and blends the result into the fidelity
    # score, preserving the reconciliation number on
    # result.structural_fidelity_score. Runs AFTER the score is computed, so it
    # cannot influence a single finding, and it never raises.
    udp_fidelity.apply(result, "LDM")
    return result
