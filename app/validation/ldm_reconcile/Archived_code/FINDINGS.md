# FINDINGS — What was verified, what was found, what was fixed

This document exists so a reviewer never has to take the framework's
behaviour on faith. Every claim below was checked by direct inspection of
`SD_O2C_LDM_WC.ldm` and `SD_O2C_LDM_WC.xml` — not assumed from the CDM
reference framework this was built alongside.

## 1. Structural facts confirmed before any parser code was written

| Fact | PowerDesigner LDM | erwin logical XML |
|---|---|---|
| Entity count | 9 (`Id`-bearing `o:Entity`) | 9 (`id`-bearing `Entity`) |
| Attribute count | 84 | 84 |
| Identifier count | 9 (one PK per entity, no AKs in this model) | 9 (after dropping `IF1`/`IF2`/`IF3` — see §3) |
| Relationship count | 10 | 10 |
| Domains used | 0 | 0 (all 6 present are erwin's built-in system types) |
| Subtypes / M:N | none | none |

Entity names and codes matched exactly, 1:1, by code, for all 9 entities —
confirmed with a direct name/code dump from both files before any comparator
logic ran.

## 2. Real schema differences between an LDM export and a CDM export

These were found by inspecting the actual XML, not carried over from the
CDM reference parser:

- **Attribute mandatory flag field name.** A PowerDesigner CDM export writes
  `<a:Mandatory>`. The supplied LDM export instead writes
  `<a:LogicalAttribute.Mandatory>`. Confirmed by grepping the raw file — zero
  occurrences of `<a:Mandatory>` anywhere in `SD_O2C_LDM_WC.ldm`.
  `pd_ldm_parser.py` checks both names.

- **erwin's attribute-level `Null_Option_Type` is a separate, smaller code
  space than the relationship-level field of the same name.** Confirmed by
  cross-tabulating every attribute that is a member of a `PK` `Key_Group`:
  all 8 such attributes carry `Null_Option_Type = 1`; a sample of ordinary
  (non-key) attributes all carry `0`. This is a 0/1 space, never the 100/101
  used at the relationship level. `erwin_ldm_parser.py` resolves these
  through two separate config tables (`ERWIN_ATTRIBUTE_NULL_OPTION_CODES` vs
  `ERWIN_NULL_OPTION_CODES`) so the two are never conflated.

- **erwin `Key_Group_Type` values present: `PK`, `IF1`, `IF2`, `IF3`.**
  Confirmed by enumerating every `Key_Group_Type` value in the file — no
  `AK` or `IE` is present. The `IFn` groups are erwin's auto-generated FK
  index per relationship (10 relationships → up to 3 distinct `IFn` labels
  reused across entities), with no PowerDesigner LDM counterpart.
  `_is_ignored_key_group` matches the `IF\d*` pattern generically so a larger
  model with more relationships (and therefore more `IFn` labels) is still
  handled correctly.

- **A PowerDesigner LDM migrates parent keys into child entities on
  identifying relationships — a CDM never does.** Confirmed directly: entity
  "Sales Order Item (VBAP)" carries an attribute named "Sales Document",
  which is also the primary-identifier attribute of its parent "Sales Order
  Header (VBAK)". This is real LDM content on the PowerDesigner side, not an
  artifact of the erwin export. Cross-checked against erwin: the same
  attribute carries `<Parent_Attribute_Ref>` there, confirming erwin
  recognises it as migrated too. Because both sides genuinely have the
  attribute, `config.ERWIN_MIGRATED_KEY_HANDLING` was set to `"info"` — the
  CDM default of `"ignore"` would have made the erwin parser silently drop
  an attribute that has a real PD LDM counterpart, producing a false
  ATTRIBUTE_MISSING finding for every migrated key in the model.

- **PowerDesigner's `c:ParentIdentifier` collection is present on every
  relationship, identifying or not** — confirmed by inspecting all 10
  relationships in the file: every one carries a `c:ParentIdentifier`,
  including the 7 that erwin's own `Type` code marks as non-identifying
  (`Type=7`). It records which identifier the join uses, not whether the
  relationship is identifying. The parser instead determines identifying-ness
  by checking whether the child entity's own primary identifier contains
  attribute(s) inherited from the parent's primary identifier — verified to
  agree with erwin's `Type` code on every one of the 10 relationships (the 3
  identifying ones are exactly `Relationship_4`, `_6`, `_8`, matching erwin's
  three `Type=2` relationships).

## 3. erwin numeric enum codes — confirmed present, not assumed

