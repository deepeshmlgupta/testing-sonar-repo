"""
Excel Report Generator
----------------------
Builds a colour-coded .xlsx workbook with:

  1. SUMMARY           one row per model pair, status, fidelity score, counters
  2. DASHBOARD         run-level totals, severity mix, worst offenders, chart
  3. FINDINGS          every individual difference across every model
  4. ENTITY_MATRIX     entity-by-entity reconciliation, including match basis
  5. RELATIONSHIPS     relationship-by-relationship reconciliation with degrees
  6. CATEGORY_ANALYSIS finding counts by category × severity
  7. CONFIG            the exact rule set that produced this report
  8. Per-model sheets  one per pair, emitted only for runs of a manageable size

Optional CSV and JSON exports sit beside the workbook for pipelines that need
machine-readable output rather than a spreadsheet.
"""

import csv
import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List, Optional

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.formatting.rule import DataBarRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from app.config.validation_config import LDM_CONFIG as config
from .comparator import Finding, ValidationResult

logger = logging.getLogger(__name__)

# ─── COLOUR PALETTE ───────────────────────────────────────────────────────────
C_GREEN   = "FF92D050"   # PASS
C_YELLOW  = "FFFFC000"   # WARN
C_RED     = "FFFF0000"   # FAIL / CRITICAL
C_DARKRED = "FFC00000"
C_BLUE    = "FF4472C4"   # INFO
C_HEADER  = "FF1F3864"   # Dark navy header
C_SUBHDR  = "FF2E75B6"   # Sub-header blue
C_ALT     = "FFD9E1F2"   # Alternating row (light blue)
C_WHITE   = "FFFFFFFF"
C_BLACK   = "FF000000"
C_GRAY    = "FFD6DCE4"

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
    "MATCHED":             PatternFill("solid", fgColor=C_GREEN),
    "MISSING_IN_ERWIN":    PatternFill("solid", fgColor=C_RED),
    "EXTRA_IN_ERWIN":      PatternFill("solid", fgColor=C_YELLOW),
    "CARDINALITY_CHANGED": PatternFill("solid", fgColor=C_YELLOW),
    "ENDPOINTS_CHANGED":   PatternFill("solid", fgColor=C_RED),
}

THIN        = Side(style="thin", color="FFB8B8B8")
BORDER      = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEADER_FONT = Font(bold=True, color=C_WHITE, name=config.REPORT_FONT, size=10)
HEADER_FILL = PatternFill("solid", fgColor=C_HEADER)
SUBHDR_FILL = PatternFill("solid", fgColor=C_SUBHDR)
CENTER      = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT        = Alignment(horizontal="left",   vertical="center", wrap_text=True)

STATUS_ICON = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL", "ERROR": "ERROR"}  # nosec B105


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


def _limited(findings: List[Finding]) -> List[Finding]:
    cap = config.MAX_DIFF_ROWS_PER_MODEL
    return findings[:cap] if cap and cap > 0 else findings


# ─── SUMMARY SHEET ────────────────────────────────────────────────────────────

SUMMARY_HEADERS = [
    "#", "SAP PD File", "erwin File", "SAP PD Model", "erwin Model",
    "Status", "Fidelity %", "Review?",
    "Entities (SAP PD)", "Entities (erwin)", "Entities Matched",
    "Ent. Missing", "Ent. Extra",
    "Attrs (SAP PD)", "Attrs (erwin)", "Attrs Matched",
    "Attr Missing", "Attr Extra",
    "Rels (SAP PD)", "Rels (erwin)", "Rels Matched",
    "Rel Missing", "Rel Extra",
    "Hierarchies (SAP PD)", "Hierarchies (erwin)",
    "Identifiers (SAP PD)", "Identifiers (erwin)",
    "Domains (SAP PD)", "Domains (erwin)",
    "Shortcuts (SAP PD)", "Shortcuts (erwin)",
    "CRITICAL", "WARNING", "INFO",
]


