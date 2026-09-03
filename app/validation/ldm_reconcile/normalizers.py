"""
Conceptual Vocabulary Normalisation
-----------------------------------
A conceptual model is a *vocabulary*, and the two tools spell that vocabulary
differently.  This module reduces both sides to comparable form so that only
genuine semantic differences survive to the report.

Three independent normalisations live here:

    names        "Customer Order" ≈ "CUSTOMER_ORDER" ≈ "customerOrders"
    data types   PowerDesigner "VA20" / erwin "Varchar(20)" / erwin "Text"
    definitions  prose compared by similarity, not equality
"""

import difflib
import re
from typing import Optional, Tuple

from app.config.validation_config import LDM_CONFIG as config

# ─── CONCEPTUAL TYPE MAP ──────────────────────────────────────────────────────
# PowerDesigner CDM stores conceptual types as short internal codes.  erwin
# logical models use either a full SQL-ish name or one of four coarse logical
# types.  Everything is folded onto a canonical name, then onto a family.
_TYPE_ALIASES = {
    # ── PowerDesigner conceptual codes ───────────────────────────────────────
    "a":     "CHAR",          # Characters
    "va":    "VARCHAR",       # Variable characters
    "la":    "LONG_CHAR",     # Long characters
    "lva":   "LONG_VARCHAR",  # Long variable characters
    "txt":   "TEXT",
    "mbt":   "CHAR",          # Multibyte
    "vmbt":  "VARCHAR",       # Variable multibyte
    "i":     "INTEGER",
    "si":    "SMALLINT",      # Short integer
    "li":    "BIGINT",        # Long integer
    "lvi":   "BIGINT",
    "bt":    "TINYINT",       # Byte
    "sh":    "SMALLINT",
    "n":     "NUMBER",        # Number
    "dc":    "DECIMAL",       # Decimal
    "no":    "NUMBER",
    "f":     "FLOAT",
    "sf":    "FLOAT",         # Short float
    "lf":    "DOUBLE",        # Long float
    "mn":    "MONEY",
    "bl":    "BOOLEAN",
    "d":     "DATE",
    "t":     "TIME",
    "dt":    "DATETIME",
    "ts":    "TIMESTAMP",
    "bin":   "BINARY",
    "vbin":  "BINARY",
    "lbin":  "LONG_BINARY",
    "bmp":   "IMAGE",
    "pic":   "IMAGE",
    "ole":   "BINARY",
    "seq":   "INTEGER",       # Serial
    "sq":    "INTEGER",

    # ── erwin / SQL-style names ──────────────────────────────────────────────
    "char":               "CHAR",
    "character":          "CHAR",
    "nchar":              "CHAR",
    "varchar":            "VARCHAR",
    "varchar2":           "VARCHAR",
    "nvarchar":           "VARCHAR",
    "nvarchar2":          "VARCHAR",
    "character varying":  "VARCHAR",
    "string":             "VARCHAR",
    "text":               "TEXT",
    "ntext":              "TEXT",
    "longtext":           "TEXT",
    "clob":               "TEXT",
    "long":               "LONG_CHAR",
    "int":                "INTEGER",
    "integer":            "INTEGER",
    "int4":               "INTEGER",
    "int2":               "SMALLINT",
    "smallint":           "SMALLINT",
    "int8":               "BIGINT",
    "bigint":             "BIGINT",
    "tinyint":            "TINYINT",
    "byteint":            "TINYINT",
    "number":             "NUMBER",
    "numeric":            "DECIMAL",
    "decimal":            "DECIMAL",
    "dec":                "DECIMAL",
    "float":              "FLOAT",
    "real":               "FLOAT",
    "double":             "DOUBLE",
    "double precision":   "DOUBLE",
    "money":              "MONEY",
    "smallmoney":         "MONEY",
    "currency":           "MONEY",
    "boolean":            "BOOLEAN",
    "bool":               "BOOLEAN",
    "bit":                "BOOLEAN",
    "logical":            "BOOLEAN",
    "flag":               "BOOLEAN",
    "date":               "DATE",
    "time":               "TIME",
    "datetime":           "DATETIME",
    "datetime2":          "DATETIME",
    "smalldatetime":      "DATETIME",
    "timestamp":          "TIMESTAMP",
    "binary":             "BINARY",
    "varbinary":          "BINARY",
    "raw":                "BINARY",
    "blob":               "LONG_BINARY",
    "image":              "IMAGE",
    "uniqueidentifier":   "UUID",
    "guid":               "UUID",
    "uuid":               "UUID",
    "xml":                "TEXT",
    "json":               "TEXT",
}