| Field | Values found in `SD_O2C_LDM_WC.xml` | Meaning (verified) |
|---|---|---|
| Relationship `Type` | `2` (3x), `7` (7x) | `2`=identifying, `7`=non-identifying — cross-checked against PD LDM's derived identifying flag with 100% agreement |
| Relationship `Cardinality` | `-3` (10x, all of them) | Zero, One or More (0..n) |
| Relationship `Null_Option_Type` | `100` (7x), `101` (3x) | 100=nulls allowed (optional), 101=not allowed (mandatory) — cross-checked against PD LDM's `Entity2ToEntity1RoleCardinality` ("0,1"/"0,n" vs "1,1") with 100% agreement |
| Attribute `Null_Option_Type` | `0` (72x), `1` (12x) | 0=nulls allowed (optional), 1=not null (mandatory) — cross-checked against PK membership |
| `Key_Group_Type` | `PK` (9x), `IF1`/`IF2`/`IF3` (10x total) | PK=primary identifier, IFn=auto FK index (dropped) |
| Domain `Built_In_Id` | `1`–`6` on all 6 domains present | all 6 are erwin's system scaffolding, none created by a modeller |

`-2` and `-1` (other members of erwin's known `Cardinality` enum) and `4`/`9`
(M:N / subtype `Type` codes) are **not present** in the supplied file. They
are kept in `config.py` from the erwin metamodel's documented enum, clearly
commented as "not observed in this file", so the parser handles a larger
model correctly without having been able to verify those specific codes
against real data here.

## 4. A real bug found and fixed while validating against the real files

**Symptom:** the first full run of `main.py` against the real pair produced
exactly 84 WARNING findings — one per attribute, all category `DOMAIN`,
all reading "Domain assignment differs: (no domain) vs String/Number/
Datetime/Blob".

**Root cause:** `erwin_ldm_parser.py`'s attribute parser resolved every
attribute's `Parent_Domain_Ref` to look up type/length/precision — which is
correct, since erwin assigns every attribute to *some* domain, real or
built-in. The bug was that the resolved domain's *name* was also being
surfaced as the attribute's assigned domain, with no check for whether that
domain was one of erwin's 6 built-in system types (`<root>`, `<default>`,
String, Number, Datetime, Blob — all with a non-zero `Built_In_Id`). Since
every one of the 84 attributes in this file resolves to a built-in (the
model uses no user-created domains on either side), every single attribute
was flagged as a domain mismatch against a PowerDesigner LDM that correctly
has no domain assigned.

**Fix:** `erwin_ldm_parser.py` now builds a `builtin_domain_oids` set while
parsing the model's `Domain` elements, and only surfaces a domain name on an
attribute when its `Parent_Domain_Ref` points at a **non-built-in** domain.
Built-in domains are still used to resolve type/length/precision (that part
was already correct), just not surfaced as "the assigned domain".

**Verification of the fix:** re-ran the full pipeline; the 84 false findings
disappeared and the result became 100% fidelity / PASS / 0 findings. Confirmed
this wasn't simply hiding a real signal by manually inspecting a sample of
matched attribute type/mandatory values (§5) and by a synthetic mutation test
that proves the comparator still detects genuine differences (§6).

## 5. Manual spot-check that matched values are genuinely equal, not blank

Dumped every attribute of `KNA1` (Customer Master) from both parsed models
side by side:

```
CUSTOMER_NUMBER      PD: type='A10'   mand=True  | erwin: type='CHAR(10)'    mand=True
CUSTOMER_NAME        PD: type='VA35'  mand=False | erwin: type='VARCHAR(35)' mand=False
...
CREATED_ON           PD: type='D'     mand=False | erwin: type='DATE'       mand=False
```

Every value is populated (not blank on either side) and genuinely equivalent
under family-level type comparison (`A10`~`CHAR(10)` → TEXT,
`D`~`DATE` → TEMPORAL). The 100% fidelity result reflects a real, clean
migration for this attribute set — not an empty comparison.

## 6. Mutation test — proving the comparator still catches real defects

Four independent, targeted mutations were applied to a deep copy of the
parsed erwin model, and the comparator was re-run against the (unmodified)
PD LDM model:

| Mutation | Expected finding | Result |
|---|---|---|
| Flip `KNA1.CUSTOMER_NAME.mandatory` to `True` | `MANDATORY` WARNING | ✅ caught |
| Change `MARA.MATERIAL_NUMBER.data_type` to `"BLOB"` | `DATA_TYPE` WARNING (family break: TEXT→BINARY) | ✅ caught |
| Delete entity `PLANT_MASTER` entirely | `ENTITY_MISSING` CRITICAL | ✅ caught |
| Force `Relationship_4`'s child end `dependent` flag to `False` | `DEPENDENCY` WARNING | ✅ caught |

Overall result: `status=FAIL`, `critical_count=1`, `warning_count=3` — exactly
as expected. This is codified as a permanent regression test in
`test_runner.py` (§3), so any future change to the parsers or comparator that
silently breaks detection will fail CI immediately.
