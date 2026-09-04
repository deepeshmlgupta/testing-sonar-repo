"""
Promotion Gate (CDM, LDM and PDM)
=================================
One rule, shared by every model type, placing a reconciled model in a band.

DEFAULT (PROMOTION_BANDS = "two"):

    fidelity >= PROMOTION_FIDELITY_THRESHOLD (90)  PASS  -> erwinmodels/3_final
    fidelity <  PROMOTION_FIDELITY_THRESHOLD (90)  FAIL  -> manual_review/

PROMOTION_BANDS = "three" restores the previous three-band behaviour:

    fidelity >= PROMOTION_FIDELITY_THRESHOLD (90)  PASS  -> erwinmodels/3_final
    REVIEW_FIDELITY_FLOOR (60) <= fidelity < 90    WARN  -> stays in 2_preprocessed
    fidelity <  REVIEW_FIDELITY_FLOOR (60)         FAIL  -> moved to 2_preprocessed/rejected

`fidelity_score` is the OVERALL score the reports show — structural fidelity
blended with UDP fidelity by app/validation/udp_fidelity.py — so a model whose
UDPs are still missing is judged on the same number a reviewer reads.

What this replaced
------------------
    CDM / LDM (app/main.py)   promoted only when status == "PASS" and no CRITICAL
                              finding, i.e. zero WARNING and zero CRITICAL findings.
    PDM (pdm_flow.py)         promoted only at a measured 100.00% (raw score) with
                              no CRITICAL, no WARNING and reconciling counts.

Those rules are still available through app/config/settings.py:

    PROMOTION_FIDELITY_THRESHOLD = 100.0
    PROMOTION_BLOCK_ON_CRITICAL  = True
    PROMOTION_BLOCK_ON_WARNING   = True
    PROMOTION_FIDELITY_BASIS     = "structural"

restores the previous behaviour exactly.  PROMOTION_FIDELITY_BASIS chooses which
number the threshold is tested against: "overall" (default — the blended score
shown in the reports, so missing UDPs block promotion) or "structural" (the
comparator's own score, ignoring UDPs).

What never changes
------------------
* An ERROR result (parse failure, empty model) is never promoted.
* A PDM whose reported counts do not reconcile is never promoted — that is an
  arithmetic-integrity check on the report, not a fidelity criterion.
* Findings are untouched: a model promoted with WARNING findings still lists
  every one of them on the FINDINGS sheet.  Only `status` is set to PASS, and the
  reason is recorded on `result.promotion_note`.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import List

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 90.0
DEFAULT_FLOOR = 60.0
DEFAULT_REJECTED_SUBDIR = "rejected"

BAND_PASS = "PASS"      # nosec B105 - a band label, not a credential; promoted to 3_final
BAND_REVIEW = "WARN"    # held in 2_preprocessed (three-band mode only)
BAND_FAIL = "FAIL"      # routed to manual_review
DEFAULT_BLOCK_ON_CRITICAL = False
DEFAULT_BLOCK_ON_WARNING = False

# ─── BANDING MODE ─────────────────────────────────────────────────────────────
# "two"   fidelity >= threshold -> PASS -> erwinmodels/3_final
#         fidelity <  threshold -> FAIL -> manual_review/
# "three" the previous behaviour: PASS / WARN (held in 2_preprocessed) /
#         FAIL (moved to 2_preprocessed/rejected), using REVIEW_FIDELITY_FLOOR.
BANDS_TWO = "two"
BANDS_THREE = "three"
DEFAULT_BANDS = BANDS_TWO
DEFAULT_MANUAL_REVIEW_DIR = "manual_review"

# Sub-folders created under the manual-review directory.
MANUAL_REVIEW_SUBDIRS = ("xml", "erwin", "reports")
_SUFFIX_SUBDIR = {".xml": "xml", ".erwin": "erwin", ".xlsx": "reports"}


def _settings():
    try:
        from app.config import settings
        return settings
    except Exception:                                      # noqa: BLE001
        return None


def _setting(name: str, default):
    settings = _settings()
    return getattr(settings, name, default) if settings is not None else default


def threshold() -> float:
    return float(_setting("PROMOTION_FIDELITY_THRESHOLD", DEFAULT_THRESHOLD))


def floor() -> float:
    return float(_setting("REVIEW_FIDELITY_FLOOR", DEFAULT_FLOOR))


def rejected_subdir() -> str:
    return str(_setting("REJECTED_SUBDIR", DEFAULT_REJECTED_SUBDIR) or DEFAULT_REJECTED_SUBDIR)


def blocks_on_critical() -> bool:
    """True when any CRITICAL finding must block promotion by itself."""
    return bool(_setting("PROMOTION_BLOCK_ON_CRITICAL", DEFAULT_BLOCK_ON_CRITICAL))


def blocks_on_warning() -> bool:
    """True when any WARNING finding must block promotion by itself."""
    return bool(_setting("PROMOTION_BLOCK_ON_WARNING", DEFAULT_BLOCK_ON_WARNING))


def bands_mode() -> str:
    """"two" (PASS / FAIL) or "three" (PASS / WARN / FAIL)."""
    mode = str(_setting("PROMOTION_BANDS", DEFAULT_BANDS)).strip().lower()
    return BANDS_THREE if mode.startswith("three") else BANDS_TWO


def manual_review_dir() -> str:
    """
    Absolute path of the folder a FAILing model is routed to, with its
    xml / erwin / reports sub-folders created.  Resolved against the project
    root when the configured value is relative.
    """
    configured = str(_setting("MANUAL_REVIEW_DIR", DEFAULT_MANUAL_REVIEW_DIR)
                     or DEFAULT_MANUAL_REVIEW_DIR)
    if os.path.isabs(configured):
        base = configured
    else:
        settings = _settings()
        root = getattr(settings, "BASE_DIR", None) if settings is not None else None
        base = os.path.join(str(root), configured) if root else os.path.abspath(configured)
    for name in MANUAL_REVIEW_SUBDIRS:
        os.makedirs(os.path.join(base, name), exist_ok=True)
    return base


def manual_review_path(path: str) -> str:
    """Destination inside manual_review for one artefact, chosen by extension."""
    subdir = _SUFFIX_SUBDIR.get(os.path.splitext(path)[1].lower(), "reports")
    return os.path.join(manual_review_dir(), subdir, os.path.basename(path))


def _destination_path(path: str, destination: str = "") -> str:
    """
    Where one artefact belongs once its model has failed the gate.

    With an explicit `destination` the file keeps its name and goes straight in
    -- that folder is already model-specific.  Without one, the configured
    manual-review directory is used and the file is filed by extension.
    """
    if destination:
        os.makedirs(destination, exist_ok=True)
        return os.path.join(destination, os.path.basename(path))
    return manual_review_path(path)


def band(result, fidelity_threshold: float = None) -> str:
    """PASS / WARN / FAIL band of a result by its gated fidelity score."""
    if result is None or getattr(result, "status", "") == "ERROR":
        return BAND_FAIL
    score = gated_score(result)
    limit = float(threshold() if fidelity_threshold is None else fidelity_threshold)
    if score >= limit and not gate_failures(result, fidelity_threshold):
        return BAND_PASS
    if bands_mode() == BANDS_TWO:
        return BAND_FAIL
    return BAND_FAIL if score < floor() else BAND_REVIEW


def _follow(result, source: str, target: str) -> None:
    """
    Repoint ``result.erwin_file`` at an artefact that has just been moved.

    The report generators re-read the erwin export to build their documentation
    sheets, so a routed model whose result still names the old staging path
    would silently lose that content.
    """
    current = getattr(result, "erwin_file", "") or ""
    if current and os.path.abspath(current) == os.path.abspath(source):
        result.erwin_file = target


def route(result, *paths, destination: str = "") -> list:
    """
    Send a FAILing model's artefacts to their destination.

    `destination` is the folder to send them to -- normally that model's own
    app/reporting/<tier>_reports/<model>/manual_review_report.  Without it the
    configured MANUAL_REVIEW_DIR is used, with xml / erwin / reports
    sub-folders.

    Three-band mode keeps the previous behaviour: reject() into the `rejected`
    sub-folder next to the file.

    Returns the new paths.  Never raises.
    """
    if bands_mode() == BANDS_THREE:
        return reject(result, *paths)

    moved = []
    for path in paths:
        try:
            if not path or not os.path.exists(path):
                continue
            target = _destination_path(path, destination)
            if os.path.abspath(target) == os.path.abspath(path):
                moved.append(target)
                continue
            if os.path.exists(target):
                os.remove(target)
            shutil.move(path, target)
            _follow(result, path, target)
            moved.append(target)
        except OSError as exc:
            logger.warning("Could not route %s to manual_review: %s", path, exc)
    return moved


def publish(result, *paths, destination: str = "") -> list:
    """
    Copy artefacts that belong with a routed model (typically its report) into
    the manual-review folder without removing them from where they were written.
    Returns the new paths.  Never raises.
    """
    copied = []
    for path in paths:
        try:
            if not path or not os.path.exists(path):
                continue
            target = _destination_path(path, destination)
            if os.path.abspath(target) != os.path.abspath(path):
                shutil.copy2(path, target)
            copied.append(target)
        except OSError as exc:
            logger.warning("Could not copy %s to manual_review: %s", path, exc)
    return copied


def reject(result, *paths) -> list:
    """
    Move a rejected model's staged files (XML / .erwin) into the `rejected`
    sub-folder next to them.  Returns the new paths.  Never raises.
    """
    moved = []
    for path in paths:
        try:
            if not path or not os.path.exists(path):
                continue
            target_dir = os.path.join(os.path.dirname(os.path.abspath(path)), rejected_subdir())
            os.makedirs(target_dir, exist_ok=True)
            target = os.path.join(target_dir, os.path.basename(path))
            if os.path.exists(target):
                os.remove(target)
            shutil.move(path, target)
            moved.append(target)
        except Exception as exc:                           # noqa: BLE001
            logger.warning("Could not move rejected file %s: %s", path, exc)
    return moved


def _basis() -> str:
    basis = str(_setting("PROMOTION_FIDELITY_BASIS", "overall")).lower()
    return "structural" if basis.startswith("struct") else "overall"


def gated_score(result) -> float:
    """The fidelity number the gate tests, per PROMOTION_FIDELITY_BASIS."""
    if _basis() == "structural":
        return float(getattr(result, "structural_fidelity_score",
                             getattr(result, "fidelity_score", 0.0)) or 0.0)
    return float(getattr(result, "fidelity_score", 0.0) or 0.0)


def gate_failures(result, fidelity_threshold: float = None) -> List[str]:
    """Every reason the model must NOT be promoted.  Empty list = promote."""
    if result is None:
        return ["no validation result"]
    if getattr(result, "status", "") == "ERROR":
        return ["validation ended in ERROR"]

    limit = float(threshold() if fidelity_threshold is None else fidelity_threshold)
    failures: List[str] = []

    score = gated_score(result)
    if score < limit:
        failures.append(f"{_basis()} fidelity {score:.2f}% is below the {limit:.2f}% threshold")

    if _setting("PROMOTION_BLOCK_ON_CRITICAL", DEFAULT_BLOCK_ON_CRITICAL):
        critical = int(getattr(result, "critical_count", 0) or 0)
        if critical:
            failures.append(f"{critical} CRITICAL finding(s)")

    if _setting("PROMOTION_BLOCK_ON_WARNING", DEFAULT_BLOCK_ON_WARNING):
        warning = int(getattr(result, "warning_count", 0) or 0)
        if warning:
            failures.append(f"{warning} WARNING finding(s) awaiting human sign-off")

    errors = getattr(result, "reconciliation_errors", None)
    if callable(errors):
        broken = errors()
        if broken:
            failures.append("reported counts do not reconcile (" + "; ".join(broken) + ")")

    return failures


def _fail_note(result, limit: float, failures: List[str]) -> str:
    """The promotion_note for a model that must not be promoted."""
    score = gated_score(result)
    if bands_mode() == BANDS_TWO:
        return (f"FAIL: {_basis()} fidelity {score:.2f}% is below the "
                f"{limit:.2f}% threshold; routed to "
                f"{_setting('MANUAL_REVIEW_DIR', DEFAULT_MANUAL_REVIEW_DIR)}. "
                f"Blocked by: {'; '.join(failures)}")
    return (f"REJECTED: {_basis()} fidelity {score:.2f}% is below the "
            f"{floor():.2f}% review floor; removed from 2_preprocessed")


def apply(result, fidelity_threshold: float = None) -> bool:
    """
    Evaluate the gate and, when it passes, mark the result PASS.

    Returns True when the model should be promoted to 3_final.  Never raises.
    """
    try:
        failures = gate_failures(result, fidelity_threshold)
        limit = float(threshold() if fidelity_threshold is None else fidelity_threshold)
        if failures:
            outcome = band(result, fidelity_threshold)
            if outcome == BAND_FAIL and getattr(result, "status", "") != "ERROR":
                result.status = BAND_FAIL
                result.promotion_note = _fail_note(result, limit, failures)
            else:
                if getattr(result, "status", "") not in ("ERROR",):
                    result.status = BAND_REVIEW
                result.promotion_note = ("HELD in 2_preprocessed for review: "
                                         + "; ".join(failures))
            return False
        previous = getattr(result, "status", "")
        result.status = "PASS"
        score = gated_score(result)
        note = f"PASS: {_basis()} fidelity {score:.2f}% >= {limit:.2f}% threshold"
        if previous and previous != "PASS":
            note += (f" (structural status was {previous}: "
                     f"{int(getattr(result, 'critical_count', 0) or 0)} CRITICAL, "
                     f"{int(getattr(result, 'warning_count', 0) or 0)} WARNING finding(s) "
                     f"remain listed on the FINDINGS sheet)")
        result.promotion_note = note
        logger.info("%s: %s", getattr(result, "pd_model", "?"), note)
        return True
    except Exception as exc:                               # noqa: BLE001
        logger.warning("Promotion gate failed for %s: %s", getattr(result, "pd_file", "?"), exc)
        return False


__all__ = ["threshold", "floor", "band", "blocks_on_critical", "blocks_on_warning", "reject", "route", "publish",
           "bands_mode", "manual_review_dir", "manual_review_path",
           "gated_score", "gate_failures", "apply",
           "BAND_PASS", "BAND_REVIEW", "BAND_FAIL", "BANDS_TWO", "BANDS_THREE"]
