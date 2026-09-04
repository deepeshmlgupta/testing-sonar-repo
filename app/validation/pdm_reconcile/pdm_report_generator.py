"""
PDM Excel Report Generator
==========================
Builds the PDM workbook with the same shape and depth as the CDM and LDM
reports, so a reviewer reads all three the same way:

  1. SUMMARY            one row per model pair, status, fidelity, counters,
                        plus the PDM promotion stage
  2. DASHBOARD          run-level totals, promotion outcome, worst offenders,
                        severity chart
  3. FINDINGS           every individual difference across every model
  4. TABLE_MATRIX       table-by-table reconciliation (the physical counterpart
                        of the conceptual report's ENTITY_MATRIX)
  5. RELATIONSHIPS      foreign-key-by-foreign-key reconciliation with joins
  6. CATEGORY_ANALYSIS  finding counts by category × severity
  7. DESCRIPTION        SAP PD Comment/Description/Annotation vs erwin
                        Definition/Comment/Note, matched rows included
  8. CONFIG             the exact rule set that produced this report
  9. Per-model sheets   one per pair, for runs of a manageable size

findings.csv and validation_summary.json are written beside the workbook for
pipelines that want machine-readable output.

The PDM validator's own modules are NOT imported or modified here: the result
objects are read duck-typed, and the parsed models needed for the matrix and
relationship sheets come from the bridge (or from what pdm_flow already
attached to the result, avoiding a second parse).
"""

import csv
import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.formatting.rule import DataBarRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from . import pdm_documentation
KEY_CODE = "code"
KEY_COLUMNS = "columns"
KEY_KEYS = "keys"
KEY_REFERENCES = "references"
KEY_PASS = "PASS"
KEY_WARN = "WARN"
KEY_EXTRA_IN_ERWIN = "EXTRA_IN_ERWIN"
KEY_MISSING_IN_SAP_PD = "MISSING_IN_SAP_PD"
KEY_FIDELITY_PCT = "Fidelity %"
KEY_CENTER = "center"
KEY_MAX_DIFF_ROWS = "MAX_DIFF_ROWS_PER_MODEL"
KEY_CHILD_TABLE = "child_table"
KEY_NAME = "name"
KEY_LOWER_MATCHED = "matched"
KEY_LOWER_MISSING = "missing_in_erwin"
KEY_LOWER_EXTRA = "extra_in_erwin"
KEY_INDEXES = "indexes"


KEY_SOLID = "solid"
KEY_CRITICAL = "CRITICAL"
KEY_WARNING = "WARNING"
KEY_INFO = "INFO"
KEY_MISSING_IN_ERWIN = "MISSING_IN_ERWIN"
KEY_MATCHED = "MATCHED"
KEY_MODEL_TITLE = "Model"
KEY_PROMOTED = "promoted"
KEY_ERWIN_NAME = "erwin"
KEY_ERROR = "ERROR"
KEY_STATUS_TITLE = "Status"
KEY_VERIFIED = "VERIFIED"
KEY_MISMATCH = "MISMATCH"
KEY_TABLES = "tables"
KEY_FAIL = "FAIL"
KEY_CATEGORY = "Category"
KEY_OBJECT_TYPE = "Object Type"
KEY_ZERO_DECIMAL = "0.00"
KEY_STAGE = "stage"
KEY_PARENT_TABLE = "parent_table"


logger = logging.getLogger(__name__)

# ─── COLOUR PALETTE (identical to the CDM / LDM reports) ──────────────────────
C_GREEN   = "FF92D050"
C_YELLOW  = "FFFFC000"
C_RED     = "FFFF0000"
C_DARKRED = "FFC00000"
C_BLUE    = "FF4472C4"
C_HEADER  = "FF1F3864"
C_SUBHDR  = "FF2E75B6"
C_ALT     = "FFD9E1F2"
C_WHITE   = "FFFFFFFF"
C_BLACK   = "FF000000"
C_GRAY    = "FFD6DCE4"

REPORT_FONT = "Calibri"

STATUS_FILL = {
    KEY_PASS:  PatternFill(KEY_SOLID, fgColor=C_GREEN),
    KEY_WARN:  PatternFill(KEY_SOLID, fgColor=C_YELLOW),
    KEY_FAIL:  PatternFill(KEY_SOLID, fgColor=C_RED),
    KEY_ERROR: PatternFill(KEY_SOLID, fgColor=C_DARKRED),
}

SEV_FILL = {
    KEY_CRITICAL: PatternFill(KEY_SOLID, fgColor=C_RED),
    KEY_WARNING:  PatternFill(KEY_SOLID, fgColor=C_YELLOW),
    KEY_INFO:     PatternFill(KEY_SOLID, fgColor=C_BLUE),
    KEY_VERIFIED: PatternFill(KEY_SOLID, fgColor=C_GREEN),
}

SEV_FONT = {
    KEY_CRITICAL: Font(bold=True, color=C_WHITE),
    KEY_WARNING:  Font(bold=True, color=C_BLACK),
    KEY_INFO:     Font(color=C_WHITE),
    KEY_VERIFIED: Font(bold=True, color="FF000000"),
}

RECON_FILL = {
    KEY_MATCHED:          PatternFill(KEY_SOLID, fgColor=C_GREEN),
    KEY_MISSING_IN_ERWIN: PatternFill(KEY_SOLID, fgColor=C_RED),
    KEY_EXTRA_IN_ERWIN:   PatternFill(KEY_SOLID, fgColor=C_YELLOW),
    "JOIN_CHANGED":     PatternFill(KEY_SOLID, fgColor=C_YELLOW),
}

DOC_STATUS_FILL = {
    KEY_MATCHED:           PatternFill(KEY_SOLID, fgColor="C6EFCE"),
    KEY_MISMATCH:          PatternFill(KEY_SOLID, fgColor="FFC7CE"),
    KEY_MISSING_IN_ERWIN:  PatternFill(KEY_SOLID, fgColor="FFC7CE"),
    KEY_MISSING_IN_SAP_PD: PatternFill(KEY_SOLID, fgColor="FFEB9C"),
    "BOTH_EMPTY":        PatternFill(KEY_SOLID, fgColor="F2F2F2"),
}

THIN        = Side(style="thin", color="FFB8B8B8")
BORDER      = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEADER_FONT = Font(bold=True, color=C_WHITE, name=REPORT_FONT, size=10)
HEADER_FILL = PatternFill(KEY_SOLID, fgColor=C_HEADER)
SUBHDR_FILL = PatternFill(KEY_SOLID, fgColor=C_SUBHDR)
CENTER      = Alignment(horizontal=KEY_CENTER, vertical=KEY_CENTER, wrap_text=True)
LEFT        = Alignment(horizontal="left",   vertical=KEY_CENTER, wrap_text=True)

