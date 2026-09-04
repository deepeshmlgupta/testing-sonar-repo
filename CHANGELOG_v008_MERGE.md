# CHANGELOG — v008 (Sonar-fixed base + v007 Migration Framework, merged)

Base: `migration-platform_v005 1.zip` (Sonar-fixed).
Merged in: the agreed Migration Framework changes from v007.

All existing Sonar-fixed code and behaviour is preserved except where a
requirement or a blocking defect made a change unavoidable. Every such change is
listed in §4 and §5.

---

## 1. Verified result

Clean run, all three sample models:

| Model | Type | V1 | V2 | V3 | Status | Destination |
|---|---|---|---|---|---|---|
| 02_logistics_segment | CDM | **47.59** | 71.12 | **96.12** | PASS | `3_final` |
| 03_Lubes… | LDM | 65.63 | 98.97 | 98.97 | PASS | `3_final` |
| 05_Carbon_Calculator | PDM | 73.67 | 74.31 | **99.31** | PASS | `3_final` |

**These are identical to v007 to the last decimal**, which was the requirement.
CDM component trace — structural is the same at all three stages, so the entire
movement comes from metadata that was genuinely absent at V1:

```
V1  structural=92.24%  documentation=  5.88%  udp=  0.00%   ->  47.59
V2  structural=92.24%  documentation=100.00%  udp=  0.00%   ->  71.12
V3  structural=92.24%  documentation=100.00%  udp=100.00%   ->  96.12
```

* 63 tests pass.
* Pyflakes clean on every new and changed file (bar the pre-existing findings
  in §6).
* Bandit clean on every new and changed file.
* FAIL routing proved: threshold temporarily raised to 99.5 forced all three
  models below the line, each one's XML and V3 report landed in its **own**
  `manual_review_report`, `erwinmodels/3_final` was empty. Threshold restored
  to 90.0.
* `app/promote_model.py` runs and correctly blocks on unapproved warnings.

---

## 2. What the Sonar-fixed base was missing

The Sonar zip is an **earlier lineage**, not the v004 base v007 was built on.
Absent entirely: `app/validation/promotion_gate.py`,
`app/validation/udp_fidelity.py`, `app/reporting/benchmark.py`,
`app/reporting/field_mapping_report.py`, and the whole `tests/` folder. It also
had `PDM_FIDELITY_TARGET = 100.0`, no `PROMOTION_FIDELITY_THRESHOLD`, CDM/LDM
promotion gated on `status == "PASS" and critical_count == 0`, and no
`structural_fidelity_score` on any `ValidationResult`.

Everything my code depends on was verified present before merging, not assumed:
`documentation_rows` (populated at the end of every `compare()`), the
`_Shared → _Semantic → CDM/LDM/PDM` config inheritance and `tier()`,
`pdm_documentation.build_rows()`, and the whole UDP tool surface —
`sow_classify.classify`, `erwin_prepare.build_udp_schema` / `build_manifest` /
`udp_name` / `STRUCTURAL`, `udp_compare.compare_model`,
`udp_report.build_report`, `ComparisonResult.comparable/.pass_rate/.status/
.counts/.warnings/.readback/.summary_line()`, `Readback.method/.reliable`, the
workbook sheet names, and `erwin_load.py`'s CLI flags — all byte-identical
signatures.

**Not one file in `app/udp_tool/` was changed.** `python batch_run.py` still
works standalone.

---

## 3. New files (9) — nothing existing touched by these

| File | Purpose |
|---|---|
| `app/validation/promotion_gate.py` | the shared two-band gate: ≥90 PASS → `3_final`; <90 FAIL → per-model `manual_review_report` |
| `app/validation/udp_fidelity.py` | scores SAP Extended Attributes against the UDPs the erwin XML carries; preserves the reconciliation number on `structural_fidelity_score` |
| `app/validation/fidelity_stages.py` | V1 → V2 → V3 scoring from structural + documentation + UDP, each re-measured per stage |
| `app/validation/udp_bridge.py` | isolated-import loader for `app/udp_tool` (mirrors `pdm_validator_bridge`) |
| `app/validation/udp_flow.py` | Phase D: classify → map → inject → report → compare; never raises |
| `app/reporting/report_layout.py` | per-model report folders, the three report names, shared sheet-copy / formula-rewrite helpers |
| `app/reporting/v2_report.py` | the V2 UDP Mapping Report |
| `app/reporting/v3_report.py` | the V3 Final Fidelity Report |
| `pytest.ini` + `tests/` (5 files, 63 tests) | coverage for the gate, staged fidelity, UDP flow and report layout |

---

## 4. Sonar-fixed files changed, and exactly why

### 4.1 `app/main.py` — extended in Sonar's own shape (your answer to Q2)

