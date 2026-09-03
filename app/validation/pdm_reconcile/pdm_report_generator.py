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
    "PASS":  PatternFill("solid", fgColor=C_GREEN),
    "WARN":  PatternFill("solid", fgColor=C_YELLOW),
    "FAIL":  PatternFill("solid", fgColor=C_RED),
    "ERROR": PatternFill("solid", fgColor=C_DARKRED),
}

SEV_FILL = {
    "CRITICAL": PatternFill("solid", fgColor=C_RED),
    "WARNING":  PatternFill("solid", fgColor=C_YELLOW),
    "INFO":     PatternFill("solid", fgColor=C_BLUE),
    "VERIFIED": PatternFill("solid", fgColor=C_GREEN),
}

SEV_FONT = {
    "CRITICAL": Font(bold=True, color=C_WHITE),
    "WARNING":  Font(bold=True, color=C_BLACK),
    "INFO":     Font(color=C_WHITE),
    "VERIFIED": Font(bold=True, color="FF000000"),
}

RECON_FILL = {
    "MATCHED":          PatternFill("solid", fgColor=C_GREEN),
    "MISSING_IN_ERWIN": PatternFill("solid", fgColor=C_RED),
    "EXTRA_IN_ERWIN":   PatternFill("solid", fgColor=C_YELLOW),
    "JOIN_CHANGED":     PatternFill("solid", fgColor=C_YELLOW),
}

DOC_STATUS_FILL = {
    "MATCHED":           PatternFill("solid", fgColor="C6EFCE"),
    "MISMATCH":          PatternFill("solid", fgColor="FFC7CE"),
    "MISSING_IN_ERWIN":  PatternFill("solid", fgColor="FFC7CE"),
    "MISSING_IN_SAP_PD": PatternFill("solid", fgColor="FFEB9C"),
    "BOTH_EMPTY":        PatternFill("solid", fgColor="F2F2F2"),
}

THIN        = Side(style="thin", color="FFB8B8B8")
BORDER      = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEADER_FONT = Font(bold=True, color=C_WHITE, name=REPORT_FONT, size=10)
HEADER_FILL = PatternFill("solid", fgColor=C_HEADER)
SUBHDR_FILL = PatternFill("solid", fgColor=C_SUBHDR)
CENTER      = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT        = Alignment(horizontal="left",   vertical="center", wrap_text=True)

SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}

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
    fill = PatternFill("solid", fgColor=C_ALT) if alt else None
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
                     color=C_WHITE if status in ("FAIL", "ERROR") else C_BLACK)
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
    cap = _cfg(config, "MAX_DIFF_ROWS_PER_MODEL", 500)
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
                "erwin": bridge.parse_erwin(result.erwin_file)}
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("Could not parse models for %s: %s", result.pd_file, exc)
        return {"pd": {"tables": {}, "references": []},
                "erwin": {"tables": {}, "references": []}}


def _pk_columns(table: Dict[str, Any]) -> str:
    for key in table.get("keys", []) or []:
        if key.get("is_pk"):
            return ", ".join(key.get("columns", []) or []) or "(no members)"
    return ""


def _join_text(reference: Dict[str, Any]) -> str:
    joins = reference.get("join_columns", []) or []
    if not joins:
        return "(no join columns)"
    return ", ".join(f"{j.get('parent_col', '?')} → {j.get('child_col', '?')}"
                     for j in joins)


def _fk_signature(reference: Dict[str, Any]) -> str:
    """Mirrors the validator comparator's own signature, so the sheet agrees."""
    parent = _key(reference.get("parent_table", ""))
    child = _key(reference.get("child_table", ""))
    joins = tuple(sorted(
        (_key(j.get("parent_col", "")), _key(j.get("child_col", "")))
        for j in reference.get("join_columns", []) or []))
    return f"{parent}→{child}:{joins}"


# ─── SUMMARY SHEET ────────────────────────────────────────────────────────────

