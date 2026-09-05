"""Tests for app/validation/promotion_gate.py (fidelity >= threshold -> PASS + 3_final)."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.validation import promotion_gate as pg  # noqa: E402


def _result(**kw):
    base = {"pd_file": "m.cdm", "pd_model": "M", "status": "WARN", "fidelity_score": 95.0,
            "structural_fidelity_score": 97.0, "critical_count": 1, "warning_count": 3}
    base.update(kw)
    return SimpleNamespace(**base)


def test_passes_at_threshold_and_marks_status(monkeypatch):
    monkeypatch.setattr(pg, "_setting", lambda n, d: {"PROMOTION_FIDELITY_THRESHOLD": 90.0}.get(n, d))
    r = _result(fidelity_score=90.0)
    assert pg.apply(r) is True
    assert r.status == "PASS" and r.promotion_note.startswith("PASS")
    assert "1 CRITICAL, 3 WARNING" in r.promotion_note      # findings stay visible


def test_blocked_below_threshold_fails_to_manual_review(monkeypatch):
    """Default banding is two: anything under the threshold is FAIL."""
    monkeypatch.setattr(pg, "_setting", lambda n, d: {"PROMOTION_FIDELITY_THRESHOLD": 90.0}.get(n, d))
    r = _result(fidelity_score=89.99)
    assert pg.apply(r) is False
    assert r.status == pg.BAND_FAIL
    assert "below the 90.00%" in r.promotion_note and "manual_review" in r.promotion_note


def test_three_band_mode_still_holds_for_review(monkeypatch):
    """PROMOTION_BANDS = "three" restores the previous WARN band exactly."""
    values = {"PROMOTION_FIDELITY_THRESHOLD": 90.0, "PROMOTION_BANDS": "three",
              "REVIEW_FIDELITY_FLOOR": 60.0}
    monkeypatch.setattr(pg, "_setting", lambda n, d: values.get(n, d))
    r = _result(fidelity_score=89.99)
    assert pg.apply(r) is False
    assert r.status == pg.BAND_REVIEW and "HELD in 2_preprocessed" in r.promotion_note


def test_error_and_reconciliation_always_block(monkeypatch):
    monkeypatch.setattr(pg, "_setting", lambda n, d: d)
    assert pg.apply(_result(status="ERROR", fidelity_score=100.0)) is False
    broken = _result(fidelity_score=100.0, reconciliation_errors=lambda: ["Tables: 3 vs 2"])
    assert pg.apply(broken) is False and "do not reconcile" in broken.promotion_note


def test_structural_basis_and_strict_mode(monkeypatch):
    settings = {"PROMOTION_FIDELITY_THRESHOLD": 90.0, "PROMOTION_FIDELITY_BASIS": "structural"}
    monkeypatch.setattr(pg, "_setting", lambda n, d: settings.get(n, d))
    r = _result(fidelity_score=73.0, structural_fidelity_score=92.0)
    assert pg.gated_score(r) == 92.0 and pg.apply(r) is True

    settings.update({"PROMOTION_FIDELITY_THRESHOLD": 100.0, "PROMOTION_BLOCK_ON_CRITICAL": True,
                     "PROMOTION_BLOCK_ON_WARNING": True})
    r = _result(fidelity_score=100.0, structural_fidelity_score=100.0, critical_count=0, warning_count=2)
    failures = pg.gate_failures(r)
    assert failures == ["2 WARNING finding(s) awaiting human sign-off"]
    r.warning_count = 0
    assert pg.apply(r) is True


def test_explicit_threshold_argument_wins(monkeypatch):
    monkeypatch.setattr(pg, "_setting", lambda n, d: d)
    r = _result(fidelity_score=95.0)
    assert pg.apply(r, 99.0) is False
    assert pg.apply(r, 95.0) is True
