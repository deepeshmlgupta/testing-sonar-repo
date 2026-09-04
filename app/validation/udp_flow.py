"""
UDP Flow (Phase D)
==================
Runs the standalone UDP engine as one phase of the main pipeline, once per
model, for CDM, LDM and PDM alike.  ``app/main.py`` calls ``run_udp_flow`` after
reconciliation and before the promotion gate.

The four phases are the same ones ``app/udp_tool/batch_run.py`` drives, in the
same order, with the same modules -- only the orchestration moves:

    1a  classify   SAP model  -> per-model baseline + SOW classification
    1b  mapping    baseline   -> udp_schema.json + property_manifest.json
    2   inject     manifest   -> erwin .erwin binary       (COM, Windows only)
    3   report     manifest + injection results -> UDP Migration workbook
    4   compare    saved .erwin read back       -> UDP Comparison workbook

Design rules, kept deliberately identical to the rest of the framework:

* NEVER RAISES.  Every failure lands on ``UdpStageOutcome.error`` / ``.messages``
  and the pipeline carries on.  A UDP problem must not turn a good structural
  validation into a failed run.
* ADDITIVE.  Nothing here is imported by a parser, a comparator, a finding
  emitter or the preprocessing code.  ``UDP_TOOL_ENABLED = False`` skips the
  whole phase and the framework behaves exactly as it did before.
* PHASE 4 STILL RUNS WHEN PHASE 2 FAILS.  Phase 3 reports what the injector
  believed happened; Phase 4 reports what is in the file.  When those disagree,
  the file wins -- so the comparison is attempted regardless.
* SEPARATE FROM ``udp_fidelity.py``.  That module scores the erwin **XML**
  export and feeds the fidelity score.  This flow works on the erwin **.erwin**
  binary and produces the migration evidence.  Both numbers appear side by side
  on the V3 report; neither replaces the other.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence

from app.validation import udp_bridge

logger = logging.getLogger(__name__)

# ─── stages ───────────────────────────────────────────────────────────────────
STAGE_SKIPPED = "SKIPPED"
STAGE_EXTRACTED = "EXTRACTED"
STAGE_INJECTED = "INJECTED"
STAGE_COMPARED = "COMPARED"
STAGE_FAILED = "FAILED"

# ─── defaults, overridable through the tier config ────────────────────────────
DEFAULT_ENABLED = True
DEFAULT_INJECT_ENABLED = True
DEFAULT_READBACK_METHOD = "auto"
DEFAULT_NAME_STYLE = "bare"
DEFAULT_WORKDIR = "data/udp"
DEFAULT_EXTRACTION_ID = "46603045"
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_REQUIRE_ERWIN_BINARY = False
DEFAULT_ERWIN_INPUT_DIRS = (
    "erwinmodels/1_initial/erwin",
    "app/udp_tool/input_erwin_models",
)
DEFAULT_ERWIN_READBACK_DIRS = (
    "erwinmodels/2_preprocessed/erwin",
    "app/udp_tool/output_erwin_models",
    "erwinmodels/1_initial/erwin",
    "app/udp_tool/input_erwin_models",
)

_ERWIN_SUFFIX = ".erwin"


@dataclass
class UdpFlowRequest:
    """One model's inputs for Phase D."""

    pd_path: str
    model_name: str
    model_type: str                 # CDM | LDM | PDM
    reports_dir: str
    erwin_out: str = ""             # where an injected binary should be written
    workdir: str = ""               # defaults to <UDP_TOOL_WORKDIR>/<model_name>


