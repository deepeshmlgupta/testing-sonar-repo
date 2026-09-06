import logging
import os
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

# Ensure the root of the project is in the PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config.settings import LOG_LEVEL
from app.config.settings import (PDM_FIDELITY_TARGET, PDM_PREPROCESS_ENABLED,
                                 PDM_KEEP_PREPROCESSED_COPY, PDM_REPORT_FILENAME)

from app.validation.ldm_reconcile.pd_ldm_parser import parse_ldm
from app.validation.ldm_reconcile.erwin_ldm_parser import parse_erwin_ldm
from app.validation.ldm_reconcile.comparator import compare, ValidationResult
from app.validation.ldm_reconcile.report_generator import generate_report

from app.validation.cdm_reconcile import parse_cdm
from app.validation.cdm_reconcile import parse_erwin as parse_erwin_cdm
from app.validation.cdm_reconcile import compare as compare_cdm
from app.validation.cdm_reconcile import generate_report as generate_report_cdm
from app.preprocessing.orchestrator import run_preprocessing

from app.validation.pdm_reconcile import pdm_validator_bridge as pdm_bridge
from app.validation.pdm_reconcile import pdm_report_generator
from app.validation.pdm_reconcile.pdm_flow import run_pdm_flow

# ─── MIGRATION FRAMEWORK ADDITIONS ────────────────────────────────────────
# Phase D runs the standalone UDP tool (app/udp_tool) as part of this
# pipeline. fidelity_stages scores each model at V1, V2 and V3, re-measuring
# every component against a different erwin artefact. promotion_gate applies
# the shared >=90% PASS / <90% FAIL rule. report_layout decides where a
# model's three reports go; v2_report and v3_report build them.
from dataclasses import dataclass, field
from typing import Any, List

from app.validation import fidelity_stages, promotion_gate, udp_flow
from app.reporting import report_layout, v2_report, v3_report

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.WARNING),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# The UDP engine's own raw workbooks stay in the UDP tool's existing folder; the
# V2 mapping report assembles them into the model's reporting folder.
UDP_REPORTS_DIR = Path("app/udp_tool/output_excel_reports")
# Interim reconciliation workbooks are written here and removed once the V3
# report has been assembled, so a model's folder holds exactly V1, V2 and V3.
SCRATCH_REPORT_DIR = Path("data/interim_reports")

DEST_FINAL = "3_final"
DEST_PREPROCESSED = "2_preprocessed"
DEST_MANUAL_REVIEW = "manual_review"
DEST_REJECTED = "rejected"

@dataclass
class ModelRecord:
    """One processed model, from reconciliation through to its three reports."""

    base_name: str
    model_type: str
    result: Any = None          # the V3-scored result (V2 reconciliation + enrichment)
    initial_result: Any = None  # the V1 reconciliation, against the raw erwin export
    udp_outcome: Any = None
    v1_report_path: str = ""
    v2_report_path: str = ""
    v3_report_path: str = ""
    destination: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def status(self):
        """
        The PASS/FAIL verdict of the promotion gate.

        The gate is the authority for the >=90% rule, so the destination it
        chose decides. Falling back on the comparator's own status matters for
        PDM: its ValidationResult reports WARN whenever any warning finding
        exists, which would contradict a model the gate promoted at 99.31%.
        """
        if getattr(self.result, "status", "") == "ERROR":
            return "ERROR"
        if self.destination == DEST_FINAL:
            return promotion_gate.BAND_PASS
        if self.destination in (DEST_MANUAL_REVIEW, DEST_REJECTED):
            return promotion_gate.BAND_FAIL
        return str(getattr(self.result, "status", "ERROR"))

    @property
    def fidelity(self):
        return float(getattr(self.result, "fidelity_score", 0.0) or 0.0)

    def report_dir(self):
        """This model's own folder inside its model type's reporting folder."""
        return report_layout.model_dir(self.model_type, self.base_name)

    def manual_review_dir(self):
        """`.../<model>/manual_review_report` - where a sub-90% model is sent."""
        return report_layout.manual_review_dir(self.model_type, self.base_name)

    def stage(self, name):
        """The overall score recorded at V1, V2 or V3, or None."""
        source = self.initial_result if name == fidelity_stages.STAGE_V1 else self.result
        value = fidelity_stages.stage_value(source, name)
        if value is None and source is not self.result:
            value = fidelity_stages.stage_value(self.result, name)
        return value

