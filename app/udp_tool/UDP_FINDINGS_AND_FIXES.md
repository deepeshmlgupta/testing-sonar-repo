# UDP Framework — Findings and Fixes

## Headline

**The UDP values are populating in erwin.** I read the saved model back off disk
independently of the injector and measured **6,837 of 6,837 entity-level UDP
values present and byte-identical to PowerDesigner**. Nothing about the write
path is broken.

What was broken is that the framework **could not tell you that**, and that 38%
of what it was writing was junk. Both are fixed.

Evidence, from `output_erwin_models/SUBSURFACE AND WELLS.erwin` as shipped in the ZIP:

| Check | Result |
|---|---|
| Output model vs input model | 2,330,273 vs 2,091,663 bytes — 238KB of new data |
| UDP definitions created | 72 (36 UDPs × Entity + Model owner) |
| Non-blank manifest values present in the saved file | **9,390 / 9,390** |
| Entity-level values matching PowerDesigner exactly | **6,837 / 6,837 (100%)** |
| `injection_results.json` | **absent** — no evidence artifact was produced |

So the symptom "UDPs not populating" was real as an *observation* but the values
are in the file. Section 4 covers the most likely reason they don't appear where
you expect in the erwin UI.

---

## Finding 1 — 38% of the manifest was junk *(root cause, fixed)*

`pd_extract.py` collected every `<o:Entity>` node in the PowerDesigner XML.
PowerDesigner uses that element name for **two different things**:

```xml
<o:Entity Id="o149"> ... </o:Entity>     a real definition
<o:Entity Ref="o149"/>                   a pointer to that definition
```

The pointer form appears wherever an entity is referenced — relationship ends,
identifier member lists, diagram symbols — and is an **empty element with no
Name, no Code, no ObjectID**.

In `SUBSURFACE AND WELLS`, **1,398 of the 1,708 `<o:Entity>` nodes are pointers.**
Only 310 are real entities.

The consequence chain:

1. 1,398 phantom entities entered `entities.json` with blank names.
2. `erwin_prepare.py` gave each one the three structural UDPs → **1,398 × 3 =
   4,194 manifest rows with a blank entity name** (exactly the 4,194 measured).
3. `erwin_load.py` sends a row with no entity name to the **model root**. So
   4,194 writes all landed on one object, each overwriting the last.
4. `PD_ObjectID` on the model root ends up **blank**, because the last phantom
   row to be written carried an empty value. I confirmed this in the readback:
   the model root's `PD_ObjectID` reads as `""`.

**The fix** is in `pd_extract._defs()` — filter on the `Ref` attribute. A
definition always has `Id`, a pointer always has `Ref`, so this is exact, not a
heuristic. Every count flows through `_defs`, so one fix corrects all of them:

| Count | Before | After |
|---|---|---|
| Entity | 1,708 | **310** |
| EntityAttribute | 992 | **471** |
| Relationship | 934 | **443** |
| Identifier | 150 | **42** |
| Inheritance | 111 | **16** |
| InheritanceLink | 114 | **55** |
| Shortcut | 4,440 | **268** |
| entities_without_identifier | 1,669 | **271** |
| attributes_untyped | 554 | **33** |
| extended_attribute_values | 5,944 | 5,944 *(unchanged — no real data lost)* |

Manifest: **11,031 rows → 6,837 rows**, with **0** nameless rows.

### This also corrupted a contractual number

The counts feed `sow_classify.py`. The band stays **B4** (driven by GTL templates
and mutually-exclusive inheritance, neither affected), so nothing needs
renegotiating. But one of the knockouts in the evidence table was pure artifact:

> ~~"1,401 of 1,708 entities (82%) lack a definition. SOW B3: 'partial metadata gaps'."~~

The real figure is **3 of 310 (1%)**. That knockout no longer fires. If that
evidence table has gone to Shell, it overstates the source model's metadata gaps
by a factor of 80.

---

## Finding 2 — the framework had no way to verify itself *(fixed)*

Two separate problems.