@dataclass
class UdpStageOutcome:
    """Everything the pipeline and the V3 report need about one model's UDPs."""

    model_name: str = ""
    model_type: str = ""
    stage: str = STAGE_SKIPPED
    schema_path: str = ""
    manifest_path: str = ""
    classification_path: str = ""
    injection_results_path: str = ""
    migration_report_path: str = ""
    comparison_report_path: str = ""
    erwin_read: str = ""
    injected: bool = False
    comparison_result: Any = None
    messages: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def udp_values(self) -> int:
        """Manifest rows with a value SAP actually held (the comparison base)."""
        result = self.comparison_result
        return int(getattr(result, "comparable", 0) or 0) if result is not None else 0

    @property
    def pass_rate(self) -> Optional[float]:
        """Share of SAP UDP values erwin holds, or None when nothing was read."""
        result = self.comparison_result
        if result is None or not self.udp_values:
            return None
        return float(getattr(result, "pass_rate", 0.0) or 0.0)

    @property
    def verdict(self) -> str:
        result = self.comparison_result
        if result is None:
            return "NOT COMPARED"
        return str(getattr(result, "status", "UNKNOWN"))

    @property
    def readback_method(self) -> str:
        readback = getattr(self.comparison_result, "readback", None)
        return str(getattr(readback, "method", "")) if readback is not None else ""

    @property
    def readback_reliable(self) -> Optional[bool]:
        readback = getattr(self.comparison_result, "readback", None)
        return bool(getattr(readback, "reliable", False)) if readback is not None else None

    def summary_line(self) -> str:
        if self.error:
            return f"UDP stage {self.stage}: {self.error}"
        result = self.comparison_result
        if result is None:
            return f"UDP stage {self.stage}; no comparison performed."
        return str(result.summary_line())


# ═════════════════════════════════════════════════════════════════════════════
#  Configuration helpers
# ═════════════════════════════════════════════════════════════════════════════

def _tier_config(model_type: str):
    try:
        from app.config.validation_config import tier
        return tier(model_type)
    except (ImportError, ValueError) as exc:                   # noqa: BLE001
        logger.debug("No tier config for %s: %s", model_type, exc)
        return None


def _cfg(config, name: str, default):
    return getattr(config, name, default) if config is not None else default


def _find_existing(directories: Sequence[str], model_name: str) -> str:
    """First ``<dir>/<model_name>.erwin`` that exists, or ''."""
    for directory in directories or ():
        candidate = Path(directory) / f"{model_name}{_ERWIN_SUFFIX}"
        if candidate.is_file():
            return str(candidate)
    return ""


# ═════════════════════════════════════════════════════════════════════════════
#  Phase steps
# ═════════════════════════════════════════════════════════════════════════════

def _run_mapping(request: UdpFlowRequest, outcome: UdpStageOutcome,
                 workdir: str, extraction_id: str) -> bool:
    """Phases 1a + 1b.  Returns True when the mapping artefacts exist."""
    _, classification_path = udp_bridge.classify(request.pd_path, workdir)
    outcome.classification_path = classification_path

    schema, manifest = udp_bridge.build_mapping(workdir, request.model_name, extraction_id)
    outcome.schema_path = str(Path(workdir) / udp_bridge.SCHEMA_FILENAME)
    outcome.manifest_path = str(Path(workdir) / udp_bridge.MANIFEST_FILENAME)
    outcome.stage = STAGE_EXTRACTED
    outcome.messages.append(
        f"Mapped {len(schema)} UDP definition(s) and {len(manifest)} property value(s).")
    return bool(schema or manifest)


