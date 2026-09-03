"""
udp_compare.py
--------------
Compares the UDP values PowerDesigner held against the UDP values erwin actually
holds, and writes one Excel report per model:

    <outdir>/<model>_UDP_Comparison.xlsx

    Sheet "UDP Comparison"  Entity Name | UDP Name | SAP Value | erwin Value | Status | Note
    Sheet "Summary"         counts by status, pass rate, verdict
    Sheet "By UDP"          per-UDP rollup, so a UDP that failed everywhere is obvious
    Sheet "Diagnostics"     how erwin was read, and whether that reading is trustworthy

The SAP side comes from `property_manifest.json`. The erwin side is read back out
of the saved model by `udp_readback.py` - not from the injector's own log, which
is what made the previous verification circular.

Statuses
--------
The three asked for:

    PASS            erwin holds exactly the value PowerDesigner held.
    MISMATCH        erwin holds a different value.
    MISSING         PowerDesigner had a value and erwin holds nothing.

Two more are needed to keep those three honest:

    BLANK IN SAP    The property exists on the source object but holds no value,
                    so erwin holding nothing is correct. Counting these as
                    MISSING would invent 1,641 failures on SUBSURFACE AND WELLS
                    that are really empty source data.
    EXTRA IN ERWIN  erwin holds a UDP value with no counterpart in the manifest.
                    Usually a leftover from an earlier run, which matters because
                    it means the model carries a value nobody can trace.

Usage:
  python udp_compare.py --model_name "MODEL" --manifest ... --schema ...
                        --erwin ... --outdir output_excel_reports
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    print("Error: The 'openpyxl' library is not installed. Please run 'pip install openpyxl'.")
    sys.exit(1)

import udp_readback

logger = logging.getLogger(__name__)

# ─── statuses ─────────────────────────────────────────────────────────────────
ST_PASS = "PASS"  # nosec B105
ST_MISMATCH = "MISMATCH"
ST_MISSING = "MISSING"
ST_BLANK = "BLANK IN SAP"
ST_EXTRA = "EXTRA IN ERWIN"

STATUS_ORDER = [ST_MISMATCH, ST_MISSING, ST_EXTRA, ST_PASS, ST_BLANK]

# ─── presentation ─────────────────────────────────────────────────────────────
FONT = "Arial"
HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(name=FONT, size=10, bold=True, color="FFFFFF")
TITLE_FONT = Font(name=FONT, size=14, bold=True, color="1F3864")
SECTION_FONT = Font(name=FONT, size=11, bold=True, color="1F3864")
LABEL_FONT = Font(name=FONT, size=10, bold=True)
SECTION_FILL = PatternFill("solid", fgColor="D9E2F3")
FILL_PASS = PatternFill("solid", fgColor="E2EFDA")
FILL_BAD = PatternFill("solid", fgColor="FCE4E4")
FILL_WARN = PatternFill("solid", fgColor="FFF2CC")
THIN = Side(style="thin", color="B4C6E7")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(vertical="top", wrap_text=True)
TOP = Alignment(vertical="top")

MAX_CELL = 2000

SH_SUMMARY = "Summary"
SH_COMPARE = "UDP Comparison"
SH_BY_UDP = "By UDP"
SH_DIAG = "Diagnostics"


def clean(value) -> str:
    if value is None:
        return ""
    text = str(value)
    text = ILLEGAL_CHARACTERS_RE.sub("", text).replace("\x00", "")
    if len(text) > MAX_CELL:
        text = text[:MAX_CELL] + " ...[truncated]"
    return text


def blank_safe(value):
    text = clean(value)
    return text if text else None


def header_row(ws, headers, row=1):
    for col, text in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=text)
        cell.font = HDR_FONT
        cell.fill = HDR_FILL
        cell.border = BOX
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 28
    ws.freeze_panes = f"A{row + 1}"
    ws.auto_filter.ref = f"A{row}:{get_column_letter(len(headers))}{row}"


def set_widths(ws, widths):
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width


# ─── the comparison ───────────────────────────────────────────────────────────
@dataclass
class ComparisonResult:
    """SAP vs erwin, for one model."""

    model_name: str
    model_type: str = ""
    sap_model: str = ""
    erwin_model: str = ""
    generated: str = ""
    rows: list = field(default_factory=list)     # comparison rows
    counts: Counter = field(default_factory=Counter)
    readback: udp_readback.Readback | None = None
    warnings: list = field(default_factory=list)

    @property
    def comparable(self) -> int:
        """Rows where SAP had a value to migrate, so a verdict is meaningful."""
        return (self.counts[ST_PASS] + self.counts[ST_MISMATCH]
                + self.counts[ST_MISSING])

    @property
    def pass_rate(self) -> float:
        return round(self.counts[ST_PASS] / self.comparable * 100, 2) if self.comparable else 0.0

    @property
    def status(self) -> str:
        if self.readback is None or self.readback.method == "unavailable":
            return "NOT VERIFIED - ERWIN COULD NOT BE READ"
        if not self.readback.reliable:
            return "NOT VERIFIED - READBACK UNRELIABLE"
        if not self.comparable:
            return "NO SOURCE VALUES TO MIGRATE"
        if self.counts[ST_PASS] == self.comparable:
            return ST_PASS
        if self.counts[ST_PASS] == 0:
            return "FAIL - NO VALUES MATCHED"
        return "REVIEW REQUIRED"

    def summary_line(self) -> str:
        if self.readback is None or self.readback.method == "unavailable":
            return f"{self.model_name}: erwin could not be read; nothing verified."
        return (f"{self.model_name}: {self.counts[ST_PASS]}/{self.comparable} UDP "
                f"values match erwin ({self.pass_rate:.2f}%), "
                f"{self.counts[ST_MISMATCH]} mismatched, "
                f"{self.counts[ST_MISSING]} missing in erwin.")


def compare(model_name: str,
            manifest: list,
            schema: list,
            readback: udp_readback.Readback,
            sap_model: str = "",
            erwin_model: str = "",
            model_type: str = "") -> ComparisonResult:
    """
    Join the SAP manifest to the erwin readback, one row per UDP per object.

    Duplicate manifest rows for the same (object, UDP) are collapsed: erwin holds
    one value per UDP per object, so reporting several source rows against it
    would imply erwin lost values it was never asked to keep. The collapse is
    counted and reported as a warning instead.
    """
    result = ComparisonResult(
        model_name=model_name,
        model_type=(model_type or "").lstrip(".").lower(),
        sap_model=sap_model,
        erwin_model=erwin_model,
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        readback=readback,
    )

    root_name = (readback.model_root_name if readback else "") or model_name

    # ---- collapse the SAP side to one value per (owner, udp) -----------------
    sap: dict = {}
    duplicates = 0
    nameless = 0
    for row in manifest:
        owner = str(row.get("entity_name", "")).strip()
        applied_to = "Entity"
        if not owner:
            # A manifest row with no entity name has nowhere to go but the model
            # root, where every such row overwrites the previous one.
            owner = root_name
            applied_to = "Model Root"
            nameless += 1
        udp = str(row.get("udp", ""))
        if not udp:
            continue
        key = (owner.strip().lower(), udp)
        if key in sap:
            duplicates += 1
            continue
        sap[key] = {
            "owner": owner,
            "udp": udp,
            "value": str(row.get("value", "")),
            "applied_to": applied_to,
            "source_path": str(row.get("source_path", "")),
        }

    if nameless:
        result.warnings.append(
            f"{nameless} manifest row(s) carry no entity name. Every one of them "
            f"resolves to the model root, so they overwrite each other and only "
            f"the last survives. This is the symptom of PowerDesigner Ref "
            f"pointer stubs being extracted as entities - see the Diagnostics "
            f"sheet.")
    if duplicates:
        result.warnings.append(
            f"{duplicates} duplicate (object, UDP) manifest row(s) were collapsed; "
            f"erwin stores one value per UDP per object.")

    # ---- one comparison row per SAP entry ------------------------------------
    for key, src in sorted(sap.items(), key=lambda kv: (kv[1]["owner"].lower(),
                                                        kv[1]["udp"])):
        erwin_value = readback.get(src["owner"], src["udp"]) if readback else None
        sap_value = src["value"]
        note = ""

        if not str(sap_value).strip():
            status = ST_BLANK
            note = ("The property exists on the source object but holds no value, "
                    "so erwin holding nothing is correct.")
        elif erwin_value is None:
            status = ST_MISSING
            if readback and src["applied_to"] == "Entity" and not readback.has_owner(src["owner"]):
                note = ("No object of this name exists in the erwin model, so the "
                        "value had nowhere to land.")
            else:
                note = "erwin holds no value for this UDP on this object."
        elif str(erwin_value) == str(sap_value):
            status = ST_PASS
        else:
            status = ST_MISMATCH
            note = "erwin holds a different value than PowerDesigner."

        result.rows.append({
            "owner": src["owner"],
            "applied_to": src["applied_to"],
            "udp": src["udp"],
            "source_path": src["source_path"],
            "sap_value": sap_value,
            "erwin_value": "" if erwin_value is None else erwin_value,
            "status": status,
            "note": note,
        })
        result.counts[status] += 1

    # ---- anything erwin holds that the manifest does not explain -------------
    if readback:
        for (owner_key, udp), value in readback.values.items():
            if (owner_key, udp) in sap:
                continue
            result.rows.append({
                "owner": readback.owners.get(owner_key, owner_key),
                "applied_to": "Model Root" if owner_key == root_name.strip().lower()
                              else "Entity",
                "udp": udp,
                "source_path": "",
                "sap_value": "",
                "erwin_value": value,
                "status": ST_EXTRA,
                "note": ("erwin holds this UDP value but the manifest has no "
                         "matching source row; it cannot be traced back to "
                         "PowerDesigner."),
            })
            result.counts[ST_EXTRA] += 1

    return result


# ─── sheets ───────────────────────────────────────────────────────────────────
def write_comparison(ws, result: ComparisonResult) -> int:
    header_row(ws, ["Entity Name", "Applied To", "UDP Name", "SAP Value",
                    "erwin Value", "Status", "Source Path", "Note"])
    set_widths(ws, [38, 12, 32, 46, 46, 16, 30, 56])

    order = {status: index for index, status in enumerate(STATUS_ORDER)}
    rows = sorted(result.rows,
                  key=lambda r: (order.get(r["status"], 9), r["owner"].lower(),
                                 r["udp"]))
    for row in rows:
        ws.append([
            blank_safe(row["owner"]), row["applied_to"], blank_safe(row["udp"]),
            blank_safe(row["sap_value"]), blank_safe(row["erwin_value"]),
            row["status"], blank_safe(row["source_path"]), blank_safe(row["note"]),
        ])
        cell = ws.cell(row=ws.max_row, column=6)
        if row["status"] == ST_PASS:
            cell.fill = FILL_PASS
        elif row["status"] in (ST_MISMATCH, ST_MISSING):
            cell.fill = FILL_BAD
        elif row["status"] == ST_EXTRA:
            cell.fill = FILL_WARN
        for col in (4, 5, 8):
            ws.cell(row=ws.max_row, column=col).alignment = WRAP
    return ws.max_row


def write_by_udp(ws, result: ComparisonResult, compare_last: int) -> int:
    header_row(ws, ["UDP Name", "Rows", "PASS", "MISMATCH", "MISSING",
                    "BLANK IN SAP", "EXTRA IN ERWIN", "Pass Rate", "Verdict"])
    set_widths(ws, [34, 10, 10, 12, 12, 14, 16, 12, 34])

    udp_rng = f"'{SH_COMPARE}'!$C$2:$C${compare_last}"
    status_rng = f"'{SH_COMPARE}'!$F$2:$F${compare_last}"

    by_udp = defaultdict(Counter)
    for row in result.rows:
        by_udp[row["udp"]][row["status"]] += 1

    for udp in sorted(by_udp):
        row_no = ws.max_row + 1
        ws.append([
            udp,
            f"=COUNTIF({udp_rng},$A{row_no})",
            f'=COUNTIFS({udp_rng},$A{row_no},{status_rng},"{ST_PASS}")',
            f'=COUNTIFS({udp_rng},$A{row_no},{status_rng},"{ST_MISMATCH}")',
            f'=COUNTIFS({udp_rng},$A{row_no},{status_rng},"{ST_MISSING}")',
            f'=COUNTIFS({udp_rng},$A{row_no},{status_rng},"{ST_BLANK}")',
            f'=COUNTIFS({udp_rng},$A{row_no},{status_rng},"{ST_EXTRA}")',
            f"=IF(($C{row_no}+$D{row_no}+$E{row_no})=0,0,"
            f"$C{row_no}/($C{row_no}+$D{row_no}+$E{row_no}))",
            f'=IF(($C{row_no}+$D{row_no}+$E{row_no})=0,"Nothing to migrate",'
            f'IF($C{row_no}=($C{row_no}+$D{row_no}+$E{row_no}),"{ST_PASS}",'
            f'IF($C{row_no}=0,"FAIL - none matched","REVIEW REQUIRED")))',
        ])
        ws.cell(row=row_no, column=8).number_format = "0.0%"
        for col in range(1, 10):
            ws.cell(row=row_no, column=col).border = BOX
    return ws.max_row


def write_diagnostics(ws, result: ComparisonResult) -> None:
    set_widths(ws, [34, 26, 96])
    ws["A1"] = "Diagnostics - how erwin was read, and whether to trust it"
    ws["A1"].font = TITLE_FONT
    ws.merge_cells("A1:C1")

    def line(label, value, note=""):
        row = ws.max_row + 1
        ws.cell(row=row, column=1, value=label).font = LABEL_FONT
        ws.cell(row=row, column=2, value=value).alignment = TOP
        ws.cell(row=row, column=3, value=note).alignment = WRAP
        return row

    readback = result.readback
    line("erwin model read", result.erwin_model or "n/a")
    line("Readback method", readback.method if readback else "none",
         "'com' asks erwin directly and is authoritative. 'binary' decodes the "
         ".erwin file offline - independent of the API that wrote the data, but "
         "reverse-engineered, so it is a cross-check.")
    line("Readback trustworthy", "Yes" if readback and readback.reliable else "No",
         "When No, the comparison is not reported as mismatches - the reading "
         "itself is in doubt.")
    if readback and readback.confidence is not None:
        row = line("Decoder self-test", readback.confidence,
                   "Share of decoded values that pair with a source value. A low "
                   "score means the property slot mapping is wrong for this erwin "
                   "build, not that the data is wrong.")
        ws.cell(row=row, column=2).number_format = "0.0%"
    line("Objects read from erwin", len(readback.owners) if readback else 0)
    line("UDP values read from erwin", len(readback.values) if readback else 0)

    row = ws.max_row + 2
    for col in range(1, 4):
        cell = ws.cell(row=row, column=col, value="Readback notes" if col == 1 else "")
        cell.fill = SECTION_FILL
        cell.font = SECTION_FONT if col == 1 else Font(name=FONT, size=10)
    for message in (readback.messages if readback else ["No readback attempted."]):
        row = ws.max_row + 1
        ws.cell(row=row, column=1, value="-").font = LABEL_FONT
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
        ws.cell(row=row, column=2, value=clean(message)).alignment = WRAP

    row = ws.max_row + 2
    for col in range(1, 4):
        cell = ws.cell(row=row, column=col,
                       value="Source data warnings" if col == 1 else "")
        cell.fill = SECTION_FILL
        cell.font = SECTION_FONT if col == 1 else Font(name=FONT, size=10)
    for warning in (result.warnings or ["None."]):
        row = ws.max_row + 1
        ws.cell(row=row, column=1, value="-").font = LABEL_FONT
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
        ws.cell(row=row, column=2, value=clean(warning)).alignment = WRAP


def write_summary(ws, result: ComparisonResult, compare_last: int) -> None:
    set_widths(ws, [34, 24, 84])
    ws["A1"] = f"UDP Comparison - SAP PowerDesigner vs erwin - {result.model_name}"
    ws["A1"].font = TITLE_FONT
    ws.merge_cells("A1:C1")
    ws.row_dimensions[1].height = 22

    def section(title):
        row = ws.max_row + 2
        for col in range(1, 4):
            cell = ws.cell(row=row, column=col, value=title if col == 1 else "")
            cell.fill = SECTION_FILL
            cell.font = SECTION_FONT if col == 1 else Font(name=FONT, size=10)
            cell.border = BOX
        return row

    def line(label, value, note="", bold=False):
        row = ws.max_row + 1
        ws.cell(row=row, column=1, value=label).font = LABEL_FONT
        cell = ws.cell(row=row, column=2, value=value)
        cell.font = Font(name=FONT, size=10, bold=bold)
        cell.alignment = TOP
        ws.cell(row=row, column=3, value=note).alignment = WRAP
        return row

    status_rng = f"'{SH_COMPARE}'!$F$2:$F${compare_last}"

    section("Run Information")
    line("Report generated", result.generated)
    line("Model", result.model_name)
    line("Model type", result.model_type.upper() or "n/a")
    line("Source SAP model", result.sap_model or "n/a")
    line("erwin model compared", result.erwin_model or "n/a")
    line("erwin readback method",
         result.readback.method if result.readback else "none",
         "See the Diagnostics sheet.")

    section("Comparison")
    total = line("UDP values compared", f"=MAX(0,COUNTA('{SH_COMPARE}'!$C:$C)-1)")
    p = line("PASS", f'=COUNTIF({status_rng},"{ST_PASS}")',
             "erwin holds exactly the value PowerDesigner held.")
    mm = line("MISMATCH", f'=COUNTIF({status_rng},"{ST_MISMATCH}")',
              "erwin holds a different value.")
    ms = line("MISSING", f'=COUNTIF({status_rng},"{ST_MISSING}")',
              "PowerDesigner had a value; erwin holds nothing.")
    line("BLANK IN SAP", f'=COUNTIF({status_rng},"{ST_BLANK}")',
         "Nothing to migrate, so erwin holding nothing is correct. Excluded from "
         "the pass rate.")
    line("EXTRA IN ERWIN", f'=COUNTIF({status_rng},"{ST_EXTRA}")',
         "erwin holds a value with no matching source row.")

    section("Result")
    comparable = line("Comparable values", f"=B{p}+B{mm}+B{ms}",
                      "PASS + MISMATCH + MISSING. Blank source values are not a "
                      "migration outcome, so they are not counted here.")
    rate = line("Pass rate", f"=IF(B{comparable}=0,0,B{p}/B{comparable})")
    ws.cell(row=rate, column=2).number_format = "0.0%"

    if result.readback is None or result.readback.method == "unavailable":
        verdict = "NOT VERIFIED - ERWIN COULD NOT BE READ"
        note = ("erwin could not be read, so no comparison was possible. This is "
                "reported rather than counted as a failure.")
    elif not result.readback.reliable:
        verdict = "NOT VERIFIED - READBACK UNRELIABLE"
        note = ("The erwin readback did not pass its own self-test, so its values "
                "are not reported as mismatches. See Diagnostics.")
    else:
        verdict = (f'=IF(B{comparable}=0,"NO SOURCE VALUES TO MIGRATE",'
                   f'IF(B{p}=B{comparable},"{ST_PASS}",'
                   f'IF(B{p}=0,"FAIL - NO VALUES MATCHED","REVIEW REQUIRED")))')
        note = "PASS only when every migratable value matches erwin exactly."
    row = line("Verdict", verdict, note, bold=True)
    ws.cell(row=row, column=2).font = Font(name=FONT, size=11, bold=True,
                                           color="C00000")

    if result.warnings:
        section("Warnings")
        for warning in result.warnings:
            row = ws.max_row + 1
            ws.cell(row=row, column=1, value="-").font = LABEL_FONT
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
            ws.cell(row=row, column=2, value=clean(warning)).alignment = WRAP

    row = ws.max_row + 2
    ws.cell(row=row, column=1,
            value="Counts are live formulas over the UDP Comparison sheet, bounded "
                  "to the rows this run wrote.").font = Font(
        name=FONT, size=9, italic=True, color="595959")
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=3)


def generate_report(result: ComparisonResult, outdir, filename: str | None = None) -> str:
    """Write one model's comparison workbook and return its path."""
    wb = Workbook()
    wb._named_styles["Normal"].font = Font(name=FONT, size=10)

    ws_summary = wb.active
    ws_summary.title = SH_SUMMARY
    ws_compare = wb.create_sheet(SH_COMPARE)
    ws_by_udp = wb.create_sheet(SH_BY_UDP)
    ws_diag = wb.create_sheet(SH_DIAG)

    compare_last = write_comparison(ws_compare, result)
    write_by_udp(ws_by_udp, result, compare_last)
    write_diagnostics(ws_diag, result)
    write_summary(ws_summary, result, compare_last)

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in result.model_name if c not in '\\/:*?"<>|').strip()
    out_path = outdir / (filename or f"{safe}_UDP_Comparison.xlsx")
    wb.save(out_path)
    return str(out_path)


