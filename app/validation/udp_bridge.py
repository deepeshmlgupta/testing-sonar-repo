"""
UDP Tool Bridge
===============
Exposes the existing, untouched standalone UDP tool (``app/udp_tool``) to the
main migration pipeline, so ``app/main.py`` can run UDP extraction, mapping,
injection, reporting and comparison as one of its own phases instead of the
operator running ``app/udp_tool/batch_run.py`` separately.

Why a bridge is needed
----------------------
``app/udp_tool`` is a folder of scripts, not a package:

* it has no ``__init__.py``;
* its modules import each other by BARE name -- ``sow_classify`` does
  ``from pd_extract import Model``, and both ``udp_compare`` and ``erwin_load``
  do ``import udp_readback``.  Those names only resolve when the tool folder is
  first on ``sys.path``, which is exactly what launching each script through
  ``subprocess`` used to guarantee;
* ``erwin_load`` imports ``win32com.client`` at MODULE level and calls
  ``sys.exit(1)`` when pywin32 is missing, so merely importing it would kill a
  non-Windows pipeline run.

This bridge loads the safe modules inside a short-lived, isolated import window
-- the same technique ``pdm_validator_bridge`` already uses for the PDM
validator:

  1. remember the current ``sys.path`` / ``sys.modules`` state,
  2. put the tool folder first on ``sys.path`` and hide clashing top-level names,
  3. import the modules (their internal bare imports now resolve to their own
     files),
  4. re-register them under the ``udp_tool.*`` namespace, then restore the
     interpreter exactly as it was found.

``erwin_load`` is deliberately NEVER imported.  It is launched as a subprocess
with ``sys.executable``, which is the only way its module-level ``sys.exit`` can
be contained.

Nothing in ``app/udp_tool`` is modified, so ``python batch_run.py`` keeps working
standalone exactly as before.

Public API
----------
    available()                       -> bool    tool folder present and importable
    com_available()                   -> bool    pywin32 importable in this process
    classify(pd_path, workdir)        -> (dict, str)   Phase 1a: baseline + classification
    build_mapping(workdir, name, id)  -> (list, list)  Phase 1b: udp_schema + property_manifest
    inject(request)                   -> bool          Phase 2: COM injection (subprocess)
    migration_report(...)             -> str | None    Phase 3: UDP migration workbook
    compare(...)                      -> (result, str) Phase 4: SAP vs erwin read-back
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess  # nosec B404 - launched only with sys.executable and a literal script path
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── LOCATION OF THE STANDALONE TOOL ──────────────────────────────────────────
# app/validation/udp_bridge.py -> parents[1] is app/ -> app/udp_tool
TOOL_DIR = str(Path(__file__).resolve().parents[1] / "udp_tool")
INJECTOR_SCRIPT = os.path.join(TOOL_DIR, "erwin_load.py")

# Top-level module names owned by the tool.  They are hidden for the duration of
# the import so nothing else on sys.path can satisfy the tool's bare imports.
_OWNED_NAMES = (
    "pd_extract",
    "erwin_prepare",
    "sow_classify",
    "udp_readback",
    "udp_compare",
    "udp_report",
)

_ALIAS_PREFIX = "udp_tool"
_lock = threading.Lock()

# A dict rather than a module-level ``global``: one mutable holder, no rebinding.
_STATE: Dict[str, Any] = {"modules": None}

# ─── ARTEFACT FILENAMES (shared with the standalone tool) ─────────────────────
BASELINE_DIRNAME = "baseline"
SCHEMA_FILENAME = "udp_schema.json"
MANIFEST_FILENAME = "property_manifest.json"
RESULTS_FILENAME = "injection_results.json"
COUNTS_FILENAME = "counts.json"
ENTITIES_FILENAME = "entities.json"
EXT_ATTRS_FILENAME = "extended_attributes.json"

_ENCODING = "utf-8"


@dataclass
class InjectionRequest:
    """Everything ``erwin_load.py`` needs for one model."""

    erwin_in: str
    erwin_out: str
    schema_path: str
    manifest_path: str
    results_path: str
    name_style: str = "bare"
    timeout_seconds: int = 1800


def _module_belongs_to_tool(module: Any) -> bool:
    """True when a module object was imported from the tool folder."""
    path = getattr(module, "__file__", None) or ""
    try:
        return os.path.commonpath([os.path.abspath(path), TOOL_DIR]) == TOOL_DIR
    except (ValueError, TypeError):
        return False


def _load_isolated() -> Dict[str, Any]:
    """Import the tool's modules without disturbing anything already loaded."""
    if not os.path.isdir(TOOL_DIR):
        raise ImportError(f"UDP tool folder not found: {TOOL_DIR}")

    saved_path = list(sys.path)
    shadowed = {name: sys.modules.pop(name, None) for name in _OWNED_NAMES}
    before = set(sys.modules)

    sys.path.insert(0, TOOL_DIR)
    try:
        modules = {name: importlib.import_module(name) for name in _OWNED_NAMES}
        for name in set(sys.modules) - before:
            module = sys.modules.get(name)
            if module is not None and _module_belongs_to_tool(module):
                modules.setdefault(name, module)
    finally:
        # Publish under a private namespace so the objects stay reachable...
        aliases = set()
        for name, module in list(sys.modules.items()):
            if name in _OWNED_NAMES and module is not None and _module_belongs_to_tool(module):
                alias = f"{_ALIAS_PREFIX}.{name}"
                sys.modules[alias] = module
                aliases.add(alias)
        # ...then restore the interpreter to exactly how we found it, leaving the
        # aliases in place (removing them too would drop the only reference the
        # bridge keeps to the loaded modules).
        for name, previous in shadowed.items():
            if previous is not None:
                sys.modules[name] = previous
            else:
                sys.modules.pop(name, None)
        for name in set(sys.modules) - before - aliases:
            module = sys.modules.get(name)
            if module is not None and _module_belongs_to_tool(module):
                sys.modules.pop(name, None)
        sys.path[:] = saved_path

    logger.info("Loaded UDP tool from %s", TOOL_DIR)
    return modules