def _write_summary_data_row(ws, result: ValidationResult, row: int, index: int) -> None:
    _data_row(ws, [
        index,
        os.path.basename(result.pd_file),
        os.path.basename(result.erwin_file),
        result.pd_model,
        result.erwin_model,
        result.status,
        result.fidelity_score,
        "YES" if result.needs_review else "",
        result.entities_pd, result.entities_erwin, result.entities_matched,
        result.entities_missing_in_erwin, result.entities_extra_in_erwin,
        result.attributes_pd, result.attributes_erwin, result.attributes_matched,
        result.attributes_missing_in_erwin, result.attributes_extra_in_erwin,
        result.relationships_pd, result.relationships_erwin,
        result.relationships_matched,
        result.relationships_missing_in_erwin, result.relationships_extra_in_erwin,
        result.inheritances_pd, result.inheritances_erwin,
        result.identifiers_pd, result.identifiers_erwin,
        result.domains_pd, result.domains_erwin,
        result.shortcuts_pd, getattr(result, "shortcuts_erwin", 0),
        result.critical_count, result.warning_count, result.info_count,
    ], row, alt=(index % 2 == 0))


def _format_summary_row(ws, result: ValidationResult, row: int) -> None:
    _paint_status(ws, row, 6, result.status)

    fidelity_cell = ws.cell(row=row, column=7)
    fidelity_cell.number_format = "0.00"
    fidelity_cell.alignment = CENTER
    if result.fidelity_score < 90:
        fidelity_cell.font = Font(bold=True, color=C_DARKRED)
    elif result.fidelity_score < config.FIDELITY_REVIEW_THRESHOLD:
        fidelity_cell.font = Font(bold=True, color="FFBF8F00")

    if result.needs_review:
        review_cell = ws.cell(row=row, column=8)
        review_cell.font = Font(bold=True, color=C_DARKRED)
        review_cell.alignment = CENTER

    if result.critical_count:
        ws.cell(row=row, column=32).font = Font(bold=True, color=C_RED)
    if result.warning_count:
        ws.cell(row=row, column=33).font = Font(bold=True, color=C_DARKRED)


def _write_summary_totals(ws, results: List[ValidationResult], total_row: int) -> None:
    ws.cell(total_row, 1, "TOTAL").font = Font(bold=True)
    numeric_columns = {
        9: "entities_pd", 10: "entities_erwin", 11: "entities_matched",
        12: "entities_missing_in_erwin", 13: "entities_extra_in_erwin",
        14: "attributes_pd", 15: "attributes_erwin", 16: "attributes_matched",
        17: "attributes_missing_in_erwin", 18: "attributes_extra_in_erwin",
        19: "relationships_pd", 20: "relationships_erwin", 21: "relationships_matched",
        22: "relationships_missing_in_erwin", 23: "relationships_extra_in_erwin",
        24: "inheritances_pd", 25: "inheritances_erwin",
        26: "identifiers_pd", 27: "identifiers_erwin",
        28: "domains_pd", 29: "domains_erwin",
        30: "shortcuts_pd", 31: "shortcuts_erwin",
        32: "critical_count", 33: "warning_count", 34: "info_count",
    }
    for column, attribute in numeric_columns.items():
        total = sum(getattr(r, attribute) for r in results)
        cell = ws.cell(total_row, column, total)
        cell.font = Font(bold=True, color=C_RED if column == 32 else C_BLACK)
        cell.fill = PatternFill("solid", fgColor=C_GRAY)
        cell.border = BORDER

    if results:
        average = round(sum(r.fidelity_score for r in results) / len(results), 2)
        cell = ws.cell(total_row, 7, average)
        cell.font = Font(bold=True)
        cell.number_format = "0.00"
        cell.fill = PatternFill("solid", fgColor=C_GRAY)