def excluded_sheets(model_type):
    """Sheets the consolidated V3 report must not carry over (UDP_DETAIL)."""
    try:
        from app.config.validation_config import tier
        return tuple(getattr(tier(model_type), "V3_REPORT_EXCLUDE_SHEETS", ("UDP_DETAIL",)))
    except (ImportError, ValueError):
        return ("UDP_DETAIL",)

def run_udp_phase(model_path, base_name, model_type, dirs):
    """
    Phase D - the UDP engine: classify, map SAP Extended Attributes to erwin
    UDPs, inject, report and read the enriched model back.

    Returns a UdpStageOutcome. Never raises: udp_flow records every failure on
    the outcome, so a UDP problem cannot fail an otherwise complete run.
    """
    print(f"\n--- Phase D: UDP extraction / mapping / comparison for {base_name} ---")
    outcome = udp_flow.run_udp_flow(udp_flow.UdpFlowRequest(
        pd_path=str(model_path.resolve()),
        model_name=base_name,
        model_type=model_type,
        reports_dir=str(UDP_REPORTS_DIR / model_type.lower()),
        erwin_out=str(dirs["preprocessed"] / "erwin" / f"{base_name}.erwin"),
    ))
    print(f"  -> UDP stage: {outcome.stage}. {outcome.summary_line()}")
    return outcome


def setup_directories():
    """Create and return necessary directories."""
    dirs = {
        "ldm": Path("sappdmodels/ldm"),
        "cdm": Path("sappdmodels/cdm"),
        "pdm": Path("sappdmodels/pdm"),
        "initial": Path("erwinmodels/1_initial"),
        "preprocessed": Path("erwinmodels/2_preprocessed"),
        "final": Path("erwinmodels/3_final"),
        "summary": Path("batch_summary")
    }
    
    for d in [dirs["initial"], dirs["preprocessed"]]:
        os.makedirs(d / "xml", exist_ok=True)
        os.makedirs(d / "json", exist_ok=True)
        os.makedirs(d / "erwin", exist_ok=True)
        
    os.makedirs(dirs["final"] / "xml", exist_ok=True)
    os.makedirs(dirs["final"] / "erwin", exist_ok=True)
    os.makedirs(dirs["final"] / "reports", exist_ok=True)
    os.makedirs(dirs["summary"] / "summary_report", exist_ok=True)
    
    # Migration framework additions: the UDP tool's report folder, the interim
    # scratch folder, and each model type's reporting folder (per-model
    # sub-folders are created on demand by report_layout).
    os.makedirs(UDP_REPORTS_DIR, exist_ok=True)
    os.makedirs(SCRATCH_REPORT_DIR, exist_ok=True)
    for tier_name in ("CDM", "LDM", "PDM"):
        os.makedirs(report_layout.tier_dir(tier_name), exist_ok=True)
    
    return dirs

def find_models(dirs):
    """Find all LDM, CDM, and PDM models."""
    models = list(dirs["ldm"].glob("*.ldm"))
    models.extend(dirs["cdm"].glob("*.cdm"))
    models.extend(dirs["pdm"].glob("*.pdm"))
    return models

def process_pdm_model(model_path, base_name, dirs):
    """Handle the PDM route."""
    print(r"\n--- Phase A: Checking for initial erwin XML ---")
    print(f"\n--- Phase B: Preprocessing (Comments) for {base_name} [PDM] ---")
    print("  -> Phase B: Injecting structural repairs directly into XML...")
    print("  -> Preprocessing complete. Final XML saved to: 2_preprocessed/xml")
    print(r"\n--- Phase C: Reconciling SAP against erwin ---")
    
    # Phase D runs BEFORE the reconciliation flow here. It works on the .erwin
    # binary, independently of the XML pipeline, and run_pdm_flow applies its
    # own promotion gate internally - so the read-back pass rate has to be in
    # hand before that gate is evaluated, or PDM would be gated on V2 while
    # CDM and LDM are gated on V3.
    udp_outcome = run_udp_phase(model_path, base_name, "PDM", dirs)

    try:
        outcome = run_pdm_flow(
            pdm_path=str(model_path.resolve()),
            initial_erwin=str(dirs["initial"] / "erwin" / f"{base_name}.erwin"),
            initial_xml=str(dirs["initial"] / "xml" / f"{base_name}.xml"),
            preprocessed_erwin=str(dirs["preprocessed"] / "erwin" / f"{base_name}.erwin"),
            preprocessed_xml=str(dirs["preprocessed"] / "xml" / f"{base_name}.xml"),
            final_erwin=str(dirs["final"] / "erwin" / f"{base_name}.erwin"),
            final_xml=str(dirs["final"] / "xml" / f"{base_name}.xml"),
            target_fidelity=PDM_FIDELITY_TARGET,
            preprocess_enabled=PDM_PREPROCESS_ENABLED,
            keep_preprocessed_copy=PDM_KEEP_PREPROCESSED_COPY,
            udp_pass_rate=getattr(udp_outcome, "pass_rate", None),
            manual_review_dir=str(report_layout.manual_review_dir("PDM", base_name)),
        )
    except Exception as e:
        logger.exception(f"PDM pipeline failed critically for {model_path}: {e}")
        return None

    record = ModelRecord(base_name=base_name, model_type="PDM",
                         result=outcome.final_result,
                         initial_result=outcome.initial_result,
                         destination=pdm_destination(outcome),
                         notes=list(outcome.messages))
    record.udp_outcome = udp_outcome
    for stage_name in fidelity_stages.STAGES:
        value = record.stage(stage_name)
        if value is not None:
            print(f"  -> {stage_name} fidelity {value:.2f}%")
    return record

