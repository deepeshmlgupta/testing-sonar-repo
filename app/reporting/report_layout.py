"""
Report Layout and Workbook Helpers
==================================
One place that decides WHERE a model's reports go and WHAT they are called, and
the openpyxl helpers the V2 and V3 builders share.

FOLDER LAYOUT
-------------
Every model gets its own folder inside its model type's existing reporting
folder, holding the three reports of the migration flow:

    app/reporting/cdm_reports/<model>/
        <model>_V1_Initial_Fidelity_Report.xlsx    SAP PD vs erwin XML V1
        <model>_V2_UDP_Mapping_Report.xlsx         Extended Attributes -> UDPs
        <model>_V3_Final_Fidelity_Report.xlsx      final, post-enrichment
        manual_review_report/                      created only when V3 < 90%
            <model>_V3_Final_Fidelity_Report.xlsx
            <model>.xml   <model>.erwin

    app/reporting/ldm_reports/<model>/   … same
    app/reporting/pdm_reports/<model>/   … same

A model that passes the gate is promoted to erwinmodels/3_final instead, and its
V3 report is copied there alongside it.

SHEET COPYING
-------------
The V2 and V3 builders both assemble a workbook from sheets written by other
generators.  Two things have to be handled or the result is silently wrong:

* Sheet names collide.  Both UDP workbooks contain a sheet called "Summary",
  and Excel sheet names are case-insensitive, so "Summary" also clashes with a
  tier report's "SUMMARY".  Every copied sheet is renamed.
* Renaming breaks formulas.  The UDP workbooks use cross-sheet formulas such as
  ``=COUNTIF('Value Reconciliation'!$C:$C,$A5)``.  Each copied formula has its
  sheet references rewritten to the new names, or the sheet renders #REF!.
"""

from __future__ import annotations

import logging
import os
import re
from copy import copy
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

# ─── FOLDERS ──────────────────────────────────────────────────────────────────
REPORTING_ROOT = Path("app/reporting")
TIER_FOLDERS: Dict[str, str] = {
    "CDM": "cdm_reports",
    "LDM": "ldm_reports",
    "PDM": "pdm_reports",
}
MANUAL_REVIEW_DIRNAME = "manual_review_report"

# ─── REPORT NAMES ─────────────────────────────────────────────────────────────
V1_SUFFIX = "V1_Initial_Fidelity_Report"
V2_SUFFIX = "V2_UDP_Mapping_Report"
V3_SUFFIX = "V3_Final_Fidelity_Report"

# ─── SHEET RENAMING (source name -> name in the assembled workbook) ───────────
MIGRATION_SHEETS: Dict[str, str] = {
    "Summary": "UDP_MAPPING_SUMMARY",
    "UDP Definition": "UDP_DEFINITIONS",
    "Value Reconciliation": "UDP_VALUE_RECON",
    "Exceptions": "UDP_EXCEPTIONS",
}
COMPARISON_SHEETS: Dict[str, str] = {
    "Summary": "UDP_COMPARISON_SUMMARY",
    "UDP Comparison": "UDP_COMPARISON",
    "By UDP": "UDP_BY_UDP",
    "Diagnostics": "UDP_DIAGNOSTICS",
}

NOT_AVAILABLE = "n/a"
HEADER_FILL = "FF1F4E79"
HEADER_FONT_COLOR = "FFFFFFFF"
PASS_FILL = "FFC6EFCE"    # nosec B105 - an ARGB fill colour, not a credential
FAIL_FILL = "FFFFC7CE"    # nosec B105 - an ARGB fill colour, not a credential


# ═════════════════════════════════════════════════════════════════════════════
#  Paths
# ═════════════════════════════════════════════════════════════════════════════

def tier_dir(model_type: str) -> Path:
    """`app/reporting/<tier>_reports` for CDM, LDM or PDM."""
    folder = TIER_FOLDERS.get((model_type or "").upper())
    if folder is None:
        raise ValueError(f"Unknown model type: {model_type!r}")
    return REPORTING_ROOT / folder


def model_dir(model_type: str, model_name: str, create: bool = True) -> Path:
    """`app/reporting/<tier>_reports/<model>` — this model's own report folder."""
    path = tier_dir(model_type) / model_name
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def manual_review_dir(model_type: str, model_name: str, create: bool = True) -> Path:
    """`…/<model>/manual_review_report` — where a sub-90% model is sent."""
    path = model_dir(model_type, model_name, create=create) / MANUAL_REVIEW_DIRNAME
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def report_name(model_name: str, suffix: str, score: float = None,
                status: str = "") -> str:
    """`<model>_<V1|V2|V3>_<…>_Report[_<score>%][_<PASS|FAIL>].xlsx`."""
    parts = [model_name, suffix]
    if score is not None:
        parts.append(f"{float(score):.1f}%")
    if status:
        parts.append(status.upper())
    return "_".join(parts) + ".xlsx"


# ═════════════════════════════════════════════════════════════════════════════
#  Workbook helpers
# ═════════════════════════════════════════════════════════════════════════════

