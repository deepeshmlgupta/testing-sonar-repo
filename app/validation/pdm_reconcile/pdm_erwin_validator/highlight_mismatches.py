"""
Mismatch Highlighter  (PDM → ERwin validation report post-processor)
====================================================================
Takes the ``validation_report.xlsx`` that ``report_generator.py`` already
produces and marks up every *non-match* so a human can review thousands of
models quickly:

  1. SUMMARY sheet
       • every mismatch counter cell that is > 0 (Tbl / Col / FK Missing &
         Extra, CRITICAL, WARNING) is painted red / amber and given an Excel
         note (hover-comment) that spells out, in plain English, what did not
         match and where to look.
       • a new "Manual Check" column is appended to each model row with a one
         line verdict, e.g.  "REVIEW → 7 cols missing, 8 FKs missing, 9 critical".
         Sort / filter on it to jump straight to the models that need eyes.

  2. FINDINGS sheet
       • a "Match?" column is inserted marking each row MISMATCH, and the
         Message cell is tinted by severity so the not-matched rows stand out.

  3. MANUAL_REVIEW sheet  (new)
       • one row per genuine mismatch (CRITICAL + WARNING by default), each with
         a ready-to-read "Review Message" sentence — table, column, what is
         wrong, PD value vs ERwin value, and the action to take.  AutoFilter is
         on, so a reviewer can filter to one model at a time.  This is the sheet
         that makes 5,000-model manual validation practical, because the
         per-model detail tabs are not emitted for large runs.

The script is header-driven (it locates columns by their header text, not a
fixed position), so it works on both the PDM and the CDM validator reports even
if the column order changes.

Usage
-----
  python highlight_mismatches.py                       # ./validation_report.xlsx  -> ./validation_report_highlighted.xlsx
  python highlight_mismatches.py path\to\report.xlsx   # explicit input
  python highlight_mismatches.py report.xlsx -o out.xlsx
  python highlight_mismatches.py report.xlsx --in-place # overwrite the input file
  python highlight_mismatches.py report.xlsx --include-info   # also flag INFO-level notes

Or call it from your batch runner right after generate_report():
  from highlight_mismatches import highlight_report
  highlight_report(report_path)          # writes <name>_highlighted.xlsx next to it

Dependencies:  pip install openpyxl
"""

import argparse
import os
import sys
import logging

from openpyxl import load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

# ─── COLOUR PALETTE  (matches report_generator.py) ──────────────────────────────
C_RED     = "FFFF0000"   # missing / critical  — hard problem
C_AMBER   = "FFFFC000"   # extra / warning     — softer problem
C_BLUE    = "FF4472C4"   # info
C_GREEN   = "FF92D050"   # OK / matched
C_HEADER  = "FF1F3864"
C_WHITE   = "FFFFFFFF"
C_BLACK   = "FF000000"
C_OKTEXT  = "FF375623"

FILL_RED   = PatternFill("solid", fgColor=C_RED)
FILL_AMBER = PatternFill("solid", fgColor=C_AMBER)
FILL_BLUE  = PatternFill("solid", fgColor=C_BLUE)
FILL_GREEN = PatternFill("solid", fgColor=C_GREEN)

THIN   = Side(style="thin", color="FFB8B8B8")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEADER_FONT = Font(bold=True, color=C_WHITE, name="Calibri", size=10)
HEADER_FILL = PatternFill("solid", fgColor=C_HEADER)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT   = Alignment(horizontal="left",   vertical="center", wrap_text=True)

COMMENT_AUTHOR = "Validator"

