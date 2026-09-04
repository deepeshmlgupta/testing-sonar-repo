"""
Tests for Phase D (app/validation/udp_flow.py, app/validation/udp_bridge.py)
and the V3 consolidated report (app/reporting/v3_report.py).

The point of these tests is the contract, not erwin: Phase D must never raise,
must record WHY it skipped work rather than failing silently, and must produce
a V3 workbook whose UDP sheets keep working formulas after being renamed.
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.reporting import v3_report                      # noqa: E402
from app.validation import udp_bridge, udp_flow          # noqa: E402


PD_CDM = """<?xml version="1.0" encoding="UTF-8"?>
<?PowerDesigner AppLocale="UTF16" ID="{1F5B9C98-0000-0000-0000-000000000001}" Name="Demo" \
Objects="3" Symbols="0" Target="" signature="CDM_DATA_MODEL_XML" version="16.7.6.7257"?>
<Model xmlns:a="attribute" xmlns:c="collection" xmlns:o="object">
<o:RootObject Id="o1"><c:Children>
<o:Model Id="o2"><a:Name>Demo</a:Name><a:Code>DEMO</a:Code>
<c:Entities>
<o:Entity Id="o3"><a:ObjectID>E1</a:ObjectID><a:Name>Customer</a:Name><a:Code>CUSTOMER</a:Code>
<a:ExtendedAttributesText>{AAAA0000-0000-0000-0000-000000000001},Ext,40=\
{AAAA0000-0000-0000-0000-000000000003},Design_Status,5=Draft
</a:ExtendedAttributesText>
</o:Entity>
<o:Entity Ref="o3"/>
</c:Entities>
</o:Model>
</c:Children></o:RootObject>
</Model>
"""


@pytest.fixture
def pd_model(tmp_path):
    path = tmp_path / "Demo.cdm"
    path.write_text(PD_CDM, encoding="utf-8")
    return path


def make_request(pd_path, tmp_path, **overrides):
    fields = dict(
        pd_path=str(pd_path),
        model_name="Demo",
        model_type="CDM",
        reports_dir=str(tmp_path / "reports"),
        workdir=str(tmp_path / "work"),
    )
    fields.update(overrides)
    return udp_flow.UdpFlowRequest(**fields)


# ─── the bridge ───────────────────────────────────────────────────────────────

def test_bridge_imports_the_tool_without_polluting_sys_modules():
    if not udp_bridge.available():
        pytest.skip("UDP tool folder not present")
    before = set(sys.modules)
    udp_bridge.load()
    leaked = {name for name in set(sys.modules) - before
              if name in ("pd_extract", "erwin_prepare", "sow_classify",
                          "udp_readback", "udp_compare", "udp_report")}
    assert not leaked, f"bare tool names left in sys.modules: {leaked}"


def test_bridge_exposes_the_tool_under_its_alias():
    if not udp_bridge.available():
        pytest.skip("UDP tool folder not present")
    assert "udp_tool.pd_extract" in sys.modules


# ─── the flow ─────────────────────────────────────────────────────────────────

def test_disabled_phase_is_skipped_not_failed(pd_model, tmp_path):
    config = SimpleNamespace(UDP_TOOL_ENABLED=False)
    outcome = udp_flow.run_udp_flow(make_request(pd_model, tmp_path), config)
    assert outcome.stage == udp_flow.STAGE_SKIPPED
    assert not outcome.error


def test_missing_model_is_reported_not_raised(tmp_path):
    outcome = udp_flow.run_udp_flow(make_request(tmp_path / "absent.cdm", tmp_path))
    assert outcome.stage == udp_flow.STAGE_FAILED
    assert "not found" in outcome.error


def test_mapping_writes_per_model_artefacts(pd_model, tmp_path):
    if not udp_bridge.available():
        pytest.skip("UDP tool folder not present")
    config = SimpleNamespace(UDP_TOOL_INJECT_ENABLED=False,
                             UDP_TOOL_ERWIN_READBACK_DIRS=())
    outcome = udp_flow.run_udp_flow(make_request(pd_model, tmp_path), config)

    assert outcome.stage in (udp_flow.STAGE_EXTRACTED, udp_flow.STAGE_COMPARED)
    assert os.path.isfile(outcome.schema_path)
    assert os.path.isfile(outcome.manifest_path)
    # Per-model working directory: the baseline must NOT be shared.
    assert os.path.isdir(os.path.join(str(tmp_path / "work"), udp_bridge.BASELINE_DIRNAME))


def test_ref_pointer_entities_are_not_counted(pd_model, tmp_path):
    """The .cdm above holds one real entity and one <o:Entity Ref=...> pointer."""
    if not udp_bridge.available():
        pytest.skip("UDP tool folder not present")
    workdir = str(tmp_path / "work")
    udp_bridge.classify(str(pd_model), workdir)
    _, manifest = udp_bridge.build_mapping(workdir, "Demo", "46603045")
    assert {row["entity_name"] for row in manifest} == {"Customer"}


def test_missing_erwin_binary_is_explained(pd_model, tmp_path):
    if not udp_bridge.available():
        pytest.skip("UDP tool folder not present")
    config = SimpleNamespace(UDP_TOOL_ERWIN_INPUT_DIRS=(),
                             UDP_TOOL_ERWIN_READBACK_DIRS=())
    outcome = udp_flow.run_udp_flow(make_request(pd_model, tmp_path), config)
    assert outcome.pass_rate is None
    assert any("erwin" in message for message in outcome.messages)


# ─── the V3 report ────────────────────────────────────────────────────────────

def test_formula_rewrite_follows_renamed_sheets():
    formula = "=COUNTIF('Value Reconciliation'!$C:$C,$A5)+COUNTIF(Exceptions!$A:$A,\"High\")"
    from app.reporting import report_layout as layout
    rewritten = layout.rewrite_formula(formula, v3_report.MIGRATION_SHEETS)
    assert "UDP_VALUE_RECON!$C:$C" in rewritten
    assert "UDP_EXCEPTIONS!$A:$A" in rewritten
    assert "Value Reconciliation" not in rewritten


def test_renamed_sheets_cannot_collide_with_tier_sheets():
    tier = {"SUMMARY", "DASHBOARD", "FINDINGS", "UDP_FIDELITY", "UDP_DETAIL", "CONFIG"}
    renamed = set(v3_report.MIGRATION_SHEETS.values()) | set(v3_report.COMPARISON_SHEETS.values())
    assert not {name.upper() for name in renamed} & tier
    assert all(len(name) <= 31 for name in renamed)


def test_missing_tier_report_returns_none(tmp_path):
    request = v3_report.V3ReportRequest(
        result=SimpleNamespace(pd_file="x.cdm", fidelity_score=50.0, status="FAIL"),
        tier_report_path=str(tmp_path / "absent.xlsx"), outdir=str(tmp_path))
    assert v3_report.build_v3_report(request) is None


def test_v3_workbook_carries_the_overview_sheet(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    tier_path = tmp_path / "tier.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.title = "SUMMARY"
    workbook.save(str(tier_path))

    result = SimpleNamespace(pd_file="Demo.cdm", pd_model="Demo", erwin_file="Demo.xml",
                             fidelity_score=73.79, structural_fidelity_score=92.24,
                             udp_fidelity_score=0.0, udp_fidelity_weight=0.2,
                             critical_count=1, warning_count=2, info_count=3,
                             status="FAIL", promotion_note="below threshold")
    path = v3_report.build_v3_report(v3_report.V3ReportRequest(
        result=result, tier_report_path=str(tier_path), outdir=str(tmp_path),
        model_name="Demo", model_type="CDM", status="FAIL",
        destination="manual_review_report",
        udp_outcome=udp_flow.UdpStageOutcome(model_name="Demo", model_type="CDM")))

    assert path and os.path.isfile(path)
    assert path.endswith("Demo_V3_Final_Fidelity_Report.xlsx")
    saved = openpyxl.load_workbook(path)
    try:
        assert saved.sheetnames[0] == v3_report.SHEET_OVERVIEW
        assert "SUMMARY" in saved.sheetnames
    finally:
        saved.close()