SUMMARY_HEADERS = [
    "#", "SAP PD File", "erwin File", "SAP PD Model", "erwin Model",
    "Status", "Fidelity %", "Review?",
    "Tables (SAP PD)", "Tables (erwin)", "Tables Matched",
    "Tbl Missing", "Tbl Extra",
    "Columns (SAP PD)", "Columns (erwin)", "Columns Matched",
    "Col Missing", "Col Extra",
    "FKs (SAP PD)", "FKs (erwin)", "FKs Matched",
    "FK Missing", "FK Extra",
    "Keys (SAP PD)", "Keys (erwin)",
    "Indexes (SAP PD)", "Indexes (erwin)",
    "CRITICAL", "WARNING", "INFO",
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


def _build_summary(wb: Workbook, results: List, config) -> None:
    ws = wb.active
    ws.title = "SUMMARY"
    ws.freeze_panes = "C3"
    ws.row_dimensions[1].height = 22
    ws.row_dimensions[2].height = 32

    last_column = get_column_letter(len(SUMMARY_HEADERS))
    ws.merge_cells(f"A1:{last_column}1")
    banner = ws["A1"]
    banner.value = ("SAP PD (PowerDesigner PDM) → erwin  |  Physical Model Validation Report"
                    f"   |   generated {datetime.now():%Y-%m-%d %H:%M}")
    banner.font      = Font(bold=True, color=C_WHITE, size=14, name=REPORT_FONT)
    banner.fill      = PatternFill("solid", fgColor=C_HEADER)
    banner.alignment = CENTER

    _header_row(ws, SUMMARY_HEADERS, 2)

    review_threshold = _cfg(config, "FIDELITY_REVIEW_THRESHOLD", 90.0)

    for index, result in enumerate(results, start=1):
        row = index + 2
        _data_row(ws, [
            index,
            os.path.basename(result.pd_file),
            os.path.basename(result.erwin_file),
            result.pd_model,
            result.erwin_model,
            result.status,
            result.fidelity_score,
            "YES" if result.needs_review else "",
            result.tables_pd, result.tables_erwin, result.tables_matched,
            result.tables_missing_in_erwin, result.tables_extra_in_erwin,
            result.columns_pd, result.columns_erwin, result.columns_matched,
            result.columns_missing_in_erwin, result.columns_extra_in_erwin,
            result.fk_pd, result.fk_erwin, result.fk_matched,
            result.fk_missing_in_erwin, result.fk_extra_in_erwin,
            getattr(result, "keys_pd", 0), getattr(result, "keys_erwin", 0),
            getattr(result, "indexes_pd", 0), getattr(result, "indexes_erwin", 0),
            result.critical_count, result.warning_count, result.info_count,
            getattr(result, "tables_duplicate_pd", 0),
            getattr(result, "tables_duplicate_erwin", 0),
            getattr(result, "columns_duplicate_pd", 0),
            getattr(result, "columns_duplicate_erwin", 0),
            getattr(result, "fk_duplicate_pd", 0),
            getattr(result, "fk_duplicate_erwin", 0),
            "YES" if getattr(result, "counts_reconcile", lambda: True)() else "NO",
        ], row, alt=(index % 2 == 0))

        _paint_status(ws, row, _COL_STATUS, result.status)

        fidelity_cell = ws.cell(row=row, column=_COL_FIDELITY)
        fidelity_cell.number_format = "0.00"
        fidelity_cell.alignment = CENTER
        if result.fidelity_score < 90:
            fidelity_cell.font = Font(bold=True, color=C_DARKRED)
        elif result.fidelity_score < review_threshold:
            fidelity_cell.font = Font(bold=True, color="FFBF8F00")

        if result.needs_review:
            review_cell = ws.cell(row=row, column=_COL_REVIEW)
            review_cell.font      = Font(bold=True, color=C_DARKRED)
            review_cell.alignment = CENTER

        # These used to paint columns 26/27 ("Indexes (SAP PD)" / "Indexes
        # (erwin)"), so a model with CRITICAL findings had its index count
        # highlighted in red while the CRITICAL cell stayed plain.
        if result.critical_count:
            ws.cell(row=row, column=_COL_CRITICAL).font = Font(bold=True, color=C_RED)
        if result.warning_count:
            ws.cell(row=row, column=_COL_WARNING).font = Font(bold=True, color=C_DARKRED)

        reconcile_cell = ws.cell(row=row, column=_COL_RECONCILE)
        reconcile_cell.alignment = CENTER
        if reconcile_cell.value == "NO":
            reconcile_cell.font = Font(bold=True, color=C_WHITE)
            reconcile_cell.fill = PatternFill("solid", fgColor=C_RED)

    # ── Totals row ───────────────────────────────────────────────────────────
    total_row = len(results) + 3
    ws.cell(total_row, 1, "TOTAL").font = Font(bold=True)
    for column, attribute in _SUMMARY_TOTAL_COLUMNS.items():
        total = sum(getattr(r, attribute, 0) for r in results)
        cell = ws.cell(total_row, column, total)
        cell.font   = Font(bold=True, color=C_RED if column == 28 else C_BLACK)
        cell.fill   = PatternFill("solid", fgColor=C_GRAY)
        cell.border = BORDER

    if results:
        average = round(sum(r.fidelity_score for r in results) / len(results), 2)
        cell = ws.cell(total_row, 7, average)
        cell.font          = Font(bold=True)
        cell.number_format = "0.00"
        cell.fill          = PatternFill("solid", fgColor=C_GRAY)

    _set_col_widths(ws, [5, 32, 32, 24, 24, 9, 11, 9]
                    + [13] * (len(SUMMARY_HEADERS) - 9) + [17])
    ws.auto_filter.ref = f"A2:{last_column}{len(results) + 2}"

    if results:
        ws.conditional_formatting.add(
            f"G3:G{len(results) + 2}",
            DataBarRule(start_type="num", start_value=0,
                        end_type="num", end_value=100,
                        color="FF63BE7B", showValue=True),
        )


# ─── DASHBOARD SHEET ──────────────────────────────────────────────────────────

def _build_dashboard(wb: Workbook, results: List, config) -> None:
    ws = wb.create_sheet("DASHBOARD")

    ws.merge_cells("A1:F1")
    banner = ws["A1"]
    banner.value     = "PDM Migration Reconciliation Dashboard"
    banner.font      = Font(bold=True, color=C_WHITE, size=14, name=REPORT_FONT)
    banner.fill      = PatternFill("solid", fgColor=C_HEADER)
    banner.alignment = CENTER
    ws.row_dimensions[1].height = 22

    status_counts = Counter(r.status for r in results)
    total_models  = len(results)
    average_score = (round(sum(r.fidelity_score for r in results) / total_models, 2)
                     if total_models else 0.0)
    promoted = sum(1 for r in results if getattr(r, "promoted", False))

    _header_row(ws, ["Run Statistic", "Value"], 3, fill=SUBHDR_FILL)
    statistics = [
        ("Models validated",        total_models),
        ("PASS",                    status_counts.get("PASS", 0)),
        ("WARN",                    status_counts.get("WARN", 0)),
        ("FAIL",                    status_counts.get("FAIL", 0)),
        ("ERROR",                   status_counts.get("ERROR", 0)),
        ("Average fidelity score",  average_score),
        ("Models needing review",   sum(1 for r in results if r.needs_review)),
        ("Promoted to 3_final",     promoted),
        ("Held for review",         total_models - promoted),
        ("Total findings",          sum(len(r.findings) for r in results)),
        ("CRITICAL findings",       sum(r.critical_count for r in results)),
        ("WARNING findings",        sum(r.warning_count for r in results)),
        ("INFO findings",           sum(r.info_count for r in results)),
        ("Tables compared",         sum(r.tables_pd for r in results)),
        ("Columns compared",        sum(r.columns_pd for r in results)),
        ("Foreign keys compared",   sum(r.fk_pd for r in results)),
    ]
    for offset, (label, value) in enumerate(statistics):
        row = 4 + offset
        _data_row(ws, [label, value], row, alt=(offset % 2 == 1))
        ws.cell(row=row, column=1).font = Font(bold=True)
        if label in STATUS_FILL:
            _paint_status(ws, row, 2, label)

    # ── Promotion outcome, the PDM-specific gate ─────────────────────────────
    promo_start = 4 + len(statistics) + 2
    ws.cell(promo_start - 1, 1, "Promotion outcome").font = Font(bold=True, size=11)
    _header_row(ws, ["Model", "Stage", "Promoted?", "Fidelity %", "Notes"],
                promo_start, fill=SUBHDR_FILL)
    for offset, result in enumerate(results, start=1):
        row = promo_start + offset
        _data_row(ws, [
            os.path.basename(result.pd_file),
            getattr(result, "stage", ""),
            "YES" if getattr(result, "promoted", False) else "NO",
            result.fidelity_score,
            getattr(result, "flow_notes", ""),
        ], row, alt=(offset % 2 == 0))
        ws.cell(row=row, column=3).font = Font(
            bold=True,
            color=C_BLACK if getattr(result, "promoted", False) else C_DARKRED)
        ws.cell(row=row, column=4).number_format = "0.00"

    # ── Lowest-fidelity models ───────────────────────────────────────────────
    worst_start = promo_start + len(results) + 3
    ws.cell(worst_start - 1, 1, "Lowest-fidelity models").font = Font(bold=True, size=11)
    _header_row(ws, ["Model", "Status", "Fidelity %", "CRITICAL", "WARNING"],
                worst_start, fill=SUBHDR_FILL)
    for offset, result in enumerate(sorted(results,
                                           key=lambda r: r.fidelity_score)[:15],
                                    start=1):
        row = worst_start + offset
        # Nine values used to be written under five headers, so the CRITICAL
        # and WARNING columns of this block showed the model's key counts.
        _data_row(ws, [
            os.path.basename(result.pd_file), result.status,
            result.fidelity_score,
            result.critical_count, result.warning_count,
        ], row, alt=(offset % 2 == 0))
        _paint_status(ws, row, 2, result.status)
        ws.cell(row=row, column=3).number_format = "0.00"

    _set_col_widths(ws, [34, 18, 13, 13, 60, 11])

    # ── Severity chart ───────────────────────────────────────────────────────
    chart_anchor_row = 4
    ws.cell(chart_anchor_row - 1, 8, "Findings by severity").font = Font(bold=True)
    severity_rows = [
        ("CRITICAL", sum(r.critical_count for r in results)),
        ("WARNING",  sum(r.warning_count for r in results)),
        ("INFO",     sum(r.info_count for r in results)),
        ("VERIFIED", sum(1 for r in results for f in r.findings if f.severity == "VERIFIED")),
    ]
    for offset, (label, value) in enumerate(severity_rows):
        ws.cell(chart_anchor_row + offset, 8, label)
        ws.cell(chart_anchor_row + offset, 9, value)

    try:
        chart = BarChart()
        chart.type   = "col"
        chart.title  = "Findings by severity"
        chart.y_axis.title = "Findings"
        chart.height = 7
        chart.width  = 13
        data       = Reference(ws, min_col=9, min_row=chart_anchor_row,
                               max_row=chart_anchor_row + len(severity_rows) - 1)
        categories = Reference(ws, min_col=8, min_row=chart_anchor_row,
                               max_row=chart_anchor_row + len(severity_rows) - 1)
        chart.add_data(data, titles_from_data=False)
        chart.set_categories(categories)
        chart.legend = None
        ws.add_chart(chart, "K3")
    except Exception as exc:                                   # pragma: no cover
        logger.warning("Could not render dashboard chart: %s", exc)


# ─── FINDINGS SHEET ───────────────────────────────────────────────────────────

FINDINGS_HEADERS = [
    "Model", "Status", "Severity", "Category", "Object Type", "Table", "Column",
    "Message", "SAP PD Value", "erwin Value", "Recommended Action", "Manual Review",
]


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

    cap = _cfg(config, "MAX_DIFF_ROWS_PER_MODEL", 500)
    omitted = 0
    if cap and cap > 0 and len(ordered) > cap:
        must_keep = [f for f in ordered
                     if f.severity in ("CRITICAL", "WARNING", "ERROR")]
        context   = [f for f in ordered
                     if f.severity not in ("CRITICAL", "WARNING", "ERROR")]
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
        REMEDIATION.get(finding.category, "") if finding.severity != "VERIFIED" else "No action required.",
    ] for finding in findings]

    if omitted:
        rows.append([
            model_label, result.status, "INFO", "REPORT_TRUNCATED", "MODEL",
            "", "",
            f"{omitted} context row(s) (VERIFIED / INFO) omitted to keep this "
            f"sheet under MAX_DIFF_ROWS_PER_MODEL={cap}. Every CRITICAL and "
            f"WARNING finding is listed above; raise the cap in "
            f"app/config/validation_config.py to see the full census.",
            str(len(result.findings)), str(len(findings)),
            "No action required — no defect was hidden.",
        ])

    return rows


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
    "Model", "Severity", "Category", "Object Type", "Table", "Column",
    "Message", "SAP PD Value", "erwin Value", "Repaired by preprocessing?",
]


