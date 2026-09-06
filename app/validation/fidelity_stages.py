"""
Staged Fidelity (V1 → V2 → V3)
==============================
Scores a model at each stage of the migration from what was ACTUALLY measured
at that stage.  Nothing is carried forward: every stage re-reads a different
erwin artefact and recomputes every component.

    V1   SAP PD  vs  erwinmodels/1_initial/xml       raw erwin export
         Comments have not been injected and no UDP has been written, so the
         documentation and UDP components read close to zero and the score is
         low.  This is the honest measure of the initial migration gap.

    V2   SAP PD  vs  erwinmodels/2_preprocessed/xml  after Comments/Notes
         (CDM/LDM) or structural remediation (PDM).  Documentation recovers.
         UDPs are still absent -- the XML export carries none.

    V3   SAP PD  vs  the ENRICHED erwin model        after Phase D
         The UDP component is taken from the read-back of the .erwin binary the
         UDP engine wrote, not from the XML, so enrichment finally shows up.

WHY A SEPARATE SCORE WAS NEEDED
-------------------------------
The comparators' own ``fidelity_score`` is structural only, later blended with
the XML-side UDP number by ``udp_fidelity.apply()``.  Neither moves between V1
and V2, because comment migration produces ``documentation_rows`` that are
report-only and never scored.  Measured on the shipped samples, the CDM scores
92.24 structural at BOTH V1 and V2, while its documentation goes from 5.88% to
100.00%.  Without a documentation component there is no progression to report.

THE THREE COMPONENTS
--------------------
    structural      the comparator's own structural_fidelity_score
    documentation   MATCHED / (rows where either side had text), from the
                    documentation rows the framework already builds
    udp             udp_fidelity's XML measurement at V1 and V2; the UDP
                    engine's read-back pass rate at V3

    overall = Σ (component × weight) / Σ (weight of measurable components)

A component that cannot be measured for a model -- an LDM with no extended
attributes, say -- is dropped and the remaining weights are renormalised, so a
model is never penalised for metadata it never had.

NOT SCORED, AND WHY
-------------------
Shortcuts and Tags are deliberately absent from this calculation.  erwin's XML
export has no shortcut object, so a shortcut reads as missing at V1, V2 and V3
alike: it would be a constant drag, not a progression, and the CDM comparator
already classifies shortcut rows as VERIFIED context rather than loss.  erwin
Tags have no representation anywhere in the framework -- no parser, no config,
and no Tag element in any shipped export.  Scoring either one would be
inventing a number rather than measuring one.  ``components`` records them as
``None`` so the report can say "not measured" instead of implying zero.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STAGE_V1 = "V1"
STAGE_V2 = "V2"
STAGE_V3 = "V3"
STAGES = (STAGE_V1, STAGE_V2, STAGE_V3)

COMPONENT_STRUCTURAL = "structural"
COMPONENT_DOCUMENTATION = "documentation"
COMPONENT_UDP = "udp"
COMPONENT_SHORTCUTS = "shortcuts"
COMPONENT_TAGS = "tags"

DEFAULT_WEIGHTS: Dict[str, float] = {
    COMPONENT_STRUCTURAL: 0.50,
    COMPONENT_DOCUMENTATION: 0.25,
    COMPONENT_UDP: 0.25,
}

# Documentation row statuses. Kept as literals so this module never has to
# import a tier-specific documentation package.
_DOC_MATCHED = "MATCHED"
_DOC_BOTH_EMPTY = "BOTH_EMPTY"


@dataclass
class StageScore:
    """One model measured at one stage."""

    stage: str
    overall: float = 0.0
    components: Dict[str, Optional[float]] = field(default_factory=dict)
    weights: Dict[str, float] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def component(self, name: str) -> Optional[float]:
        return self.components.get(name)

    def as_row(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "overall": self.overall,
            **{name: self.components.get(name) for name in
               (COMPONENT_STRUCTURAL, COMPONENT_DOCUMENTATION, COMPONENT_UDP)},
        }

    def summary_line(self) -> str:
        parts = [f"{name}={'n/a' if value is None else f'{value:.2f}%'}"
                 for name, value in self.components.items() if name in DEFAULT_WEIGHTS]
        return f"{self.stage} fidelity {self.overall:.2f}% ({', '.join(parts)})"


# ═════════════════════════════════════════════════════════════════════════════
#  Component measurement
# ═════════════════════════════════════════════════════════════════════════════

def _weights(config) -> Dict[str, float]:
    configured = getattr(config, "FIDELITY_STAGE_WEIGHTS", None) if config is not None else None
    if not isinstance(configured, dict) or not configured:
        return dict(DEFAULT_WEIGHTS)
    return {name: float(value) for name, value in configured.items() if float(value) > 0}


def structural_fidelity(result) -> Optional[float]:
    """The comparator's own structural number, untouched by this module."""
    for name in ("structural_fidelity_score", "fidelity_score_raw", "fidelity_score"):
        value = getattr(result, name, None)
        if value is not None:
            return float(value)
    return None


