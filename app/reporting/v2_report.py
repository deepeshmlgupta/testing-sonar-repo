"""
V2 — UDP Mapping Report
=======================
Step 2/3 of the migration flow: SAP PD Extended Attributes Text mapped to erwin
UDPs, and the enrichment of erwin V1 into erwin V2.

This report answers "what did the UDP tool map, what did it write, and what does
erwin actually hold now" — the question the V1 initial fidelity report cannot
answer and the V3 final report only summarises.

    V2_OVERVIEW              the mapping in numbers, and the V2 fidelity
    UDP_MAPPING_SUMMARY   ┐
    UDP_DEFINITIONS       │  from <model>_UDP_Migration_Report.xlsx
    UDP_VALUE_RECON       │  (the schema, the manifest, what the injector did)
    UDP_EXCEPTIONS        ┘
    UDP_COMPARISON_SUMMARY┐
    UDP_COMPARISON        │  from <model>_UDP_Comparison.xlsx
    UDP_BY_UDP            │  (the enriched erwin model, read back off disk)
    UDP_DIAGNOSTICS       ┘

Written to app/reporting/<tier>_reports/<model>/<model>_V2_UDP_Mapping_Report.xlsx.

The UDP tool's own two workbooks are its raw output and are not modified; this
report assembles them, with formulas repointed at the renamed sheets.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from app.reporting import report_layout as layout

logger = logging.getLogger(__name__)

SHEET_OVERVIEW = "V2_OVERVIEW"


@dataclass
class V2ReportRequest:
    """Everything one UDP mapping report needs."""

    model_name: str
    model_type: str
    outdir: str
    result: Any = None               # the tier ValidationResult (for V2 fidelity)
    udp_outcome: Any = None          # udp_flow.UdpStageOutcome
    filename: str = ""


def _percent(value) -> Any:
    return layout.NOT_AVAILABLE if value is None else round(float(value), 2)


def _counts(udp) -> dict:
    """The comparison's per-status tally, as a plain dict."""
    result = getattr(udp, "comparison_result", None)
    counts = getattr(result, "counts", None) if result is not None else None
    try:
        return {str(key): int(value) for key, value in dict(counts or {}).items()}
    except (TypeError, ValueError):
        return {}


def _udp_attr(udp, name: str, default: Any = None) -> Any:
    """Attribute of the UDP outcome, or `default` when there is no outcome."""
    if udp is None:
        return default
    return getattr(udp, name, default)


def _identity_rows(request: V2ReportRequest) -> List[Tuple[str, Any, str]]:
    return [
        ("Model", request.model_name, ""),
        ("Model type", request.model_type, ""),
        ("SAP PD file",
         os.path.basename(str(getattr(request.result, "pd_file", "") or "")), ""),
    ]


def _mapping_section(udp) -> List[Tuple[str, Any, str]]:
    """MAPPING — SAP PD Extended Attributes Text to erwin UDPs."""
    return [
        ("", "", ""),
        ("MAPPING — SAP PD Extended Attributes Text to erwin UDPs", "", ""),
        ("UDP definitions mapped",
         _count_json(getattr(udp, "schema_path", "")),
         "udp_schema.json — the erwin UDP dictionary the tool builds"),
        ("Property values mapped",
         _count_json(getattr(udp, "manifest_path", "")),
         "property_manifest.json — one row per value per object"),
        ("Mapping artefacts",
         os.path.dirname(str(getattr(udp, "manifest_path", "") or "")) or layout.NOT_AVAILABLE,
         "Per-model working directory"),
    ]


def _enrichment_section(udp) -> List[Tuple[str, Any, str]]:
    """ENRICHMENT — erwin V1 to erwin V2."""
    return [
        ("", "", ""),
        ("ENRICHMENT — erwin V1 to erwin V2", "", ""),
        ("UDPs injected into erwin",
         "Yes" if getattr(udp, "injected", False) else "No", ""),
        ("Enrichment stage",
         _udp_attr(udp, "stage", layout.NOT_AVAILABLE),
         "SKIPPED / EXTRACTED / INJECTED / COMPARED / FAILED"),
        ("erwin model read back",
         os.path.basename(str(getattr(udp, "erwin_read", "") or "")) or layout.NOT_AVAILABLE,
         "The .erwin binary the comparison was taken from"),
        ("Read-back method",
         _udp_attr(udp, "readback_method", "") or layout.NOT_AVAILABLE,
         "com = erwin SCAPI; binary = independent file decode"),
        ("Read-back reliable", _reliability(udp),
         "The binary decoder scores itself and refuses to report below 80%"),
    ]