# Broad families — the honest comparison level for a conceptual model, and the
# only level at which erwin's four-type logical palette can be reconciled with
# PowerDesigner's richer conceptual palette.
_TYPE_FAMILIES = {
    "CHAR": "TEXT", "VARCHAR": "TEXT", "LONG_CHAR": "TEXT",
    "LONG_VARCHAR": "TEXT", "TEXT": "TEXT", "UUID": "TEXT",

    "INTEGER": "NUMBER", "SMALLINT": "NUMBER", "BIGINT": "NUMBER",
    "TINYINT": "NUMBER", "NUMBER": "NUMBER", "DECIMAL": "NUMBER",
    "FLOAT": "NUMBER", "DOUBLE": "NUMBER", "MONEY": "NUMBER",

    "BOOLEAN": "BOOLEAN",

    "DATE": "TEMPORAL", "TIME": "TEMPORAL",
    "DATETIME": "TEMPORAL", "TIMESTAMP": "TEMPORAL",

    "BINARY": "BINARY", "LONG_BINARY": "BINARY", "IMAGE": "BINARY",
}

# PowerDesigner writes some conceptual types with the length glued on: "VA20",
# "A10", "DC18,2".  Split the alphabetic prefix from the numeric tail.
_GLUED_TYPE = re.compile(r"^([A-Za-z_ ]+?)\s*(\d+(?:\s*,\s*\d+)?)$")
_PARENS     = re.compile(r"^([A-Za-z0-9_ ]+?)\s*\(\s*([^)]*)\s*\)$")

_NAME_NOISE = re.compile(r"[^0-9a-zA-Z]+")
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_NULL_TOKENS = {"", "none", "null", "undefined", "n/a", "unspecified", "<none>"}