def documentation_rows(result) -> List[Any]:
    """
    The side-by-side Comment / Description / Annotation rows for this result.

    CDM and LDM results carry them already.  PDM builds them lazily in its
    report generator, so they are built here the same way, from the same
    module, and cached on the result so the report does not repeat the work.
    """
    cached = getattr(result, "documentation_rows", None)
    if cached:
        return list(cached)

    pd_file = getattr(result, "pd_file", "") or ""
    erwin_file = getattr(result, "erwin_file", "") or ""
    if not pd_file or not erwin_file:
        return []
    try:
        from app.validation.pdm_reconcile import pdm_documentation
        rows = pdm_documentation.build_rows(
            pd_file, erwin_file, getattr(result, "pd_model", "") or "")
    except Exception as exc:                                   # noqa: BLE001
        logger.debug("Documentation rows unavailable for %s: %s", pd_file, exc)
        return []
    try:
        result.documentation_rows = rows
    except AttributeError:
        logger.debug("Could not cache documentation rows on %s", type(result).__name__)
    return list(rows)


def documentation_fidelity(result) -> Optional[float]:
    """
    Share of documented objects whose text survived into erwin.

    Rows where NEITHER side carries text are excluded: an object nobody
    documented is not a migration loss, and counting it would let an
    undocumented model score 100% for free.  Returns None when the model has no
    documentation at all, so the component is dropped rather than scored zero.
    """
    rows = documentation_rows(result)
    if not rows:
        return None
    migratable = [row for row in rows if getattr(row, "status", "") != _DOC_BOTH_EMPTY]
    if not migratable:
        return None
    matched = sum(1 for row in migratable if getattr(row, "status", "") == _DOC_MATCHED)
    return round(matched / len(migratable) * 100.0, 2)


def udp_fidelity_component(result, override: Optional[float]) -> Optional[float]:
    """
    The UDP number for this stage.

    ``override`` is the UDP engine's read-back pass rate, measured against the
    enriched .erwin binary; it is passed in at V3 only.  Without it the value is
    udp_fidelity.py's measurement of the erwin XML.
    """
    if override is not None:
        return round(float(override), 2)
    if not getattr(result, "udp_total", 0):
        return None                    # the model has no UDPs to migrate
    value = getattr(result, "udp_fidelity_score", None)
    return None if value is None else round(float(value), 2)


# ═════════════════════════════════════════════════════════════════════════════
#  Scoring
# ═════════════════════════════════════════════════════════════════════════════

def _tier_config(model_type: str):
    try:
        from app.config.validation_config import tier
        return tier(model_type)
    except (ImportError, ValueError) as exc:                   # noqa: BLE001
        logger.debug("No tier config for %s: %s", model_type, exc)
        return None


