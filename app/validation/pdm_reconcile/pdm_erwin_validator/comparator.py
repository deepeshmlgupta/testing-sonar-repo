"""
Validation Comparator
---------------------
Compares a parsed PowerDesigner PDM model against a parsed ERwin model and
produces a structured list of differences (Findings) plus reconciliation counts
at three levels — tables, columns and foreign keys — and an overall fidelity
score, mirroring the CDM validator's report.

Each Finding has:
  category   : TABLE | COLUMN | DATA_TYPE | NULLABILITY | DEFAULT |
               PRIMARY_KEY | FOREIGN_KEY | INDEX
  severity   : CRITICAL | WARNING | INFO   (driven by config.FINDING_SEVERITY)
  table      : physical table name (or empty for model-level issues)
  column     : physical column name (or empty)
  message    : human-readable description of the difference
  pd_value   : value from PowerDesigner
  erwin_value: value from ERwin

Severity is looked up per finding *key* in config.FINDING_SEVERITY, so the whole
policy (what is CRITICAL vs WARNING vs INFO vs IGNORE) lives in config.py and can
be tuned without touching this module.
"""

import logging
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional
from data_type_mapper import normalize, types_match

# The PDM tier's settings live in app/config/validation_config.py. This module
# used to do a bare ``import config`` against a local config.py that no longer
# exists in this folder, which made the whole PDM engine unimportable (and the
# PDM route in app/main.py die with "No module named 'config'" before it ever
# validated anything). Import the central tier view, exactly as erwin_parser.py
# already does, and keep a local-file fallback for anyone running this folder
# standalone.
try:
    from app.config.validation_config import PDM_CONFIG as config
except Exception:                       # pragma: no cover - standalone use
    try:
        import config                                          # type: ignore
    except Exception:
        config = None

logger = logging.getLogger(__name__)


# ─── SEVERITY POLICY ──────────────────────────────────────────────────────────

_DEFAULT_SEVERITY = {
    "TABLE_MISSING": "CRITICAL", "TABLE_EXTRA": "WARNING",
    "COLUMN_MISSING": "CRITICAL", "COLUMN_EXTRA": "WARNING",
    # Two objects sharing one physical name inside the same parent. Both sides
    # used to be indexed into a dict keyed on that name, so the second object
    # simply vanished: the totals no longer added up (erwin 476 columns but
    # 473 matched and 0 extra) and nothing in the report said why.
    "TABLE_DUPLICATE": "CRITICAL", "COLUMN_DUPLICATE": "WARNING",
    "FOREIGN_KEY_DUPLICATE": "INFO",
    "DATA_TYPE": "CRITICAL", "DATA_TYPE_LENGTH": "WARNING",
    "DATA_TYPE_ABSTRACT": "INFO",
    "NULLABILITY": "INFO", "DEFAULT": "INFO",
    "PRIMARY_KEY": "CRITICAL", "PRIMARY_KEY_PD_ONLY": "INFO",
    "FOREIGN_KEY_MISSING": "WARNING", "FOREIGN_KEY_EXTRA": "INFO",
    "INDEX_MISSING": "INFO", "INDEX_EXTRA": "INFO",
    # Census keys — matched objects reported as context (never scored).
    "TABLE_VERIFIED": "VERIFIED", "COLUMN_VERIFIED": "VERIFIED",
    "KEY_VERIFIED": "VERIFIED", "INDEX_VERIFIED": "VERIFIED",
    "INDEX_MIRROR": "VERIFIED", "REFERENCE_VERIFIED": "VERIFIED",
}


def _sev(key: str) -> str:
    """Effective severity for a finding key (config override, else default)."""
    policy = getattr(config, "FINDING_SEVERITY", None) or _DEFAULT_SEVERITY
    return policy.get(key, _DEFAULT_SEVERITY.get(key, "INFO"))


# Which PowerDesigner list a finding belongs to — the FINDINGS sheet's
# "Object Type" column, so every PD list (Tables, Columns, Keys, Indexes,
# References) can be filtered in one click.
_OBJECT_TYPE_BY_CATEGORY = {
    "TABLE": "TABLE",
    "COLUMN": "COLUMN", "DATA_TYPE": "COLUMN",
    "NULLABILITY": "COLUMN", "DEFAULT": "COLUMN",
    "PRIMARY_KEY": "KEY",
    "FOREIGN_KEY": "REFERENCE",
    "INDEX": "INDEX",
    "PARSE_ERROR": "MODEL",
}