def _build_summary(wb: Workbook, results: List[ValidationResult]) -> None:
    ws = wb.active
    ws.title = "SUMMARY"
    ws.freeze_panes = "C3"
    ws.row_dimensions[1].height = 22
    ws.row_dimensions[2].height = 32

    last_column = get_column_letter(len(SUMMARY_HEADERS))
    ws.merge_cells(f"A1:{last_column}1")
    banner = ws["A1"]
    banner.value = (
        "SAP PD (PowerDesigner LDM) -> erwin  |  Logical Model Validation Report"
        f"   |   generated {datetime.now():%Y-%m-%d %H:%M}"
    )
    banner.font = Font(bold=True, color=C_WHITE, size=14, name=config.REPORT_FONT)
    banner.fill = PatternFill("solid", fgColor=C_HEADER)
    banner.alignment = CENTER

    _header_row(ws, SUMMARY_HEADERS, 2)

    for index, result in enumerate(results, start=1):
        row = index + 2
        _write_summary_data_row(ws, result, row, index)
        _format_summary_row(ws, result, row)

    _write_summary_totals(ws, results, len(results) + 3)

    _set_col_widths(ws, [5, 32, 32, 24, 24, 9, 11, 9] + [13] * 23 + [10, 10, 9])
    ws.auto_filter.ref = f"A2:{last_column}{len(results) + 2}"

    if results:
        ws.conditional_formatting.add(
            f"G3:G{len(results) + 2}",
            DataBarRule(
                start_type="num", start_value=0,
                end_type="num", end_value=100,
                color="FF63BE7B", showValue=True,
            ),
        )


# ─── DASHBOARD SHEET ──────────────────────────────────────────────────────────

def _dashboard_statistics(results: List[ValidationResult]) -> list:
    status_counts = Counter(r.status for r in results)
    total_models = len(results)
    average_score = (
        round(sum(r.fidelity_score for r in results) / total_models, 2)
        if total_models else 0.0
    )
    return [
        ("Models validated", total_models),
        ("PASS", status_counts.get("PASS", 0)),
        ("WARN", status_counts.get("WARN", 0)),
        ("FAIL", status_counts.get("FAIL", 0)),
        ("ERROR", status_counts.get("ERROR", 0)),
        ("Average fidelity score", average_score),
        ("Models needing review", sum(1 for r in results if r.needs_review)),
        ("Total findings", sum(len(r.findings) for r in results)),
        ("CRITICAL findings", sum(r.critical_count for r in results)),
        ("WARNING findings", sum(r.warning_count for r in results)),
        ("INFO findings", sum(r.info_count for r in results)),
        ("Entities compared", sum(r.entities_pd for r in results)),
        ("Attributes compared", sum(r.attributes_pd for r in results)),
        ("Relationships compared", sum(r.relationships_pd for r in results)),
    ]


def _write_dashboard_statistics(ws, statistics: list) -> None:
    _header_row(ws, ["Run Statistic", "Value"], 3, fill=SUBHDR_FILL)
    for offset, (label, value) in enumerate(statistics):
        row = 4 + offset
        _data_row(ws, [label, value], row, alt=(offset % 2 == 1))
        ws.cell(row=row, column=1).font = Font(bold=True)
        if label in STATUS_FILL:
            _paint_status(ws, row, 2, label)


def _write_worst_models(ws, results: List[ValidationResult], worst_start: int) -> None:
    ws.cell(worst_start - 1, 1, "Lowest-fidelity models").font = Font(bold=True, size=11)
    _header_row(
        ws, ["Model", "Status", "Fidelity %", "CRITICAL", "WARNING"],
        worst_start, fill=SUBHDR_FILL,
    )
    worst = sorted(results, key=lambda r: r.fidelity_score)[:15]
    for offset, result in enumerate(worst, start=1):
        row = worst_start + offset
        _data_row(ws, [
            os.path.basename(result.pd_file), result.status,
            result.fidelity_score, result.identifiers_pd, result.identifiers_erwin,
            result.domains_pd, result.domains_erwin,
            result.shortcuts_pd,
            result.critical_count, result.warning_count,
        ], row, alt=(offset % 2 == 0))
        _paint_status(ws, row, 2, result.status)
        ws.cell(row=row, column=3).number_format = "0.00"