# ─── NAME NORMALISATION ───────────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    """
    Reduce a business or technical name to a comparison token.

        "Customer Order"   → "CUSTOMERORDER"
        "CUSTOMER_ORDER"   → "CUSTOMERORDER"
        "customerOrders"   → "CUSTOMERORDER"   (when NORMALIZE_PLURALS)
    """
    if not name:
        return ""
    spaced = _CAMEL_SPLIT.sub(" ", name.strip())
    token = _NAME_NOISE.sub("", spaced).upper()
    if config.NORMALIZE_PLURALS and len(token) > 3:
        if token.endswith("IES"):
            token = token[:-3] + "Y"
        elif token.endswith("SES") or token.endswith("XES") or token.endswith("ZES"):
            token = token[:-2]
        elif token.endswith("S") and not token.endswith("SS"):
            token = token[:-1]
    return token


def compare_key(name: str) -> str:
    """Key used for direct (non-fuzzy) name comparison, honouring CASE_INSENSITIVE."""
    if not name:
        return ""
    return name.strip().upper() if config.CASE_INSENSITIVE else name.strip()


def names_equivalent(left: str, right: str) -> bool:
    """True when two names differ only in punctuation, spacing, casing or plurality."""
    return normalize_name(left) == normalize_name(right)


# ─── DATA TYPE NORMALISATION ──────────────────────────────────────────────────

def split_type(dtype: str) -> Tuple[str, str, str]:
    """
    Split a raw type string into (base, length, precision).

        "VARCHAR(20)"   → ("varchar", "20", "")
        "DC18,2"        → ("dc",      "18", "2")
        "Number"        → ("number",  "",   "")
    """
    if not dtype:
        return "", "", ""

    raw = dtype.strip()

    match = _PARENS.match(raw) or _GLUED_TYPE.match(raw)
    if match:
        base = match.group(1).strip().lower()
        dims = [d.strip() for d in match.group(2).split(",") if d.strip()]
        length    = dims[0] if len(dims) > 0 else ""
        precision = dims[1] if len(dims) > 1 else ""
        return base, length, precision

    return raw.lower(), "", ""


def canonical_type(dtype: str) -> str:
    """
    Canonical conceptual type name, without dimensions.

        canonical_type("VA")            → "VARCHAR"
        canonical_type("Varchar(20)")   → "VARCHAR"
        canonical_type("Number")        → "NUMBER"
    """
    base, _, _ = split_type(dtype)
    if base in _NULL_TOKENS:
        return ""
    return _TYPE_ALIASES.get(base, base.upper().replace(" ", "_"))


def type_family(dtype: str) -> str:
    """Broad family: TEXT | NUMBER | BOOLEAN | TEMPORAL | BINARY | OTHER | ''."""
    canonical = canonical_type(dtype)
    if not canonical:
        return ""
    return _TYPE_FAMILIES.get(canonical, "OTHER")


def describe_type(dtype: str, length: str = "", precision: str = "") -> str:
    """
    Human-readable type for the report, e.g. "VARCHAR(20) / TEXT", or
    "VBIN64000 -> VARBINARY(64000) / BINARY" when the raw declared type
    differs from its canonical form.

    The RAW declared type is shown whenever canonicalisation would hide it.
    Without this, a genuine mismatch could be reported with identical-looking
    evidence on both sides: PowerDesigner's "VBIN64000" (VARBINARY) and
    erwin's "BINARY(64000)" both canonicalise to "BINARY", so a real
    VARBINARY-vs-BINARY difference rendered as "BINARY(64000) / BINARY"
    against "BINARY(64000) / BINARY" -- making a correct finding look like a
    false positive and giving the reviewer nothing actionable.
    """
    if not dtype:
        return "(none)"
    base, glued_len, glued_prec = split_type(dtype)
    length    = (length    or glued_len).strip()
    precision = (precision or glued_prec).strip()
    canonical = canonical_type(dtype)
    dims = ""
    if length and precision:
        dims = f"({length},{precision})"
    elif length:
        dims = f"({length})"

    rendered = f"{canonical}{dims} / {type_family(dtype)}"

    # Surface the raw type when canonicalisation changed the base name, so the
    # evidence column always shows what actually differs.
    raw = (dtype or "").strip()
    if raw and base and canonical.upper() != base.upper():
        rendered = f"{raw} → {rendered}"
    return rendered


# ─── PD → erwin APPROVED TYPE MATRIX (strict) ─────────────────────────────────
# Deliberately separate from _TYPE_ALIASES/_TYPE_FAMILIES above. Those two
# tables are tuned for the *lenient* family/canonical comparison and, for
# that purpose, correctly fold BINARY and VARBINARY together (both are
# "binary" at a family level). This matrix is strict -- config.py's
# PD_TO_ERWIN_APPROVED_TYPES treats BINARY and VARBINARY as different types --
# so it needs its own base-name resolution that keeps them apart, rather than
# inheriting a conflation that would be correct for family mode but wrong
# here.
_APPROVED_TYPE_BASE_ALIASES = {
    # PowerDesigner short/glued codes
    "a": "CHAR", "mbt": "CHAR",
    "va": "VARCHAR", "vmbt": "VARCHAR",
    "txt": "TEXT",
    "i": "INTEGER", "seq": "INTEGER", "sq": "INTEGER",
    "si": "SMALLINT", "sh": "SMALLINT",
    "li": "BIGINT", "lvi": "BIGINT",
    "dc": "DECIMAL",
    "d": "DATE",
    "t": "TIME",
    "ts": "TIMESTAMP",
    "bin": "BINARY",
    "vbin": "VARBINARY",
    "bl": "BOOLEAN",

    # erwin / SQL-style names
    "char": "CHAR", "character": "CHAR", "nchar": "CHAR",
    "varchar": "VARCHAR", "varchar2": "VARCHAR", "nvarchar": "VARCHAR",
    "nvarchar2": "VARCHAR", "character varying": "VARCHAR", "string": "VARCHAR",
    "text": "TEXT", "ntext": "TEXT", "longtext": "TEXT", "clob": "TEXT",
    "long_text": "TEXT", "long text": "TEXT",
    "int": "INTEGER", "integer": "INTEGER", "int4": "INTEGER",
    "smallint": "SMALLINT", "int2": "SMALLINT",
    "bigint": "BIGINT", "int8": "BIGINT",
    "numeric": "DECIMAL", "decimal": "DECIMAL", "dec": "DECIMAL",
    "date": "DATE",
    "time": "TIME",
    "timestamp": "TIMESTAMP",
    "binary": "BINARY", "raw": "BINARY",
    "varbinary": "VARBINARY",
    "boolean": "BOOLEAN", "bool": "BOOLEAN", "bit": "BOOLEAN",
    "logical": "BOOLEAN", "flag": "BOOLEAN",
    "xml": "XML",
}


def _approved_matrix_key(dtype: str) -> Optional[str]:
    """Resolve a raw type string to one of PD_TO_ERWIN_APPROVED_TYPES' keys/values."""
    base, _, _ = split_type(dtype)
    if not base:
        return None
    return _APPROVED_TYPE_BASE_ALIASES.get(base)


