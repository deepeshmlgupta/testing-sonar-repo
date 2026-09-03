"""
PDM Validator Bridge
====================
Exposes the existing, untouched PDM → erwin validator
(``pdm_reconcile/pdm_erwin_validator_Fixed/pdm_erwin_validator``) to the main
migration pipeline.

Why a bridge is needed
----------------------
The PDM validator is a self-contained tool: its modules import each other with
plain absolute names (``import config``, ``from comparator import compare``).
The LDM validator that ``app/main.py`` already loads has modules with the *same
names* (``config.py``, ``comparator.py``, ``report_generator.py``) and its folder
is on ``sys.path`` too.  Importing the PDM validator naively would either pick up
the LDM modules or poison ``sys.modules`` for the LDM side — silently corrupting
both validators.

This bridge loads the PDM validator inside a short-lived, isolated import window:

  1. remember the current ``sys.path`` / ``sys.modules`` state,
  2. put the validator folder first on ``sys.path`` and hide the conflicting
     top-level names,
  3. import the validator modules (their internal ``import config`` now resolves
     to *their own* files),
  4. re-register everything that was loaded from the validator folder under the
     ``pdm_validator.*`` namespace, then restore the original state exactly.

The validator's own source files are never modified, so
``python main.py`` inside the validator folder keeps working standalone.

Public API
----------
    parse_pdm(path)              -> dict   (PowerDesigner .pdm  -> model dict)
    parse_erwin(path)            -> dict   (erwin .xml          -> model dict)
    compare(pd_model, erwin)     -> ValidationResult
    generate_report(results, dir)-> str    (path to the .xlsx report)
    config                       -> the validator's config module
    ValidationResult, Finding    -> the validator's dataclasses
"""

import importlib
import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

# ─── LOCATION OF THE EXISTING VALIDATOR ───────────────────────────────────────
# The validator folder has lived under two names ("pdm_erwin_validator" in this
# repo; "pdm_erwin_validator_Fixed/pdm_erwin_validator" in the standalone drop).
# Probe the candidates so the bridge works in both layouts.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_CANDIDATE_DIRS = (
    os.path.join(_THIS_DIR, "pdm_erwin_validator"),
    os.path.join(_THIS_DIR, "pdm_erwin_validator_Fixed", "pdm_erwin_validator"),
)
VALIDATOR_DIR = next((d for d in _CANDIDATE_DIRS if os.path.isdir(d)),
                     _CANDIDATE_DIRS[0])

# Top-level module names owned by the validator that clash with the LDM tool.
_OWNED_NAMES = (
        "data_type_mapper",
    "pd_parser",
    "erwin_parser",
    "comparator",
    "report_generator",
)

_ALIAS_PREFIX = "pdm_validator"
_lock = threading.Lock()
_loaded = None


def _module_belongs_to_validator(module) -> bool:
    """True when a module object was imported from the validator folder."""
    path = getattr(module, "__file__", None) or ""
    try:
        return os.path.commonpath(
            [os.path.abspath(path), VALIDATOR_DIR]
        ) == VALIDATOR_DIR
    except (ValueError, TypeError):
        return False


def _load_isolated():
    """Import the validator's modules without disturbing the LDM ones."""
    if not os.path.isdir(VALIDATOR_DIR):
        raise ImportError(f"PDM validator folder not found: {VALIDATOR_DIR}")

    saved_path = list(sys.path)
    # Hide any same-named modules (the LDM tool's) for the duration of the import.
    shadowed = {name: sys.modules.pop(name, None) for name in _OWNED_NAMES}
    before = set(sys.modules)

    sys.path.insert(0, VALIDATOR_DIR)
    try:
        modules = {name: importlib.import_module(name) for name in _OWNED_NAMES}

        # Anything else the validator pulled in from its own folder gets aliased
        # too, so nothing is left dangling under a bare top-level name.
        for name in set(sys.modules) - before:
            module = sys.modules.get(name)
            if module is not None and _module_belongs_to_validator(module):
                modules.setdefault(name, module)
    finally:
        # Publish under a private namespace so the objects stay reachable...
        for name, module in list(sys.modules.items()):
            if name in _OWNED_NAMES and module is not None and _module_belongs_to_validator(module):
                sys.modules[f"{_ALIAS_PREFIX}.{name}"] = module
        # ...then restore the interpreter to exactly how we found it.
        for name, previous in shadowed.items():
            if previous is not None:
                sys.modules[name] = previous
            else:
                sys.modules.pop(name, None)
        for name in set(sys.modules) - before:
            module = sys.modules.get(name)
            if module is not None and _module_belongs_to_validator(module):
                sys.modules.pop(name, None)
        sys.path[:] = saved_path

    logger.info("Loaded PDM validator from %s", VALIDATOR_DIR)
    return modules


def load():
    """Load (once) and return the validator's modules as a dict."""
    global _loaded
    with _lock:
        if _loaded is None:
            _loaded = _load_isolated()
    return _loaded


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def parse_pdm(path: str):
    """Parse a PowerDesigner .pdm file into the validator's model dict."""
    return load()["pd_parser"].parse_pdm(path)


def parse_erwin(path: str):
    """Parse an erwin XML export into the validator's model dict."""
    return load()["erwin_parser"].parse_erwin(path)


def compare(pd_model, erwin_model):
    """Reconcile a parsed PD model against a parsed erwin model."""
    return load()["comparator"].compare(pd_model, erwin_model)


def generate_report(results, output_dir: str, filename: str = None) -> str:
    """
    Write the PDM Excel report.

    The validator's own generator takes its filename from ``config``; the
    optional ``filename`` argument lets the pipeline name the file to match its
    other reports without editing the validator.
    """
    modules = load()
    if filename:
        # The validator's modules now read the central PDM tier view instead of
        # a local config.py, so the runtime rename has to land on THAT object.
        # modules["config"] no longer exists; using it raised KeyError the
        # moment a caller asked for a named workbook.
        get_config().REPORT_FILENAME = filename
    return modules["report_generator"].generate_report(results, output_dir)


def get_config():
    """The validator's config module (severity policy, thresholds, toggles)."""
    from app.config.validation_config import PDM_CONFIG
    return PDM_CONFIG


def get_result_classes():
    """(ValidationResult, Finding) dataclasses used by the validator."""
    comparator = load()["comparator"]
    return comparator.ValidationResult, comparator.Finding


def validate_pair(pdm_path: str, erwin_xml_path: str):
    """
    Validate one .pdm against one erwin .xml.

    Mirrors the validator's own ``validate_pair`` so a parse failure becomes an
    ERROR result instead of an exception that would stop the batch.
    """
    ValidationResult, Finding = get_result_classes()
    try:
        return compare(parse_pdm(pdm_path), parse_erwin(erwin_xml_path))
    except Exception as exc:                                  # noqa: BLE001
        logger.error("PDM validation failed (%s vs %s): %s",
                     pdm_path, erwin_xml_path, exc)
        result = ValidationResult(pd_file=pdm_path, erwin_file=erwin_xml_path,
                                  status="ERROR")
        result.add(Finding("EXCEPTION", "CRITICAL", message=str(exc)))
        result.compute_score()
        return result
