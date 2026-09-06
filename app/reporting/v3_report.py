"""
V3 — Final Fidelity Report
==========================
Step 5 of the migration flow: the reconciliation of the source model against
the enriched erwin V2, scored with the enrichment evidence, and the PASS/FAIL
verdict that decides whether the model becomes erwin V3.

    V3_OVERVIEW              V1 -> V2 -> V3 progression, verdict, destination
    SUMMARY … CONFIG         the tier reconciliation report
    UDP_MAPPING_SUMMARY   ┐
    UDP_DEFINITIONS       │  UDP mapping and enrichment, summarised
    UDP_VALUE_RECON       │
    UDP_EXCEPTIONS        ┘
    UDP_COMPARISON_SUMMARY┐
    UDP_COMPARISON        │  the enriched erwin model, read back off disk
    UDP_BY_UDP            │
    UDP_DIAGNOSTICS       ┘

Written to app/reporting/<tier>_reports/<model>/<model>_V3_Final_Fidelity_Report.xlsx,
next to that model's V1 and V2 reports.  A copy travels with the model — into
erwinmodels/3_final/reports when it passes, or into the model's own
manual_review_report folder when it scores below the threshold.

WHAT IS DELIBERATELY LEFT OUT
-----------------------------
UDP_DETAIL.  The tier generator writes a full per-value dump there — one row per
UDP per object, up to UDP_MAX_DETAIL_ROWS — which makes the final report
unwieldy and duplicates what UDP_COMPARISON already shows for the enriched
model.  The V3 report keeps the UDP_FIDELITY summary and drops the detail; the
complete tab remains in the V2 mapping report and in the tier workbook.
Configured by V3_REPORT_EXCLUDE_SHEETS.

TWO UDP NUMBERS, BOTH SHOWN
---------------------------
``udp_fidelity.py`` scores the erwin **XML** export.  The UDP engine scores the
erwin **.erwin** binary it enriched.  They measure different artefacts and can
legitimately disagree, so V3_OVERVIEW shows both, labelled, rather than picking
one.  The V3 fidelity score uses the enriched-binary number.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from app.reporting import report_layout as layout

logger = logging.getLogger(__name__)

SHEET_OVERVIEW = "V3_OVERVIEW"
DEFAULT_EXCLUDE_SHEETS = ("UDP_DETAIL",)

# Re-exported so callers and tests have one import for the sheet maps.
MIGRATION_SHEETS = layout.MIGRATION_SHEETS
COMPARISON_SHEETS = layout.COMPARISON_SHEETS


@dataclass
class V3ReportRequest:
    """Everything one final report needs."""

    result: Any                      # the tier ValidationResult, scored at V3
    tier_report_path: str            # workbook written by the tier generator
    outdir: str
    model_name: str = ""
    model_type: str = ""
    status: str = ""                 # PASS | FAIL
    destination: str = ""
    udp_outcome: Any = None          # udp_flow.UdpStageOutcome
    v1_report_path: str = ""
    v2_report_path: str = ""
    filename: str = ""
    exclude_sheets: tuple = DEFAULT_EXCLUDE_SHEETS


def _percent(value) -> Any:
    return layout.NOT_AVAILABLE if value is None else round(float(value), 2)


def _reliability(udp) -> str:
    if udp is None:
        return layout.NOT_AVAILABLE
    reliable = getattr(udp, "readback_reliable", None)
    if reliable is None:
        return layout.NOT_AVAILABLE
    return "Yes" if reliable else "No"


def _stage_rows(result) -> List[Tuple[str, Any, str]]:
    """One row per measured stage, with the component breakdown behind it."""
    from app.validation import fidelity_stages

    labels = {
        fidelity_stages.STAGE_V1: (
            "V1 fidelity % (initial migration)",
            "SAP PD vs erwin XML V1 — comments not injected, no UDPs written"),
        fidelity_stages.STAGE_V2: (
            "V2 fidelity % (after preprocessing)",
            "SAP PD vs the preprocessed erwin XML — Comments/Notes or repairs"),
        fidelity_stages.STAGE_V3: (
            "V3 fidelity % (after enrichment)",
            "UDP component read back from the enriched erwin model"),
    }
    history = fidelity_stages.history(result)
    rows: List[Tuple[str, Any, str]] = []
    for stage in fidelity_stages.STAGES:
        label, note = labels[stage]
        stage_score = history.get(stage)
        if stage_score is None:
            rows.append((label, layout.NOT_AVAILABLE, f"{note} — not measured for this model"))
            continue
        rows.append((label, round(stage_score.overall, 2), note))
        rows.append(("    components",
                     stage_score.summary_line().split("(", 1)[-1].rstrip(")"),
                     " ".join(stage_score.notes)))
    return rows


def _overview_rows(request: V3ReportRequest) -> List[Tuple[str, Any, str]]:
    result = request.result
    udp = request.udp_outcome
    model_name = request.model_name or Path(str(getattr(result, "pd_file", ""))).stem

    rows: List[Tuple[str, Any, str]] = [
        ("Model", getattr(result, "pd_model", "") or model_name, ""),
        ("Model type", request.model_type or "", ""),
        ("SAP PD file", os.path.basename(str(getattr(result, "pd_file", ""))), ""),
        ("erwin file reconciled", os.path.basename(str(getattr(result, "erwin_file", ""))), ""),
        ("", "", ""),
        ("FIDELITY PROGRESSION", "",
         "Each stage is measured against a different erwin artefact and recomputed in full"),
    ]
    rows.extend(_stage_rows(result))
    rows.extend([
        ("", "", ""),
        ("COMPONENTS AT V3", "", ""),
        ("Structural fidelity %", _percent(getattr(result, "structural_fidelity_score", None)),
         "Entities/Tables, Attributes/Columns, Relationships and Keys"),
        ("Documentation fidelity %",
         _percent(getattr(result, "documentation_fidelity_score", None)),
         "Comments / Notes, Descriptions and Annotations"),
        ("UDP mapping pass rate % (.erwin)",
         _percent(getattr(udp, "pass_rate", None)) if udp is not None else layout.NOT_AVAILABLE,
         "Read back from the enriched erwin model — this feeds the V3 score"),
        ("UDP fidelity % (erwin XML)", _percent(getattr(result, "udp_fidelity_score", None)),
         "udp_fidelity.py scores the XML export, which carries no UDPs"),
        ("Shortcut fidelity %", layout.NOT_AVAILABLE,
         "Not measured: erwin's XML export has no shortcut object"),
        ("Tag fidelity %", layout.NOT_AVAILABLE,
         "Not measured: erwin Tags have no representation in this framework"),
        ("", "", ""),
        ("ENRICHMENT", "", ""),
        ("UDP values compared", getattr(udp, "udp_values", 0) if udp is not None else 0, ""),
        ("UDP verdict",
         getattr(udp, "verdict", layout.NOT_AVAILABLE) if udp is not None else layout.NOT_AVAILABLE,
         ""),
        ("UDP stage",
         getattr(udp, "stage", layout.NOT_AVAILABLE) if udp is not None else layout.NOT_AVAILABLE,
         "SKIPPED / EXTRACTED / INJECTED / COMPARED / FAILED"),
        ("UDPs injected into erwin", "Yes" if getattr(udp, "injected", False) else "No", ""),
        ("erwin binary read back",
         os.path.basename(str(getattr(udp, "erwin_read", "") or "")) or layout.NOT_AVAILABLE, ""),
        ("Read-back reliable", _reliability(udp),
         "The binary decoder scores itself and refuses to report below 80%"),
        ("", "", ""),
        ("FINDINGS", "", ""),
        ("Errors (CRITICAL)", getattr(result, "critical_count", 0), ""),
        ("Warnings", getattr(result, "warning_count", 0), ""),
        ("Information", getattr(result, "info_count", 0), ""),
        ("", "", ""),
        ("VERDICT", "", ""),
        ("Status", request.status or getattr(result, "status", ""), ""),
        ("Destination", request.destination or "", ""),
        ("Gate note", getattr(result, "promotion_note", ""), ""),
        ("", "", ""),
        ("RELATED REPORTS", "", ""),
        ("V1 initial fidelity report",
         os.path.basename(request.v1_report_path) or layout.NOT_AVAILABLE, ""),
        ("V2 UDP mapping report",
         os.path.basename(request.v2_report_path) or layout.NOT_AVAILABLE, ""),
        ("UDP notes",
         " | ".join(getattr(udp, "messages", []) or []) if udp is not None else "", ""),
    ])
    error = getattr(udp, "error", "") if udp is not None else ""
    if error:
        rows.append(("UDP error", error, ""))
    return rows


def default_filename(request: V3ReportRequest) -> str:
    """`<model>_V3_Final_Fidelity_Report.xlsx`."""
    model_name = request.model_name or Path(
        str(getattr(request.result, "pd_file", "") or "model")).stem
    return layout.report_name(model_name, layout.V3_SUFFIX)


def build_v3_report(request: V3ReportRequest) -> Optional[str]:
    """
    Build the final report for one model.

    Returns the path, or None when the tier report is missing or the workbook
    could not be written.  Never raises: a reporting problem must not fail an
    otherwise complete pipeline run.
    """
    try:
        if not request.tier_report_path or not os.path.isfile(request.tier_report_path):
            logger.warning("No tier report to consolidate for %s",
                           getattr(request.result, "pd_model", "?"))
            return None

        from openpyxl import load_workbook
        workbook = load_workbook(request.tier_report_path)
        try:
            dropped = layout.drop_sheets(workbook, request.exclude_sheets)
            if dropped:
                logger.info("V3 report excludes %s", ", ".join(dropped))

            status = (request.status or getattr(request.result, "status", "") or "").upper()
            layout.write_key_value_sheet(
                workbook, SHEET_OVERVIEW,
                "V3 — Final Fidelity Report",
                "Reconciliation of the source model against the enriched erwin V2, "
                "scored with the enrichment evidence, and the PASS/FAIL verdict.",
                _overview_rows(request),
                highlight_label="Status", highlight_pass=(status == "PASS"))

            udp = request.udp_outcome
            layout.append_workbook(workbook, getattr(udp, "migration_report_path", "") or "",
                                   layout.MIGRATION_SHEETS)
            layout.append_workbook(workbook, getattr(udp, "comparison_report_path", "") or "",
                                   layout.COMPARISON_SHEETS)

            outdir = Path(request.outdir)
            outdir.mkdir(parents=True, exist_ok=True)
            out_path = outdir / (request.filename or default_filename(request))
            workbook.save(str(out_path))
        finally:
            workbook.close()
        logger.info("V3 report written to %s", out_path)
        return str(out_path)
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("V3 report failed for %s: %s",
                       getattr(request.result, "pd_file", "?"), exc)
        return None


__all__ = ["V3ReportRequest", "build_v3_report", "default_filename",
           "SHEET_OVERVIEW", "MIGRATION_SHEETS", "COMPARISON_SHEETS",
           "DEFAULT_EXCLUDE_SHEETS"]