Every existing function name kept: `setup_directories`, `find_models`,
`process_pdm_model`, `process_conceptual_model`, `generate_detailed_reports`,
`generate_summary_report`, `main`. New functions added alongside:
`run_udp_phase`, `reconcile`, `route_model`, `pdm_destination`,
`reconciliation_workbook`, `build_v1_report`, `build_v2_report`,
`build_v3_report`, `discard`, `summary_row`, `excluded_sheets`, plus the
`ModelRecord` dataclass.

| Function | Change |
|---|---|
| `setup_directories()` | adds `erwin/` under 1_initial and 2_preprocessed, `reports/` under 3_final, the tier report folders and the interim scratch folder |
| `process_conceptual_model()` | reconciles **twice** — V1 against `1_initial/xml`, V2 against `2_preprocessed/xml` — then Phase D, then V3 and the gate |
| `process_pdm_model()` | runs Phase D first, passes `udp_pass_rate` and `manual_review_dir` into `run_pdm_flow`, returns a `ModelRecord` |
| `generate_detailed_reports()` | writes the three reports into `app/reporting/<tier>_reports/<model>/` |
| `generate_summary_report()` | adds the V1/V2/V3, documentation, UDP and report-path columns |
| `main()` | collects `ModelRecord`s instead of two parallel lists |

`ModelRecord.status` takes the verdict from the promotion gate rather than from
the comparator. This matters for PDM: its `ValidationResult` reports WARN
whenever any warning finding exists, which would have contradicted a model the
gate promoted at 99.31%.

### 4.2 `app/config/settings.py` — additions, plus the one confirmed change

Added: `PROMOTION_FIDELITY_THRESHOLD = 90.0`, `PROMOTION_FIDELITY_BASIS`,
`PROMOTION_BANDS = "two"`, `MANUAL_REVIEW_DIR`, `REVIEW_FIDELITY_FLOOR`,
`REJECTED_SUBDIR`, `PROMOTION_BLOCK_ON_CRITICAL`, `PROMOTION_BLOCK_ON_WARNING`.

Changed (Q3, confirmed): `PDM_FIDELITY_TARGET` **100.0 → tied to the 90%
threshold**.

### 4.3 `app/config/validation_config.py` — additions only, on `_Shared`

`UDP_FIDELITY_*` (6 keys), `UDP_TOOL_*` (12 keys), `FIDELITY_STAGE_WEIGHTS`,
`V3_REPORT_ENABLED`, `V3_REPORT_EXCLUDE_SHEETS`. No existing value altered.
They reach CDM, LDM and PDM through the existing inheritance and appear on the
existing CONFIG sheet.

### 4.4 Three comparator hooks — one call each, after the score is computed

| File | Added |
|---|---|
| `cdm_reconcile/comparator.py` | `from app.validation import udp_fidelity` + `udp_fidelity.apply(result, "CDM")` after `result.finalise()` |
| `ldm_reconcile/comparator.py` | the same, `"LDM"` |
| `pdm_erwin_validator/comparator.py` | a **guarded** import (the validator also runs standalone, where `app.*` is not importable) + `udp_fidelity.apply(result, "PDM")` after `compute_score()` |

Each runs after the reconciliation is complete, so it cannot influence a single
finding, and `udp_fidelity.apply()` never raises. `UDP_FIDELITY_ENABLED = False`
removes the effect entirely.

### 4.5 `app/validation/pdm_reconcile/pdm_flow.py` — four surgical edits

1. two new optional parameters, `udp_pass_rate` and `manual_review_dir`
   (defaults keep every existing call working);
2. V1 stamped on `initial_result`, V2 then V3 on `final_result`, so the flow's
   own gate tests the V3 number;
3. `_hold_for_review()` routes to the model's `manual_review_report` in two-band
   mode, and keeps the previous "held in 2_preprocessed" wording in three-band
   mode;
4. see §5.2.

Sonar's `_gate_failures()` and `_is_complete()` are **left in place and still
called**. Only the destination and the score they test change.

### 4.6 `app/promote_model.py` — pickup location

Now scans `app/reporting/*_reports/*/manual_review_report/` **and** the original
`manual_review_reports/`, resolves the model's source files from the folder the
report was found in before falling back to `2_preprocessed`, recognises the new
report filenames, and skips `archived_promotions/` so a promoted model is not
re-promoted on the next run. Sonar's `_process_excel_report`, `_promote_files`
and `_update_and_archive_excel` keep their names and structure.

---

## 5. Two changes beyond the agreed scope — flagged, not hidden

### 5.1 A pre-existing `NameError` in the Sonar base

`app/validation/pdm_reconcile/pdm_report_generator.py:745` uses
`_MATRIX_CRITICAL_COLUMN`, which **is never defined anywhere in that file**.
Confirmed present in the untouched Sonar zip — pyflakes reports
`undefined name '_MATRIX_CRITICAL_COLUMN'` there too.

It never fired before because the old flow built one PDM report from
`final_result`, which has no CRITICAL findings, so the branch was unreachable.
The V1 initial report is built from the as-imported result, which has 9, so it
crashes the run.

