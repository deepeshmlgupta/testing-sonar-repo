"""
Excel Report Generator
----------------------
Builds a colour-coded .xlsx with the same shape as the CDM validator's report:

  1. SUMMARY sheet  — one row per model pair with Status, Fidelity %, a Review?
     flag, and Matched / Missing / Extra counts at three levels (Tables,
     Columns, Foreign Keys), plus CRITICAL / WARNING / INFO finding counts.
  2. FINDINGS sheet — every individual difference across all models.
  3. Per-model sheets (when total models <= 200) — one tab per model pair.
"""

import os
import logging
from datetime import datetime
from typing import List

from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from comparator import ValidationResult, Finding

# See the note in comparator.py: there is no config.py in this folder any more,
# so the settings come from the central PDM tier view.
try:
    from app.config.validation_config import PDM_CONFIG as config
except Exception:                       # pragma: no cover - standalone use
    import config                                              # type: ignore

logger = logging.getLogger(__name__)

# ─── COLOUR PALETTE ───────────────────────────────────────────────────────────
C_GREEN   = "FF92D050"
C_YELLOW  = "FFFFC000"
C_RED     = "FFFF0000"
C_DARKRED = "FFC00000"
C_BLUE    = "FF4472C4"
C_HEADER  = "FF1F3864"
C_SUBHDR  = "FF2E75B6"
C_ALT     = "FFD9E1F2"
C_WHITE   = "FFFFFFFF"

STATUS_FILL = {
    "PASS":  PatternFill("solid", fgColor=C_GREEN),
    "WARN":  PatternFill("solid", fgColor=C_YELLOW),
    "FAIL":  PatternFill("solid", fgColor=C_RED),
    "ERROR": PatternFill("solid", fgColor=C_RED),
}
SEV_FILL = {
    "CRITICAL": PatternFill("solid", fgColor=C_RED),
    "WARNING":  PatternFill("solid", fgColor=C_YELLOW),
    "INFO":     PatternFill("solid", fgColor=C_BLUE),
}
SEV_FONT = {
    "CRITICAL": Font(bold=True, color=C_WHITE),
    "WARNING":  Font(bold=True, color="FF000000"),
    "INFO":     Font(color=C_WHITE),
}

THIN = Side(style="thin", color="FFB8B8B8")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEADER_FONT = Font(bold=True, color=C_WHITE, name="Calibri", size=10)
HEADER_FILL = PatternFill("solid", fgColor=C_HEADER)
SUBHDR_FILL = PatternFill("solid", fgColor=C_SUBHDR)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT   = Alignment(horizontal="left",   vertical="center", wrap_text=True)


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _header_row(ws, values, row, fill=HEADER_FILL):
    for col, val in enumerate(values, start=1):
        cell = ws.cell(row=row, column=col, value=val)
        cell.font, cell.fill, cell.alignment, cell.border = (
            HEADER_FONT, fill, CENTER, BORDER)


def _data_row(ws, values, row, alt=False):
    fill = PatternFill("solid", fgColor=C_ALT) if alt else None
    for col, val in enumerate(values, start=1):
        cell = ws.cell(row=row, column=col, value=val)
        cell.alignment, cell.border = LEFT, BORDER
        if fill:
            cell.fill = fill