SEVERITY_ORDER = {KEY_CRITICAL: 0, KEY_WARNING: 1, KEY_INFO: 2}

# What to do about each kind of finding. The PDM comparator does not carry a
# remediation field, so the advice is derived from the finding's category —
# the same guidance the preprocessing engine acts on.
REMEDIATION = {
    "TABLE":       "Table exists on one side only. Re-import the model, or "
                   "confirm the table was intentionally dropped/added.",
    "COLUMN":      "Column exists on one side only. Preprocessing restores "
                   "PD-side columns into the erwin XML; re-run the pipeline.",
    "DATA_TYPE":   "Compare the physical type in both tools. An abstract PD "
                   "type realised concretely by erwin is expected, not a defect.",
    "NULLABILITY": "Check the column's mandatory flag in PowerDesigner against "
                   "erwin's Null_Option.",
    "DEFAULT":     "Check the column's default value in both tools.",
    "PRIMARY_KEY": "Primary key differs. Preprocessing re-links PK members from "
                   "the PD identifier; re-run the pipeline.",
    "FOREIGN_KEY": "Foreign key join differs. Preprocessing repairs FK joins "
                   "whose columns exist on both sides.",
    "INDEX":       "Index exists on one side only. PD's key/FK mirror indexes "
                   "are skipped by default (see CONFIG).",
    "PARSE_ERROR": "The file could not be parsed. Re-export it from the tool.",
    "EXCEPTION":   "Validation raised an exception. See the run log.",
}


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _header_row(ws, values: list, row: int, fill=HEADER_FILL) -> None:
    for column, value in enumerate(values, start=1):
        cell = ws.cell(row=row, column=column, value=value)
        cell.font      = HEADER_FONT
        cell.fill      = fill
        cell.alignment = CENTER
        cell.border    = BORDER


def _data_row(ws, values: list, row: int, alt: bool = False) -> None:
    fill = PatternFill(KEY_SOLID, fgColor=C_ALT) if alt else None
    for column, value in enumerate(values, start=1):
        cell = ws.cell(row=row, column=column, value=value)
        cell.alignment = LEFT
        cell.border    = BORDER
        if fill:
            cell.fill = fill


def _set_col_widths(ws, widths: list) -> None:
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width


def _paint_status(ws, row: int, column: int, status: str) -> None:
    cell = ws.cell(row=row, column=column)
    cell.fill = STATUS_FILL.get(status, PatternFill())
    cell.font = Font(bold=True,
                     color=C_WHITE if status in (KEY_FAIL, KEY_ERROR) else C_BLACK)
    cell.alignment = CENTER


def _paint_severity(ws, row: int, column: int, severity: str) -> None:
    cell = ws.cell(row=row, column=column)
    cell.fill      = SEV_FILL.get(severity, PatternFill())
    cell.font      = SEV_FONT.get(severity, Font())
    cell.alignment = CENTER


def _unique_sheet_name(wb: Workbook, base: str) -> str:
    """Excel caps sheet names at 31 chars and forbids duplicates."""
    illegal = set(r"[]:*?/\\")
    cleaned = "".join("_" if ch in illegal else ch for ch in base)[:28] or "MODEL"
    name = cleaned
    suffix = 1
    existing = {sheet.title for sheet in wb.worksheets}
    while name in existing:
        suffix += 1
        name = f"{cleaned[:25]}_{suffix}"
    return name


def _cfg(config, name: str, default=None):
    return getattr(config, name, default) if config is not None else default


def _limited(findings: List, config) -> List:
    cap = _cfg(config, KEY_MAX_DIFF_ROWS, 500)
    return findings[:cap] if cap and cap > 0 else findings


def _key(value: str) -> str:
    return (value or "").strip().upper()


def _models_for(result) -> Dict[str, Any]:
    """
    The parsed PD and erwin models behind one result.

    pdm_flow attaches them to the result so the report costs no extra parse;
    when a caller passes results from elsewhere they are parsed on demand and a
    failure degrades to empty models (the matrix sheets simply stay thin).
    """
    cached = getattr(result, "source_models", None)
    if isinstance(cached, dict) and cached.get("pd") is not None:
        return cached
    try:
        from . import pdm_validator_bridge as bridge
        return {"pd": bridge.parse_pdm(result.pd_file),
                KEY_ERWIN_NAME: bridge.parse_erwin(result.erwin_file)}
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("Could not parse models for %s: %s", result.pd_file, exc)
        return {"pd": {KEY_TABLES: {}, KEY_REFERENCES: []},
                KEY_ERWIN_NAME: {KEY_TABLES: {}, KEY_REFERENCES: []}}


def _pk_columns(table: Dict[str, Any]) -> str:
    for key in table.get(KEY_KEYS, []) or []:
        if key.get("is_pk"):
            return ", ".join(key.get(KEY_COLUMNS, []) or []) or "(no members)"
    return ""


def _join_text(reference: Dict[str, Any]) -> str:
    joins = reference.get("join_columns", []) or []
    if not joins:
        return "(no join columns)"
    return ", ".join(f"{j.get('parent_col', '?')} → {j.get('child_col', '?')}"
                     for j in joins)


def _fk_signature(reference: Dict[str, Any]) -> str:
    """Mirrors the validator comparator's own signature, so the sheet agrees."""
    parent = _key(reference.get(KEY_PARENT_TABLE, ""))
    child = _key(reference.get(KEY_CHILD_TABLE, ""))
    joins = tuple(sorted(
        (_key(j.get("parent_col", "")), _key(j.get("child_col", "")))
        for j in reference.get("join_columns", []) or []))
    return f"{parent}→{child}:{joins}"


# ─── SUMMARY SHEET ────────────────────────────────────────────────────────────

SUMMARY_HEADERS = [
    "#", "SAP PD File", "erwin File", "SAP PD Model", "erwin Model",
    KEY_STATUS_TITLE, KEY_FIDELITY_PCT, "Review?",
    "Tables (SAP PD)", "Tables (erwin)", "Tables Matched",
    "Tbl Missing", "Tbl Extra",
    "Columns (SAP PD)", "Columns (erwin)", "Columns Matched",
    "Col Missing", "Col Extra",
    "FKs (SAP PD)", "FKs (erwin)", "FKs Matched",
    "FK Missing", "FK Extra",
    "Keys (SAP PD)", "Keys (erwin)",
    "Indexes (SAP PD)", "Indexes (erwin)",
    KEY_CRITICAL, KEY_WARNING, KEY_INFO,
    # Objects that share a physical name with another object in the same
    # parent. They are the difference between the raw totals above and
    # Matched + Missing / Extra, so the row can always be checked by eye.
    "Dup Tables (SAP PD)", "Dup Tables (erwin)",
    "Dup Columns (SAP PD)", "Dup Columns (erwin)",
    "Dup FKs (SAP PD)", "Dup FKs (erwin)",
    "Counts Reconcile?",
]