# ─── SUMMARY MISMATCH RULES ─────────────────────────────────────────────────────
# header text  ->  (fill, level, human phrase).  {n} is replaced with the value.
# level "hard" = red, "soft" = amber.  Only fired when the cell value is > 0.
SUMMARY_RULES = {
    "Tbl Missing": (FILL_RED,   "hard",
                    "{n} table(s) exist in PowerDesigner but are MISSING in ERwin."),
    "Tbl Extra":   (FILL_AMBER, "soft",
                    "{n} table(s) exist in ERwin but are NOT in PowerDesigner (extra)."),
    "Col Missing": (FILL_RED,   "hard",
                    "{n} column(s) exist in PowerDesigner but are MISSING in ERwin."),
    "Col Extra":   (FILL_AMBER, "soft",
                    "{n} column(s) exist in ERwin but are NOT in PowerDesigner (extra)."),
    "FK Missing":  (FILL_RED,   "hard",
                    "{n} foreign key(s) in PowerDesigner are MISSING in ERwin."),
    "FK Extra":    (FILL_AMBER, "soft",
                    "{n} foreign key(s) in ERwin are NOT in PowerDesigner (extra)."),
    "CRITICAL":    (FILL_RED,   "hard",
                    "{n} CRITICAL difference(s) — this model FAILED and must be reviewed."),
    "WARNING":     (FILL_AMBER, "soft",
                    "{n} WARNING difference(s) — please review."),
}

# short label used in the "Manual Check" verdict column
SUMMARY_SHORT = {
    "Tbl Missing": "{n} tbl missing",
    "Tbl Extra":   "{n} tbl extra",
    "Col Missing": "{n} cols missing",
    "Col Extra":   "{n} cols extra",
    "FK Missing":  "{n} FKs missing",
    "FK Extra":    "{n} FKs extra",
    "CRITICAL":    "{n} critical",
    "WARNING":     "{n} warning",
}


# ─── HELPERS ────────────────────────────────────────────────────────────────────