# ─── FINDING + RESULT ─────────────────────────────────────────────────────────

@dataclass
class Finding:
    category:    str
    severity:    str          # CRITICAL | WARNING | INFO
    object_type: str = ""     # TABLE | COLUMN | KEY | INDEX | REFERENCE
    table:       str = ""
    column:      str = ""
    message:     str = ""
    pd_value:    str = ""
    erwin_value: str = ""

    def as_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass
class ValidationResult:
    pd_file:     str
    erwin_file:  str
    pd_model:    str = ""
    erwin_model: str = ""
    status:      str = "PASS"      # PASS | WARN | FAIL | ERROR
    findings:    List[Finding] = field(default_factory=list)

    # Table-level counters.
    # tables_pd / tables_erwin are RAW object counts — what PowerDesigner's
    # "List of Tables" and erwin's entity list show. Objects that collide on
    # physical name are counted here and reported separately, so
    #     tables_pd == tables_matched + tables_missing_in_erwin + tables_duplicate_pd
    # always holds and the workbook can never disagree with the source tool.
    tables_pd:      int = 0
    tables_erwin:   int = 0
    tables_matched: int = 0
    tables_missing_in_erwin: int = 0
    tables_extra_in_erwin:   int = 0
    tables_duplicate_pd:     int = 0
    tables_duplicate_erwin:  int = 0

    # Column-level counters (same reconciliation identity as above).
    columns_pd:      int = 0
    columns_erwin:   int = 0
    columns_matched: int = 0
    columns_missing_in_erwin: int = 0
    columns_extra_in_erwin:   int = 0
    columns_duplicate_pd:     int = 0
    columns_duplicate_erwin:  int = 0

    # Key / index census counters (for the SUMMARY sheet)
    keys_pd:       int = 0
    keys_erwin:    int = 0
    indexes_pd:    int = 0
    indexes_erwin: int = 0

    # Foreign-key-level counters
    fk_pd:      int = 0
    fk_erwin:   int = 0
    fk_matched: int = 0
    fk_missing_in_erwin: int = 0
    fk_extra_in_erwin:   int = 0
    fk_duplicate_pd:     int = 0
    fk_duplicate_erwin:  int = 0

    # Finding counters
    critical_count: int = 0
    warning_count:  int = 0
    info_count:     int = 0

    # Scoring.
    # fidelity_score is rounded to 2dp for display. fidelity_score_raw is NOT —
    # the promotion gate must read the raw value, because on a model with a few
    # thousand comparable objects a real defect shrinks below the rounding step
    # (1 INFO over 1000 objects = 99.995% -> displays and compares as 100.00%)
    # and a defective model would be promoted on a cosmetic number.
    fidelity_score:     float = 100.0
    fidelity_score_raw: float = 100.0
    needs_review:       bool = False

    # ── Mutators ──────────────────────────────────────────────────────────────
    def add(self, finding: Optional[Finding]) -> None:
        if finding is None or finding.severity == "IGNORE":
            return
        self.findings.append(finding)
        if finding.severity == "CRITICAL":
            self.critical_count += 1
        elif finding.severity == "WARNING":
            self.warning_count += 1
        elif finding.severity == "INFO":
            self.info_count += 1
        # Any other severity ("VERIFIED") is context-only: reported in the
        # workbook, never counted into fidelity or PASS/WARN/FAIL.

    def emit(self, key: str, *, category: str, **kwargs) -> None:
        """Create a finding at the severity configured for `key` and record it."""
        severity = _sev(key)
        if severity == "IGNORE":
            return
        kwargs.setdefault("object_type",
                          _OBJECT_TYPE_BY_CATEGORY.get(category, category))
        self.add(Finding(category=category, severity=severity, **kwargs))

    # ── Scoring ─────────────────────────────────────────────────────────────
    def compute_score(self) -> None:
        if self.status == "ERROR":
            self.fidelity_score = 0.0
            self.fidelity_score_raw = 0.0
            self.needs_review = True
            return
        comparable = max(1, self.tables_pd + self.columns_pd + self.fk_pd)
        weights = getattr(config, "FIDELITY_WEIGHTS",
                          {"CRITICAL": 1.0, "WARNING": 0.35, "INFO": 0.05})
        penalty = (self.critical_count * weights.get("CRITICAL", 1.0) +
                   self.warning_count  * weights.get("WARNING", 0.35) +
                   self.info_count     * weights.get("INFO", 0.05))
        cap = getattr(config, "FIDELITY_MAX_PENALTY_PER_OBJECT", 1.0)
        penalty = min(penalty, comparable * cap)
        self.fidelity_score_raw = max(0.0, 100.0 * (1.0 - penalty / comparable))
        self.fidelity_score = round(self.fidelity_score_raw, 2)
        threshold = getattr(config, "FIDELITY_REVIEW_THRESHOLD", 90.0)
        self.needs_review = self.fidelity_score < threshold

    # ── Count self-check ──────────────────────────────────────────────────────
    def reconciliation_errors(self) -> List[str]:
        """
        Where the reported totals fail to add up.

        Every count in the workbook must satisfy an identity against the raw
        object counts of the two source models. When one of these breaks, the
        report is claiming something arithmetically impossible (e.g. "476 erwin
        columns, 473 matched, 0 extra") and must not be trusted — so the
        breakage is surfaced instead of being left for a reader to spot.
        """
        checks = [
            ("Tables (SAP PD)", self.tables_pd,
             self.tables_matched + self.tables_missing_in_erwin
             + self.tables_duplicate_pd),
            ("Tables (erwin)", self.tables_erwin,
             self.tables_matched + self.tables_extra_in_erwin
             + self.tables_duplicate_erwin),
            ("Columns (SAP PD)", self.columns_pd,
             self.columns_matched + self.columns_missing_in_erwin
             + self.columns_duplicate_pd),
            ("Columns (erwin)", self.columns_erwin,
             self.columns_matched + self.columns_extra_in_erwin
             + self.columns_duplicate_erwin),
            ("FKs (SAP PD)", self.fk_pd,
             self.fk_matched + self.fk_missing_in_erwin),
            ("FKs (erwin)", self.fk_erwin,
             self.fk_matched + self.fk_extra_in_erwin),
        ]
        return [f"{label}: reported {total}, parts sum to {parts}"
                for label, total, parts in checks if total != parts]

    def counts_reconcile(self) -> bool:
        return not self.reconciliation_errors()

    def compute_status(self) -> None:
        if self.status == "ERROR":
            return
        if self.critical_count > 0:
            self.status = "FAIL"
        elif self.warning_count > 0:
            self.status = "WARN"
        else:
            self.status = "PASS"