def _build_as_imported(wb: Workbook, results: List, config) -> None:
    """
    What the RAW erwin import lost, before preprocessing repaired it.

    Every other sheet in this workbook describes the FINAL result — the model
    after the PowerDesigner-driven remediation has run. That is the right thing
    to gate promotion on, but it meant the defects the migration actually
    caused were reported nowhere: a model that imported into erwin with seven
    dropped columns and two emptied primary keys produced a workbook showing a
    clean primary key on every table, because preprocessing had put them back
    in the XML a moment earlier. Anyone asking "was this column migrated, and
    if so why can I not see it in erwin?" had nothing in the report to answer
    from.

    This sheet is that answer: the pass-1 findings, each marked with whether
    preprocessing repaired it. A row marked NO is still missing from erwin. A
    row marked YES exists in the remediated XML but was NOT produced by the
    erwin import — so it will not be in the .erwin binary, or in erwin's UI,
    until the model is re-imported or the binary is regenerated.
    """
    scored = [r for r in results
              if getattr(r, "initial_result", None) is not None]
    if not scored:
        return

    ws = wb.create_sheet("AS_IMPORTED")
    ws.freeze_panes = "A4"

    ws.merge_cells(f"A1:{get_column_letter(len(AS_IMPORTED_HEADERS))}1")
    banner = ws["A1"]
    banner.value = ("AS IMPORTED — what the raw erwin import lost, before "
                    "preprocessing repaired it")
    banner.font      = Font(bold=True, color=C_WHITE, size=12, name=REPORT_FONT)
    banner.fill      = PatternFill("solid", fgColor=C_HEADER)
    banner.alignment = CENTER
    ws.row_dimensions[1].height = 20

    ws.cell(2, 1, "Every other sheet describes the model AFTER remediation. "
                  "A row below marked 'NO' is still missing from erwin; a row "
                  "marked 'YES' exists only in the remediated XML, not in the "
                  "model erwin actually imported.").font = Font(italic=True)

    _header_row(ws, AS_IMPORTED_HEADERS, 3)

    row = 4
    for result in results:
        initial = getattr(result, "initial_result", None)
        if initial is None:
            continue
        model_label = os.path.basename(result.pd_file)

        # A pass-1 finding counts as repaired when no finding of the same
        # category/table/column survived into the final result.
        remaining = {(f.category, _key(f.table), _key(f.column))
                     for f in result.findings
                     if f.severity in ("CRITICAL", "WARNING")}

        defects = sorted(
            (f for f in initial.findings
             if f.severity in ("CRITICAL", "WARNING")),
            key=lambda f: (SEVERITY_ORDER.get(f.severity, 3),
                           f.category, f.table, f.column))
        for finding in _limited(defects, config):
            repaired = ((finding.category, _key(finding.table),
                         _key(finding.column)) not in remaining)
            _data_row(ws, [
                model_label, finding.severity, finding.category,
                getattr(finding, "object_type", ""),
                finding.table, finding.column, finding.message,
                finding.pd_value, finding.erwin_value,
                "YES" if repaired else "NO",
            ], row, alt=(row % 2 == 0))
            _paint_severity(ws, row, 2, finding.severity)
            flag = ws.cell(row=row, column=len(AS_IMPORTED_HEADERS))
            flag.alignment = CENTER
            flag.font = Font(bold=True,
                             color=C_BLACK if repaired else C_DARKRED)
            row += 1

        if row == 4:
            _data_row(ws, [model_label, "", "", "", "", "",
                           "The raw import produced no CRITICAL or WARNING "
                           "findings — nothing was lost on the way in.",
                           "", "", "—"], row)
            row += 1

        report = getattr(result, "preprocess_report", None)
        if report is not None:
            row += 1
            ws.cell(row, 1, "Preprocessing actions").font = Font(bold=True)
            row += 1
            for action in getattr(report, "actions", []) or []:
                ws.cell(row, 1, model_label)
                ws.cell(row, 7, action)
                row += 1
            for error in getattr(report, "errors", []) or []:
                ws.cell(row, 1, model_label)
                cell = ws.cell(row, 7, f"UNRESOLVED: {error}")
                cell.font = Font(bold=True, color=C_DARKRED)
                row += 1
            if getattr(report, "erwin_binary_stale", False):
                ws.cell(row, 1, model_label)
                cell = ws.cell(row, 7,
                               "The .erwin BINARY was not regenerated from the "
                               "remediated XML (erwin COM is Windows-only), so "
                               "it does not contain the repairs above.")
                cell.font = Font(bold=True, color=C_DARKRED)
                row += 1

    _set_col_widths(ws, [30, 11, 20, 14, 30, 26, 80, 34, 34, 22])
    ws.auto_filter.ref = (f"A3:{get_column_letter(len(AS_IMPORTED_HEADERS))}"
                          f"{max(row - 1, 3)}")