def rewrite_formula(text: str, renames: Dict[str, str]) -> str:
    """
    Repoint a formula's sheet references at the renamed sheets.

    Handles both the quoted form Excel uses for names containing spaces
    (`'Value Reconciliation'!`) and the bare form (`Exceptions!`).
    """
    for old, new in renames.items():
        text = text.replace(f"'{old}'!", f"{new}!")
        text = re.sub(rf"(?<![A-Za-z0-9_.'!]){re.escape(old)}!", f"{new}!", text)
    return text


def _copy_cell(source, target, renames: Dict[str, str]) -> None:
    value = source.value
    if isinstance(value, str) and value.startswith("="):
        value = rewrite_formula(value, renames)
    target.value = value
    if source.has_style:
        target.font = copy(source.font)
        target.fill = copy(source.fill)
        target.border = copy(source.border)
        target.alignment = copy(source.alignment)
        target.number_format = source.number_format
        target.protection = copy(source.protection)


def copy_sheet(source_ws, workbook, title: str, renames: Dict[str, str]) -> None:
    """Copy one worksheet's values, styles and layout into `workbook`."""
    target_ws = workbook.create_sheet(title)

    for row in source_ws.iter_rows():
        for cell in row:
            _copy_cell(cell, target_ws.cell(row=cell.row, column=cell.column), renames)

    for key, dimension in source_ws.column_dimensions.items():
        target_ws.column_dimensions[key].width = dimension.width
        target_ws.column_dimensions[key].hidden = dimension.hidden
    for key, dimension in source_ws.row_dimensions.items():
        target_ws.row_dimensions[key].height = dimension.height

    for merged in source_ws.merged_cells.ranges:
        target_ws.merge_cells(str(merged))

    target_ws.freeze_panes = source_ws.freeze_panes
    if source_ws.auto_filter.ref:
        target_ws.auto_filter.ref = source_ws.auto_filter.ref
    target_ws.sheet_view.showGridLines = source_ws.sheet_view.showGridLines


def append_workbook(workbook, source_path: str, mapping: Dict[str, str]) -> List[str]:
    """Copy the mapped sheets of one workbook in. Returns the titles added."""
    if not source_path or not os.path.isfile(source_path):
        return []
    try:
        from openpyxl import load_workbook
        source = load_workbook(source_path)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read workbook %s: %s", source_path, exc)
        return []

    added: List[str] = []
    try:
        for name in source.sheetnames:
            title = mapping.get(name)
            if not title or title in workbook.sheetnames:
                continue
            copy_sheet(source[name], workbook, title, mapping)
            added.append(title)
    finally:
        source.close()
    return added


def drop_sheets(workbook, titles) -> List[str]:
    """
    Remove sheets an assembled report must not carry (UDP_DETAIL by default).
    Matching is case-insensitive, because Excel sheet names are.
    """
    wanted = {str(title).strip().upper() for title in (titles or ())}
    removed = []
    for name in workbook.sheetnames:
        if name.strip().upper() in wanted:
            del workbook[name]
            removed.append(name)
    return removed


def write_key_value_sheet(workbook, title: str, heading: str, subtitle: str,
                          rows, highlight_label: str = "", highlight_pass: bool = True,
                          index: int = 0) -> None:
    """
    Write a Measure / Value / Source sheet — the shape both the V2 and the V3
    overview use. `rows` is an iterable of (label, value, note).
    """
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    worksheet = workbook.create_sheet(title, index)
    worksheet["A1"] = heading
    worksheet["A1"].font = Font(bold=True, size=14)
    worksheet.merge_cells("A1:C1")
    worksheet["A2"] = subtitle
    worksheet["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    worksheet.merge_cells("A2:C2")
    worksheet.row_dimensions[2].height = 30

    header_fill = PatternFill("solid", fgColor=HEADER_FILL)
    header_font = Font(bold=True, color=HEADER_FONT_COLOR)
    for column, caption in enumerate(("Measure", "Value", "Where it comes from"), start=1):
        cell = worksheet.cell(4, column, caption)
        cell.fill, cell.font = header_fill, header_font

    row_no = 5
    for label, value, note in rows:
        worksheet.cell(row_no, 1, label).font = Font(bold=bool(label) and not note)
        cell = worksheet.cell(row_no, 2, value)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        worksheet.cell(row_no, 3, note).alignment = Alignment(wrap_text=True, vertical="top")
        if highlight_label and label == highlight_label:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid",
                                    fgColor=PASS_FILL if highlight_pass else FAIL_FILL)
        row_no += 1

    for index_, width in enumerate((38, 62, 70), start=1):
        worksheet.column_dimensions[get_column_letter(index_)].width = width
    worksheet.freeze_panes = "A5"


__all__ = [
    "REPORTING_ROOT", "TIER_FOLDERS", "MANUAL_REVIEW_DIRNAME",
    "V1_SUFFIX", "V2_SUFFIX", "V3_SUFFIX",
    "MIGRATION_SHEETS", "COMPARISON_SHEETS", "NOT_AVAILABLE",
    "tier_dir", "model_dir", "manual_review_dir", "report_name",
    "rewrite_formula", "copy_sheet", "append_workbook", "drop_sheets",
    "write_key_value_sheet",
]
