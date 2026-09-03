"""
Quick end-to-end test using the sample files in test_data/
Run: python test_runner.py
"""

import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from app.config.validation_config import PDM_CONFIG as config
config.PD_MODELS_DIR    = os.path.join(os.path.dirname(__file__), "test_data")
config.ERWIN_MODELS_DIR = os.path.join(os.path.dirname(__file__), "test_data")
config.OUTPUT_DIR       = os.path.join(os.path.dirname(__file__), "test_output")
config.MAX_DIFF_ROWS_PER_MODEL = 500

from pd_parser        import parse_pdm
from erwin_parser     import parse_erwin
from comparator       import compare
from report_generator import generate_report

PD_FILE    = os.path.join(config.PD_MODELS_DIR,    "sample_model.pdm")
ERWIN_FILE = os.path.join(config.ERWIN_MODELS_DIR, "sample_model.xml")

print("="*60)
print("  PDM → ERwin Validation — Sample Test Run")
print("="*60)

# 1. Parse
print("\n▶ Parsing PowerDesigner PDM …")
pd_model = parse_pdm(PD_FILE)
print(f"  Tables found: {len(pd_model['tables'])}")
for t in pd_model['tables'].values():
    print(f"    • {t['code']:30s}  ({len(t['columns'])} cols)")

print("\n▶ Parsing ERwin XML …")
erwin_model = parse_erwin(ERWIN_FILE)
print(f"  Tables found: {len(erwin_model['tables'])}")
for t in erwin_model['tables'].values():
    print(f"    • {t['code']:30s}  ({len(t['columns'])} cols)")

# 2. Compare
print("\n▶ Running validation …")
result = compare(pd_model, erwin_model)

print(f"\n  STATUS: {result.status}")
print(f"  Tables PD / ERwin / Matched: {result.tables_pd} / {result.tables_erwin} / {result.tables_matched}")
print(f"  Missing in ERwin: {result.tables_missing_in_erwin}")
print(f"  Extra in ERwin:   {result.tables_extra_in_erwin}")
print(f"  CRITICAL findings: {result.critical_count}")
print(f"  WARNING findings:  {result.warning_count}")

print(f"\n  Findings ({len(result.findings)} total):")
for f in result.findings:
    icon = {"CRITICAL": "❌", "WARNING": "⚠️ ", "INFO": "ℹ️ "}.get(f.severity, "·")
    tbl  = f"[{f.table}]" if f.table else ""
    col  = f".{f.column}"  if f.column else ""
    print(f"  {icon}  [{f.category}] {tbl}{col}  →  {f.message}")
    if f.pd_value or f.erwin_value:
        print(f"         PD:    {f.pd_value}")
        print(f"         ERwin: {f.erwin_value}")

# 3. Report
print("\n▶ Generating Excel report …")
os.makedirs(config.OUTPUT_DIR, exist_ok=True)
report_path = generate_report([result], config.OUTPUT_DIR)
print(f"  ✅  Report → {report_path}")
print("\nTest complete.\n")
