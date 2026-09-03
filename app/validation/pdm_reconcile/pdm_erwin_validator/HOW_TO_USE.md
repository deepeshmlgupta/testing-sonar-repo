# PDM → ERwin Validator — Install & Where to Change Things

You are getting **two full files** that replace the ones in your validator, plus
this note.

| File | What to do with it |
|---|---|
| `erwin_parser.py` | **Replace** the old one (this is the actual fix). |
| `config.py` | **Replace** the old one, then edit the 3 folder paths (below). |

Both go inside your validator folder, next to `main.py`:

```
pdm_erwin_validator/
├── main.py
├── pd_parser.py
├── comparator.py
├── report_generator.py
├── data_type_mapper.py
├── erwin_parser.py      ← replace with the new file
├── config.py            ← replace with the new file, then edit paths
└── test_data/
```

> Tip: keep a backup of the old `erwin_parser.py` / `config.py` first
> (e.g. rename to `erwin_parser_OLD.py`) just in case.

---

## ✅ What you MUST change — `config.py`, 3 folder paths

Open `config.py` and set these three lines to YOUR folders
(near the top of the file):

```python
# Folder containing all .pdm files (PowerDesigner)
PD_MODELS_DIR = r"C:\Models\PowerDesigner"          # <-- CHANGE THIS

# Folder containing all .xml / .erwin files (ERwin)
ERWIN_MODELS_DIR = r"C:\Models\ERwin"               # <-- CHANGE THIS

# Where validation reports will be saved
OUTPUT_DIR = r"C:\Models\ValidationReports"         # <-- CHANGE THIS
```

For your Carbon Calculator test that would be, for example:

```python
PD_MODELS_DIR    = r"C:\Users\DebanujBarman\Downloads\<folder with 05_Carbon_Calculator.pdm>"
ERWIN_MODELS_DIR = r"C:\Users\DebanujBarman\Downloads\<folder with 05_Carbon_Calculator.xml>"
OUTPUT_DIR       = r"C:\Users\DebanujBarman\Downloads\ValidationReports"
```

> Keep the `r"..."` prefix — it makes Windows backslashes safe.
> The `.pdm` and `.xml` are paired automatically when their base file names
> match (`05_Carbon_Calculator.pdm` ↔ `05_Carbon_Calculator.xml`).

**You do NOT have to edit the paths** if you pass them on the command line
instead — see "How to run" below.

---

## 🔧 Optional settings — you usually leave these alone

Also in `config.py`:

- `CHECK_*` toggles — turn individual checks on/off (tables, columns, data
  types, nullability, defaults, PKs, FKs, indexes).
- `CASE_INSENSITIVE = True` — recommended; ignores upper/lower-case name
  differences between the tools.
- New `ERWIN DIALECT HANDLING` block (added by this fix):
  - `ERWIN_ATTR_NULL_OPTION_CODES = {"1": True, "0": False, "2": False}` —
    how erwin's numeric nullability code maps to NOT NULL. Leave as-is unless a
    model reports wrong nullability.
  - `ERWIN_IGNORE_FK_INDEXES = True` — hides erwin's auto-generated FK mirror
    indexes so they don't show as "index only in ERwin". Set to `False` if you
    *want* to compare them.

**`erwin_parser.py` needs NO editing.** Just drop it in.

---

## ▶ How to run

First time only, install dependencies:

```bash
pip install openpyxl tqdm
```

Quick single-pair sanity check (uses the built-in sample in `test_data/`):

```bash
python test_runner.py
```

Full run using the paths in `config.py`:

```bash
python main.py
```

Or override the folders on the command line (no need to edit `config.py`):

```bash
python main.py --pd  "C:\path\to\pdm_files" ^
               --erwin "C:\path\to\erwin_xml_files" ^
               --out "C:\path\to\reports"
```

The report is written to `<OUTPUT_DIR>\validation_report.xlsx`.

---

## What "fixed" looks like

Before, the ERwin side read as **0 tables**, so everything was FAIL /
"missing in ERwin". After this fix the ERwin tables are read correctly, so a
model that truly matches shows **STATUS: PASS** and only genuine differences are
reported. Full technical detail is in `PDM_FIX_NOTES.md`.