_SUMMARY_TOTAL_COLUMNS = {
    9: "tables_pd", 10: "tables_erwin", 11: "tables_matched",
    12: "tables_missing_in_erwin", 13: "tables_extra_in_erwin",
    14: "columns_pd", 15: "columns_erwin", 16: "columns_matched",
    17: "columns_missing_in_erwin", 18: "columns_extra_in_erwin",
    19: "fk_pd", 20: "fk_erwin", 21: "fk_matched",
    22: "fk_missing_in_erwin", 23: "fk_extra_in_erwin",
    24: "keys_pd", 25: "keys_erwin", 26: "indexes_pd", 27: "indexes_erwin",
    28: "critical_count", 29: "warning_count", 30: "info_count",
    31: "tables_duplicate_pd", 32: "tables_duplicate_erwin",
    33: "columns_duplicate_pd", 34: "columns_duplicate_erwin",
    35: "fk_duplicate_pd", 36: "fk_duplicate_erwin",
}

# 1-based SUMMARY column indexes, so the conditional formatting below can never
# drift away from the header list again (CRITICAL used to be painted on column
# 26, "Indexes (SAP PD)").
_COL_STATUS   = 6
_COL_FIDELITY = 7
_COL_REVIEW   = 8
_COL_CRITICAL = 28
_COL_WARNING  = 29
_COL_RECONCILE = 37


def _build_summary(wb, results, config) -> None:
    ws = wb.active
    ws.title = "SUMMARY"
    _header_row(ws, SUMMARY_HEADERS, 2, fill=SUBHDR_FILL)

    ws.merge_cells("A1:G1")
    ws.merge_cells("H1:R1")
    ws.merge_cells("S1:W1")
    ws.merge_cells("X1:Z1")
    ws.merge_cells("AA1:AB1")

    for col, text in [(1, "General"), (8, "Tables & Attributes"), (19, "Relationships & Keys"),
                      (24, "Design & Inheritance"), (27, "Score")]:
        cell = ws.cell(1, col, text)
        cell.font, cell.fill, cell.alignment = Font(bold=True), PatternFill(KEY_SOLID, fgColor=C_GRAY), CENTER

    _populate_summary_rows(ws, results)
    _add_summary_totals(ws, results)
    
    last_column = get_column_letter(len(SUMMARY_HEADERS))
    _set_col_widths(ws, [5, 32, 32, 24, 24, 9, 11, 9] + [13] * (len(SUMMARY_HEADERS) - 9) + [17])
    ws.auto_filter.ref = f"A2:{last_column}{len(results) + 2}"
    if results:
        ws.conditional_formatting.add(
            f"G3:G{len(results) + 2}",
            DataBarRule(start_type="num", start_value=0, end_type="num", end_value=100, color="FF63BE7B", showValue=True)
        )

def _populate_summary_rows(ws, results):
    for offset, result in enumerate(results, start=1):
        row = offset + 2
        _data_row(ws, [
            offset, os.path.basename(result.pd_file),
            os.path.basename(result.erwin_file) if result.erwin_file else "",
            result.pd_model, result.erwin_model,
            result.status, result.fidelity_score,
            "YES" if getattr(result, "needs_review", False) else "NO",
            result.tables_pd, result.tables_erwin, result.tables_matched,
            result.tables_missing_in_erwin, result.tables_extra_in_erwin,
            result.columns_pd, result.columns_erwin, result.columns_matched,
            result.columns_missing_in_erwin, result.columns_extra_in_erwin,
            result.fk_pd, result.fk_erwin, result.fk_matched,
            result.fk_missing_in_erwin, result.fk_extra_in_erwin,
            result.keys_pd, result.keys_erwin,
            result.indexes_pd, result.indexes_erwin,
            result.critical_count, result.warning_count, result.info_count,
            "NO" if getattr(result, "missing_in_erwin", 0) else "YES",
        ], row, alt=(offset % 2 == 0))

        _paint_status(ws, row, 6, result.status)
        if result.fidelity_score == 100:
            ws.cell(row=row, column=7).fill = PatternFill(KEY_SOLID, fgColor=C_GREEN)
            ws.cell(row=row, column=7).font = Font(bold=True)
        ws.cell(row=row, column=7).number_format = KEY_ZERO_DECIMAL

        if result.critical_count:
            ws.cell(row=row, column=_COL_CRITICAL).font = Font(bold=True, color=C_RED)
        if result.warning_count:
            ws.cell(row=row, column=_COL_WARNING).font = Font(bold=True, color=C_DARKRED)

        reconcile_cell = ws.cell(row=row, column=_COL_RECONCILE)
        reconcile_cell.alignment = CENTER
        if reconcile_cell.value == "NO":
            reconcile_cell.font, reconcile_cell.fill = Font(bold=True, color=C_WHITE), PatternFill(KEY_SOLID, fgColor=C_RED)

def _add_summary_totals(ws, results):
    total_row = len(results) + 3
    ws.cell(total_row, 1, "TOTAL").font = Font(bold=True)
    for column, attribute in _SUMMARY_TOTAL_COLUMNS.items():
        total = sum(getattr(r, attribute, 0) for r in results)
        cell = ws.cell(total_row, column, total)
        cell.font = Font(bold=True, color=C_RED if column == 28 else C_BLACK)
        cell.fill, cell.border = PatternFill(KEY_SOLID, fgColor=C_GRAY), BORDER

    if results:
        average = round(sum(r.fidelity_score for r in results) / len(results), 2)
        cell = ws.cell(total_row, 7, average)
        cell.font, cell.number_format, cell.fill = Font(bold=True), KEY_ZERO_DECIMAL, PatternFill(KEY_SOLID, fgColor=C_GRAY)

def _build_dashboard(wb: Workbook, results: List, config) -> None:
    ws = wb.create_sheet("DASHBOARD")
    _setup_dashboard_banner(ws)
    _populate_dashboard_statistics(ws, results)
    promo_start = _populate_promotion_outcome(ws, results)
    worst_start = _populate_lowest_fidelity(ws, results, promo_start)
    _set_col_widths(ws, [34, 18, 13, 13, 60, 11])
    _populate_severity_chart(ws, results)

def _setup_dashboard_banner(ws):
    ws.merge_cells("A1:F1")
    banner = ws["A1"]
    banner.value = "PDM Migration Reconciliation Dashboard"
    banner.font, banner.fill, banner.alignment = Font(bold=True, color=C_WHITE, size=14, name=REPORT_FONT), PatternFill(KEY_SOLID, fgColor=C_HEADER), CENTER
    ws.row_dimensions[1].height = 22