def load() -> Dict[str, Any]:
    """Load (once) and return the tool's modules as a dict."""
    with _lock:
        if _STATE["modules"] is None:
            _STATE["modules"] = _load_isolated()
    return _STATE["modules"]


def available() -> bool:
    """True when the UDP tool can be imported in this process."""
    try:
        load()
        return True
    except (ImportError, OSError) as exc:
        logger.warning("UDP tool unavailable: %s", exc)
        return False


def com_available() -> bool:
    """True when pywin32 is importable, i.e. COM injection can be attempted."""
    try:
        importlib.import_module("win32com.client")
        return True
    except ImportError:
        return False


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding=_ENCODING))
    except (OSError, ValueError) as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding=_ENCODING)


# ═════════════════════════════════════════════════════════════════════════════
#  Phase 1a — classify the SAP model and write its baseline
# ═════════════════════════════════════════════════════════════════════════════

def classify(pd_path: str, workdir: str) -> Tuple[dict, str]:
    """
    Run the tool's SOW classification, which also writes the extraction baseline.

    ``workdir`` is PER MODEL.  The standalone tool shares one ``baseline/``
    folder across every model in a batch, which is safe only because
    ``batch_run.py`` is strictly sequential; inside the pipeline's per-model loop
    that would build each manifest from the PREVIOUS model's baseline.  Giving
    each model its own working directory removes that coupling entirely.

    Returns (classification, path to the classification JSON).
    """
    modules = load()
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)

    classification = modules["sow_classify"].classify(Path(pd_path), outdir=work)
    out_path = work / f"classification_{Path(pd_path).stem}.json"
    _write_json(out_path, classification)
    return classification, str(out_path)


# ═════════════════════════════════════════════════════════════════════════════
#  Phase 1b — UDP mapping: schema + property manifest
# ═════════════════════════════════════════════════════════════════════════════

def build_mapping(workdir: str, model_name: str,
                  extraction_id: str) -> Tuple[List[dict], List[dict]]:
    """
    Turn the baseline into ``udp_schema.json`` and ``property_manifest.json``.

    Calls the tool's own ``build_udp_schema`` / ``build_manifest`` so the
    inference rules and the manifest shape stay identical to the standalone run.
    Returns (schema, manifest).
    """
    modules = load()
    prepare = modules["erwin_prepare"]
    work = Path(workdir)
    baseline = work / BASELINE_DIRNAME

    profile = _read_json(baseline / EXT_ATTRS_FILENAME, {})
    entities = _read_json(baseline / ENTITIES_FILENAME, [])

    schema = prepare.build_udp_schema(profile)
    manifest = prepare.build_manifest(entities, schema, extraction_id, model_name)

    _write_json(work / SCHEMA_FILENAME, schema)
    _write_json(work / MANIFEST_FILENAME, manifest)
    return schema, manifest


