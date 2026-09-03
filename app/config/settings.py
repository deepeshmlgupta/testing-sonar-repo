import os
from pathlib import Path

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Input/Output Directories
SAMPLES_DIR = os.path.join(BASE_DIR, "sappdmodels")
OUTPUT_DIR = os.path.join(BASE_DIR, "erwinmodels")

# Output Subdirectories
JSON_OUT_DIR = os.path.join(OUTPUT_DIR, "json")
ERWIN_OUT_DIR = os.path.join(OUTPUT_DIR, "erwin")
XML_OUT_DIR = os.path.join(OUTPUT_DIR, "xml")
REPORTS_OUT_DIR = os.path.join(OUTPUT_DIR, "reports")
LOGS_OUT_DIR = os.path.join(OUTPUT_DIR, "logs")

# General Settings
LOG_LEVEL = "WARNING"

# ─── PDM PIPELINE (Phase D for .pdm models) ───────────────────────────────────
# A PDM is only promoted to 3_final at (or above) this measured fidelity.
PDM_FIDELITY_TARGET = 100.0

# Run the PowerDesigner-driven remediation (missing columns / PK members /
# FK joins) when the first validation pass falls short of the target.
PDM_PREPROCESS_ENABLED = True

# After a successful promotion, keep a copy in 2_preprocessed as well
# (False = move the remediated files to 3_final).
PDM_KEEP_PREPROCESSED_COPY = False

# Filename of the PDM Excel workbook under app/reporting/pdm_reports/.
PDM_REPORT_FILENAME = "pdm_validation_report.xlsx"