# ─── NAME NORMALISATION ─────────────────────────────────────────────────────────

def _key(name: str) -> str:
    if getattr(config, "CASE_INSENSITIVE", True):
        return (name or "").strip().upper()
    return (name or "").strip()


def _base_and_len(dtype: str):
    """('VARCHAR(100)') -> ('VARCHAR', '100').  Uses the normaliser first."""
    norm = normalize(dtype)
    if "(" in norm and norm.endswith(")"):
        base, rest = norm.split("(", 1)
        return base.strip(), rest[:-1].strip()
    return norm, ""


# ─── COLUMN COMPARISON ──────────────────────────────────────────────────────────

def _duplicate_codes(columns: List[Dict]) -> Dict[str, int]:
    """{code: how many objects carry it} for codes carried by more than one."""
    counts: Dict[str, int] = {}
    for column in columns:
        code = _key(column.get("code", ""))
        counts[code] = counts.get(code, 0) + 1
    return {code: n for code, n in counts.items() if n > 1}


def _compare_columns(result: ValidationResult, tbl_code: str,
                     pd_cols: List[Dict], erwin_cols: List[Dict]):
    """
    Compare columns of a matched table.

    Returns (matched, missing, extra, duplicate_pd, duplicate_erwin) so that
    ``len(pd_cols) == matched + missing + duplicate_pd`` and
    ``len(erwin_cols) == matched + extra + duplicate_erwin``.

    Both sides used to be indexed straight into ``{code: column}``. When erwin
    migrates the same parent key twice — which it does when PowerDesigner holds
    two references between the same pair of tables on the same column — the
    entity really does end up with two attributes of one physical name, and the
    second one was silently discarded here. The workbook then reported 476 erwin
    columns, 473 matched and 0 extra, which cannot all be true, and the
    duplicates (invalid DDL if the model is generated) were never mentioned.
    """
    pd_map    = {_key(c["code"]): c for c in pd_cols}
    erwin_map = {_key(c["code"]): c for c in erwin_cols}
    pd_names, erwin_names = set(pd_map), set(erwin_map)

    matched = pd_names & erwin_names
    missing = pd_names - erwin_names
    extra   = erwin_names - pd_names

    pd_dupes    = _duplicate_codes(pd_cols)
    erwin_dupes = _duplicate_codes(erwin_cols)
    for code, count in sorted(pd_dupes.items()):
        result.emit("COLUMN_DUPLICATE", category="COLUMN", table=tbl_code,
                    column=code,
                    message=f"Column '{code}' is defined {count} times in "
                            f"PowerDesigner table '{tbl_code}'",
                    pd_value=f"{count} objects share this physical name",
                    erwin_value=str(erwin_dupes.get(code, 1)))
    for code, count in sorted(erwin_dupes.items()):
        result.emit("COLUMN_DUPLICATE", category="COLUMN", table=tbl_code,
                    column=code,
                    message=f"Column '{code}' is defined {count} times in the "
                            f"ERwin entity '{tbl_code}' (usually one migrated "
                            f"attribute per duplicate relationship; the "
                            f"generated DDL would be invalid)",
                    pd_value=str(pd_dupes.get(code, 1)),
                    erwin_value=f"{count} objects share this physical name")

    duplicate_pd    = len(pd_cols) - len(pd_names)
    duplicate_erwin = len(erwin_cols) - len(erwin_names)

    for col in sorted(missing):
        result.emit("COLUMN_MISSING", category="COLUMN", table=tbl_code, column=col,
                    message=f"Column '{col}' exists in PowerDesigner but NOT in ERwin",
                    pd_value=col, erwin_value="—")
    for col in sorted(extra):
        result.emit("COLUMN_EXTRA", category="COLUMN", table=tbl_code, column=col,
                    message=f"Column '{col}' exists in ERwin but NOT in PowerDesigner",
                    pd_value="—", erwin_value=col)

    for col_key in sorted(matched):
        pd_c, erwin_c = pd_map[col_key], erwin_map[col_key]
        _before = len(result.findings)

        if getattr(config, "CHECK_DATA_TYPES", True):
            pd_dt, erwin_dt = pd_c.get("data_type", ""), erwin_c.get("data_type", "")
            if pd_dt and erwin_dt and not types_match(pd_dt, erwin_dt):
                pd_base, _ = _base_and_len(pd_dt)
                ew_base, _ = _base_and_len(erwin_dt)
                abstract = {t.upper() for t in
                            getattr(config, "PD_ABSTRACT_TYPES", {"ENUM"})}
                if pd_base.upper() in abstract:
                    # PD uses an abstract/logical token (e.g. 'Enum') with no
                    # concrete SQL form; ERwin realised it physically. Not a
                    # like-for-like data-type comparison, so it is context only.
                    key, label = ("DATA_TYPE_ABSTRACT",
                                  f"PD abstract type '{pd_dt}' realised in ERwin")
                elif pd_base == ew_base:
                    # same underlying type, only length/precision differs -> softer
                    key, label = "DATA_TYPE_LENGTH", "Data type length mismatch"
                else:
                    key, label = "DATA_TYPE", "Data type mismatch"
                result.emit(key, category="DATA_TYPE", table=tbl_code, column=col_key,
                            message=f"{label} on column '{col_key}'",
                            pd_value=f"{pd_dt} → {normalize(pd_dt)}",
                            erwin_value=f"{erwin_dt} → {normalize(erwin_dt)}")

        if getattr(config, "CHECK_NULLABILITY", True):
            if pd_c.get("not_null") != erwin_c.get("not_null"):
                result.emit("NULLABILITY", category="NULLABILITY", table=tbl_code,
                            column=col_key,
                            message=f"Nullability mismatch on column '{col_key}'",
                            pd_value="NOT NULL" if pd_c.get("not_null") else "NULL",
                            erwin_value="NOT NULL" if erwin_c.get("not_null") else "NULL")

        if getattr(config, "CHECK_DEFAULT_VALUES", True):
            pd_def    = (pd_c.get("default") or "").strip()
            erwin_def = (erwin_c.get("default") or "").strip()
            if _key(pd_def) != _key(erwin_def):
                result.emit("DEFAULT", category="DEFAULT", table=tbl_code, column=col_key,
                            message=f"Default value mismatch on column '{col_key}'",
                            pd_value=pd_def or "(none)", erwin_value=erwin_def or "(none)")

        if len(result.findings) == _before:
            result.emit("COLUMN_VERIFIED", category="COLUMN", table=tbl_code,
                        column=col_key,
                        message=f"Column '{col_key}' migrated intact — present in "
                                f"ERwin with matching type, nullability and default",
                        pd_value=pd_c.get("data_type", "") or "(untyped)",
                        erwin_value=erwin_c.get("data_type", "") or "(untyped)")

    return (len(matched), len(missing), len(extra),
            duplicate_pd, duplicate_erwin)