# ═════════════════════════════════════════════════════════════════════════════
#  Phase 2 — COM injection (subprocess; never imported)
# ═════════════════════════════════════════════════════════════════════════════

def inject(request: InjectionRequest) -> bool:
    """
    Run ``erwin_load.py`` against one model.

    Launched as a subprocess because the injector imports pywin32 at module
    level and exits the interpreter when it is missing.  Returns True only when
    the injector completed AND wrote a fresh results file.  Never raises.
    """
    results = Path(request.results_path)
    if results.exists():
        # An old successful run must not be mistaken for this one's evidence.
        try:
            results.unlink()
        except OSError as exc:
            logger.warning("Could not clear %s: %s", results, exc)

    command = [
        sys.executable, INJECTOR_SCRIPT,
        "--xml", request.erwin_in,
        "--manifest", request.manifest_path,
        "--schema", request.schema_path,
        "--out_erwin", request.erwin_out,
        "--results_json", request.results_path,
        "--udp_name_style", request.name_style,
    ]
    try:
        # nosec B603 - fixed executable, fixed script, no shell, arguments are
        # pipeline-generated paths rather than user input.
        completed = subprocess.run(  # nosec B603
            command, check=False, capture_output=True, text=True,
            timeout=request.timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("UDP injection could not be started: %s", exc)
        return False

    if completed.returncode != 0:
        logger.warning("UDP injection exited with %s: %s",
                       completed.returncode, (completed.stdout or completed.stderr or "").strip())
    return results.exists()


# ═════════════════════════════════════════════════════════════════════════════
#  Phase 3 — UDP migration workbook
# ═════════════════════════════════════════════════════════════════════════════

def migration_report(model_name: str, workdir: str, sap_model: str,
                     erwin_out: str, outdir: str) -> Optional[str]:
    """
    Build ``<model>_UDP_Migration_Report.xlsx`` from the mapping artefacts and,
    when the injector produced one, the injection results.  Returns the path or
    None when there was nothing to report.
    """
    modules = load()
    work = Path(workdir)
    baseline = work / BASELINE_DIRNAME

    manifest = _read_json(work / MANIFEST_FILENAME, [])
    schema = _read_json(work / SCHEMA_FILENAME, [])
    if not manifest and not schema:
        return None

    results_path = work / RESULTS_FILENAME
    results = _read_json(results_path, None) if results_path.exists() else None

    out_path = modules["udp_report"].build_report(
        model_name, manifest, schema, results,
        _read_json(work / f"classification_{model_name}.json", {}),
        _read_json(baseline / COUNTS_FILENAME, {}),
        _read_json(baseline / ENTITIES_FILENAME, []),
        sap_model, erwin_out, Path(outdir),
    )
    return str(out_path)


# ═════════════════════════════════════════════════════════════════════════════
#  Phase 4 — read erwin back and compare
# ═════════════════════════════════════════════════════════════════════════════

def compare(model_name: str, workdir: str, erwin_model: str, sap_model: str,
            model_type: str, outdir: str, method: str = "auto") -> Tuple[Any, Optional[str]]:
    """
    Compare the SAP manifest against the UDP values erwin actually holds, by
    reading the saved model back off disk.  Returns (ComparisonResult, path).
    """
    modules = load()
    work = Path(workdir)
    manifest = _read_json(work / MANIFEST_FILENAME, [])
    schema = _read_json(work / SCHEMA_FILENAME, [])

    return modules["udp_compare"].compare_model(
        model_name, manifest, schema, erwin_model,
        outdir=Path(outdir), method=method,
        sap_model=sap_model, model_type=model_type,
    )


__all__ = [
    "InjectionRequest", "TOOL_DIR", "INJECTOR_SCRIPT",
    "available", "com_available", "load",
    "classify", "build_mapping", "inject", "migration_report", "compare",
]