def _set_col_widths(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ─── SUMMARY SHEET ────────────────────────────────────────────────────────────

SUMMARY_HEADERS = [
    "#", "PD File", "ERwin File", "PD Model", "ERwin Model",
    "Status", "Fidelity %", "Review?",
    "Tables (PD)", "Tables (ERwin)", "Tables Matched", "Tbl Missing", "Tbl Extra",
    "Columns (PD)", "Columns (ERwin)", "Columns Matched", "Col Missing", "Col Extra",
    "FKs (PD)", "FKs (ERwin)", "FKs Matched", "FK Missing", "FK Extra",
    "CRITICAL", "WARNING", "INFO",
]


SUMMARY_TOTAL_COLUMNS = [
    (9, "tables_pd"), (10, "tables_erwin"), (11, "tables_matched"),
    (12, "tables_missing_in_erwin"), (13, "tables_extra_in_erwin"),
    (14, "columns_pd"), (15, "columns_erwin"), (16, "columns_matched"),
    (17, "columns_missing_in_erwin"), (18, "columns_extra_in_erwin"),
    (19, "fk_pd"), (20, "fk_erwin"), (21, "fk_matched"),
    (22, "fk_missing_in_erwin"), (23, "fk_extra_in_erwin"),
    (24, "critical_count"), (25, "warning_count"), (26, "info_count"),
]


def _summary_banner(ws, last_col: str):
    ws.merge_cells(f"A1:{last_col}1")
    banner = ws["A1"]
    banner.value = ("PDM → ERwin  |  Physical Model Validation Report"
                    f"   |   generated {datetime.now():%Y-%m-%d %H:%M}")
    banner.font = Font(bold=True, color=C_WHITE, size=14, name="Calibri")
    banner.fill = HEADER_FILL
    banner.alignment = CENTER


def _summary_values(idx: int, r: ValidationResult) -> list:
    """One SUMMARY row's cell values, in column order."""
    return [
        idx,
        os.path.basename(r.pd_file),
        os.path.basename(r.erwin_file),
        r.pd_model,
        r.erwin_model,
        r.status,
        r.fidelity_score,
        "YES" if r.needs_review else "",
        r.tables_pd, r.tables_erwin, r.tables_matched,
        r.tables_missing_in_erwin, r.tables_extra_in_erwin,
        r.columns_pd, r.columns_erwin, r.columns_matched,
        r.columns_missing_in_erwin, r.columns_extra_in_erwin,
        r.fk_pd, r.fk_erwin, r.fk_matched,
        r.fk_missing_in_erwin, r.fk_extra_in_erwin,
        r.critical_count, r.warning_count, r.info_count,
    ]


def _paint_summary_row(ws, row: int, r: ValidationResult, review_threshold: float):
    """Status, fidelity and finding-count emphasis for one SUMMARY row."""
    status_cell = ws.cell(row=row, column=6)
    status_cell.fill = STATUS_FILL.get(r.status, PatternFill())
    status_cell.font = Font(bold=True,
                            color=C_WHITE if r.status in ("FAIL", "ERROR") else "FF000000")
    status_cell.alignment = CENTER

    fid = ws.cell(row=row, column=7)
    fid.number_format = "0.00"
    fid.alignment = CENTER
    if r.fidelity_score < 90:
        fid.font = Font(bold=True, color=C_DARKRED)
    elif r.fidelity_score < review_threshold:
        fid.font = Font(bold=True, color="FFBF8F00")
    else:
        fid.font = Font(bold=True, color="FF375623")

    ws.cell(row=row, column=24).font = Font(bold=True, color=C_RED) if r.critical_count else Font()
    ws.cell(row=row, column=25).font = Font(bold=True, color=C_DARKRED) if r.warning_count else Font()


def _write_summary_totals(ws, results: List[ValidationResult], trow: int):
    ws.cell(trow, 1, "TOTAL").font = Font(bold=True)
    def _sum(attr): return sum(getattr(r, attr) for r in results)
    for col, attr in SUMMARY_TOTAL_COLUMNS:
        ws.cell(trow, col, _sum(attr)).font = Font(bold=True)


def _write_summary_stats(ws, results: List[ValidationResult], totals: dict):
    """Stats block on the right."""
    stats_col = len(SUMMARY_HEADERS) + 2
    avg_fid = (round(sum(r.fidelity_score for r in results) / len(results), 2)
               if results else 0.0)
    stat_labels = [
        ("Total Models", len(results)),
        ("Average Fidelity %", avg_fid),
        ("PASS", totals["PASS"]),
        ("WARN", totals["WARN"]),
        ("FAIL", totals["FAIL"]),
        ("ERROR", totals["ERROR"]),
        ("Need Review", sum(1 for r in results if r.needs_review)),
    ]
    for i, (lbl, val) in enumerate(stat_labels, start=2):
        ws.cell(i, stats_col, lbl).font = Font(bold=True)
        ws.cell(i, stats_col + 1, val)


def _build_summary(wb: Workbook, results: List[ValidationResult]):
    ws = wb.active
    ws.title = "SUMMARY"
    ws.freeze_panes = "C3"
    ws.row_dimensions[1].height = 22
    ws.row_dimensions[2].height = 32

    last_col = get_column_letter(len(SUMMARY_HEADERS))
    _summary_banner(ws, last_col)

    _header_row(ws, SUMMARY_HEADERS, 2)

    totals = {"PASS": 0, "WARN": 0, "FAIL": 0, "ERROR": 0}  # nosec B105
    review_threshold = getattr(config, "FIDELITY_REVIEW_THRESHOLD", 90.0)

    for idx, r in enumerate(results, start=1):
        row = idx + 2
        _data_row(ws, _summary_values(idx, r), row, alt=(idx % 2 == 0))
        _paint_summary_row(ws, row, r, review_threshold)
        totals[r.status if r.status in totals else "ERROR"] += 1

    # Totals row
    _write_summary_totals(ws, results, len(results) + 3)

    # Stats block on the right
    _write_summary_stats(ws, results, totals)

    widths = [5, 30, 30, 20, 20, 8, 10, 8,
              10, 12, 12, 10, 9, 11, 13, 13, 10, 9,
              8, 10, 10, 9, 9, 9, 9, 8]
    _set_col_widths(ws, widths)
    ws.auto_filter.ref = f"A2:{last_col}{len(results) + 2}"


# ─── FINDINGS SHEET ───────────────────────────────────────────────────────────

def _build_findings(wb: Workbook, results: List[ValidationResult]):
    ws = wb.create_sheet("FINDINGS")
    ws.freeze_panes = "A2"
    headers = ["Model", "Status", "Fidelity %", "Category", "Severity",
               "Table", "Column", "Message", "PD Value", "ERwin Value"]
    _header_row(ws, headers, 1)

    row = 2
    cap = getattr(config, "MAX_DIFF_ROWS_PER_MODEL", 0)
    for r in results:
        model = os.path.basename(r.pd_file)
        findings = r.findings[:cap] if cap and cap > 0 else r.findings
        for f in findings:
            _data_row(ws, [
                model, r.status, r.fidelity_score, f.category, f.severity,
                f.table, f.column, f.message, f.pd_value, f.erwin_value,
            ], row, alt=(row % 2 == 0))
            sev = ws.cell(row=row, column=5)
            sev.fill = SEV_FILL.get(f.severity, PatternFill())
            sev.font = SEV_FONT.get(f.severity, Font())
            sev.alignment = CENTER
            st = ws.cell(row=row, column=2)
            st.fill = STATUS_FILL.get(r.status, PatternFill())
            st.alignment = CENTER
            row += 1

    _set_col_widths(ws, [30, 8, 10, 14, 10, 28, 24, 60, 28, 28])
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(row - 1, 1)}"


