"""
PDM Reconciliation Flow
=======================
The PDM half of Phase D, kept in its own module so it can be exercised without
starting erwin (the COM automation in ``app/erwin`` only runs on Windows).

The flow implements the promotion gate:

    1_initial ──validate──▶ 100%? ──yes──────────────────────────▶ 3_final
                             │
                             no
                             ▼
                        preprocess  (PowerDesigner-driven remediation)
                             │
                             ▼
                      2_preprocessed  ──re-validate──▶ 100%? ──yes──▶ 3_final
                                                         │
                                                         no
                                                         ▼
                                            stays in 2_preprocessed
                                            (reported, not promoted)

A model is only ever promoted on a *measured* 100% fidelity — the second
validation pass reads the rewritten XML back off disk rather than trusting what
preprocessing believes it changed.
"""

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.preprocessing import pdm_preprocessor
from app.validation.pdm_reconcile import pdm_validator_bridge as bridge
# Migration framework additions: staged V1/V2/V3 scoring and the shared
# >=90% PASS / <90% FAIL routing.
from app.validation import fidelity_stages, promotion_gate

logger = logging.getLogger(__name__)

STAGE_INITIAL = "1_initial"
STAGE_PREPROCESSED = "2_preprocessed"
STAGE_FINAL = "3_final"
STAGE_MANUAL_REVIEW = "manual_review"  # <model>/manual_review_report


@dataclass
class PdmFlowOutcome:
    """Everything the pipeline needs to know about one PDM model."""
    model_name: str
    pdm_path: str = ""
    initial_result: Any = None
    final_result: Any = None
    preprocess_report: Optional[pdm_preprocessor.PreprocessReport] = None
    preprocessed: bool = False
    promoted: bool = False
    stage: str = STAGE_INITIAL
    artifacts: Dict[str, str] = field(default_factory=dict)
    messages: List[str] = field(default_factory=list)

    @property
    def initial_fidelity(self) -> float:
        return getattr(self.initial_result, "fidelity_score", 0.0)

    @property
    def final_fidelity(self) -> float:
        return getattr(self.final_result, "fidelity_score", 0.0)

    @property
    def status(self) -> str:
        return getattr(self.final_result, "status", "ERROR")


def _move(source: str, target: str) -> str:
    """Move a file, replacing any previous copy at the destination."""
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    if os.path.abspath(source) == os.path.abspath(target):
        return target
    if os.path.exists(target):
        os.remove(target)
    shutil.move(source, target)
    return target


