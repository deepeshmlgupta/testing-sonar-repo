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


# LDM engine. Imported as a PACKAGE (not by injecting its folder onto
# sys.path): every reconcile package contains modules with the SAME bare names
# (config, comparator, normalizers, report_generator). With flat sys.path
# injection whichever folder landed first would win for all of them, so the CDM
# or PDM engine could silently be scored under LDM rules -- a plausible report
# with wrong numbers and no error anywhere.
from app.validation.ldm_reconcile.pd_ldm_parser import parse_ldm
from app.validation.ldm_reconcile.erwin_ldm_parser import parse_erwin_ldm
from app.validation.ldm_reconcile.comparator import compare, ValidationResult
from app.validation.ldm_reconcile.report_generator import generate_report

# CDM engine. Imported as a package so its config / comparator / normalizers
# cache under app.validation.cdm_reconcile.* and never collide with the bare
# module names of the other engines.
from app.validation.cdm_reconcile import parse_cdm
from app.validation.cdm_reconcile import parse_erwin as parse_erwin_cdm
from app.validation.cdm_reconcile import compare as compare_cdm
from app.validation.cdm_reconcile import generate_report as generate_report_cdm

from app.preprocessing.orchestrator import run_preprocessing

# PDM engine. The standalone validator's modules use the SAME bare names the
# LDM engine occupies on sys.path above (config, comparator, report_generator),
# so it is loaded through pdm_validator_bridge, which imports it in an isolated
# window and never disturbs the LDM modules. pdm_flow drives
# validate -> preprocess -> re-validate -> promote for each .pdm model.
from app.validation.pdm_reconcile import pdm_validator_bridge as pdm_bridge
from app.validation.pdm_reconcile import pdm_report_generator
from app.validation.pdm_reconcile.pdm_flow import run_pdm_flow

# Configure logging to write to a file instead of the terminal to keep the console clean
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.WARNING),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# def run_subprocess(cmd_list):
#     """Utility to run a subprocess command cleanly."""
#     logger.info(f"Running command: {' '.join(cmd_list)}")
#     subprocess.run(cmd_list, check=True)