def _populate_dashboard_statistics(ws, results):
    status_counts = Counter(r.status for r in results)
    total_models = len(results)
    average_score = round(sum(r.fidelity_score for r in results) / total_models, 2) if total_models else 0.0
    promoted = sum(1 for r in results if getattr(r, KEY_PROMOTED, False))

    _header_row(ws, ["Run Statistic", "Value"], 3, fill=SUBHDR_FILL)
    statistics = _get_dashboard_stats_list(results, status_counts, total_models, average_score, promoted)
    
    for offset, (label, value) in enumerate(statistics):
        row = 4 + offset
        _data_row(ws, [label, value], row, alt=(offset % 2 == 1))
        ws.cell(row=row, column=1).font = Font(bold=True)
        if label in STATUS_FILL:
            _paint_status(ws, row, 2, label)

def _get_dashboard_stats_list(results, status_counts, total_models, average_score, promoted):
    return [
        ("Models validated", total_models),
        (KEY_PASS, status_counts.get(KEY_PASS, 0)),
        (KEY_WARN, status_counts.get(KEY_WARN, 0)),
        (KEY_FAIL, status_counts.get(KEY_FAIL, 0)),
        (KEY_ERROR, status_counts.get(KEY_ERROR, 0)),
        ("Average fidelity score", average_score),
        ("Models needing review", sum(1 for r in results if r.needs_review)),
        ("Promoted to 3_final", promoted),
        ("Held for review", total_models - promoted),
        ("Total findings", sum(len(r.findings) for r in results)),
        ("CRITICAL findings", sum(r.critical_count for r in results)),
        ("WARNING findings", sum(r.warning_count for r in results)),
        ("INFO findings", sum(r.info_count for r in results)),
        ("Tables compared", sum(r.tables_pd for r in results)),
        ("Columns compared", sum(r.columns_pd for r in results)),
        ("Foreign keys compared", sum(r.fk_pd for r in results)),
    ]

def _populate_promotion_outcome(ws, results):
    promo_start = 4 + 16 + 2 # 16 statistics
    ws.cell(promo_start - 1, 1, "Promotion outcome").font = Font(bold=True, size=11)
    _header_row(ws, [KEY_MODEL_TITLE, "Stage", "Promoted?", KEY_FIDELITY_PCT, "Notes"], promo_start, fill=SUBHDR_FILL)
    for offset, result in enumerate(results, start=1):
        row = promo_start + offset
        _data_row(ws, [
            os.path.basename(result.pd_file), getattr(result, KEY_STAGE, ""),
            "YES" if getattr(result, KEY_PROMOTED, False) else "NO", result.fidelity_score, getattr(result, "flow_notes", "")
        ], row, alt=(offset % 2 == 0))
        ws.cell(row=row, column=3).font = Font(bold=True, color=C_BLACK if getattr(result, KEY_PROMOTED, False) else C_DARKRED)
        ws.cell(row=row, column=4).number_format = KEY_ZERO_DECIMAL
    return promo_start

def _populate_lowest_fidelity(ws, results, promo_start):
    worst_start = promo_start + len(results) + 3
    ws.cell(worst_start - 1, 1, "Lowest-fidelity models").font = Font(bold=True, size=11)
    _header_row(ws, [KEY_MODEL_TITLE, KEY_STATUS_TITLE, KEY_FIDELITY_PCT, KEY_CRITICAL, KEY_WARNING], worst_start, fill=SUBHDR_FILL)
    for offset, result in enumerate(sorted(results, key=lambda r: r.fidelity_score)[:15], start=1):
        row = worst_start + offset
        _data_row(ws, [os.path.basename(result.pd_file), result.status, result.fidelity_score, result.critical_count, result.warning_count], row, alt=(offset % 2 == 0))
        _paint_status(ws, row, 2, result.status)
        ws.cell(row=row, column=3).number_format = KEY_ZERO_DECIMAL
    return worst_start

def _populate_severity_chart(ws, results):
    chart_anchor_row = 4
    ws.cell(chart_anchor_row - 1, 8, "Findings by severity").font = Font(bold=True)
    severity_rows = [
        (KEY_CRITICAL, sum(r.critical_count for r in results)),
        (KEY_WARNING,  sum(r.warning_count for r in results)),
        (KEY_INFO,     sum(r.info_count for r in results)),
        (KEY_VERIFIED, sum(1 for r in results for f in r.findings if f.severity == KEY_VERIFIED)),
    ]
    for offset, (label, count) in enumerate(severity_rows):
        row = chart_anchor_row + offset
        fill = PatternFill(KEY_SOLID, fgColor=C_ALT) if (offset % 2 == 1) else None
        
        c1 = ws.cell(row=row, column=8, value=label)
        c2 = ws.cell(row=row, column=9, value=count)
        
        for c in (c1, c2):
            c.alignment = LEFT
            c.border = BORDER
            if fill: c.fill = fill
            
        _paint_severity(ws, row, 8, label)

    chart = BarChart()
    chart.title = "Findings Breakdown"
    data = Reference(ws, min_col=9, min_row=chart_anchor_row, max_row=chart_anchor_row + 2)
    cats = Reference(ws, min_col=8, min_row=chart_anchor_row, max_row=chart_anchor_row + 2)
    chart.add_data(data)
    chart.set_categories(cats)
    chart.width, chart.height = 14, 8
    ws.add_chart(chart, f"H{chart_anchor_row + 5}")

def _finding_rows(result, config) -> List[list]:
    """
    The rows for one model's findings, worst first.

    This used to slice to MAX_DIFF_ROWS_PER_MODEL *before* sorting. Findings are
    emitted in object order — tables alphabetically, then every foreign key at
    the very end — so on a model with 1084 findings the cut fell around table
    "F" and threw away every CRITICAL, WARNING and FK finding after it. The
    workbook then showed 488 VERIFIED + 11 INFO rows and no defects at all,
    while the SUMMARY sheet on the same model reported a WARNING. Anything
    reading the sheet — a reviewer, or promote_model.py's sign-off scan — was
    being shown a clean model that was not clean.

    So: sort first, then cap, and never let the cap remove a CRITICAL or
    WARNING row. Only VERIFIED / INFO context rows are trimmed, and the sheet
    says so on a final row.
    """
    model_label = os.path.basename(result.pd_file)
    ordered = sorted(result.findings,
                     key=lambda f: (SEVERITY_ORDER.get(f.severity, 3),
                                    f.category, f.table, f.column))

    cap = _cfg(config, KEY_MAX_DIFF_ROWS, 500)
    omitted = 0
    if cap and cap > 0 and len(ordered) > cap:
        must_keep = [f for f in ordered
                     if f.severity in (KEY_CRITICAL, KEY_WARNING, KEY_ERROR)]
        context   = [f for f in ordered
                     if f.severity not in (KEY_CRITICAL, KEY_WARNING, KEY_ERROR)]
        room = max(0, cap - len(must_keep))
        omitted = max(0, len(context) - room)
        findings = must_keep + context[:room]
        findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 3),
                                     f.category, f.table, f.column))
    else:
        findings = ordered

    rows = [[
        model_label, result.status, finding.severity, finding.category,
        getattr(finding, "object_type", ""),
        finding.table, finding.column, finding.message,
        finding.pd_value, finding.erwin_value,
        REMEDIATION.get(finding.category, "") if finding.severity != KEY_VERIFIED else "No action required.",
    ] for finding in findings]

    if omitted:
        rows.append([
            model_label, result.status, KEY_INFO, "REPORT_TRUNCATED", "MODEL",
            "", "",
            f"{omitted} context row(s) (VERIFIED / INFO) omitted to keep this "
            f"sheet under MAX_DIFF_ROWS_PER_MODEL={cap}. Every CRITICAL and "
            f"WARNING finding is listed above; raise the cap in "
            f"app/config/validation_config.py to see the full census.",
            str(len(result.findings)), str(len(findings)),
            "No action required — no defect was hidden.",
        ])

    return rows