# ─── TABLE MATRIX SHEET ───────────────────────────────────────────────────────

TABLE_HEADERS = [
    "Model", "Status",
    "SAP PD Table", "erwin Table",
    "Columns (SAP PD)", "Columns (erwin)", "Columns Matched",
    "Col Missing", "Col Extra", "Col Duplicate (erwin)",
    "SAP PD Primary Key", "erwin Primary Key",
    "Keys (SAP PD)", "Keys (erwin)",
    "Indexes (SAP PD)", "Indexes (erwin)",
    "CRITICAL", "WARNING", "INFO",
]

# 1-based index of the CRITICAL column, derived from the header list so the
# highlight can never drift off it again.
_MATRIX_CRITICAL_COLUMN = TABLE_HEADERS.index("CRITICAL") + 1


def _build_table_matrix(wb: Workbook, results: List) -> None:
    ws = wb.create_sheet("TABLE_MATRIX")
    ws.freeze_panes = "A2"
    _header_row(ws, TABLE_HEADERS, 1)

    row = 2
    for result in results:
        model_label = os.path.basename(result.pd_file)
        models = _models_for(result)
        pd_tables = {_key(k): v for k, v in (models["pd"].get("tables") or {}).items()}
        er_tables = {_key(k): v for k, v in (models["erwin"].get("tables") or {}).items()}

        # Findings grouped per table, so each row carries its own severity mix.
        per_table: Dict[str, Counter] = defaultdict(Counter)
        for finding in result.findings:
            per_table[_key(finding.table)][finding.severity] += 1

        for code in sorted(set(pd_tables) | set(er_tables)):
            pd_table = pd_tables.get(code)
            er_table = er_tables.get(code)
            if pd_table and er_table:
                status = "MATCHED"
            elif pd_table:
                status = "MISSING_IN_ERWIN"
            else:
                status = "EXTRA_IN_ERWIN"

            pd_columns = (pd_table or {}).get("columns", []) or []
            er_columns = (er_table or {}).get("columns", []) or []
            pd_codes = {_key(c.get("code")) for c in pd_columns}
            er_codes = {_key(c.get("code")) for c in er_columns}
            matched = len(pd_codes & er_codes)
            counts = per_table.get(code, Counter())

            # This row used to carry 12 values against 16 headers: the
            # CRITICAL / WARNING / INFO counts landed under "Keys (SAP PD)",
            # "Keys (erwin)" and "Indexes (SAP PD)", the key and index counts
            # were never written at all, and the last three columns were always
            # blank. Every value below now lines up with its own header, and
            # Missing / Extra / Duplicate are shown so the per-table column
            # figures add up the way the source tools report them.
            _data_row(ws, [
                model_label, status,
                (pd_table or {}).get("code", "") or ("" if pd_table is None else code),
                (er_table or {}).get("code", "") or ("" if er_table is None else code),
                len(pd_columns), len(er_columns), matched,
                len(pd_codes - er_codes), len(er_codes - pd_codes),
                len(er_columns) - len(er_codes),
                _pk_columns(pd_table or {}), _pk_columns(er_table or {}),
                len((pd_table or {}).get("keys", []) or []),
                len((er_table or {}).get("keys", []) or []),
                len((pd_table or {}).get("indexes", []) or []),
                len((er_table or {}).get("indexes", []) or []),
                counts.get("CRITICAL", 0), counts.get("WARNING", 0),
                counts.get("INFO", 0),
            ], row, alt=(row % 2 == 0))

            status_cell = ws.cell(row=row, column=2)
            status_cell.fill      = RECON_FILL.get(status, PatternFill())
            status_cell.font      = Font(bold=True,
                                         color=C_WHITE if status == "MISSING_IN_ERWIN"
                                         else C_BLACK)
            status_cell.alignment = CENTER
            if counts.get("CRITICAL"):
                ws.cell(row=row,
                        column=_MATRIX_CRITICAL_COLUMN).font = Font(bold=True,
                                                                    color=C_RED)
            row += 1

    _set_col_widths(ws, [30, 20, 34, 34, 15, 15, 15, 12, 12, 18,
                         40, 40, 12, 12, 14, 14, 10, 10, 9])
    ws.auto_filter.ref = f"A1:{get_column_letter(len(TABLE_HEADERS))}{max(row - 1, 1)}"


