"""
CDM → erwin Conceptual Model Validation
---------------------------------------
The standalone `cdm_erwin_validator` moved into the framework.

Validation logic is UNCHANGED.  Two kinds of edit were made:

  1. Intra-package imports converted from the flat form (``import config``) to
     the package-relative form (``from app.config.validation_config import CDM_CONFIG as config``).  Required: the LDM
     package contains modules with the same bare names (config, comparator,
     normalizers, cardinality, report_generator).  Under flat imports whichever
     package loads first owns those names in ``sys.modules``, so the other
     silently receives the wrong rule set -- a CDM scored under LDM severities
     produces a plausible report with wrong numbers and no error anywhere.

  2. Additive, report-only fields for the Comments→Notes / Definition→Definition
     mapping (see documentation.py).  Nothing there feeds the findings list,
     the fidelity score or the PASS/WARN/FAIL status.

Uniform surface, matching ldm_reconcile so main.py needs no per-type branch:

    MODEL_TYPE        "CDM"
    SOURCE_EXTENSION  ".cdm"
    parse_source()    SAP PD .cdm      -> CDMModel
    parse_target()    erwin XML export -> CDMModel
    compare()         (source, target) -> ValidationResult
    generate_report() ([results], out_dir, filename) -> path
"""

from . import cardinality, cdm_model, documentation, normalizers
from .comparator import Finding, ValidationResult, compare
from .erwin_cdm_parser import parse_erwin
from .pd_cdm_parser import parse_cdm
from .report_generator import generate_report

MODEL_TYPE = "CDM"
SOURCE_EXTENSION = ".cdm"
TARGET_EXTENSION = ".xml"

# Aliases only -- the underlying functions are untouched.
parse_source = parse_cdm
parse_target = parse_erwin

__all__ = [
    "MODEL_TYPE", "SOURCE_EXTENSION", "TARGET_EXTENSION",
    "parse_source", "parse_target", "compare", "generate_report",
    "parse_cdm", "parse_erwin", "Finding", "ValidationResult",
    "cdm_model", "normalizers", "cardinality", "documentation",
]