def main():
    print("\nStarting SAP PowerDesigner to erwin Migration Pipeline (Non-Prod)...")

    # ==========================================
    # SECTION 1: Folder Setup
    # This section defines where all our files are going to live.
    # It ensures that all the necessary input and output folders exist before we start.
    # ==========================================
    ldm_dir = Path("sappdmodels/ldm")
    cdm_dir = Path("sappdmodels/cdm")
    pdm_dir = Path("sappdmodels/pdm")
    erwin_initial_dir = Path("erwinmodels/1_initial")
    erwin_preprocessed_dir = Path("erwinmodels/2_preprocessed")
    erwin_final_dir = Path("erwinmodels/3_final")
    batch_summary_dir = Path("batch_summary")
    
    # Preprocessing directories
    
    # Ensure master directories exist
    for d in [erwin_initial_dir, erwin_preprocessed_dir]:
        os.makedirs(d / "xml", exist_ok=True)
        os.makedirs(d / "json", exist_ok=True)
        
    os.makedirs(erwin_final_dir / "xml", exist_ok=True)
    os.makedirs(erwin_final_dir / "erwin", exist_ok=True)

    os.makedirs(batch_summary_dir / "summary_report", exist_ok=True)

    # ==========================================
    # SECTION 2: Find Models to Process
    # We look inside the sap input folder to find any .ldm models to convert.
    # ==========================================
    models_to_process = list(ldm_dir.glob("*.ldm"))
    models_to_process.extend(cdm_dir.glob("*.cdm"))
    models_to_process.extend(pdm_dir.glob("*.pdm"))

    if not models_to_process:
        logger.warning(f"No sample models found in {ldm_dir}, {cdm_dir} or {pdm_dir}")
        return

    print(f"Found {len(models_to_process)} model(s) to process.")

    validation_results = []
    pdm_outcomes = []          # PdmFlowOutcome per .pdm model (Stage/Notes)

    for model_path in models_to_process:
        file_path_str = str(model_path.resolve())
        base_name = model_path.stem
        suffix = model_path.suffix.lower()
        model_type = {".cdm": "CDM", ".pdm": "PDM"}.get(suffix, "LDM")
        print(f"\n{'='*60}\n--- Processing {model_type} Model: {model_path.name} ---\n{'='*60}")

        # ==========================================
        # PDM ROUTE: validate -> preprocess -> re-validate -> promote.
        # The PDM flow manages its own staging (1_initial / 2_preprocessed /
        # 3_final) and only promotes on a MEASURED fidelity at the target.
        # ==========================================
        if model_type == "PDM":
            print("\n--- Phase A: Checking for initial erwin XML ---")
            print(f"\n--- Phase B: Preprocessing (Comments) for {base_name} [PDM] ---")
            print("  -> Phase B: Injecting structural repairs directly into XML...")
            print("  -> Preprocessing complete. Final XML saved to: 2_preprocessed/xml")
            print("\n--- Phase C: Reconciling SAP against erwin ---")
            try:
                outcome = run_pdm_flow(
                    pdm_path=file_path_str,
                    initial_erwin=str(erwin_initial_dir / "erwin" / f"{base_name}.erwin"),
                    initial_xml=str(erwin_initial_dir / "xml" / f"{base_name}.xml"),
                    preprocessed_erwin=str(erwin_preprocessed_dir / "erwin" / f"{base_name}.erwin"),
                    preprocessed_xml=str(erwin_preprocessed_dir / "xml" / f"{base_name}.xml"),
                    final_erwin=str(erwin_final_dir / "erwin" / f"{base_name}.erwin"),
                    final_xml=str(erwin_final_dir / "xml" / f"{base_name}.xml"),
                    target_fidelity=PDM_FIDELITY_TARGET,
                    preprocess_enabled=PDM_PREPROCESS_ENABLED,
                    keep_preprocessed_copy=PDM_KEEP_PREPROCESSED_COPY,
                )
                pdm_outcomes.append(outcome)
                if outcome.final_result is not None:
                    logger.info(f"PDM {base_name}: {outcome.status} | "
                                f"Fidelity: {outcome.final_fidelity}% | "
                                f"Stage: {outcome.stage}")
            except Exception as e:
                logger.exception(f"PDM pipeline failed critically for {file_path_str}: {e}")
            continue

        try:
            # ==========================================
            # PHASE A: Erwin XML Provisioning
            # What it does: Verifies that the user has manually exported the XML from erwin
            # and placed it in the 1_initial/xml folder.
            # ==========================================
            print("\n--- Phase A: Checking for initial erwin XML ---")
            initial_xml_file = erwin_initial_dir / "xml" / f"{base_name}.xml"
            
            if not initial_xml_file.exists():
                print(f"Failed to find initial XML file for {base_name} at {initial_xml_file}.")
                print(f"Please open {base_name}.ldm in erwin, click 'Save As XML', and place it in that folder.")
                continue

            # ==========================================
            # PHASE B: Preprocessing (Comments)
            # What it does: Orchestrates SAP Comment logic.
            # ==========================================
            preprocessed_xml_file = run_preprocessing(
                model_path, initial_xml_file, base_name, erwin_preprocessed_dir,
                model_type
            )

            # ==========================================
            # PHASE C: LDM Validation (Reconciliation)
            # What it does: Compares the original SAP model against the new erwin model to ensure
            # 100% of the tables, columns, and properties migrated perfectly.
            # ==========================================
            print("\n--- Phase C: Reconciling SAP against erwin ---")
            logger.info(f"Parsing SAP model: {model_path}")
            logger.info(f"Parsing PREPROCESSED erwin XML: {preprocessed_xml_file}")

            if model_type == "CDM":
                pd_model = parse_cdm(file_path_str)
                erwin_model = parse_erwin_cdm(str(preprocessed_xml_file))
                compare_models = compare_cdm
            else:
                pd_model = parse_ldm(file_path_str)
                erwin_model = parse_erwin_ldm(str(preprocessed_xml_file))
                compare_models = compare
            
            logger.info("Comparing models...")
            result = compare_models(pd_model, erwin_model)
            validation_results.append(result)
            
            logger.info(f"Reconciliation Status: {result.status} | Fidelity: {result.fidelity_score}%")
            
            # If completely successful, copy to 3_final
            if result.status == "PASS" and result.critical_count == 0:
                shutil.copy2(preprocessed_xml_file, erwin_final_dir / "xml" / f"{base_name}.xml")
                logger.info("Model successfully passed all gates and was promoted to 3_final!")

        except Exception as e:
            logger.exception(f"Pipeline failed critically for {file_path_str}: {e}")
            
    # ==========================================
    # FINAL REPORTING
    # What it does: After all models are processed, this generates the final Excel scorecards
    # so you can easily see what passed and what failed.
    # ==========================================
    if validation_results or pdm_outcomes:
        print("\nGenerating Master Validation Reports...")

        # 1. Detailed Reports -- one per model type, each written by its own
        #    engine so the CDM workbook carries the DOCUMENTATION sheet.
        ldm_results = [r for r in validation_results
                       if Path(r.pd_file).suffix.lower() == ".ldm"]
        cdm_results = [r for r in validation_results
                       if Path(r.pd_file).suffix.lower() == ".cdm"]

        if ldm_results:
            detailed_report_dir = Path("app/reporting/ldm_reports")
            os.makedirs(detailed_report_dir, exist_ok=True)
            for res in ldm_results:
                filename = f"{Path(res.pd_file).stem}_{res.fidelity_score:.1f}%_Fidelity.xlsx"
                report_path = generate_report([res], str(detailed_report_dir), filename)
                logger.info(f"Detailed LDM report generated at: {report_path}")

        if cdm_results:
            cdm_report_dir = Path("app/reporting/cdm_reports")
            os.makedirs(cdm_report_dir, exist_ok=True)
            for res in cdm_results:
                filename = f"{Path(res.pd_file).stem}_{res.fidelity_score:.1f}%_Fidelity.xlsx"
                cdm_report_path = generate_report_cdm([res], str(cdm_report_dir), filename)
                logger.info(f"Detailed CDM report generated at: {cdm_report_path}")

        # PDM workbook. Written by the pipeline's own PDM generator, which
        # produces the same sheet set as the CDM and LDM reports (dashboard,
        # matrices, documentation, config) and adds the promotion stage the
        # standalone validator knows nothing about. The FINAL
        # (post-preprocessing) result of each model is what goes in it.
        pdm_results = [o.final_result for o in pdm_outcomes
                       if o.final_result is not None]
        if pdm_results:
            pdm_report_dir = Path("app/reporting/pdm_reports")
            os.makedirs(pdm_report_dir, exist_ok=True)
            for res in pdm_results:
                if hasattr(res, 'pd_file') and res.pd_file:
                    filename = f"{Path(res.pd_file).stem}_{res.fidelity_score:.1f}%_Fidelity.xlsx"
                    pdm_report_path = pdm_report_generator.generate_report(
                        [res], str(pdm_report_dir), filename)
                logger.info(f"Detailed PDM report generated at: {pdm_report_path}")

        # 2. Simple Pass/Fail Summary Report (all model types together).
        # Stage/Notes are filled for PDM models, whose promotion is gated.
        from openpyxl import Workbook
        from openpyxl.styles import Font

        simple_summary_dir = batch_summary_dir / "summary_report"
        os.makedirs(simple_summary_dir, exist_ok=True)

        wb = Workbook()
        ws = wb.active
        ws.title = "Status Summary"

        # Header
        ws.append(["Model Name", "Model Type", "Status", "Fidelity %", "Stage", "Notes"])
        for cell in ws[1]:
            cell.font = Font(bold=True)

        # Data
        for res in validation_results:
            mtype = "CDM" if Path(res.pd_file).suffix.lower() == ".cdm" else "LDM"
            ws.append([res.pd_model, mtype, res.status, res.fidelity_score, "", ""])
        for o in pdm_outcomes:
            ws.append([o.model_name, "PDM", o.status, o.final_fidelity,
                       o.stage, " | ".join(o.messages)])

        simple_report_path = simple_summary_dir / "Pass_Fail_Summary.xlsx"
        wb.save(str(simple_report_path))
        logger.info(f"Simple Pass/Fail summary generated at: {simple_report_path}")

    print("\nPipeline execution completed successfully!\n")

if __name__ == "__main__":
    main()