def _add_severity_chart(ws, results: List[ValidationResult]) -> None:
    chart_anchor_row = 4
    ws.cell(chart_anchor_row - 1, 8, "Findings by severity").font = Font(bold=True)
    severity_rows = [
        ("CRITICAL", sum(r.critical_count for r in results)),
        ("WARNING", sum(r.warning_count for r in results)),
        ("INFO", sum(r.info_count for r in results)),
        ("VERIFIED", sum(1 for r in results for f in r.findings if f.severity == "VERIFIED")),
    ]
    for offset, (label, value) in enumerate(severity_rows):
        ws.cell(chart_anchor_row + offset, 8, label)
        ws.cell(chart_anchor_row + offset, 9, value)

    try:
        chart = BarChart()
        chart.type = "col"
        chart.title = "Findings by severity"
        chart.y_axis.title = "Findings"
        chart.height = 7
        chart.width = 13
        data = Reference(
            ws, min_col=9, min_row=chart_anchor_row,
            max_row=chart_anchor_row + len(severity_rows) - 1,
        )
        categories = Reference(
            ws, min_col=8, min_row=chart_anchor_row,
            max_row=chart_anchor_row + len(severity_rows) - 1,
        )
        chart.add_data(data, titles_from_data=False)
        chart.set_categories(categories)
        chart.legend = None
        ws.add_chart(chart, "K3")
    except Exception as exc:  # pragma: no cover
        logger.warning("Could not render dashboard chart: %s", exc)


def _build_dashboard(wb: Workbook, results: List[ValidationResult]) -> None:
    ws = wb.create_sheet("DASHBOARD")

    ws.merge_cells("A1:F1")
    banner = ws["A1"]
    banner.value = "Migration Reconciliation Dashboard"
    banner.font = Font(bold=True, color=C_WHITE, size=14, name=config.REPORT_FONT)
    banner.fill = PatternFill("solid", fgColor=C_HEADER)
    banner.alignment = CENTER
    ws.row_dimensions[1].height = 22

    statistics = _dashboard_statistics(results)
    _write_dashboard_statistics(ws, statistics)

    worst_start = 4 + len(statistics) + 2
    _write_worst_models(ws, results, worst_start)
    _set_col_widths(ws, [34, 14, 13, 11, 11, 11])
    _add_severity_chart(ws, results)


# ─── FINDINGS SHEET ───────────────────────────────────────────────────────────

OBJECT_TYPE_HEADER = "Object Type"

FINDINGS_HEADERS = [
    "Model", "Status", "Severity", "Category", OBJECT_TYPE_HEADER,
    "Object", "Member", "Message",
    "SAP PD Value", "erwin Value", "Recommended Action", "Manual Review",
]

SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}


def _build_findings(wb: Workbook, results: List[ValidationResult]) -> None:
    ws = wb.create_sheet("FINDINGS")
    ws.freeze_panes = "A2"
    _header_row(ws, FINDINGS_HEADERS, 1)

    row = 2
    for result in results:
        model_label = os.path.basename(result.pd_file)
        findings = sorted(_limited(result.findings),
                          key=lambda f: (SEVERITY_ORDER.get(f.severity, 3),
                                         f.category, f.object_name, f.member))
        for finding in findings:
            _data_row(ws, [
                model_label, result.status, finding.severity, finding.category,
                finding.object_type, finding.object_name, finding.member,
                finding.message, finding.pd_value, finding.erwin_value,
                finding.remediation,
            ], row, alt=(row % 2 == 0))
            _paint_severity(ws, row, 3, finding.severity)
            _paint_status(ws, row, 2, result.status)
            row += 1

    _set_col_widths(ws, [30, 9, 11, 22, 13, 28, 26, 62, 34, 34, 56])
    ws.auto_filter.ref = f"A1:{get_column_letter(len(FINDINGS_HEADERS))}{max(row - 1, 1)}"
    
    if row > 2:
        dv = DataValidation(type="list", formula1='"Acceptable,Not Acceptable"', allow_blank=True)
        ws.add_data_validation(dv)
        # Column L (12) is Manual Review in FINDINGS
        dv.add(f"L2:L{row - 1}")


