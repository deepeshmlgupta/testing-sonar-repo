"""
Data Type Normalization
-----------------------
Both PowerDesigner and ERwin may store the same logical type with slightly
different strings.  This module normalises both sides to a canonical form
before comparison so trivial differences (INT vs INTEGER, VARCHAR vs
VARCHAR2) don't produce false failures.
"""

import re

# ─── CANONICAL TYPE MAP ───────────────────────────────────────────────────────
# Each key maps to the canonical name.  Aliases of the same type share one key.
_TYPE_ALIASES = {
    # Integer variants
    "int":          "INTEGER",
    "integer":      "INTEGER",
    "int4":         "INTEGER",
    "int8":         "BIGINT",
    "bigint":       "BIGINT",
    "smallint":     "SMALLINT",
    "int2":         "SMALLINT",
    "tinyint":      "TINYINT",
    "byteint":      "TINYINT",

    # Numeric / Decimal
    "number":       "NUMERIC",
    "numeric":      "NUMERIC",
    "decimal":      "NUMERIC",
    "dec":          "NUMERIC",
    "float":        "FLOAT",
    "float4":       "FLOAT",
    "float8":       "DOUBLE",
    "double":       "DOUBLE",
    "double precision": "DOUBLE",
    "real":         "FLOAT",
    "money":        "MONEY",
    "smallmoney":   "MONEY",

    # Boolean
    "boolean":      "BOOLEAN",
    "bool":         "BOOLEAN",
    "bit":          "BOOLEAN",

    # Character
    "char":         "CHAR",
    "character":    "CHAR",
    "nchar":        "NCHAR",
    "varchar":      "VARCHAR",
    "varchar2":     "VARCHAR",
    "character varying": "VARCHAR",
    "nvarchar":     "NVARCHAR",
    "nvarchar2":    "NVARCHAR",
    "national character varying": "NVARCHAR",
    "text":         "TEXT",
    "ntext":        "NTEXT",
    "longtext":     "TEXT",
    "clob":         "CLOB",
    "nclob":        "NCLOB",
    "long":         "CLOB",

    # Binary
    "binary":       "BINARY",
    "varbinary":    "VARBINARY",
    "raw":          "VARBINARY",
    "long raw":     "BLOB",
    "blob":         "BLOB",
    "image":        "BLOB",

    # Date / Time
    "date":         "DATE",
    "time":         "TIME",
    "datetime":     "DATETIME",
    "datetime2":    "DATETIME",
    "timestamp":    "TIMESTAMP",
    "smalldatetime":"DATETIME",

    # Other
    "uniqueidentifier": "UUID",
    "guid":         "UUID",
    "uuid":         "UUID",
    "xml":          "XML",
    "json":         "JSON",
    "jsonb":        "JSON",
}


def _split_type_and_precision(dtype: str):
    """
    Split 'VARCHAR(100)' → ('varchar', '100')
    Split 'NUMERIC(18,4)' → ('numeric', '18,4')
    Split 'INTEGER'       → ('integer', '')
    """
    dtype = dtype.strip()
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9_]*(?:[ \t]+[a-zA-Z0-9_]+)*)\s*(?:\(([^)]*)\))?$", dtype)
    if m:
        base = m.group(1).strip().lower()
        prec = (m.group(2) or "").strip()
        return base, prec
    return dtype.lower(), ""


def normalize(dtype: str) -> str:
    """
    Return a normalised data type string for comparison.
    Examples
    --------
    normalize("VARCHAR2(100)")  → "VARCHAR(100)"
    normalize("NUMBER(18,4)")   → "NUMERIC(18,4)"
    normalize("INT")            → "INTEGER"
    normalize("FLOAT(24)")      → "FLOAT(24)"
    """
    if not dtype:
        return ""

    base, prec = _split_type_and_precision(dtype)
    canonical = _TYPE_ALIASES.get(base, base.upper())

    if prec:
        return f"{canonical}({prec})"
    return canonical


def types_match(dtype_pd: str, dtype_erwin: str) -> bool:
    """Return True if the two data type strings are equivalent after normalisation."""
    return normalize(dtype_pd) == normalize(dtype_erwin)