# ─── RELATIONSHIP (FOREIGN KEY) SHEET ─────────────────────────────────────────

RELATIONSHIP_HEADERS = [
    "Model", "Status", "Tables (Parent → Child)",
    "SAP PD Foreign Key", "erwin Foreign Key",
    "SAP PD Join Columns", "erwin Join Columns",
]


def _build_relationships(wb: Workbook, results: List) -> None:
    ws = wb.create_sheet("RELATIONSHIPS")
    ws.freeze_panes = "A2"
    _header_row(ws, RELATIONSHIP_HEADERS, 1)

    row = 2
    for result in results:
        model_label = os.path.basename(result.pd_file)
        models = _models_for(result)
        pd_refs = {_fk_signature(r): r for r in (models["pd"].get("references") or [])}
        er_refs = {_fk_signature(r): r for r in (models["erwin"].get("references") or [])}

        # Endpoint pairs let an FK whose JOIN changed be reported as such,
        # rather than as one missing plus one extra.
        pd_by_pair: Dict[tuple, List] = defaultdict(list)
        for signature, reference in pd_refs.items():
            if signature not in er_refs:
                pd_by_pair[(_key(reference.get("parent_table")),
                            _key(reference.get("child_table")))].append(reference)
        er_by_pair: Dict[tuple, List] = defaultdict(list)
        for signature, reference in er_refs.items():
            if signature not in pd_refs:
                er_by_pair[(_key(reference.get("parent_table")),
                            _key(reference.get("child_table")))].append(reference)

        rows: List[tuple] = []
        for signature in sorted(set(pd_refs) & set(er_refs)):
            pd_ref, er_ref = pd_refs[signature], er_refs[signature]
            rows.append(("MATCHED", pd_ref, er_ref))

        for pair in sorted(set(pd_by_pair) | set(er_by_pair)):
            unmatched_pd = list(pd_by_pair.get(pair, []))
            unmatched_er = list(er_by_pair.get(pair, []))
            while unmatched_pd and unmatched_er:
                rows.append(("JOIN_CHANGED", unmatched_pd.pop(0),
                             unmatched_er.pop(0)))
            rows.extend(("MISSING_IN_ERWIN", reference, None)
                        for reference in unmatched_pd)
            rows.extend(("EXTRA_IN_ERWIN", None, reference)
                        for reference in unmatched_er)

        for status, pd_ref, er_ref in rows:
            source = pd_ref or er_ref or {}
            _data_row(ws, [
                model_label, status,
                f"{source.get('parent_table', '?')} → {source.get('child_table', '?')}",
                (pd_ref or {}).get("name", ""),
                (er_ref or {}).get("name", ""),
                _join_text(pd_ref) if pd_ref else "—",
                _join_text(er_ref) if er_ref else "—",
            ], row, alt=(row % 2 == 0))

            status_cell = ws.cell(row=row, column=2)
            status_cell.fill      = RECON_FILL.get(status, PatternFill())
            status_cell.font      = Font(bold=True,
                                         color=C_WHITE if status == "MISSING_IN_ERWIN"
                                         else C_BLACK)
            status_cell.alignment = CENTER
            row += 1

    _set_col_widths(ws, [30, 20, 44, 30, 30, 46, 46])
    ws.auto_filter.ref = f"A1:{get_column_letter(len(RELATIONSHIP_HEADERS))}{max(row - 1, 1)}"