# ─── ENTITY MATRIX SHEET ──────────────────────────────────────────────────────

ENTITY_HEADERS = [
    "Model", "Status", "Match Basis",
    "SAP PD Entity Name", "SAP PD Code", "erwin Entity Name", "erwin Code",
    "Attrs (SAP PD)", "Attrs (erwin)", "Attrs Matched",
    "SAP PD Primary Identifier", "erwin Primary Identifier",
    "CRITICAL", "WARNING", "INFO",
]


def _build_entity_matrix(wb: Workbook, results: List[ValidationResult]) -> None:
    ws = wb.create_sheet("ENTITY_MATRIX")
    ws.freeze_panes = "A2"
    _header_row(ws, ENTITY_HEADERS, 1)

    row = 2
    for result in results:
        model_label = os.path.basename(result.pd_file)
        for record in result.entity_records:
            _data_row(ws, [
                model_label, record.status, record.match_basis,
                record.pd_name, record.pd_code,
                record.erwin_name, record.erwin_code,
                record.pd_attributes, record.erwin_attributes,
                record.attributes_matched,
                record.pd_primary_id, record.erwin_primary_id,
                record.critical, record.warning, record.info,
            ], row, alt=(row % 2 == 0))

            status_cell = ws.cell(row=row, column=2)
            status_cell.fill      = RECON_FILL.get(record.status, PatternFill())
            status_cell.font      = Font(bold=True,
                                         color=C_WHITE if record.status == "MISSING_IN_ERWIN"
                                         else C_BLACK)
            status_cell.alignment = CENTER
            row += 1

    _set_col_widths(ws, [30, 20, 18, 28, 26, 28, 26, 12, 13, 13, 40, 40, 10, 10, 9])
    ws.auto_filter.ref = f"A1:{get_column_letter(len(ENTITY_HEADERS))}{max(row - 1, 1)}"


# ─── RELATIONSHIP SHEET ───────────────────────────────────────────────────────

RELATIONSHIP_HEADERS = [
    "Model", "Status", "Match Basis", "Entities",
    "SAP PD Relationship", "erwin Relationship",
    "SAP PD Degree", "erwin Degree",
    "SAP PD Semantics", "erwin Semantics",
]


def _build_relationships(wb: Workbook, results: List[ValidationResult]) -> None:
    ws = wb.create_sheet("RELATIONSHIPS")
    ws.freeze_panes = "A2"
    _header_row(ws, RELATIONSHIP_HEADERS, 1)

    row = 2
    for result in results:
        model_label = os.path.basename(result.pd_file)
        for record in result.relationship_records:
            _data_row(ws, [
                model_label, record.status, record.match_basis, record.entities,
                record.pd_name, record.erwin_name,
                record.pd_degree, record.erwin_degree,
                record.pd_signature, record.erwin_signature,
            ], row, alt=(row % 2 == 0))

            status_cell = ws.cell(row=row, column=2)
            status_cell.fill      = RECON_FILL.get(record.status, PatternFill())
            status_cell.font      = Font(bold=True,
                                         color=C_WHITE if record.status in
                                         ("MISSING_IN_ERWIN", "ENDPOINTS_CHANGED")
                                         else C_BLACK)
            status_cell.alignment = CENTER

            if record.pd_degree and record.erwin_degree and \
                    record.pd_degree != record.erwin_degree:
                ws.cell(row=row, column=8).font = Font(bold=True, color=C_RED)
            row += 1

    _set_col_widths(ws, [30, 22, 22, 34, 26, 26, 12, 12, 52, 52])
    ws.auto_filter.ref = f"A1:{get_column_letter(len(RELATIONSHIP_HEADERS))}{max(row - 1, 1)}"