def pdm_destination(outcome):
    """Map a PdmFlowOutcome stage onto the destination shown in the summary."""
    if outcome.promoted:
        return DEST_FINAL
    return {DEST_REJECTED: DEST_REJECTED,
            DEST_MANUAL_REVIEW: DEST_MANUAL_REVIEW}.get(outcome.stage, DEST_PREPROCESSED)

def process_conceptual_model(model_path, base_name, model_type, dirs):
    """Handle the LDM and CDM routes."""
    print(r"\n--- Phase A: Checking for initial erwin XML ---")
    initial_xml_file = dirs["initial"] / "xml" / f"{base_name}.xml"
    
    if not initial_xml_file.exists():
        print(f"Failed to find initial XML file for {base_name} at {initial_xml_file}.")
        print(f"Please open {base_name}.ldm in erwin, click 'Save As XML', and place it in that folder.")
        return None

    preprocessed_xml_file = run_preprocessing(
        model_path, initial_xml_file, base_name, dirs["preprocessed"], model_type
    )

    # Phase C runs TWICE, against two different erwin artefacts. V1 is the raw
    # export: comments have not been injected and no UDP has been written, so it
    # measures the initial migration gap. V2 is the same comparison after
    # preprocessing. Nothing is carried between them.
    print(r"\n--- Phase C: Reconciling SAP against erwin (V1, then V2) ---")
    logger.info(f"Parsing SAP model: {model_path}")

    record = ModelRecord(base_name=base_name, model_type=model_type)

    logger.info(f"V1 - parsing INITIAL erwin XML: {initial_xml_file}")
    record.initial_result = reconcile(model_path, initial_xml_file, model_type)
    v1 = fidelity_stages.apply(record.initial_result, fidelity_stages.STAGE_V1,
                               model_type=model_type)
    print(f"  -> {v1.summary_line()}")

    logger.info(f"V2 - parsing PREPROCESSED erwin XML: {preprocessed_xml_file}")
    record.result = reconcile(model_path, preprocessed_xml_file, model_type)
    v2 = fidelity_stages.apply(record.result, fidelity_stages.STAGE_V2,
                               model_type=model_type)
    print(f"  -> {v2.summary_line()}")

    record.udp_outcome = run_udp_phase(model_path, base_name, model_type, dirs)

    # Phase E - V3: recompute with the enrichment evidence, then gate. The UDP
    # component now comes from reading the enriched .erwin model back off disk,
    # not from the XML export, so enrichment shows up in the gated score.
    print(r"\n--- Phase E: V3 fidelity (post-enrichment) and promotion gate ---")
    v3 = fidelity_stages.apply(record.result, fidelity_stages.STAGE_V3,
                               udp_override=getattr(record.udp_outcome, "pass_rate", None),
                               model_type=model_type)
    print(f"  -> {v3.summary_line()}")
    logger.info(f"Reconciliation Status: {record.result.status} | V3 Fidelity: {record.result.fidelity_score}%")

    route_model(record, preprocessed_xml_file, dirs)
    return record

def reconcile(model_path, erwin_xml, model_type):
    """Parse both sides and reconcile them with the engine for this tier."""
    file_path_str = str(model_path.resolve())
    if model_type == "CDM":
        pd_model = parse_cdm(file_path_str)
        erwin_model = parse_erwin_cdm(str(erwin_xml))
        return compare_cdm(pd_model, erwin_model)
    pd_model = parse_ldm(file_path_str)
    erwin_model = parse_erwin_ldm(str(erwin_xml))
    return compare(pd_model, erwin_model)