# ─── PER-MODEL SHEETS ─────────────────────────────────────────────────────────

def _build_model_sheet(wb: Workbook, r: ValidationResult):
    base = os.path.splitext(os.path.basename(r.pd_file))[0][:28] or "model"
    existing = [s.title for s in wb.worksheets]
    name, n = base, 1
    while name in existing:
        name = f"{base[:25]}_{n}"; n += 1

    ws = wb.create_sheet(name)
    ws.freeze_panes = "A3"
    ws.merge_cells("A1:J1")
    title = ws["A1"]
    title.value = (
        f"{os.path.basename(r.pd_file)}  vs  {os.path.basename(r.erwin_file)}"
        f"   |   {r.status}   |   Fidelity {r.fidelity_score:.2f}%"
        f"   |   Tables {r.tables_matched}/{r.tables_pd}"
        f"   |   Columns {r.columns_matched}/{r.columns_pd}"
        f"   |   {r.critical_count} critical / {r.warning_count} warning / {r.info_count} info"
    )
    title.font = Font(bold=True, color=C_WHITE, size=10)
    title.fill = STATUS_FILL.get(r.status, HEADER_FILL)
    title.alignment = LEFT

    headers = ["#", "Category", "Severity", "Table", "Column",
               "Message", "PD Value", "ERwin Value"]
    _header_row(ws, headers, 2, fill=SUBHDR_FILL)

    cap = getattr(config, "MAX_DIFF_ROWS_PER_MODEL", 0)
    findings = r.findings[:cap] if cap and cap > 0 else r.findings
    for i, f in enumerate(findings, start=1):
        row = i + 2
        _data_row(ws, [i, f.category, f.severity, f.table, f.column,
                       f.message, f.pd_value, f.erwin_value], row, alt=(i % 2 == 0))
        sev = ws.cell(row=row, column=3)
        sev.fill = SEV_FILL.get(f.severity, PatternFill())
        sev.font = SEV_FONT.get(f.severity, Font())
        sev.alignment = CENTER

    _set_col_widths(ws, [5, 14, 10, 28, 24, 60, 28, 28])
    if cap and cap > 0 and len(r.findings) > cap:
        note = len(findings) + 3
        ws.cell(note, 1,
                f"⚠ {len(r.findings) - cap} more findings not shown — see FINDINGS sheet."
                ).font = Font(italic=True, color=C_DARKRED)


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def generate_report(results: List[ValidationResult], output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, config.REPORT_FILENAME)

    wb = Workbook()
    _build_summary(wb, results)
    _build_findings(wb, results)
    if len(results) <= 200:
        for r in results:
            try:
                _build_model_sheet(wb, r)
            except Exception as e:
                logger.warning("Could not create sheet for %s: %s", r.pd_file, e)

    wb.save(out_path)
    logger.info("Report saved → %s", out_path)
    return out_path