def pd_erwin_type_match(pd_type: str, erwin_type: str) -> Optional[bool]:
    """
    Strict business-approved comparison for the PD types listed in
    config.PD_TO_ERWIN_APPROVED_TYPES.

    Returns True/False when the PD type resolves to one of the matrix's keys
    (a real, decisive answer), or None when it doesn't -- signalling the
    caller to fall back to the configured TYPE_COMPARISON_MODE, exactly as
    before, for any type outside the reviewed set.
    """
    pd_key = _approved_matrix_key(pd_type)
    if pd_key is None or pd_key not in config.PD_TO_ERWIN_APPROVED_TYPES:
        return None
    erwin_key = _approved_matrix_key(erwin_type)
    return erwin_key in config.PD_TO_ERWIN_APPROVED_TYPES[pd_key]


def pd_type_in_approved_matrix(pd_type: str) -> bool:
    """
    True when pd_type is one of the PD types the business has explicitly
    reviewed (config.PD_TO_ERWIN_APPROVED_TYPES). Lets a caller such as the
    comparator's report-message logic state which comparison basis actually
    decided a DATA_TYPE finding, without reaching into this module's private
    helpers.
    """
    return _approved_matrix_key(pd_type) in config.PD_TO_ERWIN_APPROVED_TYPES


def types_match(pd_type: str, erwin_type: str, mode: Optional[str] = None) -> bool:
    """
    Compare two conceptual types under the configured strictness.
    An empty type on either side is treated as "not specified" and never fails —
    absence of a type is reported separately by the comparator.
    """
    mode = mode or config.TYPE_COMPARISON_MODE

    left_raw  = (pd_type or "").strip().lower()
    right_raw = (erwin_type or "").strip().lower()

    if left_raw in _NULL_TOKENS or right_raw in _NULL_TOKENS:
        return True

    # Business-approved matrix takes precedence for the PD types it covers
    # (see config.PD_TO_ERWIN_APPROVED_TYPES) -- e.g. CHAR and VARCHAR are
    # genuinely different types under this matrix, not interchangeable
    # members of one "TEXT" family. Anything outside the matrix falls
    # through to the mode-based comparison exactly as before.
    approved_result = pd_erwin_type_match(pd_type, erwin_type)
    if approved_result is not None:
        return approved_result

    if mode == "exact":
        return left_raw == right_raw
    if mode == "canonical":
        return canonical_type(pd_type) == canonical_type(erwin_type)
    return type_family(pd_type) == type_family(erwin_type)


def dimensions_match(pd_attr_type: str, pd_len: str, pd_prec: str,
                     er_attr_type: str, er_len: str, er_prec: str) -> bool:
    """
    Compare declared length/precision, skipping the check whenever either side
    has left the dimension unspecified — normal and legitimate in a conceptual
    model, and not evidence of a migration defect.
    """
    _, pd_glued_len, pd_glued_prec = split_type(pd_attr_type)
    _, er_glued_len, er_glued_prec = split_type(er_attr_type)

    pd_length = (pd_len or pd_glued_len).strip()
    er_length = (er_len or er_glued_len).strip()
    pd_precision = (pd_prec or pd_glued_prec).strip()
    er_precision = (er_prec or er_glued_prec).strip()

    if pd_length and er_length and pd_length != er_length:
        return False
    if pd_precision and er_precision and pd_precision != er_precision:
        return False
    return True


# ─── DEFINITION NORMALISATION ─────────────────────────────────────────────────

def normalize_definition(text: str) -> str:
    """Collapse whitespace and casing so cosmetic edits do not read as changes."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


def definition_similarity(left: str, right: str) -> float:
    """Ratio in [0.0, 1.0] between two definitions after normalisation."""
    left_norm  = normalize_definition(left)
    right_norm = normalize_definition(right)
    if not left_norm and not right_norm:
        return 1.0
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    return difflib.SequenceMatcher(None, left_norm, right_norm).ratio()


def definitions_match(left: str, right: str) -> bool:
    """True when two definitions are similar enough under the configured threshold."""
    return definition_similarity(left, right) >= config.DEFINITION_SIMILARITY_THRESHOLD


def truncate(text: str, limit: int = 120) -> str:
    """Shorten free text for a report cell without losing the leading meaning."""
    if not text:
        return ""
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ─── EXCLUSION FILTERS ────────────────────────────────────────────────────────

_entity_excludes    = [re.compile(p, re.IGNORECASE) for p in config.EXCLUDE_ENTITY_PATTERNS]
_attribute_excludes = [re.compile(p, re.IGNORECASE) for p in config.EXCLUDE_ATTRIBUTE_PATTERNS]


def is_excluded_entity(name: str, code: str = "") -> bool:
    return any(rx.search(name or "") or rx.search(code or "") for rx in _entity_excludes)


def is_excluded_attribute(name: str, code: str = "") -> bool:
    return any(rx.search(name or "") or rx.search(code or "") for rx in _attribute_excludes)