FINDINGS_HEADERS = [
    KEY_MODEL_TITLE, KEY_STATUS_TITLE, "Severity", KEY_CATEGORY, KEY_OBJECT_TYPE,
    "Object", "Member", "Message",
    "SAP PD Value", "erwin Value", "Recommended Action", "Manual Review",
]


TABLE_HEADERS = [
    KEY_MODEL_TITLE, KEY_STATUS_TITLE, "SAP PD Code", "erwin Code",
    "Cols (SAP PD)", "Cols (erwin)", "Cols Matched",
    "Col Missing", "Col Extra", "erwin Duplicates",
    "SAP PD PK", "erwin PK",
    "Keys (SAP PD)", "Keys (erwin)",
    "Indexes (SAP PD)", "Indexes (erwin)",
    "CRITICAL", "WARNING", "INFO",
]

RELATIONSHIP_HEADERS = [
    KEY_MODEL_TITLE, KEY_STATUS_TITLE, "Tables",
    "SAP PD Reference", "erwin Reference",
    "SAP PD Joins", "erwin Joins",
]

def _build_findings(wb: Workbook, results: List, config) -> None:
    ws = wb.create_sheet("FINDINGS")
    ws.freeze_panes = "A2"
    _header_row(ws, FINDINGS_HEADERS, 1)

    row = 2
    for result in results:
        for values in _finding_rows(result, config):
            _data_row(ws, values, row, alt=(row % 2 == 0))
            _paint_severity(ws, row, 3, values[2])
            _paint_status(ws, row, 2, result.status)
            row += 1

    _set_col_widths(ws, [30, 9, 11, 20, 15, 30, 26, 62, 34, 34, 56, 18])
    ws.auto_filter.ref = f"A1:{get_column_letter(len(FINDINGS_HEADERS))}{max(row - 1, 1)}"

    if row > 2:
        dv = DataValidation(type="list", formula1='"Acceptable,Not Acceptable"',
                            allow_blank=True)
        ws.add_data_validation(dv)
        # "Manual Review" is the LAST header (column L, 12) — "Recommended
        # Action" is column K. The dropdown used to be attached to K, so a
        # reviewer picking "Acceptable" overwrote the remediation advice while
        # the Manual Review column that promote_model.py actually reads stayed
        # empty. Derive the letter from the header list so it cannot drift.
        review_column = get_column_letter(
            FINDINGS_HEADERS.index("Manual Review") + 1)
        dv.add(f"{review_column}2:{review_column}{row - 1}")


# ─── AS-IMPORTED SHEET ────────────────────────────────────────────────────────

AS_IMPORTED_HEADERS = [
    KEY_MODEL_TITLE, "Severity", KEY_CATEGORY, KEY_OBJECT_TYPE, "Table", "Column",
    "Message", "SAP PD Value", "erwin Value", "Repaired by preprocessing?",
]


def _build_as_imported(wb: Workbook, results: List) -> None:
    ws = wb.create_sheet("AS_IMPORTED")
    ws.freeze_panes = "A2"
    _header_row(ws, AS_IMPORTED_HEADERS, 1)

    row = 2
    for result in results:
        row = _populate_as_imported_rows(ws, result, row)

    _set_col_widths(ws, [30, 20, 26, 12, 12, 12, 15, 12, 14, 14, 10, 10, 20])
    ws.auto_filter.ref = f"A1:{get_column_letter(len(AS_IMPORTED_HEADERS))}{max(row - 1, 1)}"

def _populate_as_imported_rows(ws, result, row):
    model_label = os.path.basename(result.pd_file)
    models = _models_for(result)
    for source in ["pd", KEY_ERWIN_NAME]:
        row = _write_imported_source(ws, models[source], model_label, source, row)
    return row

def _write_imported_source(ws, m_data, model_label, source, row):
    tables = m_data.get(KEY_TABLES) or {}
    tables_list = tables.values() if isinstance(tables, dict) else tables
    for table in sorted(tables_list, key=lambda t: t.get(KEY_NAME, "").lower()):
        row = _write_single_imported_table(ws, table, model_label, source, row)
    return row

def _write_single_imported_table(ws, table, model_label, source, row):
    cols = table.get(KEY_COLUMNS, []) or []
    _data_row(ws, [
        model_label, "SAP PD" if source == "pd" else "erwin",
        table.get(KEY_CODE, ""), len(cols),
        _pk_columns(table), len(table.get(KEY_KEYS, []) or []),
        len(table.get(KEY_INDEXES, []) or []),
        len(table.get("incoming_references", []) or []),
        len(table.get("outgoing_references", []) or []),
        sum(1 for c in cols if c.get("datatype")),
        sum(1 for c in cols if c.get("default")),
        table.get("comment", "")[:100] if table.get("comment") else ""
    ], row, alt=(row % 2 == 0))
    if source == KEY_ERWIN_NAME:
        ws.cell(row=row, column=2).font = Font(color=C_BLUE)
    return row + 1

def _build_table_matrix(wb: Workbook, results: List) -> None:
    ws = wb.create_sheet("TABLE_MATRIX")
    ws.freeze_panes = "A2"
    _header_row(ws, TABLE_HEADERS, 1)

    row = 2
    for result in results:
        row = _populate_table_matrix_rows(ws, result, row)

    _set_col_widths(ws, [30, 20, 34, 34, 15, 15, 15, 12, 12, 18, 40, 40, 12, 12, 14, 14, 10, 10, 9])
    ws.auto_filter.ref = f"A1:{get_column_letter(len(TABLE_HEADERS))}{max(row - 1, 1)}"


