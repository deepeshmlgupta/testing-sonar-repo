"""
Tests for the V1 -> V2 -> V3 fidelity progression
(app/validation/fidelity_stages.py) and the two V3 report rules that go with it:
the report lives in the tier folder, and it must not carry UDP_DETAIL.

The point of these tests is that each stage is MEASURED, not carried forward:
a stage whose documentation or UDP component changed must produce a different
overall score even when the structural number is identical.
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.reporting import report_layout as layout        # noqa: E402
from app.reporting import v3_report                      # noqa: E402
from app.validation import fidelity_stages as fs         # noqa: E402


def doc_row(status):
    return SimpleNamespace(status=status)


def make_result(structural=92.24, doc_statuses=(), udp_total=0, udp_score=None):
    return SimpleNamespace(
        pd_file="m.cdm", pd_model="M", erwin_file="m.xml", status="WARN",
        structural_fidelity_score=structural, fidelity_score=structural,
        documentation_rows=[doc_row(s) for s in doc_statuses],
        udp_total=udp_total, udp_fidelity_score=udp_score,
        critical_count=0, warning_count=0, info_count=0,
    )


WEIGHTS = SimpleNamespace(FIDELITY_STAGE_WEIGHTS={
    "structural": 0.50, "documentation": 0.25, "udp": 0.25})


# ─── component measurement ────────────────────────────────────────────────────

def test_documentation_ignores_objects_nobody_documented():
    """BOTH_EMPTY rows are excluded: an undocumented object is not a loss."""
    result = make_result(doc_statuses=["MATCHED", "MISSING_IN_ERWIN"] + ["BOTH_EMPTY"] * 500)
    assert fs.documentation_fidelity(result) == 50.0


def test_documentation_is_none_when_nothing_is_documented():
    assert fs.documentation_fidelity(make_result(doc_statuses=["BOTH_EMPTY"] * 10)) is None
    assert fs.documentation_fidelity(make_result()) is None


def test_udp_component_is_none_when_the_model_has_no_udps():
    assert fs.udp_fidelity_component(make_result(udp_total=0), None) is None


def test_udp_override_replaces_the_xml_measurement():
    """At V3 the UDP number comes from the enriched .erwin, not the XML."""
    result = make_result(udp_total=219, udp_score=0.0)
    assert fs.udp_fidelity_component(result, None) == 0.0
    assert fs.udp_fidelity_component(result, 100.0) == 100.0


# ─── the progression ──────────────────────────────────────────────────────────

def test_v1_v2_v3_are_each_recomputed_not_carried_forward():
    """
    Identical structural score at every stage; documentation and UDP move.
    The overall score must move with them.
    """
    v1 = fs.score(make_result(doc_statuses=["MATCHED"] + ["MISSING_IN_ERWIN"] * 16,
                              udp_total=219, udp_score=0.0), fs.STAGE_V1, config=WEIGHTS)
    v2 = fs.score(make_result(doc_statuses=["MATCHED"] * 17,
                              udp_total=219, udp_score=0.0), fs.STAGE_V2, config=WEIGHTS)
    v3 = fs.score(make_result(doc_statuses=["MATCHED"] * 17,
                              udp_total=219, udp_score=0.0), fs.STAGE_V3,
                  udp_override=100.0, config=WEIGHTS)

    assert v1.overall < 60.0, "V1 must reflect the un-migrated metadata"
    assert v2.overall > v1.overall
    assert v3.overall > v2.overall
    assert v3.overall >= 90.0, "V3 must reflect the enriched model"
    # structural was identical throughout, so the movement came from elsewhere
    assert v1.component("structural") == v3.component("structural")


def test_unmeasurable_components_are_dropped_not_zeroed():
    """A model with no UDPs must not be punished for having none."""
    scored = fs.score(make_result(structural=98.45, doc_statuses=["MATCHED"] * 22),
                      fs.STAGE_V2, config=WEIGHTS)
    assert scored.component("udp") is None
    assert "udp" not in scored.weights
    # 98.45 * (0.50/0.75) + 100 * (0.25/0.75)
    assert scored.overall == pytest.approx(98.97, abs=0.01)


def test_shortcuts_and_tags_are_reported_as_not_measured():
    scored = fs.score(make_result(), fs.STAGE_V1, config=WEIGHTS)
    assert scored.component(fs.COMPONENT_SHORTCUTS) is None
    assert scored.component(fs.COMPONENT_TAGS) is None


def test_apply_records_history_and_drives_the_gated_score():
    result = make_result(doc_statuses=["MATCHED"] * 4, udp_total=10, udp_score=0.0)
    fs.apply(result, fs.STAGE_V1, model_type="CDM", config=WEIGHTS)
    v1 = result.fidelity_score
    fs.apply(result, fs.STAGE_V3, udp_override=100.0, model_type="CDM", config=WEIGHTS)

    assert result.fidelity_stage == fs.STAGE_V3
    assert result.fidelity_score > v1
    assert set(fs.history(result)) == {fs.STAGE_V1, fs.STAGE_V3}
    assert fs.stage_value(result, fs.STAGE_V1) == v1
    # the comparator's own number is never overwritten
    assert result.structural_fidelity_score == 92.24


def test_error_results_are_left_alone():
    result = make_result()
    result.status = "ERROR"
    result.fidelity_score = 0.0
    fs.apply(result, fs.STAGE_V3, udp_override=100.0, config=WEIGHTS)
    assert result.fidelity_score == 0.0


def test_scoring_never_raises_on_a_broken_result():
    scored = fs.score(object(), fs.STAGE_V1, config=WEIGHTS)
    assert scored.overall == 0.0 and scored.notes


# ─── V3 report rules ──────────────────────────────────────────────────────────

def test_udp_detail_is_dropped_from_the_consolidated_report(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    tier_path = tmp_path / "tier.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.title = "SUMMARY"
    for name in ("UDP_FIDELITY", "UDP_DETAIL", "CONFIG"):
        workbook.create_sheet(name)
    workbook.save(str(tier_path))

    result = make_result()
    result.status = "PASS"
    fs.apply(result, fs.STAGE_V3, udp_override=100.0, config=WEIGHTS)
    path = v3_report.build_v3_report(v3_report.V3ReportRequest(
        result=result, tier_report_path=str(tier_path), outdir=str(tmp_path),
        model_name="M", model_type="CDM", status="PASS", destination="3_final"))

    assert path
    saved = openpyxl.load_workbook(path)
    try:
        assert "UDP_DETAIL" not in saved.sheetnames, "the full detail tab must not be carried over"
        assert "UDP_FIDELITY" in saved.sheetnames, "the UDP summary must stay"
        assert saved.sheetnames[0] == v3_report.SHEET_OVERVIEW
    finally:
        saved.close()


def test_exclusion_is_case_insensitive():
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    workbook.active.title = "Udp_Detail"
    assert layout.drop_sheets(workbook, ("UDP_DETAIL",)) == ["Udp_Detail"]


def test_no_v3_folder_constant_exists():
    """Reports go into per-model folders; there must be no V3 output folder."""
    import app.main as pipeline
    assert not hasattr(pipeline, "V3_REPORTS_DIR")
    assert "v3_report" not in str(pipeline.UDP_REPORTS_DIR)
    # The tier folders come from report_layout, not from a constant in main.
    assert set(layout.TIER_FOLDERS) == {"CDM", "LDM", "PDM"}
    for folder in layout.TIER_FOLDERS.values():
        assert folder.endswith("_reports")


# ─── per-model report layout ──────────────────────────────────────────────────

def test_each_model_gets_its_own_folder_under_its_tier(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "REPORTING_ROOT", tmp_path)
    folder = layout.model_dir("CDM", "02_logistics_segment")
    assert folder == tmp_path / "cdm_reports" / "02_logistics_segment"
    assert folder.is_dir()


def test_manual_review_lives_inside_the_model_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "REPORTING_ROOT", tmp_path)
    review = layout.manual_review_dir("PDM", "05_Carbon_Calculator")
    assert review == (tmp_path / "pdm_reports" / "05_Carbon_Calculator"
                      / "manual_review_report")
    assert review.is_dir()


def test_unknown_model_type_is_rejected():
    with pytest.raises(ValueError):
        layout.tier_dir("XDM")


@pytest.mark.parametrize("suffix,expected", [
    (layout.V1_SUFFIX, "M_V1_Initial_Fidelity_Report.xlsx"),
    (layout.V2_SUFFIX, "M_V2_UDP_Mapping_Report.xlsx"),
    (layout.V3_SUFFIX, "M_V3_Final_Fidelity_Report.xlsx"),
])
def test_the_three_reports_are_named_by_stage(suffix, expected):
    assert layout.report_name("M", suffix) == expected


def test_gate_routes_artefacts_into_an_explicit_destination(tmp_path):
    from app.validation import promotion_gate as gate
    model = tmp_path / "m.xml"
    model.write_text("<erwin/>", encoding="utf-8")
    review = tmp_path / "cdm_reports" / "m" / "manual_review_report"

    moved = gate.route(make_result(), str(model), destination=str(review))

    assert moved == [str(review / "m.xml")]
    assert (review / "m.xml").is_file() and not model.exists()


def test_v2_mapping_report_needs_udp_workbooks(tmp_path):
    from app.reporting import v2_report
    path = v2_report.build_v2_report(v2_report.V2ReportRequest(
        model_name="M", model_type="CDM", outdir=str(tmp_path),
        result=make_result(), udp_outcome=None))
    assert path is None, "no UDP workbooks means no mapping report"