def compare_model(model_name: str,
                  manifest: list,
                  schema: list,
                  erwin_model,
                  outdir=None,
                  method: str = "auto",
                  sap_model: str = "",
                  model_type: str = "") -> tuple[ComparisonResult, str | None]:
    """
    Read erwin, compare it to the manifest, and optionally write the report.

    Returns (result, report_path). `report_path` is None when `outdir` is None.
    """
    expected = udp_readback.expected_from_manifest(manifest, Path(erwin_model).stem)
    readback = udp_readback.read_udps(erwin_model, schema, method, expected)

    result = compare(model_name, manifest, schema, readback,
                     sap_model=sap_model, erwin_model=str(erwin_model),
                     model_type=model_type)

    report_path = generate_report(result, outdir) if outdir else None
    return result, report_path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compare SAP UDP values against erwin UDP values.")
    ap.add_argument("--model_name", required=True)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--schema", required=True, type=Path)
    ap.add_argument("--erwin", required=True, help="The .erwin model to read back")
    ap.add_argument("--sap_model", default="")
    ap.add_argument("--model_type", default="")
    ap.add_argument("--method", default="auto", choices=["auto", "com", "binary"])
    ap.add_argument("--outdir", required=True, type=Path)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))

    result, path = compare_model(args.model_name, manifest, schema, args.erwin,
                                 outdir=args.outdir, method=args.method,
                                 sap_model=args.sap_model,
                                 model_type=args.model_type)
    print(f"  {result.readback.summary()}")
    print(f"  {result.summary_line()}")
    print(f"  Verdict: {result.status}")
    for warning in result.warnings:
        print(f"  ! {warning}")
    print(f"  -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