def _copy(source: str, target: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    shutil.copy2(source, target)
    return target


def _stamp(outcome: "PdmFlowOutcome", pd_model=None, erwin_xml: str = "") -> "PdmFlowOutcome":
    """
    Carry the flow's own context onto the result object the report reads.

    The validator's ValidationResult knows nothing about staging or promotion —
    those are this module's concepts — so the report would otherwise have no way
    to show WHY a 99% model was not promoted. The parsed models ride along too,
    so the report's matrix sheets cost no second parse.
    """
    result = outcome.final_result
    if result is None:
        return outcome
    result.stage = outcome.stage
    result.promoted = outcome.promoted
    result.flow_notes = " | ".join(outcome.messages)

    # The workbook is built from the FINAL result only, so everything the raw
    # erwin import actually lost — the columns preprocessing then put back —
    # used to be invisible: a model that imported with seven dropped columns
    # and two emptied primary keys produced a report showing none of that.
    # Carry the as-imported result and the remediation log through so the
    # report can show what the migration did before it was repaired.
    result.initial_result = outcome.initial_result
    result.preprocess_report = outcome.preprocess_report
    if pd_model is not None:
        # The erwin side is whichever XML produced this result.
        try:
            erwin_model = bridge.parse_erwin(erwin_xml) if erwin_xml else None
        except Exception:                                      # noqa: BLE001
            erwin_model = None
        result.source_models = {"pd": pd_model, "erwin": erwin_model}
    return outcome


def _gate_failures(result, target_fidelity: float) -> List[str]:
    """
    Every reason this model must NOT be promoted automatically. Empty = promote.

    The gate used to be one line: ``fidelity_score >= target``. Three things
    were wrong with it, and each of them promotes a defective model:

    1. ``fidelity_score`` is rounded to 2 decimal places. The penalty for one
       defect is divided by the number of comparable objects, so on a large
       physical model the defect vanishes into the rounding: one INFO finding
       across 1000 objects scores 99.995% and displays — and compared — as
       100.00%. At 20 000 objects even a CRITICAL finding rounds to 100.00%.
       The raw score is used here instead.
    2. Nothing looked at the findings. A model could carry CRITICAL findings
       (a dropped table, a lost primary key) and still pass on the number
       alone. The LDM/CDM route in app/main.py has always required
       ``status == "PASS" and critical_count == 0``; the PDM route did not.
    3. Nothing checked that the report's own arithmetic held. If the counts do
       not reconcile the fidelity score is computed over a denominator that
       does not describe the model, so the number means nothing.

    A model with WARNING findings and no CRITICAL ones is deliberately NOT
    auto-promoted: warnings are the human-sign-off path through
    app/promote_model.py, which re-validates before it copies anything.
    """
    if result is None:
        return ["no validation result"]
    if getattr(result, "status", "") == "ERROR":
        return ["validation ended in ERROR"]

    failures: List[str] = []

    # Whether findings block promotion outright is now a setting, so the
    # ">= PROMOTION_FIDELITY_THRESHOLD means PASS" rule holds for PDM exactly as
    # it does for CDM and LDM. Set PROMOTION_BLOCK_ON_CRITICAL /
    # PROMOTION_BLOCK_ON_WARNING to True in app/config/settings.py to restore
    # the previous behaviour, where any finding held the model back.
    critical = int(getattr(result, "critical_count", 0) or 0)
    if critical and promotion_gate.blocks_on_critical():
        failures.append(f"{critical} CRITICAL finding(s)")

    warning = int(getattr(result, "warning_count", 0) or 0)
    if warning and promotion_gate.blocks_on_warning():
        failures.append(f"{warning} WARNING finding(s) awaiting human sign-off")

    raw = float(getattr(result, "fidelity_score_raw",
                        getattr(result, "fidelity_score", 0.0)))
    if raw < float(target_fidelity):
        failures.append(f"measured fidelity {raw:.4f}% is below the "
                        f"{float(target_fidelity):.2f}% target")

    errors = getattr(result, "reconciliation_errors", None)
    if callable(errors):
        broken = errors()
        if broken:
            failures.append("reported counts do not reconcile (" +
                            "; ".join(broken) + ")")

    return failures


def _is_complete(result, target_fidelity: float) -> bool:
    """True only when nothing blocks automatic promotion."""
    return not _gate_failures(result, target_fidelity)


def _missing_input(pdm_path: str, initial_xml: str) -> str:
    """
    Why the flow cannot start, or "" when both inputs are present.

    We need both the source model and the erwin XML export.
    """
    if not os.path.exists(pdm_path):
        return f"PowerDesigner model not found: {pdm_path}"
    if not os.path.exists(initial_xml):
        return (f"erwin XML export not found: {initial_xml}. "
                "Phase B must produce the XML before reconciliation can run.")
    return ""


def _log_pass(pass_number: int, result) -> None:
    logger.info("  pass %d → status=%s fidelity=%.2f%% (critical=%d warning=%d info=%d)",
                pass_number, result.status, result.fidelity_score,
                result.critical_count,
                result.warning_count,
                result.info_count)


def _validate_initial(outcome: "PdmFlowOutcome", pdm_path: str, initial_xml: str,
                      initial_erwin: str, model_name: str):
    """Pass 1: validate the raw import; returns the parsed PD model."""
    logger.info("PDM validation (pass 1): %s", model_name)
    pd_model = bridge.parse_pdm(pdm_path)
    outcome.initial_result = bridge.validate_pair(pdm_path, initial_xml)
    # V1: the raw erwin export, before any remediation or enrichment.
    fidelity_stages.apply(outcome.initial_result, fidelity_stages.STAGE_V1,
                          model_type="PDM")
    outcome.final_result = outcome.initial_result
    outcome.artifacts["initial_xml"] = initial_xml
    if os.path.exists(initial_erwin):
        outcome.artifacts["initial_erwin"] = initial_erwin

    _log_pass(1, outcome.initial_result)
    return pd_model


def _promote_from_initial(outcome: "PdmFlowOutcome", initial_xml: str, initial_erwin: str,
                          final_xml: str, final_erwin: str) -> None:
    """Already perfect: promote straight from 1_initial."""
    logger.info("  fidelity target met on import; promoting to %s", STAGE_FINAL)
    outcome.artifacts["final_xml"] = _copy(initial_xml, final_xml)
    if os.path.exists(initial_erwin):
        outcome.artifacts["final_erwin"] = _copy(initial_erwin, final_erwin)
    outcome.promoted = True
    outcome.stage = STAGE_FINAL
    outcome.messages.append(
        f"Fidelity {outcome.initial_result.fidelity_score:.2f}% on import; "
        f"promoted to {STAGE_FINAL} without preprocessing.")


def _run_preprocessing(outcome: "PdmFlowOutcome", pd_model, model_name: str,
                       initial_xml: str, initial_erwin: str,
                       preprocessed_xml: str, preprocessed_erwin: str,
                       target_fidelity: float):
    """Preprocess into 2_preprocessed; returns the preprocessing report."""
    logger.info("  fidelity %.2f%% below target %.2f%%; applying preprocessing",
                outcome.initial_result.fidelity_score, target_fidelity)
    report = pdm_preprocessor.preprocess_model(
        pd_model=pd_model,
        result=outcome.initial_result,
        source_xml=initial_xml,
        target_xml=preprocessed_xml,
        source_erwin=initial_erwin,
        target_erwin=preprocessed_erwin,
        validator_config=bridge.get_config(),
        model_name=model_name,
    )
    outcome.preprocess_report = report
    outcome.preprocessed = True
    outcome.stage = STAGE_PREPROCESSED
    outcome.artifacts["preprocessed_xml"] = preprocessed_xml
    if os.path.exists(preprocessed_erwin):
        outcome.artifacts["preprocessed_erwin"] = preprocessed_erwin
    logger.info("  preprocessing → %s", report.summary())
    return report


def _revalidate(outcome: "PdmFlowOutcome", pdm_path: str, preprocessed_xml: str,
                model_name: str, report,
                udp_pass_rate: Optional[float] = None) -> None:
    """Pass 2: re-validate what was actually written."""
    logger.info("PDM validation (pass 2): %s", model_name)
    outcome.final_result = bridge.validate_pair(pdm_path, preprocessed_xml)
    # V2: after structural remediation, still against the XML export.
    fidelity_stages.apply(outcome.final_result, fidelity_stages.STAGE_V2,
                          model_type="PDM")
    # V3: the same reconciliation, with the UDP component taken from the
    # enriched erwin model the UDP engine wrote. This is the score the gate
    # below tests, so promotion reflects the migration as finally delivered.
    fidelity_stages.apply(outcome.final_result, fidelity_stages.STAGE_V3,
                          udp_override=udp_pass_rate, model_type="PDM")
    _log_pass(2, outcome.final_result)

    gain = outcome.final_result.fidelity_score - outcome.initial_result.fidelity_score
    outcome.messages.append(
        f"Preprocessing applied ({report.summary()}); fidelity "
        f"{outcome.initial_result.fidelity_score:.2f}% → "
        f"{outcome.final_result.fidelity_score:.2f}% ({gain:+.2f}).")


def _warn_if_binary_stale(outcome: "PdmFlowOutcome", report) -> None:
    """
    The XML that was validated is the one being promoted. The .erwin
    binary next to it is only a byte copy of the pre-remediation file
    unless erwin's COM API was available to regenerate it — so say so
    rather than letting a "promoted" model ship a binary that still
    lacks the restored columns.
    """
    if getattr(report, "erwin_binary_stale", False):
        outcome.messages.append(
            "WARNING: the promoted .erwin binary was carried forward "
            "unchanged and does NOT contain the remediation — only the "
            "XML does. Regenerate it on a Windows host with erwin Data "
            "Modeler before treating the binary as the final artefact.")
        logger.warning("  %s", outcome.messages[-1])


def _promote_after_preprocessing(outcome: "PdmFlowOutcome", report,
                                 preprocessed_xml: str, preprocessed_erwin: str,
                                 final_xml: str, final_erwin: str,
                                 keep_preprocessed_copy: bool) -> None:
    logger.info("  fidelity target met after preprocessing; promoting to %s",
                STAGE_FINAL)
    transfer = _copy if keep_preprocessed_copy else _move
    outcome.artifacts["final_xml"] = transfer(preprocessed_xml, final_xml)
    if os.path.exists(preprocessed_erwin):
        outcome.artifacts["final_erwin"] = transfer(preprocessed_erwin,
                                                    final_erwin)
    if not keep_preprocessed_copy:
        outcome.artifacts.pop("preprocessed_xml", None)
        outcome.artifacts.pop("preprocessed_erwin", None)
    outcome.promoted = True
    outcome.stage = STAGE_FINAL
    outcome.messages.append(f"Promoted to {STAGE_FINAL}.")
    _warn_if_binary_stale(outcome, report)


def _hold_for_review(outcome: "PdmFlowOutcome", target_fidelity: float,
                     preprocessed_xml: str = "", preprocessed_erwin: str = "",
                     manual_review_dir: str = "") -> None:
    """
    The model did not reach the promotion threshold.

    Two-band mode (the default) moves it and its .erwin binary into that
    model's own manual_review_report folder. Three-band mode keeps the previous
    behaviour and leaves it in 2_preprocessed for review.
    """
    blockers = _gate_failures(outcome.final_result, target_fidelity)

    if promotion_gate.bands_mode() == promotion_gate.BANDS_TWO:
        moved = promotion_gate.route(outcome.final_result,
                                     preprocessed_xml, preprocessed_erwin,
                                     destination=manual_review_dir)
        outcome.stage = STAGE_MANUAL_REVIEW
        for path in moved:
            key = ("manual_review_erwin" if path.lower().endswith(".erwin")
                   else "manual_review_xml")
            outcome.artifacts[key] = path
        outcome.artifacts.pop("preprocessed_xml", None)
        outcome.artifacts.pop("preprocessed_erwin", None)
        outcome.messages.append(
            f"FAIL — fidelity below the {target_fidelity:.2f}% threshold; "
            f"moved to {STAGE_MANUAL_REVIEW}. Blocked by: {'; '.join(blockers)}.")
        logger.warning("  %s", outcome.messages[-1])
        return

    outcome.messages.append(
        f"NOT promoted — held in {STAGE_PREPROCESSED} for review. "
        f"Blocked by: {'; '.join(blockers)}.")
    logger.info("  %s", outcome.messages[-1])


def run_pdm_flow(pdm_path: str,
                 initial_erwin: str,
                 initial_xml: str,
                 preprocessed_erwin: str,
                 preprocessed_xml: str,
                 final_erwin: str,
                 final_xml: str,
                 target_fidelity: float = 100.0,
                 preprocess_enabled: bool = True,
                 keep_preprocessed_copy: bool = False,
                 udp_pass_rate: Optional[float] = None,
                 manual_review_dir: str = "") -> PdmFlowOutcome:
    """
    Validate one PDM model, remediate it if it falls short, and promote it only
    when it measures at the fidelity target.

    All path arguments are full file paths; parent folders are created on demand.
    """
    model_name = os.path.splitext(os.path.basename(pdm_path))[0]
    outcome = PdmFlowOutcome(model_name=model_name, pdm_path=pdm_path)

    # ── Guard: we need both the source model and the erwin XML export ──────────
    missing = _missing_input(pdm_path, initial_xml)
    if missing:
        outcome.messages.append(missing)
        logger.error(outcome.messages[-1])
        return outcome

    # ── Pass 1: validate the raw import ───────────────────────────────────────
    pd_model = _validate_initial(outcome, pdm_path, initial_xml,
                                 initial_erwin, model_name)

    # ── Already perfect: promote straight from 1_initial ──────────────────────
    if _is_complete(outcome.initial_result, target_fidelity):
        _promote_from_initial(outcome, initial_xml, initial_erwin,
                              final_xml, final_erwin)
        return _stamp(outcome, pd_model, initial_xml)

    if not preprocess_enabled:
        outcome.messages.append(
            f"NOT promoted and preprocessing is disabled. Blocked by: "
            f"{'; '.join(_gate_failures(outcome.initial_result, target_fidelity))}.")
        logger.warning("  %s", outcome.messages[-1])
        return _stamp(outcome, pd_model, initial_xml)

    # ── Preprocess into 2_preprocessed ────────────────────────────────────────
    report = _run_preprocessing(outcome, pd_model, model_name,
                                initial_xml, initial_erwin,
                                preprocessed_xml, preprocessed_erwin,
                                target_fidelity)

    # ── Pass 2: re-validate what was actually written ─────────────────────────
    _revalidate(outcome, pdm_path, preprocessed_xml, model_name, report,
                udp_pass_rate)

    # ── Promotion gate ────────────────────────────────────────────────────────
    if _is_complete(outcome.final_result, target_fidelity):
        _promote_after_preprocessing(outcome, report,
                                     preprocessed_xml, preprocessed_erwin,
                                     final_xml, final_erwin,
                                     keep_preprocessed_copy)
    else:
        _hold_for_review(outcome, target_fidelity,
                         preprocessed_xml, preprocessed_erwin,
                         manual_review_dir)

    # Pass 2 read the remediated XML, so that is the erwin side of the report.
    return _stamp(outcome, pd_model,
                  outcome.artifacts.get("final_xml") or preprocessed_xml)
