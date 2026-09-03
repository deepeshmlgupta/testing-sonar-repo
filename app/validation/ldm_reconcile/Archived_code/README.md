# LDM Validation Framework — PowerDesigner → erwin

Reconciles a SAP PowerDesigner **Logical** Data Model (`.ldm`) against an
erwin Data Modeler logical XML export after migration, and produces an
audit-ready Excel report plus CSV/JSON exports.

Built using the same proven architecture as the companion CDM framework
(parsers → canonical model → comparator → report generator), but the LDM
parsers were written and verified from scratch against the actual supplied
files (`SD_O2C_LDM_WC.ldm` / `SD_O2C_LDM_WC.xml`) — not copied from the CDM
parsers' assumptions. See **FINDINGS.md** for exactly what was verified,
what real differences between an LDM and a CDM export were found, and one
real bug that was found and fixed while validating against real data.

---

## Result on the supplied model pair

```
Model pairs        : 1
PASS               : 1
Average fidelity   : 100.00%
Entities matched   : 9 / 9
Attributes matched : 84 / 84
Relationships matched : 10 / 10
Identifiers matched   : 9 / 9
CRITICAL findings  : 0
WARNING findings   : 0
```

This is a genuinely clean migration — confirmed not by an absent check, but
by a synthetic mutation test (see `test_runner.py`) that proves the same
comparator correctly flags CRITICAL/WARNING findings the moment a real
defect (flipped mandatory flag, broken data type, deleted entity, broken
identifying-relationship flag) is introduced.

---

## Install

```bash
pip install -r requirements.txt      # openpyxl required, tqdm optional
```

Python 3.8+.

---

## Quick start

```bash
# 1. Confirm the framework works and see what it reports (26 assertions)
python test_runner.py

# 2. Point config.py at your model folders, then check the pairing
python main.py --dry-run

# 3. Full run against the supplied SD_O2C_LDM_WC pair (default paths)
python main.py

# 4. Point at your own folders of .ldm / erwin .xml files
python main.py --pd "C:\Models\LDM" --erwin "C:\Models\ERWIN_LDM" --out "C:\Reports"
```

## Command line

```
python main.py [options]

  --pd PATH             Folder of PowerDesigner .ldm files
  --erwin PATH          Folder of erwin XML exports
  --out PATH            Output folder for the report
  --workers N           Pairs validated in parallel            (default 8)
  --limit N             Validate only the first N pairs        (0 = all)
  --resume              Skip pairs in validated_pairs.csv
  --match {filename,prefix,csv}
  --type-mode {exact,canonical,family}
  --report-name FILE    Output workbook name
  --fail-on {none,critical,warning}   Gate the exit code for CI/CD
  --dry-run             List discovered pairs and exit
  --verbose             Debug-level console logging
```

Exit codes: `0` completed and passed the gate · `1` no pairs found or fatal
config problem · `2` the `--fail-on` threshold was breached.

---

## What gets validated

| Layer | Checks |
|---|---|
| **Structure** | entities present · attributes present · primary identifiers · alternate identifiers |
| **Semantics** | relationship existence · cardinality (1:1, 1:N, N:1, M:N) · optionality per end · identifying (existence) dependency · role/verb phrases · generalisation hierarchies |
| **Vocabulary** | business names · definitions (similarity-scored) · domain assignment · logical data types (family/canonical/exact) · length and precision |
| **Governance** | business rules · subject-area membership · orphan entities · entities without identifiers or attributes |

Every check is a toggle in `config.py`; every finding category's severity
can be retuned — or switched off — through `SEVERITY_OVERRIDES` without
touching code.

---

## Architecture

```
main.py                 CLI, pair discovery, parallel execution, checkpoint/resume
  ├── pd_ldm_parser.py      PowerDesigner .ldm   → canonical model
  ├── erwin_ldm_parser.py   erwin logical XML    → canonical model
  ├── ldm_model.py          canonical objects both parsers emit
  ├── comparator.py         reconciliation engine, findings, fidelity score
  │     ├── normalizers.py    names, logical types, definition similarity
  │     └── cardinality.py    cardinality vocabulary, relationship signatures
  ├── report_generator.py   Excel workbook + CSV/JSON exports
  ├── diagnose_cardinality.py  standalone erwin cardinality/nullability diagnostic
  └── config.py             paths, toggles, severities, weights, erwin enum codes
```

Both parsers emit the same canonical model from `ldm_model.py`, so the
comparator never sees a tool-specific structure.