def _num(value) -> float:
    """Best-effort numeric read of a cell value (blank / text -> 0)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _find_header_row(ws, wanted, max_scan=5):
    """
    Return (header_row_index, {header_text: column_index}) for the first row
    (within the first `max_scan` rows) that contains any of the `wanted` labels.
    Header text is matched case-insensitively and trimmed.
    """
    wanted_lc = {w.lower() for w in wanted}
    for row in range(1, max_scan + 1):
        mapping = {}
        for col in range(1, ws.max_column + 1):
            val = ws.cell(row=row, column=col).value
            if isinstance(val, str) and val.strip():
                mapping[val.strip()] = col
        if any(h.lower() in wanted_lc for h in mapping):
            return row, mapping
    return None, {}


def _col_by_name(mapping, *names):
    """First matching column index for any of the given header names (case-insensitive)."""
    lower = {k.lower(): v for k, v in mapping.items()}
    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _note(cell, text):
    """Attach an Excel note (hover comment) to a cell, sized to fit."""
    comment = Comment(text, COMMENT_AUTHOR)
    comment.width = 300
    comment.height = 120
    cell.comment = comment


# ─── 1. SUMMARY SHEET ───────────────────────────────────────────────────────────

def _process_summary(ws):
    """Highlight mismatch cells, add notes, and append a Manual Check verdict."""
    header_row, headers = _find_header_row(ws, list(SUMMARY_RULES) + ["Status", "PD File"])
    if not header_row:
        logger.warning("SUMMARY: could not locate a header row; skipping.")
        return 0

    model_col  = _col_by_name(headers, "PD File", "CDM File", "Model")
    status_col = _col_by_name(headers, "Status")
    review_col = _col_by_name(headers, "Review?")

    # rule columns actually present in this report
    rule_cols = {h: headers[h] for h in SUMMARY_RULES if h in headers}

    # append a "Manual Check" verdict column
    verdict_col = ws.max_column + 1
    hdr = ws.cell(row=header_row, column=verdict_col, value="Manual Check")
    hdr.font, hdr.fill, hdr.alignment, hdr.border = HEADER_FONT, HEADER_FILL, CENTER, BORDER
    ws.column_dimensions[get_column_letter(verdict_col)].width = 42

    flagged_rows = 0
    row = header_row + 1
    while row <= ws.max_row:
        # stop at the TOTAL / blank tail
        first = ws.cell(row=row, column=1).value
        model_name = ws.cell(row=row, column=model_col).value if model_col else None
        if isinstance(first, str) and first.strip().upper() == "TOTAL":
            break
        if first is None and model_name is None:
            row += 1
            continue

        parts = []           # for the verdict string
        row_has_issue = False

        for header, col in rule_cols.items():
            cell = ws.cell(row=row, column=col)
            n = _num(cell.value)
            if n > 0:
                fill, _level, phrase = SUMMARY_RULES[header]
                cell.fill = fill
                cell.font = Font(bold=True,
                                 color=C_WHITE if fill is FILL_RED else C_BLACK)
                cell.alignment = CENTER
                msg = phrase.format(n=int(n))
                if model_name:
                    msg += (f"\nOpen the FINDINGS / MANUAL_REVIEW sheet and filter "
                            f"Model = '{model_name}' to see exactly which.")
                _note(cell, msg)
                parts.append(SUMMARY_SHORT[header].format(n=int(n)))
                row_has_issue = True

        # verdict cell
        vcell = ws.cell(row=row, column=verdict_col)
        vcell.border = BORDER
        vcell.alignment = LEFT
        if row_has_issue:
            vcell.value = "REVIEW → " + ", ".join(parts)
            vcell.fill  = FILL_AMBER
            vcell.font  = Font(bold=True, color=C_BLACK)
            flagged_rows += 1
        else:
            vcell.value = "OK — full match"
            vcell.font  = Font(bold=True, color=C_OKTEXT)

        # extra emphasis on a non-PASS status cell
        if status_col:
            st = ws.cell(row=row, column=status_col)
            if isinstance(st.value, str) and st.value.strip().upper() not in ("PASS", ""):
                _note(st, f"Status = {st.value}. See the Manual Check column for a summary "
                          f"and the MANUAL_REVIEW sheet for line-by-line detail.")
        row += 1

    # keep the filter spanning the new column
    if ws.auto_filter.ref:
        last = get_column_letter(verdict_col)
        try:
            top_left = ws.auto_filter.ref.split(":")[0]
            ws.auto_filter.ref = f"{top_left}:{last}{row - 1}"
        except Exception:  # nosec B110
            pass
    return flagged_rows


# ─── 2. FINDINGS SHEET  → build MANUAL_REVIEW from it ───────────────────────────

MISMATCH_LABEL = {
    "TABLE":       "Table not matched",
    "COLUMN":      "Column not matched",
    "DATA_TYPE":   "Data type differs",
    "NULLABILITY": "Nullability differs",
    "DEFAULT":     "Default value differs",
    "PRIMARY_KEY": "Primary key differs",
    "FOREIGN_KEY": "Foreign key not matched",
    "INDEX":       "Index not matched",
}


def _process_findings(ws):
    """Mark every finding row as a MISMATCH and tint the message; return the rows."""
    header_row, headers = _find_header_row(ws, ["Message", "Severity", "Category"])
    if not header_row:
        logger.warning("FINDINGS: could not locate a header row; skipping.")
        return []

    c_model = _col_by_name(headers, "Model")
    c_stat  = _col_by_name(headers, "Status")
    c_fid   = _col_by_name(headers, "Fidelity %")
    c_cat   = _col_by_name(headers, "Category")
    c_sev   = _col_by_name(headers, "Severity")
    c_tbl   = _col_by_name(headers, "Table", "Object")
    c_col   = _col_by_name(headers, "Column", "Member")
    c_msg   = _col_by_name(headers, "Message")
    c_pd    = _col_by_name(headers, "PD Value", "PowerDesigner Value")
    c_ew    = _col_by_name(headers, "ERwin Value", "erwin Value")

    # insert a "Match?" column right after Severity (or at the front if not found)
    insert_at = (c_sev + 1) if c_sev else 1
    ws.insert_cols(insert_at)
    # any column indices at/after the insert point shift right by one
    shift = lambda c: (c + 1) if (c is not None and c >= insert_at) else c
    c_model, c_stat, c_fid, c_cat, c_sev = map(shift, (c_model, c_stat, c_fid, c_cat, c_sev))
    c_tbl, c_col, c_msg, c_pd, c_ew      = map(shift, (c_tbl, c_col, c_msg, c_pd, c_ew))

    hdr = ws.cell(row=header_row, column=insert_at, value="Match?")
    hdr.font, hdr.fill, hdr.alignment, hdr.border = HEADER_FONT, HEADER_FILL, CENTER, BORDER
    ws.column_dimensions[get_column_letter(insert_at)].width = 12

    def read(row, col):
        return ws.cell(row=row, column=col).value if col else None

    rows = []
    for row in range(header_row + 1, ws.max_row + 1):
        if read(row, c_msg) is None and read(row, c_cat) is None:
            continue
        sev = (read(row, c_sev) or "").strip().upper() if isinstance(read(row, c_sev), str) else read(row, c_sev)

        mark = ws.cell(row=row, column=insert_at, value="MISMATCH")
        mark.font = Font(bold=True, color=C_WHITE if sev == "CRITICAL" else C_BLACK)
        mark.fill = FILL_RED if sev == "CRITICAL" else (FILL_AMBER if sev == "WARNING" else FILL_BLUE)
        mark.alignment = CENTER
        mark.border = BORDER

        # tint the message so it stands out when scanning
        if c_msg:
            mcell = ws.cell(row=row, column=c_msg)
            if sev in ("CRITICAL", "WARNING"):
                mcell.font = Font(bold=(sev == "CRITICAL"),
                                  color=C_RED if sev == "CRITICAL" else "FFBF8F00")

        rows.append({
            "model": read(row, c_model), "status": read(row, c_stat),
            "fidelity": read(row, c_fid), "category": read(row, c_cat),
            "severity": sev, "table": read(row, c_tbl), "column": read(row, c_col),
            "message": read(row, c_msg), "pd": read(row, c_pd), "ew": read(row, c_ew),
        })

    # keep the filter spanning the new column
    if ws.auto_filter.ref:
        try:
            last = get_column_letter(ws.max_column)
            end_row = ws.auto_filter.ref.split(":")[-1]
            end_row = "".join(ch for ch in end_row if ch.isdigit())
            ws.auto_filter.ref = f"A{header_row}:{last}{end_row}"
        except Exception:  # nosec B110
            pass
    return rows


# ─── 3. MANUAL_REVIEW SHEET ─────────────────────────────────────────────────────

MANUAL_HEADERS = [
    "#", "Model", "Status", "Fidelity %", "Severity", "Mismatch Type",
    "Table", "Column", "Review Message (what to check manually)",
    "PD Value", "ERwin Value",
]


def _build_manual_review(wb, findings, include_info=False):
    """Create a single flat sheet of only the mismatches, with a review sentence."""
    if "MANUAL_REVIEW" in wb.sheetnames:
        del wb["MANUAL_REVIEW"]
    ws = wb.create_sheet("MANUAL_REVIEW", index=1)   # right after SUMMARY
    ws.freeze_panes = "A2"

    for col, val in enumerate(MANUAL_HEADERS, start=1):
        cell = ws.cell(row=1, column=col, value=val)
        cell.font, cell.fill, cell.alignment, cell.border = HEADER_FONT, HEADER_FILL, CENTER, BORDER

    keep = ("CRITICAL", "WARNING") + (("INFO",) if include_info else ())
    order = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    rows = [f for f in findings if (f["severity"] or "INFO") in keep]
    rows.sort(key=lambda f: (str(f["model"] or ""),
                             order.get(f["severity"], 3),
                             str(f["category"] or "")))

    r = 2
    for i, f in enumerate(rows, start=1):
        table  = f["table"] or ""
        column = f["column"] or ""
        where  = ".".join(x for x in (table, column) if x) or "(model level)"
        msg    = f["message"] or MISMATCH_LABEL.get(f["category"], "Difference")
        pd_v   = "" if f["pd"] in (None, "—") else f["pd"]
        ew_v   = "" if f["ew"] in (None, "—") else f["ew"]
        detail = ""
        if pd_v or ew_v:
            detail = f"  [PD: {pd_v or '(none)'}  vs  ERwin: {ew_v or '(none)'}]"
        review = f"{where} — {msg}.{detail}  → Verify manually in the model."

        values = [
            i, f["model"], f["status"], f["fidelity"], f["severity"],
            MISMATCH_LABEL.get(f["category"], f["category"]),
            table, column, review, f["pd"], f["ew"],
        ]
        for col, val in enumerate(values, start=1):
            cell = ws.cell(row=r, column=col, value=val)
            cell.alignment, cell.border = LEFT, BORDER
        # colour the severity cell
        sev_cell = ws.cell(row=r, column=5)
        sev_cell.fill = (FILL_RED if f["severity"] == "CRITICAL"
                         else FILL_AMBER if f["severity"] == "WARNING" else FILL_BLUE)
        sev_cell.font = Font(bold=True,
                             color=C_WHITE if f["severity"] in ("CRITICAL", "INFO") else C_BLACK)
        sev_cell.alignment = CENTER
        r += 1

    if r == 2:   # nothing to review
        cell = ws.cell(row=2, column=1, value="No CRITICAL or WARNING mismatches found — all models match.")
        cell.font = Font(bold=True, color=C_OKTEXT)

    widths = [6, 30, 8, 10, 10, 22, 26, 22, 80, 26, 26]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.auto_filter.ref = f"A1:{get_column_letter(len(MANUAL_HEADERS))}{max(r - 1, 1)}"
    return len(rows)


# ─── PUBLIC API ─────────────────────────────────────────────────────────────────

def highlight_report(input_path, output_path=None, in_place=False, include_info=False):
    """
    Post-process a validation_report.xlsx: highlight non-matches, add notes,
    a Manual Check column, and a MANUAL_REVIEW sheet.  Returns the output path.
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(input_path)

    wb = load_workbook(input_path)

    flagged = 0
    if "SUMMARY" in wb.sheetnames:
        flagged = _process_summary(wb["SUMMARY"])
    else:
        logger.warning("No SUMMARY sheet found in %s", input_path)

    findings = []
    if "FINDINGS" in wb.sheetnames:
        findings = _process_findings(wb["FINDINGS"])
    else:
        logger.warning("No FINDINGS sheet found in %s", input_path)

    reviewed = _build_manual_review(wb, findings, include_info=include_info)

    if in_place:
        output_path = input_path
    elif not output_path:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_highlighted{ext}"

    wb.save(output_path)
    logger.info("Highlighted report saved → %s", output_path)
    print(f"[OK] {flagged} model(s) flagged for review, "
          f"{reviewed} mismatch line(s) in MANUAL_REVIEW.")
    print(f"[OK] Saved → {output_path}")
    return output_path


def _parse_args():
    p = argparse.ArgumentParser(
        description="Highlight non-matches in a PDM→ERwin validation report.")
    p.add_argument("input", nargs="?", default="validation_report.xlsx",
                   help="Path to the validation report .xlsx (default: ./validation_report.xlsx)")
    p.add_argument("-o", "--output", help="Output path (default: <input>_highlighted.xlsx)")
    p.add_argument("--in-place", action="store_true", help="Overwrite the input file")
    p.add_argument("--include-info", action="store_true",
                   help="Also include INFO-level notes in MANUAL_REVIEW")
    return p.parse_args()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    args = _parse_args()
    try:
        highlight_report(args.input, output_path=args.output,
                         in_place=args.in_place, include_info=args.include_info)
    except FileNotFoundError:
        print(f"[ERROR] File not found: {args.input}")
        print("        Run this from your report folder, or pass the path, e.g.:")
        print(r"        python highlight_mismatches.py C:\...\ValidationReports\validation_report.xlsx")
        sys.exit(1)


if __name__ == "__main__":
    main()
