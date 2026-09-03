import logging
import os
import shutil
import subprocess  # nosec B404
from pathlib import Path

# NOTE: ErwinSession / ErwinExporter were imported here but never used. They
# pull in pywin32, which made this module -- and therefore app/main.py --
# unimportable anywhere except Windows. Removed; no behaviour depended on them.

logger = logging.getLogger(__name__)

def run_subprocess(cmd_list):
    # Capture output so it doesn't flood the terminal
    result = subprocess.run(cmd_list, check=True, capture_output=True, text=True)  # nosec B603
    
    # Save the detailed script output to a log file invisibly
    log_file = Path("app/preprocessing/preprocessing.log")
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"\n--- Output of {' '.join(cmd_list)} ---\n")
        f.write(result.stdout)
        if result.stderr:
            f.write(result.stderr)

def run_preprocessing(model_path: Path, initial_xml_file: Path, base_name: str,
                      erwin_preprocessed_dir: Path, model_type: str = "LDM"):
    """
    Orchestrates the Comment injection without cluttering main.py.

    `model_type` is informational only -- it is printed so the operator can see
    which model type a given injection belongs to. The underlying script works
    on any SAP PD model that exposes Entity / EntityAttribute with a Comment,
    which covers both LDM and CDM, so no branching is required here.
    """
    print(f"\n--- Phase B: Preprocessing (Comments) for {base_name} [{model_type}] ---")
    
    file_path_str = str(model_path.resolve())
    scripts_dir = Path(__file__).parent / "scripts"
    
    # 1. Inject Comments into XML (Stage 1)
    print("  -> Phase B: Injecting Comments directly into XML...")
    preprocessed_xml_file = erwin_preprocessed_dir / "xml" / f"{base_name}.xml"
    os.makedirs(erwin_preprocessed_dir / "xml", exist_ok=True)
    
    try:
        run_subprocess([
            "python", str(scripts_dir / "pd_comment_to_erwin_note.py"), "migrate",
            "--ldm", file_path_str,
            "--erwin", str(initial_xml_file.resolve()),
            "--out", str(preprocessed_xml_file.resolve()),
            "--author", "Migration_Bot"
        ])
    except Exception as e:
        print(f"  -> Warning: Comment Injection failed: {e}")
        shutil.copy2(initial_xml_file, preprocessed_xml_file) # fallback
        
    print(f"  -> Preprocessing complete. Final XML saved to: 2_preprocessed/xml")
    
    return preprocessed_xml_file