# ─── PRIMARY KEY ─────────────────────────────────────────────────────────────────

def _pk_columns(tbl: Dict) -> Optional[List[str]]:
    for key in tbl.get("keys", []):
        if key.get("is_pk"):
            return [_key(c) for c in key.get("columns", [])]
    return None


def _compare_primary_keys(result, tbl_code, pd_tbl, erwin_tbl):
    pd_pk, erwin_pk = _pk_columns(pd_tbl), _pk_columns(erwin_tbl)
    if pd_pk is None and erwin_pk is None:
        return
    if pd_pk is None:
        result.emit("PRIMARY_KEY_PD_ONLY", category="PRIMARY_KEY", table=tbl_code,
                    message="No Primary Key in PowerDesigner; ERwin has one",
                    pd_value="(none)", erwin_value=str(sorted(erwin_pk)))
        return
    if erwin_pk is None or not erwin_pk:
        # erwin sometimes does not serialise PK members for fully-migrated keys;
        # only flag if PD genuinely has PK columns and ERwin genuinely has none.
        result.emit("PRIMARY_KEY", category="PRIMARY_KEY", table=tbl_code,
                    message="Primary Key exists in PowerDesigner but NOT in ERwin",
                    pd_value=str(sorted(pd_pk)), erwin_value="(none)")
        return
    if set(pd_pk) != set(erwin_pk):
        result.emit("PRIMARY_KEY", category="PRIMARY_KEY", table=tbl_code,
                    message="Primary Key column mismatch",
                    pd_value=str(sorted(pd_pk)), erwin_value=str(sorted(erwin_pk)))
    else:
        pk_name = next((k.get("name") or k.get("code", "")
                        for k in pd_tbl.get("keys", []) if k.get("is_pk")), "")
        result.emit("KEY_VERIFIED", category="PRIMARY_KEY", table=tbl_code,
                    column=", ".join(sorted(pd_pk)),
                    message=f"Primary key '{pk_name or 'PK'}' migrated intact — "
                            f"same column set in both models",
                    pd_value=str(sorted(pd_pk)), erwin_value=str(sorted(erwin_pk)))