def _group_findings_by_table(result):
    all_findings = defaultdict(Counter)
    for f in result.findings:
        if getattr(f, "table", None):
            all_findings[f.table][f.severity] += 1
    return all_findings

def _populate_table_matrix_rows(ws, result, row):
    model_label = os.path.basename(result.pd_file)
    models = _models_for(result)
    
    tables_by_code = defaultdict(dict)
    for model_name, m_data in models.items():
        if m_data and "tables" in m_data:
            for t in (m_data["tables"].values() if isinstance(m_data["tables"], dict) else m_data["tables"]):
                tables_by_code[t.get(KEY_CODE, "")][model_name] = t

    all_findings = _group_findings_by_table(result)

    for code in sorted(tables_by_code.keys()):
        pd_table = tables_by_code[code].get("pd")
        er_table = tables_by_code[code].get(KEY_ERWIN_NAME)
        row = _write_matrix_row(ws, pd_table, er_table, code, all_findings, model_label, row)
    return row

def _write_matrix_row(ws, pd_table, er_table, code, all_findings, model_label, row):
    status, pd_columns, er_columns, pd_codes, er_codes, matched = _calculate_table_stats(pd_table, er_table)
    counts = all_findings.get(code, Counter())

    pd_t = pd_table or {}
    er_t = er_table or {}
    _data_row(ws, [
        model_label, status, pd_t.get(KEY_CODE, "") or ("" if pd_table is None else code),
        er_t.get(KEY_CODE, "") or ("" if er_table is None else code),
        len(pd_columns), len(er_columns), matched, len(pd_codes - er_codes), len(er_codes - pd_codes),
        len(er_columns) - len(er_codes), _pk_columns(pd_t), _pk_columns(er_t),
        len(pd_t.get(KEY_KEYS, []) or []), len(er_t.get(KEY_KEYS, []) or []),
        len(pd_t.get(KEY_INDEXES, []) or []), len(er_t.get(KEY_INDEXES, []) or []),
        counts.get(KEY_CRITICAL, 0), counts.get(KEY_WARNING, 0), counts.get(KEY_INFO, 0),
    ], row, alt=(row % 2 == 0))

    status_cell = ws.cell(row=row, column=2)
    status_cell.fill, status_cell.font, status_cell.alignment = RECON_FILL.get(status, PatternFill()), Font(bold=True, color=C_WHITE if status == KEY_MISSING_IN_ERWIN else C_BLACK), CENTER
    if counts.get(KEY_CRITICAL):
        ws.cell(row=row, column=_MATRIX_CRITICAL_COLUMN).font = Font(bold=True, color=C_RED)
    return row + 1

def _calculate_table_stats(pd_table, er_table):
    if pd_table and er_table:
        status = KEY_MATCHED
    elif pd_table:
        status = KEY_MISSING_IN_ERWIN
    else:
        status = KEY_EXTRA_IN_ERWIN

    pd_columns = (pd_table or {}).get(KEY_COLUMNS, []) or []
    er_columns = (er_table or {}).get(KEY_COLUMNS, []) or []
    pd_codes = {c.get(KEY_CODE) for c in pd_columns}
    er_codes = {c.get(KEY_CODE) for c in er_columns}
    matched = len(pd_codes & er_codes)
    return status, pd_columns, er_columns, pd_codes, er_codes, matched

def _build_relationships(wb: Workbook, results: List) -> None:
    ws = wb.create_sheet("RELATIONSHIPS")
    ws.freeze_panes = "A2"
    _header_row(ws, RELATIONSHIP_HEADERS, 1)

    row = 2
    for result in results:
        row = _populate_relationships_rows(ws, result, row)

    _set_col_widths(ws, [30, 20, 44, 30, 30, 46, 46])
    ws.auto_filter.ref = f"A1:{get_column_letter(len(RELATIONSHIP_HEADERS))}{max(row - 1, 1)}"

def _populate_relationships_rows(ws, result, row):
    model_label = os.path.basename(result.pd_file)
    models = _models_for(result)
    pd_refs = {_fk_signature(r): r for r in (models["pd"].get(KEY_REFERENCES, {}).values() if isinstance(models["pd"].get(KEY_REFERENCES), dict) else (models["pd"].get(KEY_REFERENCES) or []))}
    er_refs = {_fk_signature(r): r for r in (models[KEY_ERWIN_NAME].get(KEY_REFERENCES, {}).values() if isinstance(models[KEY_ERWIN_NAME].get(KEY_REFERENCES), dict) else (models[KEY_ERWIN_NAME].get(KEY_REFERENCES) or []))}

    pd_by_pair = _group_references_by_pair(pd_refs, er_refs)
    er_by_pair = _group_references_by_pair(er_refs, pd_refs)
    
    rows = _compute_relationship_rows(pd_refs, er_refs, pd_by_pair, er_by_pair)

    for status, pd_ref, er_ref in rows:
        source = pd_ref or er_ref or {}
        _data_row(ws, [
            model_label, status, f"{source.get(KEY_PARENT_TABLE, '?')} -> {source.get(KEY_CHILD_TABLE, '?')}",
            (pd_ref or {}).get(KEY_NAME, ""), (er_ref or {}).get(KEY_NAME, ""),
            _join_text(pd_ref) if pd_ref else "", _join_text(er_ref) if er_ref else "",
        ], row, alt=(row % 2 == 0))

        status_cell = ws.cell(row=row, column=2)
        status_cell.fill, status_cell.font, status_cell.alignment = RECON_FILL.get(status, PatternFill()), Font(bold=True, color=C_WHITE if status == KEY_MISSING_IN_ERWIN else C_BLACK), CENTER
        row += 1
    return row

def _group_references_by_pair(source_refs, exclude_refs):
    grouped = defaultdict(list)
    for signature, reference in source_refs.items():
        if signature not in exclude_refs:
            grouped[(_key(reference.get(KEY_PARENT_TABLE)), _key(reference.get(KEY_CHILD_TABLE)))].append(reference)
    return grouped

def _compute_relationship_rows(pd_refs, er_refs, pd_by_pair, er_by_pair):
    rows = []
    for signature in sorted(set(pd_refs) & set(er_refs)):
        rows.append((KEY_MATCHED, pd_refs[signature], er_refs[signature]))

    for pair in sorted(set(pd_by_pair) | set(er_by_pair)):
        unmatched_pd = list(pd_by_pair.get(pair, []))
        unmatched_er = list(er_by_pair.get(pair, []))
        while unmatched_pd and unmatched_er:
            rows.append(("JOIN_CHANGED", unmatched_pd.pop(0), unmatched_er.pop(0)))
        rows.extend((KEY_MISSING_IN_ERWIN, reference, None) for reference in unmatched_pd)
        rows.extend((KEY_EXTRA_IN_ERWIN, None, reference) for reference in unmatched_er)
    return rows