def _run_injection(request: UdpFlowRequest, outcome: UdpStageOutcome,
                   workdir: str, config) -> None:
    """Phase 2.  Records why it was skipped rather than failing silently."""
    if not _cfg(config, "UDP_TOOL_INJECT_ENABLED", DEFAULT_INJECT_ENABLED):
        outcome.messages.append("Injection disabled by configuration.")
        return

    erwin_in = _find_existing(
        _cfg(config, "UDP_TOOL_ERWIN_INPUT_DIRS", DEFAULT_ERWIN_INPUT_DIRS),
        request.model_name)
    if not erwin_in:
        outcome.messages.append(
            "No source .erwin binary found; injection skipped. Save the model as "
            "<name>.erwin next to its XML export in erwinmodels/1_initial/erwin/.")
        return

    if not udp_bridge.com_available():
        outcome.messages.append(
            "pywin32 / erwin COM not available on this host; injection skipped. "
            "The comparison phase still reads the model back off disk.")
        return

    erwin_out = request.erwin_out or str(
        Path("erwinmodels/2_preprocessed/erwin") / f"{request.model_name}{_ERWIN_SUFFIX}")
    Path(erwin_out).parent.mkdir(parents=True, exist_ok=True)
    results_path = str(Path(workdir) / udp_bridge.RESULTS_FILENAME)

    injected = udp_bridge.inject(udp_bridge.InjectionRequest(
        erwin_in=erwin_in,
        erwin_out=erwin_out,
        schema_path=outcome.schema_path,
        manifest_path=outcome.manifest_path,
        results_path=results_path,
        name_style=str(_cfg(config, "UDP_TOOL_NAME_STYLE", DEFAULT_NAME_STYLE)),
        timeout_seconds=int(_cfg(config, "UDP_TOOL_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
    ))

    outcome.injected = injected
    if injected:
        outcome.injection_results_path = results_path
        outcome.stage = STAGE_INJECTED
        outcome.messages.append(f"UDPs injected into {erwin_out}.")
    else:
        outcome.messages.append(
            "Injection did not complete; no fresh results file was written. "
            "Continuing to the comparison phase, which reads erwin off disk.")


def _run_reporting(request: UdpFlowRequest, outcome: UdpStageOutcome,
                   workdir: str, config) -> None:
    """Phases 3 + 4."""
    erwin_read = _find_existing(
        _cfg(config, "UDP_TOOL_ERWIN_READBACK_DIRS", DEFAULT_ERWIN_READBACK_DIRS),
        request.model_name)

    outcome.migration_report_path = udp_bridge.migration_report(
        request.model_name, workdir, request.pd_path, erwin_read,
        request.reports_dir) or ""

    if not erwin_read:
        outcome.messages.append(
            "No .erwin binary available to read back; comparison skipped.")
        return

    outcome.erwin_read = erwin_read
    result, report_path = udp_bridge.compare(
        request.model_name, workdir, erwin_read, request.pd_path,
        request.model_type, request.reports_dir,
        method=str(_cfg(config, "UDP_TOOL_READBACK_METHOD", DEFAULT_READBACK_METHOD)))

    outcome.comparison_result = result
    outcome.comparison_report_path = report_path or ""
    outcome.stage = STAGE_COMPARED
    outcome.messages.append(result.summary_line())
    outcome.messages.extend(result.warnings)


# ═════════════════════════════════════════════════════════════════════════════
#  Public entry point
# ═════════════════════════════════════════════════════════════════════════════

def run_udp_flow(request: UdpFlowRequest, config=None) -> UdpStageOutcome:
    """
    Run the UDP engine for one model.  Never raises: every failure is recorded
    on the returned outcome so the pipeline can report it and carry on.
    """
    outcome = UdpStageOutcome(model_name=request.model_name,
                              model_type=(request.model_type or "").upper())
    try:
        config = config if config is not None else _tier_config(request.model_type)

        if not _cfg(config, "UDP_TOOL_ENABLED", DEFAULT_ENABLED):
            outcome.messages.append("UDP phase disabled by configuration.")
            return outcome

        if not os.path.isfile(request.pd_path):
            outcome.stage = STAGE_FAILED
            outcome.error = f"SAP PD model not found: {request.pd_path}"
            return outcome

        if not udp_bridge.available():
            outcome.messages.append(
                f"UDP tool not importable from {udp_bridge.TOOL_DIR}; phase skipped.")
            return outcome

        workdir = request.workdir or str(
            Path(_cfg(config, "UDP_TOOL_WORKDIR", DEFAULT_WORKDIR)) / request.model_name)
        Path(request.reports_dir).mkdir(parents=True, exist_ok=True)

        extraction_id = str(_cfg(config, "UDP_TOOL_EXTRACTION_ID", DEFAULT_EXTRACTION_ID))
        if not _run_mapping(request, outcome, workdir, extraction_id):
            outcome.messages.append("No UDP values found in the SAP model.")
            return outcome

        _run_injection(request, outcome, workdir, config)
        _run_reporting(request, outcome, workdir, config)

        if _cfg(config, "UDP_TOOL_REQUIRE_ERWIN_BINARY", DEFAULT_REQUIRE_ERWIN_BINARY) \
                and not outcome.erwin_read:
            outcome.stage = STAGE_FAILED
            outcome.error = "UDP_TOOL_REQUIRE_ERWIN_BINARY is set but no .erwin was found."
        return outcome
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("UDP phase failed for %s: %s", request.model_name, exc)
        outcome.stage = STAGE_FAILED
        outcome.error = f"{type(exc).__name__}: {exc}"
        return outcome


__all__ = [
    "UdpFlowRequest", "UdpStageOutcome", "run_udp_flow",
    "STAGE_SKIPPED", "STAGE_EXTRACTED", "STAGE_INJECTED",
    "STAGE_COMPARED", "STAGE_FAILED",
]