# ─── CATEGORY ANALYSIS SHEET ──────────────────────────────────────────────────

def _build_category_analysis(wb: Workbook, results: List) -> None:
    ws = wb.create_sheet("CATEGORY_ANALYSIS")
    ws.freeze_panes = "A2"

    tally: Dict[str, Counter] = defaultdict(Counter)
    models_affected: Dict[str, set] = defaultdict(set)

    for result in results:
        for finding in result.findings:
            tally[finding.category][finding.severity] += 1
            models_affected[finding.category].add(result.pd_file)

    _header_row(ws, ["Category", "Keys (SAP PD)", "Keys (erwin)",
    "Indexes (SAP PD)", "Indexes (erwin)",
    "CRITICAL", "WARNING", "INFO",
                     "Total", "Models Affected"], 1)

    ordered = sorted(tally.items(),
                     key=lambda item: (-sum(item[1].values()), item[0]))

    for offset, (category, counts) in enumerate(ordered, start=1):
        row = offset + 1
        _data_row(ws, [
            category, counts.get("CRITICAL", 0), counts.get("WARNING", 0),
            counts.get("INFO", 0), sum(counts.values()),
            len(models_affected[category]),
        ], row, alt=(offset % 2 == 0))
        if counts.get("CRITICAL", 0):
            ws.cell(row=row, column=2).font = Font(bold=True, color=C_RED)

    _set_col_widths(ws, [28, 12, 12, 10, 10, 18])
    if ordered:
        ws.auto_filter.ref = f"A1:F{len(ordered) + 1}"