**The read-back was circular.** `erwin_load.py` verified each write like this:

```python
prop.Value = str(val)      # write
readback = prop.Value      # "verify"
```

That reads the property just set, on the same in-memory object, in the same open
session, **before anything is saved**. It proves the session accepted the
assignment. It cannot detect a value that never reaches the file, because at that
point no file has been written. A check that shares its state with the thing it
is checking is not a check.

**And no evidence file was produced.** `injection_results.json` is absent from
the ZIP, so `udp_report.py` had nothing to report from — and `batch_run.py` did
`continue` on injection failure, skipping every later phase. The result is that a
run where 6,837 values migrated perfectly produced **no artifact saying so**.

**The fixes:**

- New `udp_readback.py` reads UDP values out of a **saved** model from a fresh
  handle — no shared state with the writer.
- `erwin_load.py` gained a post-save verification step (`STEP 5b`) that re-reads
  the saved file and re-grades every value against what is actually on disk. Each
  value now carries `persisted: yes | no | altered | blank source` alongside the
  old in-session status.
- `batch_run.py` no longer abandons a model when injection fails, and only passes
  `--results` when the injector really wrote one.

---

## Finding 3 — the SAP vs erwin comparison *(new)*

`udp_compare.py` produces one workbook per model:

```
output_excel_reports/<type>/<MODEL>_UDP_Comparison.xlsx
```

| Sheet | Contents |
|---|---|
| **Summary** | Counts by status, pass rate, verdict |
| **UDP Comparison** | **Entity Name · Applied To · UDP Name · SAP Value · erwin Value · Status · Source Path · Note** |
| **By UDP** | Per-UDP rollup, so a UDP that failed on every entity is one glance |
| **Diagnostics** | How erwin was read and whether that reading is trustworthy |

### Statuses

The three you asked for:

- **PASS** — erwin holds exactly the value PowerDesigner held
- **MISMATCH** — erwin holds a different value
- **MISSING** — PowerDesigner had a value, erwin holds nothing

Two more, needed to keep those three honest:

- **BLANK IN SAP** — the property exists on the source object but holds no value,
  so erwin holding nothing is *correct*. Counting these as MISSING would have
  invented 244 failures on this model. Excluded from the pass rate.
- **EXTRA IN ERWIN** — erwin holds a UDP value with no matching source row.
  Usually a leftover from a previous run, which matters: it is a value in a
  signed-off model that nobody can trace to a source.

---

## Finding 4 — why the UDPs may not *appear* in the erwin UI *(hypothesis, not applied)*

The values are stored. If your UDP tab still looks empty, the most likely reason
is how the definitions are **named**.

`erwin_load.py` creates each UDP with `Name = "Entity.Logical.<udp>"` **and**
separately sets `tag_Udp_Owner_Type = "Entity"`. The owner is specified twice —
once structurally, once baked into the display name. So in erwin's UDP editor the
properties are literally called `Entity.Logical.PD_ObjectID`, not `PD_ObjectID`.

**I did not change this**, because the measurement above proves the current naming
does store and retrieve values correctly, and changing a working write path on a
hypothesis I cannot test without erwin would be reckless.

Instead there is now a switch:

```bash
python erwin_load.py --udp_name_style bare   ...   # Name = "PD_ObjectID"
python erwin_load.py --udp_name_style qualified ... # current, default
```

Run it once with `bare` on a copy, then run `udp_compare.py`. If the pass rate
holds *and* the UDPs now show up correctly in the UI, switch the default. If the
pass rate drops, you have your answer in five minutes and have lost nothing.

---

## The erwin readback, and how far to trust it

`udp_readback.py` has two backends.

**`com`** — asks erwin through SCAPI. Authoritative. Needs Windows, erwin Data
Modeler and pywin32.

**`binary`** — decodes the `.erwin` file directly. No erwin needed, so it runs in
CI, and it is a *genuinely independent* check because it does not go through the
API that wrote the data. The record layout is:

```
f6 <slot:uint16> 20 41 fb 00 00 <type:uint8> 00 00 <len:uint32> <utf8>
   |             |     |
   |             |     `- 0x41 marks a UDP; built-ins use 0x40
   `- property slot id
```

with slots assigned in creation order — `1 + 6i` for the Entity owner and
`4 + 6i` for the Model owner, where `i` is the UDP's index in `udp_schema.json`.

**This container format is proprietary and undocumented.** I reverse-engineered it
against erwin build 10.10.00.38485. It is a cross-check, not a replacement for the
COM reading, and `--method auto` prefers COM.

Because a future erwin build could assign slots differently — which would line
every value up against the wrong UDP and produce thousands of *false* MISMATCH
rows — the decoder **scores itself**. It pairs decoded values against the manifest
and, below 80% agreement, declares the readback unreliable and refuses to report
mismatches at all. I verified this fires: feeding it a deliberately rotated schema
dropped confidence to 12.7% and the readback was discarded rather than reported.

A tool that knows when it cannot be trusted is worth more than one that is
confidently wrong.

---

## Testing

Everything below was run here.

| Test | Result |
|---|---|
| Ref-stub fix against the real `.ldm` | 1,708 → 310 entities; `extended_attribute_values` unchanged at 5,944 |
| Readback vs SAP manifest, real injected model | **6,840 values read, 6,840/6,840 paired values agree (100%)** |
| Decoder self-test with a deliberately rotated schema | confidence 12.7% → readback correctly discarded |
| Comparison, buggy manifest | 6,596 PASS / 244 BLANK, plus warnings naming the 4,194 nameless rows |
| Comparison, fixed manifest | 6,594 PASS / 243 BLANK, **no warnings** |
| MISSING path — compare against the un-injected input model | 6,594 MISSING, verdict `FAIL - NO VALUES MATCHED` |
| MISMATCH path — 500 SAP values tampered | **exactly 500 MISMATCH**, 6,094 PASS, verdict `REVIEW REQUIRED` |
| Workbook formulas (LibreOffice recalc) | 281 formulas, **0 errors**, Excel counts cross-checked against Python |
| `batch_run.py` end to end | Phases 1, 3, 4 complete; Phase 2 fails cleanly (no pywin32 here) and no longer aborts the model |

### Not tested

**Phase 2 injection, and the COM readback backend.** Both need erwin Data Modeler
over COM, which is not available here. The injection loop itself is unchanged
apart from the additive verification step and the naming switch. The COM backend
in `udp_readback.py` has never been executed — run it once against a known model
before relying on it, and use `--method binary` to cross-check the first time.

---

## Running it

```bash
# Full pipeline: extract, inject, report, compare
python batch_run.py

# Comparison on its own, against an already-injected model
python udp_compare.py --model_name "SUBSURFACE AND WELLS" \
    --manifest "output_jsons/SUBSURFACE AND WELLS/property_manifest.json" \
    --schema   "output_jsons/SUBSURFACE AND WELLS/udp_schema.json" \
    --erwin    "output_erwin_models/SUBSURFACE AND WELLS.erwin" \
    --method   auto \
    --outdir   output_excel_reports/ldm

# Just read erwin back and see what is in there
python udp_readback.py --erwin "output_erwin_models/SUBSURFACE AND WELLS.erwin" \
    --schema "output_jsons/SUBSURFACE AND WELLS/udp_schema.json" \
    --manifest "output_jsons/SUBSURFACE AND WELLS/property_manifest.json"
```

## Files

| File | Status |
|---|---|
| `udp_readback.py` | **new** — read UDP values out of a saved erwin model |
| `udp_compare.py` | **new** — SAP vs erwin comparison + per-model Excel report |
| `pd_extract.py` | **fixed** — `_defs()` skips `Ref` pointer stubs |
| `erwin_load.py` | **fixed** — post-save verification; `--udp_name_style`; `--no_verify` |
| `batch_run.py` | **fixed** — Phase 4 comparison; no longer aborts a model on injection failure |
| `udp_report.py`, `erwin_prepare.py`, `sow_classify.py` | unchanged |