# ─── FOREIGN KEYS ──────────────────────────────────────────────────────────────

def _fk_signature(ref: Dict) -> str:
    parent = _key(ref.get("parent_table", ""))
    child  = _key(ref.get("child_table", ""))
    joins  = tuple(sorted(
        (_key(j.get("parent_col", "")), _key(j.get("child_col", "")))
        for j in ref.get("join_columns", [])))
    return f"{parent}→{child}:{joins}"


def _compare_foreign_keys(result, pd_refs, erwin_refs):
    """
    Reconcile references one OBJECT at a time, not one signature at a time.

    Keying references straight into ``{signature: reference}`` silently absorbed
    every reference that shared a signature with another, so a PowerDesigner
    model whose "List of References" shows 157 was reported as 154 and three
    genuine duplicate references — the ones that make erwin migrate the same
    column twice — never appeared anywhere in the workbook. Counting per object
    keeps the totals honest:

        fk_pd    == fk_matched + fk_missing_in_erwin
        fk_erwin == fk_matched + fk_extra_in_erwin
    """
    pd_by_sig: Dict[str, List[Dict]] = {}
    for reference in pd_refs:
        pd_by_sig.setdefault(_fk_signature(reference), []).append(reference)
    erwin_by_sig: Dict[str, List[Dict]] = {}
    for reference in erwin_refs:
        erwin_by_sig.setdefault(_fk_signature(reference), []).append(reference)

    matched = missing = extra = 0

    for sig in sorted(set(pd_by_sig) | set(erwin_by_sig)):
        pd_side    = pd_by_sig.get(sig, [])
        erwin_side = erwin_by_sig.get(sig, [])
        pair_count = min(len(pd_side), len(erwin_side))
        matched += pair_count
        missing += len(pd_side) - pair_count
        extra   += len(erwin_side) - pair_count

        for reference in pd_side[:pair_count]:
            result.emit(
                "REFERENCE_VERIFIED", category="FOREIGN_KEY",
                table=f"{reference.get('parent_table', '?')}→{reference.get('child_table', '?')}",
                column=reference.get("name") or reference.get("code", ""),
                message=f"Reference '{reference.get('name') or reference.get('code', '?')}' "
                        f"migrated intact — same parent/child and join columns",
                pd_value=sig, erwin_value=sig)
        for reference in pd_side[pair_count:]:
            result.emit(
                "FOREIGN_KEY_MISSING", category="FOREIGN_KEY",
                table=f"{reference.get('parent_table','?')}→{reference.get('child_table','?')}",
                column=reference.get("name") or reference.get("code", ""),
                message=f"FK '{reference.get('name','?')}' exists in PowerDesigner but NOT in ERwin",
                pd_value=sig, erwin_value="—")
        for reference in erwin_side[pair_count:]:
            result.emit(
                "FOREIGN_KEY_EXTRA", category="FOREIGN_KEY",
                table=f"{reference.get('parent_table','?')}→{reference.get('child_table','?')}",
                column=reference.get("name") or reference.get("code", ""),
                message=f"FK '{reference.get('name','?')}' exists in ERwin but NOT in PowerDesigner",
                pd_value="—", erwin_value=sig)

        # Two references with an identical parent/child/join signature are the
        # root cause of erwin's duplicate migrated columns; say so explicitly.
        if len(pd_side) > 1:
            names = ", ".join(r.get("name") or r.get("code", "?") for r in pd_side)
            result.emit(
                "FOREIGN_KEY_DUPLICATE", category="FOREIGN_KEY",
                table=f"{pd_side[0].get('parent_table','?')}→{pd_side[0].get('child_table','?')}",
                column=names,
                message=f"{len(pd_side)} PowerDesigner references share one "
                        f"parent/child/join signature ({names}); erwin migrates "
                        f"the join column once per reference",
                pd_value=sig, erwin_value=str(len(erwin_side)))

    result.fk_pd      = len(pd_refs)
    result.fk_erwin   = len(erwin_refs)
    result.fk_matched = matched
    result.fk_missing_in_erwin = missing
    result.fk_extra_in_erwin   = extra
    result.fk_duplicate_pd     = len(pd_refs) - len(pd_by_sig)
    result.fk_duplicate_erwin  = len(erwin_refs) - len(erwin_by_sig)


