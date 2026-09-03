import argparse
import copy
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

HERE = os.path.dirname(os.path.abspath(__file__))
config.PD_MODELS_DIR    = os.path.join(HERE, "models", "pd_ldm")
config.ERWIN_MODELS_DIR = os.path.join(HERE, "models", "erwin_ldm")
config.OUTPUT_DIR       = os.path.join(HERE, "test_output")
config.REPORT_FILENAME  = "ldm_validation_report.xlsx"

from comparator import compare                        # noqa: E402
from erwin_ldm_parser import parse_erwin_ldm           # noqa: E402
from pd_ldm_parser import parse_ldm                    # noqa: E402
from report_generator import generate_report           # noqa: E402

PD_FILE    = os.path.join(config.PD_MODELS_DIR,    "SD_O2C_LDM_WC.ldm")
ERWIN_FILE = os.path.join(config.ERWIN_MODELS_DIR, "SD_O2C_LDM_WC.xml")

SEV_MARK = {"CRITICAL": "[CRIT]", "WARNING": "[WARN]", "INFO": "[INFO]"}


# ─── ASSERTION HARNESS ────────────────────────────────────────────────────────

class Checks:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list = []

    def expect(self, condition: bool, description: str) -> None:
        if condition:
            self.passed += 1
            print(f"    PASS  {description}")
        else:
            self.failed.append(description)
            print(f"    FAIL  {description}")

    def report(self) -> int:
        total = self.passed + len(self.failed)
        print(f"\n  {self.passed}/{total} assertions passed.")
        if self.failed:
            print("\n  Failed assertions:")
            for description in self.failed:
                print(f"    - {description}")
            return 1
        return 0


def _has(result, category: str, *needles: str) -> bool:
    for finding in result.findings:
        if finding.category != category:
            continue
        haystack = " ".join([finding.object_name, finding.member, finding.message,
                             finding.pd_value, finding.erwin_value]).upper()
        if all(needle.upper() in haystack for needle in needles):
            return True
    return False


def _count(result, category: str) -> int:
    return sum(1 for f in result.findings if f.category == category)


# ─── SYNTHETIC REGRESSION: erwin hidden role-merged duplicate attribute ───────
#
# Found in production (03_Lubes_Direct_Sales_Marketing_Portfolio_LDM.xml,
# entity DELIVER_OWN_ACCOUNT): a key attribute that reaches one entity via two
# migration paths gets ONE visible <Attribute> (the "lead", self-referencing
# via <Logical_Lead_Attribute_Ref>) and one <Hide_In_Logical>true</...> copy
# erwin's own GUI never shows. Before the fix, both were read as real
# attributes, colliding on the same code and leaving the PD side unmatched —
# a false "exists in SAP PD but NOT in erwin" CRITICAL finding.
#
# This uses a minimal SYNTHETIC snippet, not the real production file, so the
# regression test ships with the framework without depending on any
# proprietary model data.

_HIDDEN_DUPLICATE_ERWIN_XML = """<?xml version="1.0"?>
<Repository>
  <Entity id="{E0000000-0000-0000-0000-000000000001}" name="CUSTOMER">
    <EntityProps>
      <Name>CUSTOMER</Name>
      <Attribute_Groups>
        <Attribute id="{E0000000-0000-0000-0000-000000000010}" name="Customer_ID">
          <AttributeProps>
            <Name>Customer_ID</Name>
            <Physical_Name>CUSTOMER_ID</Physical_Name>
            <Physical_Data_Type>CHAR(18)</Physical_Data_Type>
            <Null_Option_Type>1</Null_Option_Type>
          </AttributeProps>
        </Attribute>
      </Attribute_Groups>
    </EntityProps>
  </Entity>
  <Entity id="{E0000000-0000-0000-0000-000000000002}" name="TARGET_ENTITY">
    <EntityProps>
      <Name>TARGET_ENTITY</Name>
      <Attribute_Groups>
        <Attribute id="{E0000000-0000-0000-0000-000000000020}" name="Customer_ID">
          <AttributeProps>
            <Name>Customer_ID</Name>
            <Physical_Name>CUSTOMER_ID</Physical_Name>
            <Physical_Data_Type>CHAR(18)</Physical_Data_Type>
            <Null_Option_Type>0</Null_Option_Type>
            <Parent_Attribute_Ref>{E0000000-0000-0000-0000-000000000010}</Parent_Attribute_Ref>
            <Hide_In_Logical>false</Hide_In_Logical>
            <Logical_Lead_Attribute_Ref>{E0000000-0000-0000-0000-000000000020}</Logical_Lead_Attribute_Ref>
          </AttributeProps>
        </Attribute>
        <Attribute id="{E0000000-0000-0000-0000-000000000021}" name="Customer_ID">
          <AttributeProps>
            <Name>Customer_ID</Name>
            <Physical_Name>CUSTOMER_ID</Physical_Name>
            <Physical_Data_Type>INTEGER</Physical_Data_Type>
            <Null_Option_Type>1</Null_Option_Type>
            <Parent_Attribute_Ref>{E0000000-0000-0000-0000-000000000011}</Parent_Attribute_Ref>
            <Hide_In_Logical>true</Hide_In_Logical>
            <Logical_Lead_Attribute_Ref>{E0000000-0000-0000-0000-000000000020}</Logical_Lead_Attribute_Ref>
          </AttributeProps>
        </Attribute>
      </Attribute_Groups>
    </EntityProps>
  </Entity>
</Repository>
"""


