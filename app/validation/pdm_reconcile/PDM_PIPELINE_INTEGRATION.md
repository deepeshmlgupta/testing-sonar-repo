# PDM Validator → Main Pipeline Integration

The PDM → erwin validator in `pdm_erwin_validator_Fixed/pdm_erwin_validator/` is now
wired into `app/main.py` as Phase D for `.pdm` models, with a preprocessing and
re-validation loop in front of the promotion gate.

**The validator's own files were not modified.** `python main.py` inside its folder
still works exactly as documented in `HOW_TO_USE.md`.

## The gate

```
sappdmodels/pdm/*.pdm
        │
        ▼
1_initial (.erwin + .xml)  ──validate──▶  fidelity == 100% ──yes──▶  3_final
                                              │
                                              no
                                              ▼
                                        preprocessing
                                    (PowerDesigner-driven)
                                              │
                                              ▼
                              2_preprocessed (.erwin + .xml)
                                              │
                                        re-validate
                                              │
                                   fidelity == 100% ──yes──▶  3_final
                                              │
                                              no
                                              ▼
                                 held in 2_preprocessed
                              (reported, never promoted)
```

Promotion always follows a **measured** score: the second pass re-parses the
rewritten XML from disk rather than trusting what preprocessing reports.
`1_initial` is never emptied — it stays as the audit copy of the raw import.

## Files added

| File | Purpose |
|---|---|
| `pdm_validator_bridge.py` | Loads the validator in an isolated import window |
| `pdm_flow.py` | Drives validate → preprocess → re-validate → promote |
| `app/preprocessing/pdm_preprocessor.py` | The remediation engine |
| `tests/test_pdm_flow.py` | End-to-end tests for all three routes |

## Files modified

| File | Change |
|---|---|
| `app/main.py` | Registers the PDM parsers via the bridge; routes `.pdm` through `pdm_flow`; writes the PDM workbook with the PDM report generator; adds Stage/Notes to the pass-fail summary; skips the erwin COM session when `1_initial` already holds both files |
| `app/config/settings.py` | `PDM_FIDELITY_TARGET`, `PDM_PREPROCESS_ENABLED`, `PDM_KEEP_PREPROCESSED_COPY`, `PDM_REPORT_FILENAME` |
| `app/erwin/erwin_session.py` | `win32com` / `pythoncom` imported lazily, so validation and preprocessing are importable without erwin installed; `start()` still fails on Windows exactly as before |

## Why a bridge was needed

The PDM validator's modules import each other by plain name (`import config`,
`from comparator import compare`). The LDM validator that `main.py` already loads
has files with the **same names** and its folder is on `sys.path` too. Importing
the PDM tool naively would either pick up the LDM modules or leave the LDM ones
poisoned in `sys.modules`, silently corrupting both.

The bridge imports the validator with its own folder first on `sys.path` and the
clashing names hidden, re-registers what it loaded under `pdm_validator.*`, then
restores `sys.modules` and `sys.path` exactly as they were. `tests/test_pdm_flow.py`
asserts the LDM modules survive untouched.

## What preprocessing repairs

PowerDesigner is the source of truth. One missing attribute normally causes three
separate findings, so all three are fixed together:

1. **Missing columns** — restored with the PD data type, nullability and default.
2. **Empty primary keys** — `Key_Group_Member` entries re-linked (a PK group is
   created if erwin has none), plus erwin's ordering arrays.
3. **Broken foreign-key joins** — restored columns are marked as migrated
   (`Parent_Relationship_Ref` / `Parent_Attribute_Ref`), and children whose parent
   pointer no longer resolves are repointed. A pointer that already resolves is
   never rewritten.

Columns are added in one pass and linked in a second, because a relationship can
only be wired once both of its attributes exist and either end may have been the
missing one.

### What it deliberately does not do

It never invents data to raise a score. Two classes of difference are left in the
report:

- **Legitimate modelling differences** — PowerDesigner's abstract `Enum` has no SQL
  form and erwin correctly realises it as `CHAR(18)`. Writing `ENUM` into a physical
  erwin model would corrupt it.
- **Defects in the source model** — e.g. a PD reference with no child column bound.
  Nothing on the erwin side can match a join PowerDesigner never completed.

If those remain, the model does not reach 100% and is not promoted. That is the
gate working, not failing.

## `.erwin` files are binary

`.erwin` is erwin's proprietary binary format (`GDMM` magic bytes), so it can only
be rewritten through erwin's COM API on Windows with erwin Data Modeler installed.
Remediation is therefore applied to the `.xml` standard export — which is what the
validator reads and what the next stage consumes — and the `.erwin` file is carried
forward beside it. When erwin *is* available, `rebuild_erwin_binary()` regenerates
the binary from the corrected XML; when it is not, that is logged and the run
continues.

## Current result for `05_Carbon_Calculator`

| | Fidelity | Status | CRITICAL | WARNING | INFO |
|---|---|---|---|---|---|
| Pass 1 (`1_initial`) | 98.15% | FAIL | 9 | 8 | 33 |
| Pass 2 (`2_preprocessed`) | **99.77%** | WARN | **0** | 1 | 26 |

Preprocessing restored 7 columns, re-linked 2 primary keys and repaired 7 of 8
foreign keys, clearing every CRITICAL. The remaining 27 findings are the two
classes above: 24 abstract-`Enum` realisations, 2 index entries where PD reports an
identifier as non-unique and erwin as unique (a parser convention difference), and
1 PD reference with no child column. **The model is held in `2_preprocessed`.**

If your team considers the abstract-type and index notes non-defects, they are a
severity policy decision, not a data problem — set them to `IGNORE` in the
validator's `config.py` (`FINDING_SEVERITY`) and re-run. That is left to you
deliberately rather than changed on your behalf.

## Running

```bash
python app/main.py            # full pipeline
python tests/test_pdm_flow.py # PDM flow tests (no pytest needed)
pytest tests/test_pdm_flow.py -v
```