### Object mapping (verified against the supplied files)

| Canonical | PowerDesigner LDM | erwin (logical export) |
|---|---|---|
| Entity | `o:Entity` | `Entity` |
| Attribute | `o:EntityAttribute` | `Attribute` |
| Identifier | `o:Identifier` + `c:PrimaryIdentifier` | `Key_Group` (PK / AK) |
| Relationship | `o:Relationship` | `Relationship` |
| Inheritance | `o:Inheritance` | `Subtype_Relationship` |
| Domain | `o:Domain` | `Domain` |
| Business rule | `o:BusinessRule` | `Validation_Rule` |
| Subject area | `o:Package` | `Subject_Area` |

erwin's auto-generated `IF1`/`IF2`/`IF3` key groups (one per relationship,
confirmed present in the supplied file) are dropped — they are physical
FK indexes with no PowerDesigner LDM counterpart, exactly analogous to a
CDM's `IE` inversion entries.

### Real differences from a CDM export this parser handles explicitly

1. **Attribute mandatory flag has a different element name.** A PowerDesigner
   CDM writes `<a:Mandatory>`; an LDM writes `<a:LogicalAttribute.Mandatory>`.
2. **A PowerDesigner LDM migrates parent keys into child entities** on
   identifying relationships (unlike a CDM, which never does). Both sides of
   the supplied model genuinely carry these attributes, so
   `ERWIN_MIGRATED_KEY_HANDLING = "info"` (not `"ignore"`) is the correct LDM
   default — dropping them would falsely report a missing attribute.
3. **erwin's attribute-level `Null_Option_Type` is a separate code space**
   from the relationship-level field of the same name (0/1 vs 100/101),
   verified by cross-referencing every PK-member attribute in the file.
4. **erwin's built-in system domains** (`<root>`, `<default>`, String,
   Number, Datetime, Blob) resolve on every attribute's `Parent_Domain_Ref`
   and must not be surfaced as "the attribute's assigned domain" — a bug
   found and fixed during validation against the real file (see
   FINDINGS.md).
5. **Identifying-relationship detection.** PowerDesigner's LDM export writes
   `c:ParentIdentifier` on every relationship (identifying or not) — it names
   the join, not the relationship kind. This parser instead checks whether
   the child entity's own primary identifier contains attribute(s) inherited
   from the parent's primary identifier, cross-verified against erwin's
   `Type` code with 100% agreement across all 10 relationships in the
   supplied file.

---

## The report

`ldm_validation_report.xlsx`:

| Sheet | Contents |
|---|---|
| `SUMMARY` | one row per model pair — status, fidelity score, object counters |
| `DASHBOARD` | run totals, severity mix, lowest-fidelity models, chart |
| `FINDINGS` | every difference, sorted by severity, with a recommended action per row |
| `ENTITY_MATRIX` | entity-by-entity reconciliation including the match basis |
| `RELATIONSHIPS` | relationship-by-relationship reconciliation with both degrees |
| `CATEGORY_ANALYSIS` | findings by category × severity |
| `CONFIG` | the exact rule set that produced this report |
| *per model* | one detail sheet per pair |

Alongside it: `findings.csv` and `validation_summary.json` for pipelines,
`validated_pairs.csv` for `--resume`, and `ldm_validation.log`.

---

## Testing

```bash
python test_runner.py            # 26 assertions: real pair + mutation-test proof
python test_runner.py --quiet    # CI mode
```

`FINDINGS.md` documents exactly what was verified against the real files,
the bug found and fixed, and the mutation-test evidence that the zero-finding
result on the real pair reflects a genuinely clean migration rather than a
comparator that silently passes everything.

---

## Extending it

**A new check** — add a category to `SEVERITY_DEFAULTS` in `comparator.py`,
add a toggle to `config.py`, and emit through `result.emit(...)`.

**A new erwin enum code** — add it to the relevant table in `config.py`
(`ERWIN_CARDINALITY_CODES`, `ERWIN_NULL_OPTION_CODES`,
`ERWIN_ATTRIBUTE_NULL_OPTION_CODES`, `ERWIN_RELATIONSHIP_TYPE_CODES`,
`ERWIN_IGNORED_KEY_GROUP_TYPES`) and re-run `diagnose_cardinality.py` against
the file to confirm.

**Model quality issue?** Run
`python diagnose_cardinality.py your_erwin_export.xml` first — it prints the
raw cardinality/nullability values next to what the parser resolved them to,
and flags collapsed-variance or unrecognised-code problems directly.