**Fix:** defined `_MATRIX_CRITICAL_COLUMN = 17` above `_write_matrix_row`, which
is the index of the CRITICAL value in the row that function writes (model,
status, 2 codes, 6 column counts, 2 PK, 2 key, 2 index → critical is 17th).

### 5.2 Sonar's PDM gate blocked on any warning

`_gate_failures()` appended a blocker for **any** WARNING finding, so the PDM at
99.31% could never promote and your "≥90% = PASS" rule could not hold for PDM.

**Fix:** the two blocks now consult `PROMOTION_BLOCK_ON_CRITICAL` and
`PROMOTION_BLOCK_ON_WARNING` (both default `False`). Setting either to `True` in
`app/config/settings.py` restores Sonar's previous behaviour exactly.

Related: `fidelity_stages.apply()` now also writes `fidelity_score_raw` when the
result has it, because that is the field Sonar's PDM gate tests. Without it PDM
would still have been gated on its structural number while CDM and LDM were
gated on V3. `structural_fidelity_score` still carries the comparator's own
reconciliation figure, untouched.

---

## 6. Pre-existing findings left exactly as they were

Per your instruction not to modify anything not required:

| File | Finding |
|---|---|
| `app/main.py` | four unused imports — `subprocess`, `PDM_REPORT_FILENAME`, `ValidationResult`, `pdm_bridge` — and one f-string without placeholders. All present in the Sonar original |
| `app/validation/pdm_reconcile/pdm_report_generator.py` | unused `typing.Optional`; unused local `worst_start` |
| `app/reporting/migration_audit.py` | untouched |

The only pre-existing defect I fixed is §5.1, because it blocks the run.

---

## 7. Output layout

```
app/reporting/
├── cdm_reports/02_logistics_segment/
│   ├── 02_logistics_segment_V1_Initial_Fidelity_Report.xlsx    9 sheets
│   ├── 02_logistics_segment_V2_UDP_Mapping_Report.xlsx          9 sheets
│   ├── 02_logistics_segment_V3_Final_Fidelity_Report.xlsx      18 sheets
│   └── manual_review_report/          created only when V3 < 90%
├── ldm_reports/<model>/                … same three reports
└── pdm_reports/<model>/                … same three reports

erwinmodels/3_final/       models at or above 90%
├── xml/<model>.xml   erwin/<model>.erwin
└── reports/<model>_V3_Final_Fidelity_Report.xlsx

app/udp_tool/output_excel_reports/{cdm,ldm,pdm}/   the UDP tool's raw workbooks
data/udp/<model>/                                  per-model UDP working dir
data/interim_reports/                              scratch, emptied each run
```

No `v3_reports` folder, no `udp_reports` folder.

**`UDP_DETAIL`:** excluded from every V3 report, as required. Note that Sonar's
tier report generators do not emit `UDP_FIDELITY` or `UDP_DETAIL` sheets at all
(that wiring belonged to the other lineage), so the requirement is satisfied and
`V3_REPORT_EXCLUDE_SHEETS` now stands as a safeguard. The per-value UDP evidence
lives in the V2 mapping report's `UDP_VALUE_RECON` and `UDP_COMPARISON` sheets.

---

## 8. Tuning

| Setting | File | Effect |
|---|---|---|
| `PROMOTION_FIDELITY_THRESHOLD` | `settings.py` | the 90% PASS line |
| `PROMOTION_BANDS` | `settings.py` | `"three"` restores PASS/WARN/FAIL |
| `PROMOTION_BLOCK_ON_CRITICAL/_WARNING` | `settings.py` | `True` restores Sonar's finding-blocks |
| `PROMOTION_FIDELITY_BASIS` | `settings.py` | `"structural"` gates on reconciliation only |
| `FIDELITY_STAGE_WEIGHTS` | `validation_config.py` | component weighting; raising documentation/udp lowers every V1 |
| `UDP_FIDELITY_ENABLED` | `validation_config.py` | `False` removes the UDP scoring layer's effect |
| `UDP_TOOL_ENABLED` | `validation_config.py` | `False` skips Phase D; V3 then equals V2 |
| `V3_REPORT_EXCLUDE_SHEETS` | `validation_config.py` | which tier sheets the V3 report drops |

---

## 9. Still open, unchanged from v007

**LDM V1 = 65.63% and PDM V1 = 73.67%, not ~60%.** Those models genuinely have
less to lose at V1 — the LDM has 22 documented objects, and the PDM's erwin
import carries its comments natively (98.68% documentation before preprocessing
even runs). The CDM, which has real gaps, lands at 47.59%. The honest lever is
`FIDELITY_STAGE_WEIGHTS`, not a penalty that measures nothing.

**Shortcuts and Tags are reported as "Not measured", not scored.** erwin's XML
export has no shortcut object, so a shortcut reads as missing at every stage
alike. erwin Tags have no parser, no config key and no element in any shipped
export. Tell me which erwin construct you mean by Tags and I can build it.

**The `.git` directory** in the archive has an older HEAD than the working tree.
Do not run `git checkout -- .` in this tree.