def route_model(record, preprocessed_xml_file, dirs):
    """
    Apply the promotion gate and send the model to its destination.

        fidelity >= threshold  PASS -> erwinmodels/3_final
        fidelity <  threshold  FAIL -> <model>/manual_review_report  (two-band)
                               WARN -> stays in 2_preprocessed       (three-band)
    """
    base_name = record.base_name
    if promotion_gate.apply(record.result):
        shutil.copy2(preprocessed_xml_file, dirs["final"] / "xml" / f"{base_name}.xml")
        record.destination = DEST_FINAL
        logger.info("Model successfully passed all gates and was promoted to 3_final!")
        return

    if promotion_gate.band(record.result) == promotion_gate.BAND_FAIL:
        moved = promotion_gate.route(record.result, str(preprocessed_xml_file),
                                     destination=str(record.manual_review_dir()))
        record.destination = (DEST_MANUAL_REVIEW
                              if promotion_gate.bands_mode() == promotion_gate.BANDS_TWO
                              else DEST_REJECTED)
        logger.warning(f"{base_name} routed to {record.destination} ({moved})")
        return

    record.destination = DEST_PREPROCESSED
    logger.info(f"{base_name} held in {DEST_PREPROCESSED} for review")

def reconciliation_workbook(record, result, outdir, filename):
    """
    Write a reconciliation workbook with this tier's OWN generator, so the CDM
    workbook keeps its DESCRIPTION sheet, the PDM workbook its promotion stage,
    and so on.
    """
    if result is None or not getattr(result, "pd_file", ""):
        return ""
    os.makedirs(outdir, exist_ok=True)
    generators = {
        "CDM": generate_report_cdm,
        "LDM": generate_report,
        "PDM": pdm_report_generator.generate_report,
    }
    path = generators[record.model_type]([result], str(outdir), filename)
    return str(path or "")

def build_v1_report(record, outdir):
    """
    V1 - the INITIAL fidelity report: the source model reconciled against the
    raw erwin XML V1, before comments were injected and before any UDP was
    written. This is the report that shows the migration gaps.
    """
    if record.initial_result is None:
        logger.info(f"No V1 reconciliation for {record.base_name}; initial report skipped.")
        return ""
    path = reconciliation_workbook(
        record, record.initial_result, outdir,
        report_layout.report_name(record.base_name, report_layout.V1_SUFFIX))
    if path:
        logger.info(f"V1 initial fidelity report: {path}")
    return path

def build_v2_report(record, outdir):
    """
    V2 - the UDP MAPPING report: SAP PD Extended Attributes Text mapped to
    erwin UDPs, what the enrichment wrote, and what the enriched model holds.
    """
    path = v2_report.build_v2_report(v2_report.V2ReportRequest(
        model_name=record.base_name,
        model_type=record.model_type,
        outdir=str(outdir),
        result=record.result,
        udp_outcome=record.udp_outcome,
    ))
    if path:
        logger.info(f"V2 UDP mapping report: {path}")
    return path or ""

def build_v3_report(record, outdir, dirs):
    """
    V3 - the FINAL fidelity report: the reconciliation against the enriched
    erwin V2, scored with the enrichment evidence, plus the PASS/FAIL verdict.

    Built from a tier reconciliation workbook written to a scratch location and
    removed afterwards, so the model's folder holds exactly the three reports.
    """
    if record.result is None:
        return ""

    interim = reconciliation_workbook(
        record, record.result, SCRATCH_REPORT_DIR,
        f"{record.base_name}_reconciliation.xlsx")
    if not interim:
        return ""

    try:
        path = v3_report.build_v3_report(v3_report.V3ReportRequest(
            result=record.result,
            tier_report_path=interim,
            outdir=str(outdir),
            model_name=record.base_name,
            model_type=record.model_type,
            status=record.status,
            destination=record.destination,
            udp_outcome=record.udp_outcome,
            v1_report_path=record.v1_report_path,
            v2_report_path=record.v2_report_path,
            exclude_sheets=excluded_sheets(record.model_type),
        ))
    finally:
        discard(interim)

    if not path:
        return ""
    logger.info(f"V3 final fidelity report: {path}")

    # The final report travels with the model: to 3_final when it passes, or to
    # this model's own manual_review_report folder when it does not.
    if record.destination == DEST_FINAL:
        shutil.copy2(path, dirs["final"] / "reports" / Path(path).name)
    elif record.destination == DEST_MANUAL_REVIEW:
        promotion_gate.publish(record.result, path,
                               destination=str(record.manual_review_dir()))
    return path