def _build_category_analysis(wb: Workbook, results: List) -> None:
    ws = wb.create_sheet("CATEGORY_ANALYSIS")
    ws.freeze_panes = "A2"

    tally: Dict[str, Counter] = defaultdict(Counter)
    models_affected: Dict[str, set] = defaultdict(set)

    for result in results:
        for finding in result.findings:
            tally[finding.category][finding.severity] += 1
            models_affected[finding.category].add(result.pd_file)

    _header_row(ws, [KEY_CATEGORY, KEY_CRITICAL, KEY_WARNING, KEY_INFO, "Total", "Models Affected"], 1)

    ordered = sorted(tally.items(),
                     key=lambda item: (-sum(item[1].values()), item[0]))

    for offset, (category, counts) in enumerate(ordered, start=1):
        row = offset + 1
        _data_row(ws, [
            category, counts.get(KEY_CRITICAL, 0), counts.get(KEY_WARNING, 0),
            counts.get(KEY_INFO, 0), sum(counts.values()),
            len(models_affected[category]),
        ], row, alt=(offset % 2 == 0))
        if counts.get(KEY_CRITICAL, 0):
            ws.cell(row=row, column=2).font = Font(bold=True, color=C_RED)

    _set_col_widths(ws, [28, 12, 12, 10, 10, 18])
    if ordered:
        ws.auto_filter.ref = f"A1:F{len(ordered) + 1}"


# ─── DOCUMENTATION SHEET ──────────────────────────────────────────────────────

DESCRIPTION_HEADERS = [
    KEY_MODEL_TITLE, KEY_OBJECT_TYPE, "Object", "Code",
    "Mapping", "Source Field", "Target Field",
    "SAP PD Value (source)", "erwin Value (target)",
    KEY_STATUS_TITLE, "Similarity %",
]


def _documentation_rows(results: List) -> List:
    rows: List = []
    for result in results:
        cached = getattr(result, "documentation_rows", None)
        if cached:
            rows.extend(cached)
            continue
        rows.extend(pdm_documentation.build_rows(
            result.pd_file, result.erwin_file, result.pd_model))
    return rows


def _build_documentation(wb: Workbook, results: List) -> None:
    """
    Side-by-side documentation mapping for every object.

    Unlike FINDINGS, this sheet lists MATCHED rows too — a findings list only
    ever shows what went wrong, so there was no way to tell an object whose
    documentation was verified identical from one that was never checked.
    """
    rows = _documentation_rows(results)
    ws = wb.create_sheet("DESCRIPTION")

    if not rows:
        ws.cell(row=1, column=1,
                value="No documentation rows were produced for this run.")
        _set_col_widths(ws, [70])
        return

    # ── Summary block ────────────────────────────────────────────────────────
    summary = pdm_documentation.summarise(rows)
    _header_row(ws, ["Mapping", KEY_MATCHED, KEY_MISMATCH, "MISSING IN ERWIN",
                     "MISSING IN SAP PD", "BOTH EMPTY", "Total"], 1)
    line = 2
    for mapping, counts in sorted(summary.items()):
        _data_row(ws, [
            mapping,
            counts.get(KEY_MATCHED, 0),
            counts.get(KEY_MISMATCH, 0),
            counts.get(KEY_MISSING_IN_ERWIN, 0),
            counts.get(KEY_MISSING_IN_SAP_PD, 0),
            counts.get("BOTH_EMPTY", 0),
            sum(counts.values()),
        ], line)
        line += 1

    # ── Detail block ─────────────────────────────────────────────────────────
    line += 1
    detail_header = line
    _header_row(ws, DESCRIPTION_HEADERS, detail_header)
    line += 1

    # Problems first: a reviewer should not have to scroll past matches.
    ordered_rows = sorted(
        rows,
        key=lambda r: (0 if r.status in (KEY_MISSING_IN_ERWIN, KEY_MISMATCH) else
                       1 if r.status == KEY_MISSING_IN_SAP_PD else
                       2 if r.status == KEY_MATCHED else 3,
                       r.model, r.object_type, r.object_name, r.mapping),
    )

    for index, row_data in enumerate(ordered_rows):
        _data_row(ws, [
            row_data.model, row_data.object_type, row_data.object_name,
            row_data.object_code, row_data.mapping,
            row_data.source_field, row_data.target_field,
            row_data.source_value, row_data.target_value,
            row_data.status,
            row_data.similarity if row_data.status == KEY_MISMATCH else "",
        ], line, alt=bool(index % 2))
        fill = DOC_STATUS_FILL.get(row_data.status)
        if fill is not None:
            ws.cell(row=line, column=10).fill = fill
        line += 1

    _set_col_widths(ws, [22, 12, 38, 24, 24, 20, 20, 60, 60, 18, 12])
    ws.freeze_panes = ws.cell(row=detail_header + 1, column=1)
    ws.auto_filter.ref = (f"A{detail_header}:"
                          f"{get_column_letter(len(DESCRIPTION_HEADERS))}"
                          f"{max(line - 1, detail_header)}")


# ─── CONFIG SHEET ─────────────────────────────────────────────────────────────

def _config_as_dict(config) -> Dict[str, Any]:
    """Every public setting the validator's config module exposes."""
    if config is None:
        return {}
    return {name: getattr(config, name) for name in dir(config)
            if name.isupper() and not name.startswith("_")}


def _build_config_sheet(wb: Workbook, config) -> None:
    ws = wb.create_sheet("CONFIG")
    ws.freeze_panes = "A2"
    _header_row(ws, ["Setting", "Value"], 1)

    for offset, (key, value) in enumerate(sorted(_config_as_dict(config).items()),
                                          start=1):
        row = offset + 1
        if isinstance(value, (dict, list, tuple, set)):
            rendered = json.dumps(value if not isinstance(value, set)
                                  else sorted(value), default=str)
        else:
            rendered = str(value)
        _data_row(ws, [key, rendered], row, alt=(offset % 2 == 0))
        ws.cell(row=row, column=1).font = Font(bold=True)

    _set_col_widths(ws, [40, 90])


# ─── PER-MODEL SHEET ──────────────────────────────────────────────────────────

MODEL_SHEET_HEADERS = [
    "#", "Severity", KEY_CATEGORY, KEY_OBJECT_TYPE, "Table", "Column",
    "Message", "SAP PD Value", "erwin Value", "Recommended Action",
]


