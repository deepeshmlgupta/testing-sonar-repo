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

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.WARNING),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

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
        
    os.makedirs(dirs["final"] / "xml", exist_ok=True)
    os.makedirs(dirs["final"] / "erwin", exist_ok=True)
    os.makedirs(dirs["summary"] / "summary_report", exist_ok=True)
    
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
    
    try:
        return run_pdm_flow(
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
        )
    except Exception as e:
        logger.exception(f"PDM pipeline failed critically for {model_path}: {e}")
        return None

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

    print(r"\n--- Phase C: Reconciling SAP against erwin ---")
    logger.info(f"Parsing SAP model: {model_path}")
    logger.info(f"Parsing PREPROCESSED erwin XML: {preprocessed_xml_file}")

    file_path_str = str(model_path.resolve())
    if model_type == "CDM":
        pd_model = parse_cdm(file_path_str)
        erwin_model = parse_erwin_cdm(str(preprocessed_xml_file))
        result = compare_cdm(pd_model, erwin_model)
    else:
        pd_model = parse_ldm(file_path_str)
        erwin_model = parse_erwin_ldm(str(preprocessed_xml_file))
        result = compare(pd_model, erwin_model)
    
    logger.info(f"Reconciliation Status: {result.status} | Fidelity: {result.fidelity_score}%")
    
    if result.status == "PASS" and result.critical_count == 0:
        shutil.copy2(preprocessed_xml_file, dirs["final"] / "xml" / f"{base_name}.xml")
        logger.info("Model successfully passed all gates and was promoted to 3_final!")
        
    return result

def generate_detailed_reports(validation_results, pdm_outcomes):
    """Generate detailed Excel reports for all processed models."""
    ldm_results = [r for r in validation_results if Path(r.pd_file).suffix.lower() == ".ldm"]
    if ldm_results:
        rep_dir = Path("app/reporting/ldm_reports")
        os.makedirs(rep_dir, exist_ok=True)
        for res in ldm_results:
            fname = f"{Path(res.pd_file).stem}_{res.fidelity_score:.1f}%_Fidelity.xlsx"
            logger.info(f"Detailed LDM report generated at: {generate_report([res], str(rep_dir), fname)}")

    cdm_results = [r for r in validation_results if Path(r.pd_file).suffix.lower() == ".cdm"]
    if cdm_results:
        rep_dir = Path("app/reporting/cdm_reports")
        os.makedirs(rep_dir, exist_ok=True)
        for res in cdm_results:
            fname = f"{Path(res.pd_file).stem}_{res.fidelity_score:.1f}%_Fidelity.xlsx"
            logger.info(f"Detailed CDM report generated at: {generate_report_cdm([res], str(rep_dir), fname)}")

    pdm_results = [o.final_result for o in pdm_outcomes if o.final_result is not None]
    if pdm_results:
        rep_dir = Path("app/reporting/pdm_reports")
        os.makedirs(rep_dir, exist_ok=True)
        for res in pdm_results:
            if hasattr(res, "pd_file") and res.pd_file:
                fname = f"{Path(res.pd_file).stem}_{res.fidelity_score:.1f}%_Fidelity.xlsx"
                logger.info(f"Detailed PDM report generated at: {pdm_report_generator.generate_report([res], str(rep_dir), fname)}")

def generate_summary_report(validation_results, pdm_outcomes, summary_dir):
    """Generate the Pass/Fail summary report."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    
    rep_dir = summary_dir / "summary_report"
    os.makedirs(rep_dir, exist_ok=True)
    
    wb = Workbook()
    ws = wb.active
    ws.title = "Status Summary"
    
    ws.append(["Model Name", "Model Type", "Status", "Fidelity %", "Stage", "Notes"])
    for cell in ws[1]:
        cell.font = Font(bold=True)
        
    for res in validation_results:
        mtype = "CDM" if Path(res.pd_file).suffix.lower() == ".cdm" else "LDM"
        ws.append([res.pd_model, mtype, res.status, res.fidelity_score, "", ""])
        
    for o in pdm_outcomes:
        ws.append([o.model_name, "PDM", o.status, o.final_fidelity, o.stage, " | ".join(o.messages)])
        
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
    validation_results = []
    pdm_outcomes = []
    
    for m in models:
        base_name = m.stem
        model_type = {".cdm": "CDM", ".pdm": "PDM"}.get(m.suffix.lower(), "LDM")
        print(f"\n{'='*60}\n--- Processing {model_type} Model: {m.name} ---\n{'='*60}")
        
        if model_type == "PDM":
            outcome = process_pdm_model(m, base_name, dirs)
            if outcome:
                pdm_outcomes.append(outcome)
                if outcome.final_result is not None:
                    logger.info(f"PDM {base_name}: {outcome.status} | Fidelity: {outcome.final_fidelity}% | Stage: {outcome.stage}")
        else:
            try:
                res = process_conceptual_model(m, base_name, model_type, dirs)
                if res:
                    validation_results.append(res)
            except Exception as e:
                logger.exception(f"Pipeline failed critically for {m.resolve()}: {e}")
                
    if validation_results or pdm_outcomes:
        print(r"\nGenerating Master Validation Reports...")
        generate_detailed_reports(validation_results, pdm_outcomes)
        generate_summary_report(validation_results, pdm_outcomes, dirs["summary"])
        
    print(r"\nPipeline execution completed successfully!\n")

if __name__ == "__main__":  # pragma: no cover
    main()