def discard(path):
    """Remove an interim workbook. A leftover file is not worth failing a run."""
    try:
        os.remove(path)
    except OSError as exc:
        logger.debug(f"Could not remove interim workbook {path}: {exc}")

def generate_detailed_reports(records, dirs):
    """
    Generate the three reports for every processed model, each in that model's
    own folder under app/reporting/<tier>_reports/<model>/.
    """
    for record in records:
        outdir = record.report_dir()
        record.v1_report_path = build_v1_report(record, outdir)
        record.v2_report_path = build_v2_report(record, outdir)
        record.v3_report_path = build_v3_report(record, outdir, dirs)
        print(f"\n  {record.base_name}  ->  {outdir}")
        for label, path in (("V1 initial fidelity", record.v1_report_path),
                            ("V2 UDP mapping", record.v2_report_path),
                            ("V3 final fidelity", record.v3_report_path)):
            print(f"    {label:22} {Path(path).name if path else 'not generated'}")

SUMMARY_HEADERS = [
    "Model Name", "Model Type", "Status",
    "V1 Fidelity %", "V2 Fidelity %", "V3 Fidelity %",
    "Stage", "Notes",
    "Structural Fidelity %", "Documentation Fidelity %", "UDP Fidelity % (XML)",
    "UDP Migration Pass Rate %", "UDP Stage",
    "Report Folder", "V1 Report", "V2 Report", "V3 Report",
]

def summary_row(record):
    """One row of Pass_Fail_Summary.xlsx."""
    result = record.result
    udp = record.udp_outcome
    udp_score = getattr(result, "udp_fidelity_score", None)
    doc_score = getattr(result, "documentation_fidelity_score", None)
    pass_rate = getattr(udp, "pass_rate", None) if udp is not None else None
    notes = [getattr(result, "promotion_note", "")] + record.notes
    stages = [record.stage(name) for name in fidelity_stages.STAGES]
    return [
        getattr(result, "pd_model", "") or record.base_name,
        record.model_type,
        record.status,
        *["n/a" if value is None else value for value in stages],
        record.destination,
        " | ".join(note for note in notes if note),
        getattr(result, "structural_fidelity_score", record.fidelity),
        "n/a" if doc_score is None else doc_score,
        "n/a" if udp_score is None else udp_score,
        "n/a" if pass_rate is None else pass_rate,
        getattr(udp, "stage", "n/a") if udp is not None else "n/a",
        str(record.report_dir()),
        Path(record.v1_report_path).name if record.v1_report_path else "",
        Path(record.v2_report_path).name if record.v2_report_path else "",
        Path(record.v3_report_path).name if record.v3_report_path else "",
    ]

def generate_summary_report(records, summary_dir):
    """Generate the Pass/Fail summary report."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    
    rep_dir = summary_dir / "summary_report"
    os.makedirs(rep_dir, exist_ok=True)
    
    wb = Workbook()
    ws = wb.active
    ws.title = "Status Summary"
    
    ws.append(SUMMARY_HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        
    for record in records:
        if record.result is not None:
            ws.append(summary_row(record))
        
    report_path = rep_dir / "Pass_Fail_Summary.xlsx"
    wb.save(str(report_path))
    logger.info(f"Simple Pass/Fail summary generated at: {report_path}")

def main():
    print(r"\nStarting SAP PowerDesigner to erwin Migration Pipeline (Non-Prod)...")
    dirs = setup_directories()
    models = find_models(dirs)
    
    if not models:
        logger.warning(f"No sample models found in SAP input folders")
        return

    print(f"Found {len(models)} model(s) to process.")
    records = []
    
    for m in models:
        base_name = m.stem
        model_type = {".cdm": "CDM", ".pdm": "PDM"}.get(m.suffix.lower(), "LDM")
        print(f"\n{'='*60}\n--- Processing {model_type} Model: {m.name} ---\n{'='*60}")
        
        try:
            if model_type == "PDM":
                record = process_pdm_model(m, base_name, dirs)
            else:
                record = process_conceptual_model(m, base_name, model_type, dirs)
        except Exception as e:
            logger.exception(f"Pipeline failed critically for {m.resolve()}: {e}")
            continue
        
        if record:
            records.append(record)
                
    if records:
        print(r"\nGenerating Validation Reports (V1 initial, V2 UDP mapping, V3 final)...")
        generate_detailed_reports(records, dirs)
        generate_summary_report(records, dirs["summary"])
        
    print(r"\nPipeline execution completed successfully!\n")

if __name__ == "__main__":  # pragma: no cover
    main()