# ─── CATEGORY ANALYSIS SHEET ──────────────────────────────────────────────────

def _build_category_analysis(wb: Workbook, results: List[ValidationResult]) -> None:
    ws = wb.create_sheet("CATEGORY_ANALYSIS")
    ws.freeze_panes = "A2"

    tally: Dict[str, Counter] = defaultdict(Counter)
    models_affected: Dict[str, set] = defaultdict(set)

    for result in results:
        for finding in result.findings:
            tally[finding.category][finding.severity] += 1
            models_affected[finding.category].add(result.pd_file)

    _header_row(ws, ["Category", "CRITICAL", "WARNING", "INFO", "VERIFIED",
                     "Total", "Models Affected"], 1)

    ordered = sorted(tally.items(),
                     key=lambda item: (-sum(item[1].values()), item[0]))

    for offset, (category, counts) in enumerate(ordered, start=1):
        row = offset + 1
        total = sum(counts.values())
        _data_row(ws, [
            category, counts.get("CRITICAL", 0), counts.get("WARNING", 0),
            counts.get("INFO", 0), counts.get("VERIFIED", 0), total, len(models_affected[category]),
        ], row, alt=(offset % 2 == 0))
        if counts.get("CRITICAL", 0):
            ws.cell(row=row, column=2).font = Font(bold=True, color=C_RED)

    _set_col_widths(ws, [28, 12, 12, 10, 12, 10, 18])
    if ordered:
        ws.auto_filter.ref = f"A1:G{len(ordered) + 1}"


# ─── CONFIG SHEET ─────────────────────────────────────────────────────────────

# ─── DOCUMENTATION SHEET (Comments → Notes, Definition → Definition) ──────────

DESCRIPTION_HEADERS = [
    "Model", OBJECT_TYPE_HEADER, "Object", "Code",
    "Mapping", "Source Field", "Target Field",
    "SAP PD Value (source)", "erwin Value (target)",
    "Status", "Similarity %",
]

DOC_STATUS_FILL = {
    "MATCHED":           PatternFill("solid", fgColor="C6EFCE"),
    "MISMATCH":          PatternFill("solid", fgColor="FFC7CE"),
    "MISSING_IN_ERWIN":  PatternFill("solid", fgColor="FFC7CE"),
    "MISSING_IN_SAP_PD": PatternFill("solid", fgColor="FFEB9C"),
    "BOTH_EMPTY":        PatternFill("solid", fgColor="F2F2F2"),
}