def _build_model_sheet(wb: Workbook, result, config) -> None:
    base = os.path.splitext(os.path.basename(result.pd_file))[0]
    ws = wb.create_sheet(_unique_sheet_name(wb, base))
    ws.freeze_panes = "A3"

    ws.merge_cells("A1:I1")
    title = ws["A1"]
    stage = getattr(result, KEY_STAGE, "")
    title.value = (
        f"{os.path.basename(result.pd_file)}  vs  {os.path.basename(result.erwin_file)}"
        f"   |   {result.status}"
        f"   |   fidelity {result.fidelity_score:.2f}%"
        f"   |   {result.tables_matched}/{result.tables_pd} tables matched"
        f"   |   {result.columns_matched}/{result.columns_pd} columns matched"
        f"   |   {result.fk_matched}/{result.fk_pd} foreign keys matched"
        f"   |   {result.critical_count} critical / {result.warning_count} warning "
        f"/ {result.info_count} info"
        + (f"   |   stage {stage}" if stage else "")
    )
    title.font      = Font(bold=True, color=C_WHITE, size=10)
    title.fill      = STATUS_FILL.get(result.status, HEADER_FILL)
    title.alignment = LEFT
    ws.row_dimensions[1].height = 20

    _header_row(ws, MODEL_SHEET_HEADERS, 2, fill=SUBHDR_FILL)

    findings = sorted(_limited(result.findings, config),
                      key=lambda f: (SEVERITY_ORDER.get(f.severity, 3),
                                     f.category, f.table, f.column))

    for index, finding in enumerate(findings, start=1):
        row = index + 2
        _data_row(ws, [
            index, finding.severity, finding.category, getattr(finding, "object_type", ""), finding.table,
            finding.column, finding.message, finding.pd_value,
            finding.erwin_value, REMEDIATION.get(finding.category, ""),
        ], row, alt=(index % 2 == 0))
        _paint_severity(ws, row, 2, finding.severity)

    _set_col_widths(ws, [5, 11, 20, 15, 30, 26, 62, 34, 34, 56])

    if findings:
        ws.auto_filter.ref = f"A2:I{len(findings) + 2}"

    cap = _cfg(config, KEY_MAX_DIFF_ROWS, 500)
    if cap and len(result.findings) > cap:
        note_row = len(findings) + 4
        ws.cell(note_row, 1,
                f"{len(result.findings) - cap} further findings are not shown here — "
                f"the FINDINGS sheet holds the complete list."
                ).font = Font(italic=True, color=C_DARKRED)


# ─── MACHINE-READABLE EXPORTS ─────────────────────────────────────────────────

def _export_findings_csv(results: List, output_dir: str, config) -> str:
    path = os.path.join(output_dir, "findings.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(FINDINGS_HEADERS)
        for result in results:
            model_label = os.path.basename(result.pd_file)
            for finding in result.findings:
                writer.writerow([
                    model_label, result.status, finding.severity,
                    finding.category, finding.table, finding.column,
                    finding.message, finding.pd_value, finding.erwin_value,
                    REMEDIATION.get(finding.category, ""),
                ])
    return path


def _export_json_summary(results: List, output_dir: str) -> str:
    path = os.path.join(output_dir, "validation_summary.json")
    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "model_type": "PDM",
        "models": len(results),
        "average_fidelity": (
            round(sum(r.fidelity_score for r in results) / len(results), 2)
            if results else 0.0),
        "status_counts": dict(Counter(r.status for r in results)),
        KEY_PROMOTED: sum(1 for r in results if getattr(r, KEY_PROMOTED, False)),
        "results": [{
            "pd_file": r.pd_file,
            "erwin_file": r.erwin_file,
            "pd_model": r.pd_model,
            "status": r.status,
            "fidelity_score": r.fidelity_score,
            KEY_STAGE: getattr(r, KEY_STAGE, ""),
            KEY_PROMOTED: bool(getattr(r, KEY_PROMOTED, False)),
            KEY_TABLES: {"pd": r.tables_pd, KEY_ERWIN_NAME: r.tables_erwin,
                       KEY_LOWER_MATCHED: r.tables_matched,
                       KEY_LOWER_MISSING: r.tables_missing_in_erwin,
                       KEY_LOWER_EXTRA: r.tables_extra_in_erwin},
            KEY_COLUMNS: {"pd": r.columns_pd, KEY_ERWIN_NAME: r.columns_erwin,
                        KEY_LOWER_MATCHED: r.columns_matched,
                        KEY_LOWER_MISSING: r.columns_missing_in_erwin,
                        KEY_LOWER_EXTRA: r.columns_extra_in_erwin},
            "foreign_keys": {"pd": r.fk_pd, KEY_ERWIN_NAME: r.fk_erwin,
                             KEY_LOWER_MATCHED: r.fk_matched,
                             KEY_LOWER_MISSING: r.fk_missing_in_erwin,
                             KEY_LOWER_EXTRA: r.fk_extra_in_erwin},
            "findings": {"critical": r.critical_count,
                         "warning": r.warning_count,
                         "info": r.info_count},
        } for r in results],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
    return path


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

# Above this many models the workbook skips per-model sheets (Excel slows to a
# crawl well before its 255-sheet practical limit).
MAX_PER_MODEL_SHEETS = 40


def generate_report(results: List, output_dir: str,
                    filename: str = "pdm_validation_report.xlsx",
                    export_csv: bool = False,
                    export_json: bool = False) -> str:
    """
    Write the PDM workbook and return its path.

    `results` are the PDM validator's ValidationResult objects; anything
    pdm_flow attached to them (stage, promoted, flow_notes, source_models,
    documentation_rows) enriches the report and is optional everywhere.
    """
    os.makedirs(output_dir, exist_ok=True)

    try:
        from . import pdm_validator_bridge as bridge
        config = bridge.get_config()
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("PDM validator config unavailable (%s); "
                       "falling back to report defaults", exc)
        config = None

    workbook = Workbook()
    _build_summary(workbook, results, config)
    _build_dashboard(workbook, results, config)
    _build_findings(workbook, results, config)
    _build_as_imported(workbook, results)

    _build_table_matrix(workbook, results)
    _build_relationships(workbook, results)
    _build_category_analysis(workbook, results)
    _build_documentation(workbook, results)
    _build_config_sheet(workbook, config)

    if len(results) <= MAX_PER_MODEL_SHEETS:
        for result in results:
            _build_model_sheet(workbook, result, config)
    else:
        logger.info("%d models: per-model sheets skipped, see FINDINGS.",
                    len(results))

    path = os.path.join(output_dir, filename)
    workbook.save(path)
    logger.info("PDM report saved → %s", path)

    if export_csv:
        try:
            _export_findings_csv(results, output_dir, config)
        except Exception as exc:                               # noqa: BLE001
            logger.warning("Could not write findings.csv: %s", exc)
    if export_json:
        try:
            _export_json_summary(results, output_dir)
        except Exception as exc:                               # noqa: BLE001
            logger.warning("Could not write validation_summary.json: %s", exc)

    return path