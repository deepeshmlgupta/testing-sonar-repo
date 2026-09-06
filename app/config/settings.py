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

# ─── PROMOTION GATE (shared by CDM, LDM and PDM) ──────────────────────────────
# A model is promoted to erwinmodels/3_final only at or above this MEASURED
# fidelity. Below it, the model and its final report are routed to that model's
# own app/reporting/<tier>_reports/<model>/manual_review_report folder.
PROMOTION_FIDELITY_THRESHOLD = 90.0

# Which number the gate tests.
#   "overall"     result.fidelity_score - the V3 staged score (default)
#   "structural"  result.structural_fidelity_score - reconciliation only
PROMOTION_FIDELITY_BASIS = "overall"

# How many bands the gate uses.
#   "two"   (default)  fidelity >= threshold -> PASS -> erwinmodels/3_final
#                      fidelity <  threshold -> FAIL -> manual_review_report
#   "three"            PASS / WARN (held in 2_preprocessed) / FAIL, using
#                      REVIEW_FIDELITY_FLOOR below.
PROMOTION_BANDS = "two"

# Where a sub-threshold model is routed when no per-model folder is supplied.
MANUAL_REVIEW_DIR = "manual_review"

# Honoured only when PROMOTION_BANDS = "three".
REVIEW_FIDELITY_FLOOR = 60.0
REJECTED_SUBDIR = "rejected"          # relative to erwinmodels/2_preprocessed

# Block promotion outright on any CRITICAL / WARNING finding, independently of
# the fidelity score.
PROMOTION_BLOCK_ON_CRITICAL = False
PROMOTION_BLOCK_ON_WARNING = False

# ─── PDM PIPELINE (Phase D for .pdm models) ───────────────────────────────────
# A PDM is only promoted to 3_final at (or above) this measured fidelity.
# Tied to the shared promotion threshold above (was 100.0 before the
# two-band gate; confirmed change).
PDM_FIDELITY_TARGET = PROMOTION_FIDELITY_THRESHOLD

# Run the PowerDesigner-driven remediation (missing columns / PK members /
# FK joins) when the first validation pass falls short of the target.
PDM_PREPROCESS_ENABLED = True

# After a successful promotion, keep a copy in 2_preprocessed as well
# (False = move the remediated files to 3_final).
PDM_KEEP_PREPROCESSED_COPY = False

# Filename of the PDM Excel workbook under app/reporting/pdm_reports/.
PDM_REPORT_FILENAME = "pdm_validation_report.xlsx"