def _build_documentation(wb: Workbook, results: List[ValidationResult]) -> None:
    """
    Side-by-side documentation mapping for every object.

    Unlike FINDINGS, this sheet lists MATCHED rows too — a findings list only
    ever shows what went wrong, so there was no way to tell an object whose
    documentation was verified identical from one that was never checked.

    Two mappings are reported per object:
        SAP PD Comment      → erwin Note        (created by preprocessing)
        SAP PD Description  → erwin Definition
    """
    rows = []
    for result in results:
        rows.extend(getattr(result, "documentation_rows", []) or [])

    ws = wb.create_sheet("DESCRIPTION")

    if not rows:
        ws.cell(row=1, column=1,
                value="No documentation rows were produced for this run.")
        _set_col_widths(ws, [70])
        return

    # ── Summary block ────────────────────────────────────────────────────────
    summary: Dict[str, Dict[str, int]] = {}
    for row in rows:
        bucket = summary.setdefault(row.mapping, {})
        bucket[row.status] = bucket.get(row.status, 0) + 1

    _header_row(ws, ["Mapping", "MATCHED", "MISMATCH", "MISSING IN ERWIN",
                     "MISSING IN SAP PD", "BOTH EMPTY", "Total"], 1)
    line = 2
    for mapping, counts in sorted(summary.items()):
        total = sum(counts.values())
        _data_row(ws, [
            mapping,
            counts.get("MATCHED", 0),
            counts.get("MISMATCH", 0),
            counts.get("MISSING_IN_ERWIN", 0),
            counts.get("MISSING_IN_SAP_PD", 0),
            counts.get("BOTH_EMPTY", 0),
            total,
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

    for index, row in enumerate(ordered_rows):
        _data_row(ws, [
            row.model, row.object_type, row.object_name, row.object_code,
            row.mapping, row.source_field, row.target_field,
            row.source_value, row.target_value,
            row.status,
            row.similarity if row.status == "MISMATCH" else "",
        ], line, alt=bool(index % 2))
        fill = DOC_STATUS_FILL.get(row.status)
        if fill is not None:
            ws.cell(row=line, column=10).fill = fill
        line += 1

    _set_col_widths(ws, [22, 12, 38, 24, 24, 20, 20, 60, 60, 18, 12])
    ws.freeze_panes = ws.cell(row=detail_header + 1, column=1)
    ws.auto_filter.ref = (f"A{detail_header}:"
                          f"{get_column_letter(len(DESCRIPTION_HEADERS))}"
                          f"{max(line - 1, detail_header)}")


def _build_config_sheet(wb: Workbook) -> None:
    ws = wb.create_sheet("CONFIG")
    ws.freeze_panes = "A2"
    _header_row(ws, ["Setting", "Value"], 1)

    for offset, (key, value) in enumerate(sorted(config.as_dict().items()), start=1):
        row = offset + 1
        if isinstance(value, (dict, list, tuple)):
            rendered = json.dumps(value, default=str)
        else:
            rendered = str(value)
        _data_row(ws, [key, rendered], row, alt=(offset % 2 == 0))
        ws.cell(row=row, column=1).font = Font(bold=True)

    _set_col_widths(ws, [40, 90])


# ─── PER-MODEL SHEET ──────────────────────────────────────────────────────────

MODEL_SHEET_HEADERS = [
    "#", "Severity", "Category", OBJECT_TYPE_HEADER, "Object", "Member",
    "Message", "SAP PD Value", "erwin Value", "Recommended Action",
]


def _build_model_sheet(wb: Workbook, result: ValidationResult) -> None:
    base = os.path.splitext(os.path.basename(result.pd_file))[0]
    ws = wb.create_sheet(_unique_sheet_name(wb, base))
    ws.freeze_panes = "A3"

    ws.merge_cells("A1:J1")
    title = ws["A1"]
    title.value = (
        f"{os.path.basename(result.pd_file)}  vs  {os.path.basename(result.erwin_file)}"
        f"   |   {result.status}"
        f"   |   fidelity {result.fidelity_score:.2f}%"
        f"   |   {result.entities_matched}/{result.entities_pd} entities matched"
        f"   |   {result.relationships_matched}/{result.relationships_pd} relationships matched"
        f"   |   {result.critical_count} critical / {result.warning_count} warning "
        f"/ {result.info_count} info"
    )
    title.font      = Font(bold=True, color=C_WHITE, size=10)
    title.fill      = STATUS_FILL.get(result.status, HEADER_FILL)
    title.alignment = LEFT
    ws.row_dimensions[1].height = 20

    _header_row(ws, MODEL_SHEET_HEADERS, 2, fill=SUBHDR_FILL)

    findings = sorted(_limited(result.findings),
                      key=lambda f: (SEVERITY_ORDER.get(f.severity, 3),
                                     f.category, f.object_name, f.member))

    for index, finding in enumerate(findings, start=1):
        row = index + 2
        _data_row(ws, [
            index, finding.severity, finding.category, finding.object_type,
            finding.object_name, finding.member, finding.message,
            finding.pd_value, finding.erwin_value, finding.remediation,
        ], row, alt=(index % 2 == 0))
        _paint_severity(ws, row, 2, finding.severity)

    _set_col_widths(ws, [5, 11, 22, 13, 28, 26, 62, 34, 34, 56])

    if findings:
        ws.auto_filter.ref = f"A2:J{len(findings) + 2}"

    cap = config.MAX_DIFF_ROWS_PER_MODEL
    if cap and len(result.findings) > cap:
        note_row = len(findings) + 4
        ws.cell(note_row, 1,
                f"{len(result.findings) - cap} further findings are not shown here — "
                f"the FINDINGS sheet holds the complete list."
                ).font = Font(italic=True, color=C_DARKRED)


# ─── MACHINE-READABLE EXPORTS ─────────────────────────────────────────────────

def _export_findings_csv(results: List[ValidationResult], output_dir: str) -> str:
    path = os.path.join(output_dir, "findings.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(FINDINGS_HEADERS)
        for result in results:
            model_label = os.path.basename(result.pd_file)
            for finding in result.findings:
                writer.writerow([
                    model_label, result.status, finding.severity, finding.category,
                    finding.object_type, finding.object_name, finding.member,
                    finding.message, finding.pd_value, finding.erwin_value,
                    finding.remediation,
                ])
    logger.info("Findings CSV saved → %s", path)
    return path


def _export_json_summary(results: List[ValidationResult], output_dir: str) -> str:
    path = os.path.join(output_dir, "validation_summary.json")
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "models": len(results),
        "status_counts": dict(Counter(r.status for r in results)),
        "average_fidelity": (round(sum(r.fidelity_score for r in results) / len(results), 2)
                             if results else 0.0),
        "totals": {
            "critical": sum(r.critical_count for r in results),
            "warning":  sum(r.warning_count for r in results),
            "info":     sum(r.info_count for r in results),
        },
        "results": [
            {
                "pd_file":     r.pd_file,
                "erwin_file":  r.erwin_file,
                "status":      r.status,
                "fidelity":    r.fidelity_score,
                "needs_review": r.needs_review,
                "entities":      {"pd": r.entities_pd, "erwin": r.entities_erwin,
                                  "matched": r.entities_matched},
                "attributes":    {"pd": r.attributes_pd, "erwin": r.attributes_erwin,
                                  "matched": r.attributes_matched},
                "relationships": {"pd": r.relationships_pd, "erwin": r.relationships_erwin,
                                  "matched": r.relationships_matched},
                "findings": {"critical": r.critical_count,
                             "warning":  r.warning_count,
                             "info":     r.info_count},
            }
            for r in results
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    logger.info("JSON summary saved → %s", path)
    return path


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def generate_report(results: List[ValidationResult],
                    output_dir: str,
                    filename: Optional[str] = None) -> str:
    """
    Build the Excel validation report (plus optional CSV / JSON exports) and
    return the path to the workbook.
    """
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, filename or config.REPORT_FILENAME)

    # Deterministic ordering: worst first, so a reviewer starts where it matters.
    ordered = sorted(results,
                     key=lambda r: (r.fidelity_score,
                                    -r.critical_count,
                                    os.path.basename(r.pd_file)))

    wb = Workbook()
    _build_summary(wb, ordered)
    _build_dashboard(wb, ordered)
    _build_findings(wb, ordered)
    _build_entity_matrix(wb, ordered)
    _build_relationships(wb, ordered)
    _build_category_analysis(wb, ordered)
    _build_documentation(wb, ordered)
    _build_config_sheet(wb)

    if len(ordered) <= config.MAX_MODELS_FOR_DETAIL_SHEETS:
        for result in ordered:
            try:
                _build_model_sheet(wb, result)
            except Exception as exc:                           # pragma: no cover
                logger.warning("Could not create detail sheet for %s: %s",
                               result.pd_file, exc)

    wb.save(out_path)
    logger.info("Report saved → %s", out_path)

    if config.EXPORT_FINDINGS_CSV:
        try:
            _export_findings_csv(ordered, output_dir)
        except OSError as exc:
            logger.warning("Could not write findings CSV: %s", exc)

    if config.EXPORT_JSON_SUMMARY:
        try:
            _export_json_summary(ordered, output_dir)
        except OSError as exc:
            logger.warning("Could not write JSON summary: %s", exc)

    return out_path