# ─── INDEXES ─────────────────────────────────────────────────────────────────

def _index_signature(idx: Dict) -> str:
    cols = tuple(sorted(_key(c) for c in idx.get("columns", [])))
    return f"{'U' if idx.get('is_unique') else 'I'}:{cols}"


def _compare_indexes(result, tbl_code, pd_tbl, erwin_tbl):
    def non_pk_keys(tbl):
        return [k for k in tbl.get("keys", []) if not k.get("is_pk")]

    skip_mirror = getattr(config, "PD_IGNORE_KEY_FK_INDEXES", True)
    if skip_mirror:
        # PD's "List of Indexes" counts these too; ERwin represents them as
        # the key/FK itself, so they are accounted for rather than compared.
        for idx in pd_tbl.get("indexes", []):
            if idx.get("mirror"):
                result.emit(
                    "INDEX_MIRROR", category="INDEX", table=tbl_code,
                    column=idx.get("name") or idx.get("code", ""),
                    message=f"Index '{idx.get('name') or idx.get('code', '?')}' "
                            f"mirrors a key/FK — ERwin carries it as the key "
                            f"itself, not as an index object; accounted for",
                    pd_value=_index_signature(idx),
                    erwin_value="(represented as the key/FK)")
    pd_index_objs = [i for i in pd_tbl.get("indexes", [])
                     if not (skip_mirror and i.get("mirror"))]
    pd_idxs = {_index_signature(i): i
               for i in pd_index_objs + non_pk_keys(pd_tbl)}
    erwin_idxs = {_index_signature(i): i
                  for i in erwin_tbl.get("indexes", []) + non_pk_keys(erwin_tbl)
                  if i.get("kg_type", "IE") != "PK"}

    for sig in sorted(set(pd_idxs) & set(erwin_idxs)):
        idx = pd_idxs[sig]
        result.emit("INDEX_VERIFIED", category="INDEX", table=tbl_code,
                    column=idx.get("name") or idx.get("code", ""),
                    message=f"Index '{idx.get('name') or idx.get('code', '?')}' "
                            f"migrated intact — same columns and uniqueness",
                    pd_value=sig, erwin_value=sig)
    for sig in set(pd_idxs) - set(erwin_idxs):
        idx = pd_idxs[sig]
        result.emit("INDEX_MISSING", category="INDEX", table=tbl_code,
                    message=f"Index '{idx.get('name','?')}' in PowerDesigner missing from ERwin",
                    pd_value=sig, erwin_value="—")
    for sig in set(erwin_idxs) - set(pd_idxs):
        idx = erwin_idxs[sig]
        result.emit("INDEX_EXTRA", category="INDEX", table=tbl_code,
                    message=f"Index '{idx.get('name','?')}' in ERwin not in PowerDesigner",
                    pd_value="—", erwin_value=sig)


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def compare(pd_model: Dict[str, Any], erwin_model: Dict[str, Any]) -> ValidationResult:
    result = ValidationResult(
        pd_file    = pd_model.get("source_file", ""),
        erwin_file = erwin_model.get("source_file", ""),
        pd_model   = pd_model.get("model_name", ""),
        erwin_model= erwin_model.get("model_name", ""),
    )

    if "error" in pd_model:
        result.status = "ERROR"
        result.add(Finding("PARSE_ERROR", "CRITICAL",
                           message=f"PD parse error: {pd_model['error']}"))
        result.compute_score()
        return result
    if "error" in erwin_model:
        result.status = "ERROR"
        result.add(Finding("PARSE_ERROR", "CRITICAL",
                           message=f"ERwin parse error: {erwin_model['error']}"))
        result.compute_score()
        return result

    pd_tables    = pd_model.get("tables", {})
    erwin_tables = erwin_model.get("tables", {})
    pd_keys    = {_key(k): v for k, v in pd_tables.items()}
    erwin_keys = {_key(k): v for k, v in erwin_tables.items()}
    pd_names, erwin_names = set(pd_keys), set(erwin_keys)

    # Tables that collided on physical name were dropped by the parsers. Add
    # them back into the totals and report them, so "Tables (SAP PD)" is the
    # number PowerDesigner's own List of Tables shows and a dropped table is
    # never invisible.
    pd_dupe_tables    = pd_model.get("duplicate_tables", {}) or {}
    erwin_dupe_tables = erwin_model.get("duplicate_tables", {}) or {}
    result.tables_duplicate_pd    = sum(n - 1 for n in pd_dupe_tables.values())
    result.tables_duplicate_erwin = sum(n - 1 for n in erwin_dupe_tables.values())
    for code, count in sorted(pd_dupe_tables.items()):
        result.emit("TABLE_DUPLICATE", category="TABLE", table=code,
                    message=f"{count} PowerDesigner tables share the physical "
                            f"name '{code}'; only the first was compared",
                    pd_value=f"{count} tables", erwin_value="1 compared")
    for code, count in sorted(erwin_dupe_tables.items()):
        result.emit("TABLE_DUPLICATE", category="TABLE", table=code,
                    message=f"{count} ERwin entities share the physical name "
                            f"'{code}'; only the first was compared",
                    pd_value="1 compared", erwin_value=f"{count} entities")

    result.tables_pd    = len(pd_keys) + result.tables_duplicate_pd
    result.tables_erwin = len(erwin_keys) + result.tables_duplicate_erwin
    result.tables_matched = len(pd_names & erwin_names)

    # Column totals across ALL tables (denominator for fidelity + summary).
    result.columns_pd    = sum(len(t.get("columns", [])) for t in pd_keys.values())
    result.columns_erwin = sum(len(t.get("columns", [])) for t in erwin_keys.values())

    # Key / index totals for the SUMMARY census columns.
    result.keys_pd       = sum(len(t.get("keys", [])) for t in pd_keys.values())
    result.keys_erwin    = sum(len(t.get("keys", [])) for t in erwin_keys.values())
    result.indexes_pd    = sum(len(t.get("indexes", [])) for t in pd_keys.values())
    result.indexes_erwin = sum(len(t.get("indexes", [])) for t in erwin_keys.values())

    # ── Table presence ────────────────────────────────────────────────────────
    if getattr(config, "CHECK_TABLES", True):
        for tbl in sorted(pd_names - erwin_names):
            result.tables_missing_in_erwin += 1
            result.emit("TABLE_MISSING", category="TABLE", table=tbl,
                        message=f"Table '{tbl}' in PowerDesigner is MISSING from ERwin",
                        pd_value=tbl, erwin_value="—")
            # every column of a dropped table is a missing column
            result.columns_missing_in_erwin += len(pd_keys[tbl].get("columns", []))
        for tbl in sorted(erwin_names - pd_names):
            result.tables_extra_in_erwin += 1
            result.emit("TABLE_EXTRA", category="TABLE", table=tbl,
                        message=f"Table '{tbl}' in ERwin does NOT exist in PowerDesigner",
                        pd_value="—", erwin_value=tbl)
            result.columns_extra_in_erwin += len(erwin_keys[tbl].get("columns", []))

    # ── Per-matched-table checks ────────────────────────────────────────────────
    for tbl_key in sorted(pd_names & erwin_names):
        pd_tbl, erwin_tbl = pd_keys[tbl_key], erwin_keys[tbl_key]

        if getattr(config, "CHECK_TABLES", True):
            result.emit(
                "TABLE_VERIFIED", category="TABLE", table=tbl_key,
                message=f"Table '{tbl_key}' migrated — present in both models",
                pd_value=f"{len(pd_tbl.get('columns', []))} column(s)",
                erwin_value=f"{len(erwin_tbl.get('columns', []))} column(s)")

        if getattr(config, "CHECK_COLUMNS", True):
            m, miss, ext, dup_pd, dup_er = _compare_columns(
                result, tbl_key,
                pd_tbl.get("columns", []),
                erwin_tbl.get("columns", []))
            result.columns_matched += m
            result.columns_missing_in_erwin += miss
            result.columns_extra_in_erwin += ext
            result.columns_duplicate_pd += dup_pd
            result.columns_duplicate_erwin += dup_er

        if getattr(config, "CHECK_PRIMARY_KEYS", True):
            _compare_primary_keys(result, tbl_key, pd_tbl, erwin_tbl)

        if getattr(config, "CHECK_INDEXES", True):
            _compare_indexes(result, tbl_key, pd_tbl, erwin_tbl)

    # ── Foreign keys (model level) ──────────────────────────────────────────────
    if getattr(config, "CHECK_FOREIGN_KEYS", True):
        _compare_foreign_keys(result, pd_model.get("references", []),
                              erwin_model.get("references", []))

    result.compute_status()
    result.compute_score()
    return result