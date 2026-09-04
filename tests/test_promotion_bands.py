"""
Tests for the two-band promotion gate (app/validation/promotion_gate.py).

    fidelity >= PROMOTION_FIDELITY_THRESHOLD  ->  PASS  ->  erwinmodels/3_final
    fidelity <  PROMOTION_FIDELITY_THRESHOLD  ->  FAIL  ->  manual_review/

PROMOTION_BANDS = "three" must still restore the previous PASS / WARN / FAIL
behaviour exactly, so both modes are exercised here.
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings                          # noqa: E402
from app.validation import promotion_gate as gate        # noqa: E402


def make_result(score, status="WARN", critical=0, warning=0):
    return SimpleNamespace(
        pd_file="model.cdm", pd_model="model", status=status,
        fidelity_score=score, structural_fidelity_score=score,
        critical_count=critical, warning_count=warning, promotion_note="",
    )


@pytest.fixture
def two_band(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PROMOTION_BANDS", "two", raising=False)
    monkeypatch.setattr(settings, "MANUAL_REVIEW_DIR", str(tmp_path / "manual_review"),
                        raising=False)
    return tmp_path / "manual_review"


@pytest.fixture
def three_band(monkeypatch):
    monkeypatch.setattr(settings, "PROMOTION_BANDS", "three", raising=False)


# ─── banding ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("score,expected", [
    (100.0, gate.BAND_PASS),
    (90.0, gate.BAND_PASS),
    (89.99, gate.BAND_FAIL),
    (73.79, gate.BAND_FAIL),      # the CDM sample
    (59.9, gate.BAND_FAIL),
    (0.0, gate.BAND_FAIL),
])
def test_two_band_has_no_warn_band(two_band, score, expected):
    assert gate.band(make_result(score)) == expected


@pytest.mark.parametrize("score,expected", [
    (90.0, gate.BAND_PASS),
    (73.79, gate.BAND_REVIEW),    # above the 60% floor -> held for review
    (59.9, gate.BAND_FAIL),
])
def test_three_band_mode_is_unchanged(three_band, score, expected):
    assert gate.band(make_result(score)) == expected


def test_error_results_are_never_promoted(two_band):
    result = make_result(100.0, status="ERROR")
    assert gate.band(result) == gate.BAND_FAIL
    assert gate.apply(result) is False
    assert result.status == "ERROR"


# ─── apply() ──────────────────────────────────────────────────────────────────

def test_pass_sets_status_and_note(two_band):
    result = make_result(98.45)
    assert gate.apply(result) is True
    assert result.status == "PASS"
    assert "98.45" in result.promotion_note


def test_fail_note_names_manual_review(two_band):
    result = make_result(73.79)
    assert gate.apply(result) is False
    assert result.status == gate.BAND_FAIL
    assert "manual_review" in result.promotion_note


def test_findings_are_not_removed_on_pass(two_band):
    result = make_result(95.0, warning=12)
    assert gate.apply(result) is True
    assert result.warning_count == 12


# ─── routing ──────────────────────────────────────────────────────────────────

def test_route_moves_artefacts_by_extension(two_band, tmp_path):
    xml = tmp_path / "model.xml"
    xml.write_text("<erwin/>", encoding="utf-8")
    binary = tmp_path / "model.erwin"
    binary.write_text("binary", encoding="utf-8")

    moved = gate.route(make_result(50.0), str(xml), str(binary))

    assert len(moved) == 2
    assert (two_band / "xml" / "model.xml").is_file()
    assert (two_band / "erwin" / "model.erwin").is_file()
    assert not xml.exists()


def test_route_is_silent_about_missing_files(two_band, tmp_path):
    assert gate.route(make_result(50.0), str(tmp_path / "absent.xml")) == []


def test_publish_copies_without_removing(two_band, tmp_path):
    report = tmp_path / "model_V3_73.8%_FAIL.xlsx"
    report.write_text("workbook", encoding="utf-8")

    copied = gate.publish(make_result(73.8), str(report))

    assert len(copied) == 1
    assert report.is_file()                                   # original kept
    assert (two_band / "reports" / report.name).is_file()


def test_manual_review_dir_creates_subfolders(two_band):
    gate.manual_review_dir()
    for name in gate.MANUAL_REVIEW_SUBDIRS:
        assert (two_band / name).is_dir()
