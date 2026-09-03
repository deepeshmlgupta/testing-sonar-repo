# PDM → ERwin Validator — Fix Notes

## Symptom
`validation_report.xlsx` showed, for `05_Carbon_Calculator`:

| Tables (PD) | Tables (ERwin) | Matched | Missing in ERwin | CRITICAL | Status |
|---|---|---|---|---|---|
| 101 | **0** | **0** | **101** | **255** | **FAIL** |

Nothing came through from the ERwin side, so every PowerDesigner table was
reported as "missing" and every one produced CRITICAL findings.

## Root cause — `erwin_parser.py` understood only one ERwin dialect
The old parser found entities with `root.iter("Entity")` and read every field
from XML **attributes** (`entity.get("Physical_Name")`). That only works for the
old "flat" export (the shape of `test_data/sample_model.xml`).

Real erwin (Data Modeler 2019–2022, i.e. the **10.x "EMX" / metamodel** export —
what your `05_Carbon_Calculator.xml` is) is structured completely differently:

* The file has **XML namespaces** — the root is
  `<erwin xmlns="http://www.erwin.com/dm">` and the model body sits under
  `xmlns="http://www.erwin.com/dm/data"`. `root.iter("Entity")` matches the
  *unqualified* tag name, so on a namespaced file it matches **nothing** →
  **0 tables** → every table "missing" → the all-CRITICAL report.
* Scalar fields (`Name`, `Physical_Name`, `Physical_Data_Type`, …) are **child
  elements inside a `<…Props>` wrapper** (`EntityProps`, `AttributeProps`,
  `Key_GroupProps`), not attributes.
* Identity is in **lowercase** `id` / `name` attributes.
* Enums are **integer codes** (e.g. `Null_Option_Type` `1` = NOT NULL, `0` = NULL)
  instead of English text.
* Primary/foreign key columns are `Key_Group_Member`s that reference the column
  through a nested `<Attribute_Ref>`, and FK join columns come from the child
  entity's **migrated** attributes (`Parent_Relationship_Ref` /
  `Parent_Attribute_Ref`).

This is the same class of bug that was fixed for the CDM validator (see that
project's `FIX_NOTES.md`); the diagnosis and cure are reused here.

## The fix — `erwin_parser.py` rewritten to be dialect-robust
* Matches on the **local** tag name, so XML namespaces no longer hide anything.
* A single `_val()` helper reads each field from, in order: an attribute (exact
  case), an attribute (case-insensitive, for lowercase `id`/`name`), a direct
  child element, or a child inside the `<…Props>` wrapper — so one code path
  covers every erwin version and export option.
* `Null_Option_Type` integer codes are decoded (config-overridable).
* Key-group members resolve to physical column names via `Attribute_Ref` (EMX)
  **or** the member `name` **or** the flat dialect's `Attribute_Ref`/`Sequence`.
* Foreign-key join columns are read from `RI_Constraint` (flat) **or** derived
  from the child's migrated FK attributes (EMX).
* erwin's auto-generated FK mirror indexes (`Key_Group_Type` `IF1/IF2/…`) are
  ignored by default so they don't create cosmetic "index only in ERwin"
  noise (toggle: `config.ERWIN_IGNORE_FK_INDEXES`).

The **public API and returned dict shape are unchanged**, so
`comparator.py`, `report_generator.py`, `pd_parser.py` and `main.py` were not
touched. Only `erwin_parser.py` was rewritten and a small, optional
`ERWIN DIALECT HANDLING` block was added to `config.py`.

## Verification
1. **Self-test, flat dialect** (`python test_runner.py`) — still produces exactly
   the four differences that `sample_model.xml` was designed to contain
   (EMAIL data type CRITICAL, LAST_NAME nullability WARNING, STATUS default INFO,
   extra AUDIT_LOG table WARNING) and no spurious findings. No regression.
2. **Real EMX export** (the 12 MB namespaced erwin file from the CDM project) now
   parses to **191 tables / 245 references** — it returned **0** before the fix.
   Columns, physical names, data types, nullability, PK members and FK joins all
   resolve.
3. **Full pipeline on a physical EMX file that matches its PDM** → **STATUS: PASS,
   3/3 tables matched, 0 findings** (previously this same situation reported
   everything as missing).
4. **Negative control** — introducing a real data-type change and a nullability
   change into the EMX file is still detected (1 CRITICAL + 1 WARNING), so
   detection was not weakened.

## To re-run on your models
```
python main.py --pd <folder-with-.pdm> --erwin <folder-with-.xml> --out <reports>
# or a single quick check:
python test_runner.py
```
Re-run against `05_Carbon_Calculator.pdm` / `05_Carbon_Calculator.xml`; the
ERwin side will now be read and only genuine differences (if any) will be
reported instead of 101 false "missing table" CRITICALs.