def _verification_section(udp, counts: dict) -> List[Tuple[str, Any, str]]:
    """VERIFICATION — what erwin actually holds, plus the per-status tally."""
    rows: List[Tuple[str, Any, str]] = [
        ("", "", ""),
        ("VERIFICATION — what erwin actually holds", "", ""),
        ("UDP values compared", _udp_attr(udp, "udp_values", 0), ""),
        ("UDP mapping pass rate %",
         _percent(_udp_attr(udp, "pass_rate", None)),
         "Share of SAP values present and equal in the enriched erwin model"),
        ("Verdict", _udp_attr(udp, "verdict", layout.NOT_AVAILABLE), ""),
    ]
    for status, count in sorted(counts.items()):
        rows.append((f"    {status}", count, ""))
    return rows


def _fidelity_section(result, fidelity_stages) -> List[Tuple[str, Any, str]]:
    """V2 FIDELITY (after preprocessing, before enrichment is scored)."""
    return [
        ("", "", ""),
        ("V2 FIDELITY (after preprocessing, before enrichment is scored)", "", ""),
        ("V2 fidelity %", _percent(fidelity_stages.stage_value(result, fidelity_stages.STAGE_V2)),
         "SAP PD vs the preprocessed erwin XML"),
        ("Structural fidelity %", _percent(getattr(result, "structural_fidelity_score", None)), ""),
        ("Documentation fidelity %",
         _percent(getattr(result, "documentation_fidelity_score", None)),
         "Comments / Descriptions / Annotations that survived"),
        ("UDP fidelity % (erwin XML)", _percent(getattr(result, "udp_fidelity_score", None)),
         "The XML export carries no UDPs — the enriched binary is the evidence above"),
    ]


def _notes_section(udp) -> List[Tuple[str, Any, str]]:
    rows: List[Tuple[str, Any, str]] = [
        ("", "", ""),
        ("Notes", " | ".join(_udp_attr(udp, "messages", []) or []), ""),
    ]
    error = _udp_attr(udp, "error", "")
    if error:
        rows.append(("Error", error, ""))
    return rows


def _mapping_rows(request: V2ReportRequest) -> List[Tuple[str, Any, str]]:
    from app.validation import fidelity_stages

    udp = request.udp_outcome
    counts = _counts(udp)

    rows: List[Tuple[str, Any, str]] = []
    rows.extend(_identity_rows(request))
    rows.extend(_mapping_section(udp))
    rows.extend(_enrichment_section(udp))
    rows.extend(_verification_section(udp, counts))
    rows.extend(_fidelity_section(request.result, fidelity_stages))
    rows.extend(_notes_section(udp))
    return rows


def _reliability(udp) -> str:
    if udp is None:
        return layout.NOT_AVAILABLE
    reliable = getattr(udp, "readback_reliable", None)
    if reliable is None:
        return layout.NOT_AVAILABLE
    return "Yes" if reliable else "No"


def _count_json(path: str) -> Any:
    """Number of entries in one of the tool's JSON artefacts."""
    if not path or not os.path.isfile(path):
        return layout.NOT_AVAILABLE
    try:
        import json
        with open(path, encoding="utf-8") as handle:
            return len(json.load(handle))
    except (OSError, ValueError) as exc:
        logger.debug("Could not count %s: %s", path, exc)
        return layout.NOT_AVAILABLE


def default_filename(request: V2ReportRequest) -> str:
    return layout.report_name(request.model_name, layout.V2_SUFFIX)


def build_v2_report(request: V2ReportRequest) -> Optional[str]:
    """
    Build the UDP mapping report for one model.

    Returns the path, or None when the UDP stage produced nothing to report.
    Never raises: a reporting problem must not fail an otherwise complete run.
    """
    try:
        udp = request.udp_outcome
        migration = getattr(udp, "migration_report_path", "") or ""
        comparison = getattr(udp, "comparison_report_path", "") or ""
        if not migration and not comparison:
            logger.info("No UDP workbooks for %s; V2 mapping report skipped.",
                        request.model_name)
            return None

        from openpyxl import Workbook
        workbook = Workbook()
        try:
            workbook.remove(workbook.active)          # replaced by V2_OVERVIEW
            layout.write_key_value_sheet(
                workbook, SHEET_OVERVIEW,
                "V2 — UDP Mapping Report",
                "SAP PowerDesigner Extended Attributes Text mapped to erwin UDPs, "
                "and the enrichment of erwin V1 into erwin V2.",
                _mapping_rows(request))
            layout.append_workbook(workbook, migration, layout.MIGRATION_SHEETS)
            layout.append_workbook(workbook, comparison, layout.COMPARISON_SHEETS)

            outdir = Path(request.outdir)
            outdir.mkdir(parents=True, exist_ok=True)
            out_path = outdir / (request.filename or default_filename(request))
            workbook.save(str(out_path))
        finally:
            workbook.close()
        logger.info("V2 UDP mapping report written to %s", out_path)
        return str(out_path)
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("V2 mapping report failed for %s: %s", request.model_name, exc)
        return None


__all__ = ["V2ReportRequest", "build_v2_report", "default_filename", "SHEET_OVERVIEW"]