def _test_hidden_logical_duplicate_attribute(checks: "Checks") -> None:
    from erwin_ldm_parser import parse_erwin_ldm

    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as tmp:
        tmp.write(_HIDDEN_DUPLICATE_ERWIN_XML)
        tmp_path = tmp.name
    try:
        model = parse_erwin_ldm(tmp_path)
        entity = model.entities.get("TARGET_ENTITY")
        checks.expect(entity is not None, "Synthetic TARGET_ENTITY parses")
        if entity is not None:
            checks.expect(
                len(entity.attributes) == 1,
                "Hidden role-merged duplicate is excluded -- exactly 1 "
                "Customer_ID attribute survives, not 2",
            )
            if entity.attributes:
                checks.expect(
                    entity.attributes[0].data_type == "CHAR(18)",
                    "The surviving attribute is the LEAD copy (CHAR(18)), "
                    "not the hidden INTEGER duplicate",
                )
    finally:
        os.unlink(tmp_path)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.quiet:
        global print
        _real_print = print
        def _quiet_print(*a, **k):
            pass
        print = _quiet_print  # noqa: A001

    checks = Checks()

    print("=" * 78)
    print("  LDM FRAMEWORK REGRESSION TEST")
    print("=" * 78)

    print("\n[1] Parsing the real SD_O2C_LDM_WC pair ...")
    pd_model = parse_ldm(PD_FILE)
    erwin_model = parse_erwin_ldm(ERWIN_FILE)

    checks.expect(not pd_model.parse_error, "PD LDM parses without error")
    checks.expect(not erwin_model.parse_error, "erwin LDM parses without error")
    checks.expect(len(pd_model.entities) == 9, "PD LDM has 9 entities")
    checks.expect(len(erwin_model.entities) == 9, "erwin LDM has 9 entities")
    checks.expect(pd_model.attribute_count == 84, "PD LDM has 84 attributes")
    checks.expect(erwin_model.attribute_count == 84, "erwin LDM has 84 attributes")
    checks.expect(pd_model.identifier_count == 9, "PD LDM has 9 identifiers (one PK per entity)")
    checks.expect(erwin_model.identifier_count == 9,
                  "erwin LDM has 9 identifiers after IFn key groups are dropped")
    checks.expect(len(pd_model.relationships) == 10, "PD LDM has 10 relationships")
    checks.expect(len(erwin_model.relationships) == 10, "erwin LDM has 10 relationships")

    identifying_pd = {r.name for r in pd_model.relationships if r.identifying}
    identifying_erwin = {r.name for r in erwin_model.relationships if r.identifying}
    checks.expect(identifying_pd == {"Relationship_4", "Relationship_6", "Relationship_8"},
                  "PD LDM identifying relationships are exactly 4, 6, 8")
    checks.expect(identifying_erwin == identifying_pd,
                  "erwin identifying relationships agree with PD LDM (verified against erwin Type=2)")

    print("\n[2] Reconciling the real pair (expected: clean migration) ...")
    result = compare(pd_model, erwin_model)
    checks.expect(result.status == "PASS", "Real pair reconciles to PASS")
    checks.expect(result.critical_count == 0, "Real pair raises zero CRITICAL findings")
    checks.expect(result.fidelity_score == 100.0, "Real pair scores 100.0% fidelity")
    checks.expect(result.entities_matched == 9, "All 9 entities matched")
    checks.expect(result.attributes_matched == 84, "All 84 attributes matched")
    checks.expect(result.relationships_matched == 10, "All 10 relationships matched")
    checks.expect(not _has(result, "DOMAIN"),
                  "No false DOMAIN findings from erwin's built-in system domains")

    print("\n[3] Proving detection is real: mutating parsed models and re-comparing ...")
    mutated = copy.deepcopy(erwin_model)

    mutated.entities["KNA1"].attribute_by_code("CUSTOMER_NAME").mandatory = True
    mutated.entities["MARA"].attribute_by_code("MATERIAL_NUMBER").data_type = "BLOB"
    del mutated.entities["PLANT_MASTER"]
    r4 = next(r for r in mutated.relationships if r.name == "Relationship_4")
    r4.end2.dependent = False

    mutated_result = compare(pd_model, mutated)

    checks.expect(mutated_result.status == "FAIL",
                  "Mutated pair reconciles to FAIL")
    checks.expect(_has(mutated_result, "MANDATORY", "CUSTOMER_NAME"),
                  "Detects the flipped mandatory flag on KNA1.CUSTOMER_NAME")
    checks.expect(_has(mutated_result, "DATA_TYPE", "MATERIAL_NUMBER"),
                  "Detects the broken data-type family on MARA.MATERIAL_NUMBER")
    checks.expect(_has(mutated_result, "ENTITY_MISSING", "PLANT_MASTER"),
                  "Detects the deleted PLANT_MASTER entity")
    checks.expect(_has(mutated_result, "DEPENDENCY", "Relationship_4"),
                  "Detects the broken identifying-dependency flag on Relationship_4")
    checks.expect(mutated_result.critical_count >= 1,
                  "Mutated pair raises at least one CRITICAL finding (missing entity)")

    print("\n[4] Writing a real Excel/CSV/JSON report for the real pair ...")
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    result.pd_file = PD_FILE
    result.erwin_file = ERWIN_FILE
    result.pd_model = pd_model.model_name
    result.erwin_model = erwin_model.model_name
    workbook_path = generate_report([result], config.OUTPUT_DIR)
    checks.expect(os.path.isfile(workbook_path),
                  "Excel workbook was written to test_output/")

    print("\n[5] erwin hidden role-merged duplicate attribute (synthetic) ...")
    _test_hidden_logical_duplicate_attribute(checks)

    if args.quiet:
        print = _real_print  # noqa: A001 restore

    return checks.report()


if __name__ == "__main__":
    sys.exit(main())