def score(result, stage: str, udp_override: Optional[float] = None,
          model_type: str = "", config=None) -> StageScore:
    """
    Measure one result at one stage.  Never raises: an unmeasurable component
    is dropped, and a result with nothing measurable scores 0 with a note.
    """
    stage_score = StageScore(stage=stage)
    try:
        config = config if config is not None else _tier_config(model_type)
        weights = _weights(config)

        measured = {
            COMPONENT_STRUCTURAL: structural_fidelity(result),
            COMPONENT_DOCUMENTATION: documentation_fidelity(result),
            COMPONENT_UDP: udp_fidelity_component(result, udp_override),
        }
        # Recorded so the report can say "not measured" rather than imply zero.
        measured[COMPONENT_SHORTCUTS] = None
        measured[COMPONENT_TAGS] = None
        stage_score.components = measured

        applicable = {name: weight for name, weight in weights.items()
                      if measured.get(name) is not None}
        stage_score.weights = applicable
        total_weight = sum(applicable.values())
        if not total_weight:
            stage_score.notes.append("No fidelity component could be measured.")
            return stage_score

        stage_score.overall = round(
            sum(measured[name] * weight for name, weight in applicable.items()) / total_weight, 2)

        dropped = [name for name in weights if name not in applicable]
        if dropped:
            stage_score.notes.append(
                f"Not measurable for this model, weights renormalised: {', '.join(sorted(dropped))}.")
        if udp_override is not None:
            stage_score.notes.append(
                "UDP component taken from the enriched erwin model read-back.")
        return stage_score
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("Stage scoring failed at %s: %s", stage, exc)
        stage_score.notes.append(f"{type(exc).__name__}: {exc}")
        return stage_score


def apply(result, stage: str, udp_override: Optional[float] = None,
          model_type: str = "", config=None) -> StageScore:
    """
    Score a result at one stage and make that the result's fidelity.

    Writes ``fidelity_score`` (so the promotion gate tests the stage score),
    ``fidelity_stage``, ``documentation_fidelity_score`` and a ``stage_scores``
    dict keyed by stage.  ``structural_fidelity_score`` is left exactly as the
    comparator set it.  ERROR results are left alone.
    """
    stage_score = score(result, stage, udp_override, model_type, config)
    if result is None or getattr(result, "status", "") == "ERROR":
        return stage_score
    try:
        history = getattr(result, "stage_scores", None)
        if not isinstance(history, dict):
            history = {}
        history[stage] = stage_score
        result.stage_scores = history
        result.fidelity_stage = stage
        result.fidelity_score = stage_score.overall
        # The PDM flow's gate tests fidelity_score_raw (unrounded, so a single
        # defect cannot vanish into 2dp rounding on a large model). Keep it in
        # step with the staged score, or PDM would still be gated on its
        # structural number while CDM and LDM are gated on V3. The comparator's
        # own reconciliation number stays on structural_fidelity_score.
        if hasattr(result, "fidelity_score_raw"):
            result.fidelity_score_raw = stage_score.overall
        result.documentation_fidelity_score = stage_score.component(COMPONENT_DOCUMENTATION)
        result.udp_stage_fidelity_score = stage_score.component(COMPONENT_UDP)
    except AttributeError as exc:
        logger.warning("Could not stamp stage score onto result: %s", exc)
    return stage_score


def history(result) -> Dict[str, StageScore]:
    """Every stage this result has been scored at, keyed V1 / V2 / V3."""
    scores = getattr(result, "stage_scores", None)
    return dict(scores) if isinstance(scores, dict) else {}


def stage_value(result, stage: str) -> Optional[float]:
    """The overall score recorded at one stage, or None if never measured."""
    stage_score = history(result).get(stage)
    return None if stage_score is None else stage_score.overall


__all__ = [
    "StageScore", "STAGES", "STAGE_V1", "STAGE_V2", "STAGE_V3",
    "COMPONENT_STRUCTURAL", "COMPONENT_DOCUMENTATION", "COMPONENT_UDP",
    "COMPONENT_SHORTCUTS", "COMPONENT_TAGS", "DEFAULT_WEIGHTS",
    "score", "apply", "history", "stage_value",
    "structural_fidelity", "documentation_fidelity", "documentation_rows",
]