# ─── DOCUMENTATION SHEET ──────────────────────────────────────────────────────

DESCRIPTION_HEADERS = [
    "Model", "Object Type", "Object", "Code",
    "Mapping", "Source Field", "Target Field",
    "SAP PD Value (source)", "erwin Value (target)",
    "Status", "Similarity %",
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
    _header_row(ws, ["Mapping", "MATCHED", "MISMATCH", "MISSING IN ERWIN",
                     "MISSING IN SAP PD", "BOTH EMPTY", "Total"], 1)
    line = 2
    for mapping, counts in sorted(summary.items()):
        _data_row(ws, [
            mapping,
            counts.get("MATCHED", 0),
            counts.get("MISMATCH", 0),
            counts.get("MISSING_IN_ERWIN", 0),
            counts.get("MISSING_IN_SAP_PD", 0),
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
        key=lambda r: (0 if r.status in ("MISSING_IN_ERWIN", "MISMATCH") else
                       1 if r.status == "MISSING_IN_SAP_PD" else
                       2 if r.status == "MATCHED" else 3,
                       r.model, r.object_type, r.object_name, r.mapping),
    )

    for index, row_data in enumerate(ordered_rows):
        _data_row(ws, [
            row_data.model, row_data.object_type, row_data.object_name,
            row_data.object_code, row_data.mapping,
            row_data.source_field, row_data.target_field,
            row_data.source_value, row_data.target_value,
            row_data.status,
            row_data.similarity if row_data.status == "MISMATCH" else "",
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
    "#", "Severity", "Category", "Object Type", "Table", "Column",
    "Message", "SAP PD Value", "erwin Value", "Recommended Action",
]


def _build_model_sheet(wb: Workbook, result, config) -> None:
    base = os.path.splitext(os.path.basename(result.pd_file))[0]
    ws = wb.create_sheet(_unique_sheet_name(wb, base))
    ws.freeze_panes = "A3"

    ws.merge_cells("A1:I1")
    title = ws["A1"]
    stage = getattr(result, "stage", "")
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

    cap = _cfg(config, "MAX_DIFF_ROWS_PER_MODEL", 500)
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
        "promoted": sum(1 for r in results if getattr(r, "promoted", False)),
        "results": [{
            "pd_file": r.pd_file,
            "erwin_file": r.erwin_file,
            "pd_model": r.pd_model,
            "status": r.status,
            "fidelity_score": r.fidelity_score,
            "stage": getattr(r, "stage", ""),
            "promoted": bool(getattr(r, "promoted", False)),
            "tables": {"pd": r.tables_pd, "erwin": r.tables_erwin,
                       "matched": r.tables_matched,
                       "missing_in_erwin": r.tables_missing_in_erwin,
                       "extra_in_erwin": r.tables_extra_in_erwin},
            "columns": {"pd": r.columns_pd, "erwin": r.columns_erwin,
                        "matched": r.columns_matched,
                        "missing_in_erwin": r.columns_missing_in_erwin,
                        "extra_in_erwin": r.columns_extra_in_erwin},
            "foreign_keys": {"pd": r.fk_pd, "erwin": r.fk_erwin,
                             "matched": r.fk_matched,
                             "missing_in_erwin": r.fk_missing_in_erwin,
                             "extra_in_erwin": r.fk_extra_in_erwin},
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
    _build_as_imported(workbook, results, config)
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
