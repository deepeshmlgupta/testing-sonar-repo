#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migration_audit.py
==================

The provenance ledger for the migration platform.

For every model, for every step of the pipeline, this answers three questions
that nothing else in the framework answers:

    1. Did it happen?
    2. WHO or WHAT did it -- the framework, a script, or a human?
    3. What is the evidence, and when?

WHY IT MATTERS
--------------
This pipeline is a HYBRID. erwin's OEM licence blocks headless automation of
the MIT Bridge, so the erwin import and the XML export are done by hand, in
the erwin UI, by a person. Everything downstream is automated. A fidelity
score of 98% therefore does not mean "the platform migrated this model" -- it
means "a human did two steps correctly and then the platform measured the
result". An auditor needs to be able to tell those apart, and so does anyone
debugging a bad run.

PROVENANCE TAGS
---------------
    AUTOMATED         the framework did this with no human involvement
    SCRIPT            a script in this repo produced the artefact
    MANUAL            a person had to do this in erwin or PowerDesigner
    NOT_IMPLEMENTED   the step exists in the design but not in the code

OUTCOME TAGS
------------
    DONE      completed, evidence on disk
    PENDING   required, not yet done -- this is what blocks the model
    FAILED    attempted and did not succeed
    N/A       does not apply to this model

HOW IT WORKS
------------
It is a SCANNER, not instrumentation. It reads the filesystem and the model
files and infers state from evidence, so it works on runs that happened before
this script existed, and it cannot drift out of sync with the other tools by
being forgotten in a code path.

Every run appends to an append-only history at
`batch_summary/audit/ledger.jsonl`, so you keep a record of what the state was
each time the audit ran -- not just the state now.

USAGE
-----
    python app/reporting/migration_audit.py

No arguments needed. Add --note "text" to stamp a run with a comment.

Output:
    batch_summary/audit/migration_audit_report.xlsx   the report
    batch_summary/audit/ledger.jsonl                  append-only history
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import xml.etree.ElementTree as ET  # nosec B405
from defusedxml.ElementTree import parse as safe_parse, iterparse as safe_iterparse
from collections import Counter, OrderedDict

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# --------------------------------------------------------------------------
# Anchored to this file, so the Run button and the terminal behave alike.
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, os.pardir, os.pardir))

SAP_DIRS = {
    ".ldm": os.path.join(PROJECT_ROOT, "sappdmodels", "ldm"),
    ".cdm": os.path.join(PROJECT_ROOT, "sappdmodels", "cdm"),
    ".pdm": os.path.join(PROJECT_ROOT, "sappdmodels", "pdm"),
}
INITIAL_ERWIN = os.path.join(PROJECT_ROOT, "erwinmodels", "1_initial", "erwin")
INITIAL_XML = os.path.join(PROJECT_ROOT, "erwinmodels", "1_initial", "xml")
PREPROCESSED_XML = os.path.join(PROJECT_ROOT, "erwinmodels", "2_preprocessed", "xml")
FINAL_ERWIN = os.path.join(PROJECT_ROOT, "erwinmodels", "3_final", "final_erwin")
FINAL_XML = os.path.join(PROJECT_ROOT, "erwinmodels", "3_final", "final_xml")
REPORT_DIRS = {
    ".ldm": os.path.join(PROJECT_ROOT, "app", "reporting", "ldm_reports"),
    ".cdm": os.path.join(PROJECT_ROOT, "app", "reporting", "cdm_reports"),
    ".pdm": os.path.join(PROJECT_ROOT, "app", "reporting", "pdm_reports"),
}

def field_map_report_for(suffix):
    """Each model type gets its own field-mapping workbook, beside its report."""
    return os.path.join(REPORT_DIRS.get(suffix, REPORT_DIRS[".ldm"]),
                        "field_mapping_report.xlsx")


# Kept for callers that still expect the old module-level constant.
FIELD_MAP_REPORT = os.path.join(PROJECT_ROOT, "app", "reporting",
                                "ldm_reports", "field_mapping_report.xlsx")
SUMMARY_REPORT = os.path.join(PROJECT_ROOT, "batch_summary", "summary_report",
                              "Pass_Fail_Summary.xlsx")
AUDIT_DIR = os.path.join(PROJECT_ROOT, "batch_summary", "audit")
LEDGER_PATH = os.path.join(AUDIT_DIR, "ledger.jsonl")
TIMINGS_PATH = os.path.join(AUDIT_DIR, "timings.jsonl")
REPORT_PATH = os.path.join(AUDIT_DIR, "migration_audit_report.xlsx")

# Provenance = WHO ran it. The axis is "what do YOU have to type", because that
# is the only distinction that changes what a reader has to do:
#   PIPELINE  runs inside `py -m app.main` - one command covers it
#   SCRIPT    a separate script you invoke yourself, by name
#   MANUAL    a person, in the erwin or PowerDesigner UI
# Everything in the first two is code; calling one "AUTOMATED" and the other
# "SCRIPT" implied the reconciliation was not a script, which was misleading.
PIPELINE, SCRIPT, MANUAL, NOT_IMPL = ("PIPELINE", "SCRIPT", "MANUAL",
                                      "NOT_IMPLEMENTED")
AUTOMATED = PIPELINE          # backwards-compatible alias
DONE, PENDING, FAILED, NA = "DONE", "PENDING", "FAILED", "N/A"

# Provenance answers WHO, outcome answers WHETHER -- so "MANUAL / DONE" means
# "a person had to do this, and they did". That reads as a contradiction at a
# glance, so every row also carries this plain-language combination, which is
# the column to look at first.
STATUS_TEXT = {
    (MANUAL, DONE):       "Done by a person",
    (MANUAL, PENDING):    "WAITING ON A PERSON",
    (MANUAL, FAILED):     "Person attempted, failed",
    (MANUAL, NA):         "Not applicable",
    (PIPELINE, DONE):     "Ran in the pipeline (py -m app.main)",
    (PIPELINE, PENDING):  "PIPELINE NOT RUN for this model",
    (PIPELINE, FAILED):   "PIPELINE STEP FAILED",
    (PIPELINE, NA):       "Not applicable",
    (SCRIPT, DONE):       "Ran as a separate script",
    (SCRIPT, PENDING):    "SEPARATE SCRIPT NOT RUN",
    (SCRIPT, FAILED):     "SEPARATE SCRIPT FAILED",
    (SCRIPT, NA):         "Not applicable",
    (NOT_IMPL, NA):       "Not implemented in the code",
}
STATUS_FILL = {
    "Done by a person":                    PatternFill("solid", fgColor="FFF2CC"),
    "WAITING ON A PERSON":                 PatternFill("solid", fgColor="FFC7CE"),
    "Person attempted, failed":            PatternFill("solid", fgColor="FFC7CE"),
    "Ran in the pipeline (py -m app.main)": PatternFill("solid", fgColor="C6EFCE"),
    "PIPELINE NOT RUN for this model":     PatternFill("solid", fgColor="FFC7CE"),
    "PIPELINE STEP FAILED":                PatternFill("solid", fgColor="FFC7CE"),
    "Ran as a separate script":            PatternFill("solid", fgColor="DDEBF7"),
    "SEPARATE SCRIPT NOT RUN":             PatternFill("solid", fgColor="FFC7CE"),
    "SEPARATE SCRIPT FAILED":              PatternFill("solid", fgColor="FFC7CE"),
    "Not implemented in the code": PatternFill("solid", fgColor="E7E6E6"),
    "Not applicable":              PatternFill("solid", fgColor="E7E6E6"),
}


def status_text(prov, outcome):
    return STATUS_TEXT.get((prov, outcome), "%s / %s" % (prov, outcome))

HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(bold=True, color="FFFFFF")
PROV_FILL = {
    PIPELINE: PatternFill("solid", fgColor="C6EFCE"),
    SCRIPT:    PatternFill("solid", fgColor="DDEBF7"),
    MANUAL:    PatternFill("solid", fgColor="FFF2CC"),
    NOT_IMPL:  PatternFill("solid", fgColor="E7E6E6"),
}
OUTCOME_FILL = {
    DONE:    PatternFill("solid", fgColor="C6EFCE"),
    PENDING: PatternFill("solid", fgColor="FFC7CE"),
    FAILED:  PatternFill("solid", fgColor="FFC7CE"),
    NA:      PatternFill("solid", fgColor="E7E6E6"),
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def stat(path):
    """-> (exists, iso timestamp, bytes)"""
    if not path or not os.path.isfile(path):
        return False, "", 0
    st = os.stat(path)
    ts = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return True, ts, st.st_size


def rel(path):
    try:
        return os.path.relpath(path, PROJECT_ROOT)
    except ValueError:
        return path


def localname(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def pd_authors(ldm_path):
    """
    Who touched the PowerDesigner model, from the model itself.

    PD stamps Creator / Modifier on every object. This is the only reliable
    record of the humans behind the MANUAL steps, so it is worth surfacing.
    """
    try:
        root = safe_parse(ldm_path).getroot()
    except Exception:
        return "", ""
    A = "{attribute}"
    creators, modifiers = Counter(), Counter()
    for el in root.iter():
        if localname(el.tag) == "Creator" and el.tag.startswith(A) and el.text:
            creators[el.text.strip()] += 1
        elif localname(el.tag) == "Modifier" and el.tag.startswith(A) and el.text:
            modifiers[el.text.strip()] += 1
    top = lambda c: ", ".join(n for n, _ in c.most_common(3))
    return top(creators), top(modifiers)


# ---------------------------------------------------------------------------
# Object kinds, per model type.
#
# A conceptual or logical model is made of Entities, Attributes and
# Relationships; a PHYSICAL model is made of Tables, Columns and References.
# PowerDesigner uses different XML tags for each, so a single hard-coded kind
# list silently reported "0 objects" for every .pdm -- which read as "there is
# nothing to migrate" when in fact nothing had been looked at.
#
# On the erwin side all three types land in the same physical metamodel
# (Entity / Attribute / Relationship), so only the SAP side varies; the
# erwin_props entry maps each kind onto the props block that holds its text.
# ---------------------------------------------------------------------------
KIND_SPECS = {
    ".ldm": (("Entity",       "Entity",           "EntityProps"),
             ("Attribute",    "EntityAttribute",  "AttributeProps"),
             ("Relationship", "Relationship",     "RelationshipProps")),
    ".cdm": (("Entity",       "Entity",           "EntityProps"),
             ("Attribute",    "EntityAttribute",  "AttributeProps"),
             ("Relationship", "Relationship",     "RelationshipProps")),
    ".pdm": (("Table",        "Table",            "EntityProps"),
             ("Column",       "Column",           "AttributeProps"),
             ("Reference",    "Reference",        "RelationshipProps")),
}

# Default for callers that predate the per-type split.
PD_KINDS = ("Entity", "Attribute", "Relationship")
PD_TAG = {"Entity": "Entity", "Attribute": "EntityAttribute",
          "Relationship": "Relationship"}


def kinds_for(suffix):
    """(kind, pd_tag, erwin_props) triples for one model type."""
    return KIND_SPECS.get(suffix, KIND_SPECS[".ldm"])


def kind_names(suffix):
    return tuple(k for k, _t, _p in kinds_for(suffix))


def latest_timings():
    """
    The most recent record written by benchmark.py, or None.

    Timing lives in its own append-only file rather than being measured here,
    because an audit must be cheap to run and must not re-parse 22 MB of XML
    just to produce a ledger. Run benchmark.py when you want fresh numbers.
    """
    if not os.path.isfile(TIMINGS_PATH):
        return None
    last = None
    try:
        with open(TIMINGS_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = json.loads(line)
    except Exception:
        return None
    return last


def pd_text_inventory(ldm_path, suffix=".ldm"):
    """
    Per-object-kind census of the SOURCE model.

    Returns {kind: {"total": n, "comment": n, "definition": n}}, where the
    kinds are Entity/Attribute/Relationship for a conceptual or logical model
    and Table/Column/Reference for a physical one.

    Per-kind rather than one aggregate, because "22 Comments" tells you
    nothing about WHERE the gap is: 22 entity comments with zero on the
    relationships is a completely different situation from an even spread,
    and only the breakdown distinguishes them.
    """
    specs = kinds_for(suffix)
    inv = OrderedDict((k, {"total": 0, "comment": 0, "definition": 0})
                      for k, _t, _p in specs)
    try:
        root = safe_parse(ldm_path).getroot()
    except Exception:
        return inv
    A, O = "{attribute}", "{object}"
    for kind, pd_tag, _props in specs:
        for el in root.iter(O + pd_tag):
            if el.get("Id") is None:            # Ref= pointers are not objects
                continue
            inv[kind]["total"] += 1
            # PowerDesigner writes some scalars class-qualified
            # (<a:Table.Comment>), so both spellings have to be accepted or a
            # populated field reads as empty.
            if _pd_text(el, "Comment"):
                inv[kind]["comment"] += 1
            if _pd_text(el, "Description") or _pd_text(el, "Annotation"):
                inv[kind]["definition"] += 1
    return inv


def _pd_text(el, name):
    """Text of a PD scalar field, plain or class-qualified."""
    A = "{attribute}"
    suffix = "." + name
    for child in list(el):
        tag = child.tag
        if isinstance(tag, str) and tag.startswith(A):
            local = tag[len(A):]
            if local == name or local.endswith(suffix):
                if child.text and child.text.strip():
                    return child.text.strip()
    return ""


ER_PROPS = {"Entity": "EntityProps", "Attribute": "AttributeProps",
            "Relationship": "RelationshipProps"}


def erwin_text_inventory(xml_path, suffix=".ldm"):
    """
    The same census on the erwin side: how many objects carry a Note or a
    Definition, per kind. Put beside the PD figures this shows exactly what
    crossed and what did not.

    The kind LABELS follow the SAP side (Table/Column/Reference for a physical
    model) while the props blocks read are erwin's own
    Entity/Attribute/Relationship — erwin holds all three model types in one
    physical metamodel, so a .pdm's tables live in <EntityProps>.
    """
    specs = kinds_for(suffix)
    inv = OrderedDict((k, {"total": 0, "note": 0, "definition": 0})
                      for k, _t, _p in specs)
    if not xml_path or not os.path.isfile(xml_path):
        return inv
    try:
        with open(xml_path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except Exception:
        return inv
    for kind, _pd_tag, props in specs:
        for block in re.findall(r"<%s>(.*?)</%s>" % (props, props), raw, re.S):
            inv[kind]["total"] += 1
            if re.search(r"<Note_List\b", block):
                inv[kind]["note"] += 1
            if re.search(r"<Definition\b[^>]*>(?!\s*</Definition>)", block):
                inv[kind]["definition"] += 1
    return inv


def totals(inv, key):
    return sum(v[key] for v in inv.values())


def erwin_export_origin(xml_path):
    """
    Which machine produced the erwin export.

    erwin writes a <Locator> holding the local path of the .erwin file it was
    saved from. On a shared project that reveals WHOSE manual export this is --
    which matters when one person's model is validated against another's export.
    """
    try:
        with open(xml_path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(8000)
    except Exception:
        return ""
    m = re.search(r"<Locator>(.*?)</Locator>", head, re.S)
    return m.group(1).strip() if m else ""


def notes_stats(xml_path):
    """Count migrated Notes and Definitions in an erwin export."""
    try:
        with open(xml_path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except Exception:
        return 0, 0, ""
    notes = len(re.findall(r"<Note_List\b", raw))
    defs = len(re.findall(r"<Definition\b[^>]*>(?!\s*</Definition>)", raw))
    author = ""
    m = re.search(r"<Note_List\b[^>]*>(.*?)</Note_List>", raw, re.S)
    if m:
        fields = m.group(1).split(r"\#x1F")
        if len(fields) > 2:
            author = fields[2]
    return notes, defs, author


def read_validation(report_path, model_name):
    """Pull status / fidelity for one model out of the detailed xlsx report."""
    ok, _, _ = stat(report_path)
    if not ok:
        return None
    try:
        import openpyxl
        wb = openpyxl.load_workbook(report_path, data_only=True, read_only=True)
        if "SUMMARY" not in wb.sheetnames:
            return None
        ws = wb["SUMMARY"]
        rows = list(ws.iter_rows(values_only=True))
        header = None
        for r in rows:
            if r and r[0] == "#":
                header = list(r)
                continue
            if header and r and str(r[0]).isdigit():
                rec = dict(zip(header, r))
                if model_name in str(rec.get("SAP PD Model", "")) or \
                   model_name in str(rec.get("SAP PD File", "")):
                    return {"status": rec.get("Status"),
                            "fidelity": rec.get("Fidelity %"),
                            "critical": rec.get("CRITICAL"),
                            # Present only in the PDM report, whose promotion
                            # is gated on a measured fidelity target.
                            "stage": rec.get("Stage"),
                            "promoted": rec.get("Promoted?")}
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# the step table -- this IS the audit definition
# --------------------------------------------------------------------------

def build_steps(ctx):
    """
    Return the ordered list of step records for one model.

    Each record carries a provenance tag (who did it) and an outcome tag (did
    it happen), so the report can be filtered either way.

    The first four steps are the same for every model type — a person authors
    the SAP model and performs the two erwin steps erwin's OEM licence forbids
    automating — and the pipeline half then differs: a .pdm runs through
    validate -> remediate -> re-validate -> promotion gate, while a .ldm/.cdm
    runs Comment->Note preprocessing and a single reconciliation.
    """
    S = []

    def add(step, phase, prov, outcome, detail, evidence="", when="", size=0):
        S.append({
            "step": step, "phase": phase, "provenance": prov,
            "outcome": outcome, "detail": detail,
            "evidence": rel(evidence) if evidence else "",
            "when": when, "bytes": size,
        })

    label_for = {".pdm": "SAP PD physical model (.pdm)",
                 ".cdm": "SAP PD conceptual model (.cdm)"}.get(
                     ctx["suffix"], "SAP PD logical model (.ldm)")

    # ---- 1. author the source model ------------------------------------
    creators, modifiers = ctx["authors"]
    add("Author SAP PD model", "Source", MANUAL, DONE,
        "%s modelled by hand in PowerDesigner. Creator(s): %s. Modifier(s): %s"
        % (label_for, creators or "unknown", modifiers or "unknown"),
        ctx["ldm"], ctx["ldm_when"], ctx["ldm_size"])

    # ---- 2. Phase A ------------------------------------------------------
    add("Phase A - extract UDPs", "A", NOT_IMPL, NA,
        "Removed from main.py ('completely removed per user request'). No UDP "
        "extraction happens, so custom properties are not carried across.")

    # ---- 3/4. the manual erwin steps -------------------------------------
    for label, path, when, size, kind in (
        ("Import into erwin (.erwin)", ctx["erwin"], ctx["erwin_when"],
         ctx["erwin_size"], "erwin"),
        ("Export erwin XML (.xml)", ctx["xml"], ctx["xml_when"],
         ctx["xml_size"], "xml"),
    ):
        if path and os.path.isfile(path):
            extra = ""
            if kind == "xml" and ctx["origin"]:
                extra = " Saved from: %s" % ctx["origin"]
            stale = ""
            if kind == "xml" and ctx["stale_hours"] is not None and ctx["stale_hours"] > 0:
                stale = (" WARNING: the .ldm is %.1f h NEWER than this export, "
                         "so any score computed from it is out of date."
                         % ctx["stale_hours"])
            add(label, "B", MANUAL, DONE,
                "Done by hand in the erwin UI - erwin's OEM licence blocks "
                "headless MIT Bridge automation.%s%s" % (extra, stale),
                path, when, size)
        else:
            add(label, "B", MANUAL, PENDING,
                "REQUIRED AND NOT DONE. A person must open erwin, import the "
                "%s and save this file. The pipeline cannot proceed past "
                "Phase B without it." % ctx["suffix"],
                ctx["expect_%s" % kind])

    # ---- 5. Phase B gate -------------------------------------------------
    # The XML is what every downstream step reads; the .erwin is only the
    # promotion copy. Reporting the gate as failed when just the binary is
    # absent told you nothing could run, while reconciliation ran fine.
    xml_ok = os.path.isfile(ctx["xml"] or "")
    both = xml_ok and os.path.isfile(ctx["erwin"] or "")
    add("Phase B - import gate", "B", AUTOMATED, DONE if xml_ok else FAILED,
        "GATE, not an import: erwin_importer.py only checks that the "
        "hand-made files exist, then stands aside and lets the pipeline "
        "continue past Phase B. It imports nothing. %s"
        % ("Both files present, gate passed." if both else
           ("XML present, gate passed; the .erwin binary is absent so only "
            "the promotion copy is affected." if xml_ok else
            "XML missing, gate failed - nothing downstream can run.")))

    if ctx["suffix"] == ".pdm":
        _pdm_steps(ctx, add, xml_ok)
        return S

    # ---- 6/7. the script-generated enrichment ---------------------------
    src_comments = totals(ctx["pd_inv"], "comment")
    src_defs = totals(ctx["pd_inv"], "definition")

    def per_kind(inv, key):
        return ", ".join("%s %d" % (k, v[key]) for k, v in inv.items() if v[key])
    kinds_text = " or ".join(kind_names(ctx["suffix"]))
    census = ", ".join("%s %d objects" % (k, v["total"])
                       for k, v in ctx["pd_inv"].items())
    via = ctx["notes_via"]          # PIPELINE when main.py ran it, else SCRIPT
    ran_by = ("app/main.py's preprocessing orchestrator"
              if via == PIPELINE else "pd_comment_to_erwin_note.py, run by hand")

    if ctx["notes_xml"]:
        n, d, author = ctx["notes_counts"]

        if src_comments == 0:
            add("Comment -> Note migration", "C", via, NA,
                "Nothing to migrate: no %s in the SAP PD model carries a "
                "Comment (checked %s)." % (kinds_text, census),
                ctx["notes_xml"], ctx["notes_when"], ctx["notes_size"])
        else:
            add("Comment -> Note migration", "C", via,
                DONE if n else PENDING,
                "%s wrote %d Note record(s) from %d PD Comment(s). Source by "
                "kind: %s. erwin now carries Notes on: %s. Note author "
                "recorded as %r."
                % (ran_by, n, src_comments,
                   per_kind(ctx["pd_inv"], "comment") or "none",
                   per_kind(ctx["er_inv"], "note") or "none",
                   author or "unset") if n else
                "The model has %d Comment(s) but the output has no Notes."
                % src_comments,
                ctx["notes_xml"], ctx["notes_when"], ctx["notes_size"])

        if src_defs == 0:
            add("Definition -> Definition migration", "C", via, NA,
                "Nothing to migrate: no %s in the SAP PD model has text in "
                "its Definition tab (checked %s). Fill the Definition tab in "
                "PowerDesigner if erwin Definitions are expected."
                % (kinds_text, census),
                ctx["notes_xml"], ctx["notes_when"], ctx["notes_size"])
        else:
            add("Definition -> Definition migration", "C", via,
                DONE if d else PENDING,
                "Wrote %d Definition(s) from %d in the source. Source by kind: "
                "%s. erwin now carries Definitions on: %s."
                % (d, src_defs, per_kind(ctx["pd_inv"], "definition") or "none",
                   per_kind(ctx["er_inv"], "definition") or "none") if d else
                "The model has %d Definition(s) but none reached erwin - the "
                "script was run WITHOUT --definitions." % src_defs,
                ctx["notes_xml"], ctx["notes_when"], ctx["notes_size"])
    else:
        expected = os.path.join(PREPROCESSED_XML, ctx["base"] + ".xml")
        add("Comment -> Note migration", "C", PIPELINE,
            NA if src_comments == 0 else PENDING,
            "Nothing to migrate: the model has no Comments."
            if src_comments == 0 else
            "Not run. The model has %d Comment(s) waiting to migrate. "
            "`py -m app.main` runs this as Phase C." % src_comments, expected)
        add("Definition -> Definition migration", "C", PIPELINE,
            NA if src_defs == 0 else PENDING,
            "Nothing to migrate: the model has no Definitions."
            if src_defs == 0 else
            "Not run. The model has %d Definition(s) waiting to migrate."
            % src_defs, expected)

    # ---- 8. Phase C ------------------------------------------------------
    add("Phase C - inject UDPs", "C", NOT_IMPL, NA,
        "Removed from main.py. UDP injection does not run.")

    # ---- 9. Phase D ------------------------------------------------------
    v = ctx["validation"]
    if v:
        add("Phase D - reconciliation", "D", AUTOMATED, DONE,
            "Compared SAP PD against the erwin XML. Status %s, fidelity %s%%, "
            "%s critical finding(s)."
            % (v.get("status"), v.get("fidelity"), v.get("critical")),
            ctx["detail_report"], ctx["detail_when"], ctx["detail_size"])
    else:
        add("Phase D - reconciliation", "D", AUTOMATED,
            PENDING if both else NA,
            "No entry for this model in the detailed validation report."
            if both else
            "Cannot run: Phase B gate not satisfied.",
            ctx["detail_report"])

    # ---- 10. reports -----------------------------------------------------
    _report_steps(ctx, add)

    # ---- 11. promotion ---------------------------------------------------
    promoted_path = promoted_artefact(ctx["base"])
    passed = bool(v and str(v.get("status")).upper() == "PASS"
                  and str(v.get("critical")) in ("0", "0.0", "None"))
    add("Promote to 3_final", "Gate", AUTOMATED,
        DONE if promoted_path else (PENDING if passed else NA),
        "Copied into erwinmodels/3_final - this is the blessed output."
        if promoted_path else
        ("Eligible but not copied." if passed else
         "Not eligible. main.py copies a model into 3_final only when status is "
         "PASS with zero criticals; anything else stays put."),
        promoted_path or os.path.join(FINAL_XML, ctx["base"] + ".xml"))
    return S


def _report_steps(ctx, add):
    """
    The reporting artefacts, which are the same question for every model type
    but live in a different folder per type.
    """
    for label, path, prov in (
            ("Detailed validation report", ctx["detail_report"], PIPELINE),
            ("Pass/Fail summary", SUMMARY_REPORT, PIPELINE),
            ("Field mapping report", field_map_report_for(ctx["suffix"]), SCRIPT)):
        ok, when, size = stat(path)
        detail = ("Generated by the framework." if prov == PIPELINE
                  else "Generated by a separate script.")
        add(label, "Reporting", prov, DONE if ok else PENDING,
            detail if ok else
            ("Not generated yet — run `py -m app.main`." if prov == PIPELINE else
             "Not generated yet — run the script by name."),
            path, when, size)

    # This report itself is DONE by construction: the run producing the row is
    # the run writing the file. Checking the disk here made the first audit of
    # a fresh checkout report ITSELF as a blocker.
    add("Provenance audit (this report)", "Reporting", SCRIPT, DONE,
        "Written by this audit run.", REPORT_PATH)


def _pdm_steps(ctx, add, xml_ok):
    """
    The pipeline half of a PHYSICAL model, which is a different shape from the
    conceptual/logical one: there is no Comment->Note preprocessing step, and
    promotion is gated on a MEASURED fidelity target rather than on PASS.

        pass 1 validate  ->  below target?  ->  remediate (columns / PKs / FK
        joins from PowerDesigner)  ->  pass 2 re-validate  ->  promote only at
        the target, otherwise held in 2_preprocessed.
    """
    v = ctx["validation"] or {}
    stage = str(v.get("stage") or "").strip()
    promoted_flag = str(v.get("promoted") or "").strip().upper() == "YES"
    remediated = bool(ctx["notes_xml"])          # 2_preprocessed/xml/<base>.xml

    # ---- pass 1: validate the raw import --------------------------------
    if v:
        add("Phase D - validate 1_initial", "D", PIPELINE, DONE,
            "pdm_flow validated the raw erwin export against the .pdm. "
            "The report holds the FINAL result (status %s, fidelity %s%%, %s "
            "critical); pass-1 numbers are in the run log."
            % (v.get("status"), v.get("fidelity"), v.get("critical")),
            ctx["detail_report"], ctx["detail_when"], ctx["detail_size"])
    else:
        add("Phase D - validate 1_initial", "D", PIPELINE,
            PENDING if xml_ok else NA,
            "No entry for this model in the PDM validation report."
            if xml_ok else "Cannot run: Phase B gate not satisfied.",
            ctx["detail_report"])

    # ---- remediation ----------------------------------------------------
    if remediated:
        add("Remediation (preprocessing)", "C", PIPELINE, DONE,
            "pdm_preprocessor rewrote the erwin XML from the PowerDesigner "
            "source: missing columns restored, empty primary keys re-linked "
            "and broken foreign-key joins repaired. It never invents data to "
            "raise a score, so legitimate modelling differences and defects "
            "in the source model stay in the report.",
            ctx["notes_xml"], ctx["notes_when"], ctx["notes_size"])
        add("Phase D - re-validate 2_preprocessed", "D", PIPELINE,
            DONE if v else PENDING,
            "The remediated XML was re-parsed from disk and scored again — "
            "promotion follows a MEASURED result, never what preprocessing "
            "believes it changed. Final status %s, fidelity %s%%."
            % (v.get("status"), v.get("fidelity")) if v else
            "Remediated XML exists but no result was recorded.",
            ctx["notes_xml"], ctx["notes_when"], ctx["notes_size"])
    else:
        expected = os.path.join(PREPROCESSED_XML, ctx["base"] + ".xml")
        at_target = bool(v) and str(v.get("stage") or "") == "3_final"
        add("Remediation (preprocessing)", "C", PIPELINE,
            NA if at_target else (PENDING if xml_ok else NA),
            "Not needed: the model met the fidelity target on import."
            if at_target else
            ("Not run. `py -m app.main` remediates a PDM that falls short of "
             "the target (see PDM_FIDELITY_TARGET in app/config/settings.py)."
             if xml_ok else "Cannot run: Phase B gate not satisfied."),
            expected)
        add("Phase D - re-validate 2_preprocessed", "D", PIPELINE, NA,
            "Only runs when remediation runs.", expected)

    # ---- reports --------------------------------------------------------
    _report_steps(ctx, add)

    # ---- promotion gate -------------------------------------------------
    promoted_path = promoted_artefact(ctx["base"])
    if promoted_path or promoted_flag:
        add("Promote to 3_final", "Gate", PIPELINE, DONE,
            "Fidelity target met on a measured score; copied into "
            "erwinmodels/3_final - this is the blessed output.",
            promoted_path or os.path.join(FINAL_XML, ctx["base"] + ".xml"))
    else:
        add("Promote to 3_final", "Gate", PIPELINE, NA,
            "Not promoted. The model measured %s%% and is held in %s for "
            "review. This is the gate working, not failing: the remaining "
            "findings are modelling differences or defects in the source "
            "model, and inventing data to clear them would corrupt the "
            "physical model."
            % (v.get("fidelity", "?"), stage or "2_preprocessed"),
            os.path.join(FINAL_XML, ctx["base"] + ".xml"))


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------

def promoted_artefact(base):
    """
    The promoted copy of a model, or "" when it was never promoted.

    3_final has been laid out two ways across versions of main.py
    (final_erwin/final_xml, and plain erwin/xml), so both are checked — a
    promotion reported as "not done" because the folder was renamed is worse
    than no report at all.
    """
    final_root = os.path.join(PROJECT_ROOT, "erwinmodels", "3_final")
    for folder, ext in (("final_erwin", ".erwin"), ("erwin", ".erwin"),
                        ("final_xml", ".xml"), ("xml", ".xml")):
        path = os.path.join(final_root, folder, base + ext)
        if os.path.isfile(path):
            return path
    return ""


def discover_models():
    found = []
    for suffix, d in SAP_DIRS.items():
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(suffix):
                found.append((suffix, os.path.join(d, f)))
    return found


def scan_model(suffix, ldm_path):
    base = os.path.splitext(os.path.basename(ldm_path))[0]
    _, ldm_when, ldm_size = stat(ldm_path)

    erwin = os.path.join(INITIAL_ERWIN, base + ".erwin")
    xml = os.path.join(INITIAL_XML, base + ".xml")
    erwin_ok, erwin_when, erwin_size = stat(erwin)
    xml_ok, xml_when, xml_size = stat(xml)

    stale_hours = None
    if xml_ok:
        stale_hours = (os.stat(ldm_path).st_mtime - os.stat(xml).st_mtime) / 3600.0

    # Which preprocessed file exists tells you WHO produced it: main.py's
    # orchestrator writes <base>.xml, while running the comment script by hand
    # writes <base>_notes.xml. That is the difference between "one command did
    # this" and "someone remembered to run a second tool".
    notes_xml, notes_via = "", PIPELINE
    for cand, via in ((base + ".xml", PIPELINE),
                      (base + "_notes.xml", SCRIPT)):
        p = os.path.join(PREPROCESSED_XML, cand)
        if os.path.isfile(p):
            notes_xml, notes_via = p, via
            break
    _, notes_when, notes_size = stat(notes_xml)

    detail_report = os.path.join(REPORT_DIRS.get(suffix, ""),
                                 "%s_validation_report.xlsx" % suffix.lstrip("."))
    _, detail_when, detail_size = stat(detail_report)

    ctx = {
        "base": base, "suffix": suffix,
        "ldm": ldm_path, "ldm_when": ldm_when, "ldm_size": ldm_size,
        # Creator / Modifier are stamped by PowerDesigner on every model type,
        # so every type can name the people behind its MANUAL steps.
        "authors": pd_authors(ldm_path),
        "erwin": erwin if erwin_ok else "", "erwin_when": erwin_when,
        "erwin_size": erwin_size, "expect_erwin": erwin,
        "xml": xml if xml_ok else "", "xml_when": xml_when,
        "xml_size": xml_size, "expect_xml": xml,
        "origin": erwin_export_origin(xml) if xml_ok else "",
        "stale_hours": stale_hours,
        "notes_xml": notes_xml, "notes_when": notes_when,
        "notes_size": notes_size, "notes_via": notes_via,
        "notes_counts": notes_stats(notes_xml) if notes_xml else (0, 0, ""),
        "detail_report": detail_report, "detail_when": detail_when,
        "detail_size": detail_size,
        "validation": read_validation(detail_report, base),
        "pd_inv": pd_text_inventory(ldm_path, suffix),
        "er_inv": erwin_text_inventory(notes_xml or xml, suffix),
        "kinds": kind_names(suffix),
    }
    return ctx, build_steps(ctx)


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def style(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for cell in ws[1]:
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def write_report(scanned, run_ts, note):
    wb = Workbook()
    wb.remove(wb.active)

    # ---- STEP_LEDGER -----------------------------------------------------
    ws = wb.create_sheet("STEP_LEDGER")
    ws.append(["Model", "Type", "Phase", "Step", "What happened",
               "Provenance", "Outcome",
               "Detail", "Evidence file", "Evidence timestamp", "Bytes"])
    for ctx, steps in scanned:
        for s in steps:
            ws.append([ctx["base"], ctx["suffix"], s["phase"], s["step"],
                       status_text(s["provenance"], s["outcome"]),
                       s["provenance"], s["outcome"], s["detail"],
                       s["evidence"], s["when"], s["bytes"]])
    for row in ws.iter_rows(min_row=2, min_col=5, max_col=7):
        if row[0].value in STATUS_FILL:
            row[0].fill = STATUS_FILL[row[0].value]
        if row[1].value in PROV_FILL:
            row[1].fill = PROV_FILL[row[1].value]
        if row[2].value in OUTCOME_FILL:
            row[2].fill = OUTCOME_FILL[row[2].value]
    for row in ws.iter_rows(min_row=2, min_col=8, max_col=9):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    style(ws, [30, 8, 10, 32, 26, 17, 10, 70, 46, 20, 11])

    # ---- MANUAL_ACTIONS --------------------------------------------------
    ws = wb.create_sheet("MANUAL_ACTIONS")
    ws.append(["Model", "Step", "What happened", "What the person must do / did",
               "File", "Timestamp"])
    for ctx, steps in scanned:
        for s in steps:
            if s["provenance"] == MANUAL:
                ws.append([ctx["base"], s["step"],
                           status_text(s["provenance"], s["outcome"]),
                           s["detail"], s["evidence"], s["when"]])
    for row in ws.iter_rows(min_row=2, min_col=3, max_col=3):
        if row[0].value in STATUS_FILL:
            row[0].fill = STATUS_FILL[row[0].value]
    for row in ws.iter_rows(min_row=2, min_col=4, max_col=4):
        row[0].alignment = Alignment(vertical="top", wrap_text=True)
    style(ws, [30, 30, 26, 80, 46, 20])

    # ---- BLOCKERS --------------------------------------------------------
    ws = wb.create_sheet("BLOCKERS")
    ws.append(["Model", "Step", "Provenance", "Why it is blocking", "Expected file"])
    for ctx, steps in scanned:
        for s in steps:
            if s["outcome"] in (PENDING, FAILED):
                ws.append([ctx["base"], s["step"], s["provenance"],
                           s["detail"], s["evidence"]])
    for row in ws.iter_rows(min_row=2, min_col=4, max_col=4):
        row[0].alignment = Alignment(vertical="top", wrap_text=True)
    style(ws, [30, 32, 17, 86, 46])

    # ---- ARTEFACTS -------------------------------------------------------
    ws = wb.create_sheet("ARTEFACTS")
    ws.append(["Model", "Artefact", "Produced by", "Path", "Timestamp", "Bytes"])
    for ctx, steps in scanned:
        for s in steps:
            if s["evidence"] and s["outcome"] == DONE:
                ws.append([ctx["base"], s["step"], s["provenance"],
                           s["evidence"], s["when"], s["bytes"]])
    style(ws, [30, 32, 17, 52, 20, 12])

    # ---- CONTENT_INVENTORY ----------------------------------------------
    # The census that answers "how much is there to migrate, and where".
    # Per object kind, both sides, side by side. A gap in the last two columns
    # against the middle two is text that did not cross.
    ws = wb.create_sheet("CONTENT_INVENTORY")
    ws.append(["Model", "Type", "Object kind",
               "SAP PD objects", "SAP PD with Comment", "SAP PD with Definition",
               "erwin objects", "erwin with Note", "erwin with Definition",
               "Comment not carried over", "Definition not carried over"])
    for ctx, _steps in scanned:
        # Per-model kinds: a .pdm reports Table/Column/Reference, a .ldm or
        # .cdm reports Entity/Attribute/Relationship.
        for kind in ctx["kinds"]:
            pdk = ctx["pd_inv"][kind]
            erk = ctx["er_inv"][kind]
            # A .pdm carries its Comment into erwin's Definition (erwin's own
            # importer does it); .ldm/.cdm carry it into a Note via the
            # preprocessing step. Measuring every type against Note flagged a
            # fully-migrated physical model as a 100% gap.
            carried = (max(erk["note"], erk["definition"])
                       if ctx["suffix"] == ".pdm" else erk["note"])
            ws.append([ctx["base"], ctx["suffix"], kind,
                       pdk["total"], pdk["comment"], pdk["definition"],
                       erk["total"], erk["note"], erk["definition"],
                       max(0, pdk["comment"] - carried),
                       max(0, pdk["definition"] - erk["definition"])])
        pdt = ctx["pd_inv"]
        ert = ctx["er_inv"]
        carried_total = (max(totals(ert, "note"), totals(ert, "definition"))
                         if ctx["suffix"] == ".pdm" else totals(ert, "note"))
        ws.append([ctx["base"], ctx["suffix"], "TOTAL",
                   totals(pdt, "total"), totals(pdt, "comment"),
                   totals(pdt, "definition"),
                   totals(ert, "total"), totals(ert, "note"),
                   totals(ert, "definition"),
                   max(0, totals(pdt, "comment") - carried_total),
                   max(0, totals(pdt, "definition") - totals(ert, "definition"))])
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
    # flag any non-zero gap
    for row in ws.iter_rows(min_row=2, min_col=10, max_col=11):
        for c in row:
            if isinstance(c.value, int) and c.value > 0:
                c.fill = PatternFill("solid", fgColor="FFC7CE")
    style(ws, [30, 8, 14, 15, 20, 22, 14, 16, 20, 20, 24])

    # ---- TIMINGS ---------------------------------------------------------
    # Deliberately small. The question is "how long does it take?", so the
    # sheet answers that and stops. Full per-phase detail stays in
    # batch_summary/audit/timings.jsonl for anyone who needs it.
    timings = latest_timings()
    ws = wb.create_sheet("TIMINGS")
    if not timings:
        ws["A1"] = "No timing recorded yet."
        ws["A2"] = "Run:  py app\\reporting\\benchmark.py"
        ws.column_dimensions["A"].width = 60
    else:
        scen = timings.get("scenarios", [])
        ws.append(["How long the framework takes"])
        ws["A1"].font = Font(bold=True, size=14)
        ws.append(["Measured", timings.get("run", "")])
        ws.append([])

        ws.append(["Models", "Total seconds", "Seconds per model"])
        hdr = ws.max_row
        for r in scen:
            size = r.get("size")
            distinct = r.get("distinct_models")
            # Say plainly when a scenario was bigger than the project: the
            # batch was filled by re-running real models, so "5" on a 4-model
            # project is a scenario size, not a count of files on disk.
            if r.get("repeats_used") and distinct:
                size = "%d (%d real, %d reused)" % (size, distinct,
                                                    size - distinct)
            ws.append([size, r.get("end_to_end_s"),
                       r.get("per_model_avg_s")])
        for cell in ws[hdr]:
            cell.fill = HDR_FILL
            cell.font = HDR_FONT

        if len(scen) >= 2:
            one, many = scen[0], scen[-1]
            naive = round((one.get("end_to_end_s") or 0) * (many.get("size") or 0), 1)
            ws.append([])
            ws.append(["%d models together" % many.get("size"),
                       many.get("end_to_end_s"), "seconds"])
            ws.append(["%d models one at a time" % many.get("size"),
                       naive, "seconds"])

        # which step is slowest - the one number an optimisation needs
        rows = [m for r in scen for m in r.get("per_model", [])]
        if rows:
            m = rows[0]
            ws.append([])
            ws.append(["Where the time goes, for one model"])
            ws[ws.max_row][0].font = Font(bold=True, size=12)
            ws.append(["Reading the SAP PD model", m.get("parse_pd_s"), "seconds"])
            ws.append(["Reading the erwin XML", m.get("parse_erwin_s"), "seconds"])
            ws.append(["Comparing them", m.get("compare_s"), "seconds"])

        if any(r.get("repeats_used") for r in scen):
            ws.append([])
            ws.append(["Note: not enough models available, so one model was "
                       "reused to fill the batch."])

        for i, w in enumerate([34, 16, 18], start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

    # ---- SUMMARY (first) -------------------------------------------------
    ws = wb.create_sheet("SUMMARY", 0)
    ws.append(["Migration Provenance Audit"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["Generated", run_ts])
    ws.append(["Project", PROJECT_ROOT])
    if note:
        ws.append(["Run note", note])
    ws.append([])
    ws.append(["Model", "Type", "Validation", "Fidelity %",
               "PD Comments in source", "PD Definitions in source",
               "Pipeline done", "Separate script done", "Manual done",
               "Manual PENDING", "Not implemented", "Blockers"])
    hdr = ws.max_row
    for ctx, steps in scanned:
        c = Counter((s["provenance"], s["outcome"]) for s in steps)
        blockers = sum(1 for s in steps if s["outcome"] in (PENDING, FAILED))
        v = ctx["validation"] or {}
        ws.append([ctx["base"], ctx["suffix"], v.get("status", "-"),
                   v.get("fidelity", "-"),
                   totals(ctx["pd_inv"], "comment"),
                   totals(ctx["pd_inv"], "definition"),
                   c[(AUTOMATED, DONE)], c[(SCRIPT, DONE)], c[(MANUAL, DONE)],
                   c[(MANUAL, PENDING)],
                   sum(n for (p, _o), n in c.items() if p == NOT_IMPL),
                   blockers])
    for cell in ws[hdr]:
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    ws.append([])
    ws.append(["How to read the ledger"])
    ws[ws.max_row][0].font = Font(bold=True, size=12)
    ws.append(["Provenance = WHO ran it.  Outcome = WHETHER it happened.",
               "'MANUAL / DONE' therefore means a person had to do this step "
               "and they did it. Read the 'What happened' column first."])
    ws.append([])
    ws.append(["Provenance legend"])
    ws[ws.max_row][0].font = Font(bold=True, size=12)
    for tag, meaning in (
        (PIPELINE, "Runs inside `py -m app.main`. One command covers it."),
        (SCRIPT, "A separate script you invoke yourself, by name."),
        (MANUAL, "A person had to do this in erwin or PowerDesigner. "
                 "erwin's OEM licence blocks headless automation."),
        (NOT_IMPL, "Designed but absent from the code (Phases A and C)."),
    ):
        ws.append([tag, meaning])
        ws[ws.max_row][0].fill = PROV_FILL[tag]
    for i, w in enumerate([30, 8, 12, 11, 16, 18, 15, 12, 12, 15, 16, 10], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    wb.save(REPORT_PATH)


def append_ledger(scanned, run_ts, note):
    """Append-only history, so past state is never overwritten."""
    os.makedirs(AUDIT_DIR, exist_ok=True)
    with open(LEDGER_PATH, "a", encoding="utf-8") as fh:
        for ctx, steps in scanned:
            fh.write(json.dumps({
                "run": run_ts,
                "note": note,
                "model": ctx["base"],
                "type": ctx["suffix"],
                "validation": ctx["validation"],
                "steps": [{k: s[k] for k in
                           ("step", "phase", "provenance", "outcome",
                            "evidence", "when")} for s in steps],
            }, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Provenance audit: what was automated, scripted or manual.")
    ap.add_argument("--note", default="", help="stamp this run with a comment")
    args = ap.parse_args(argv)

    run_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    models = discover_models()
    if not models:
        print("No models found under sappdmodels/. Nothing to audit.")
        return 1

    scanned = [scan_model(sfx, p) for sfx, p in models]
    write_report(scanned, run_ts, args.note)
    append_ledger(scanned, run_ts, args.note)

    print("=" * 78)
    print("MIGRATION PROVENANCE AUDIT   %s" % run_ts)
    print("=" * 78)
    for ctx, steps in scanned:
        c = Counter((s["provenance"], s["outcome"]) for s in steps)
        blockers = [s for s in steps if s["outcome"] in (PENDING, FAILED)]
        v = ctx["validation"] or {}
        print("\n  %s  (%s)" % (ctx["base"], ctx["suffix"]))
        print("     validation      : %s %s%%"
              % (v.get("status", "-"), v.get("fidelity", "-")))
        print("     source content  :")
        for kind in ctx["kinds"]:
            pdk, erk = ctx["pd_inv"][kind], ctx["er_inv"][kind]
            print("        %-13s PD %3d objects | Comment %3d -> Note %3d | "
                  "Definition %3d -> Definition %3d"
                  % (kind, pdk["total"], pdk["comment"], erk["note"],
                     pdk["definition"], erk["definition"]))
        print("     pipeline done   : %d" % c[(PIPELINE, DONE)])
        print("     separate script : %d" % c[(SCRIPT, DONE)])
        print("     manual done     : %d" % c[(MANUAL, DONE)])
        print("     manual PENDING  : %d" % c[(MANUAL, PENDING)])
        print("     not implemented : %d"
              % sum(n for (p, _o), n in c.items() if p == NOT_IMPL))
        if blockers:
            print("     BLOCKERS:")
            for s in blockers:
                print("        [%s/%s] %s" % (s["provenance"], s["outcome"], s["step"]))
    t = latest_timings()
    if t and t.get("scenarios"):
        print()
        for r in t["scenarios"]:
            print("     %2d model(s) takes %.1f seconds"
                  % (r.get("size", 0), r.get("end_to_end_s", 0)))

    print("\n  report  -> %s" % rel(REPORT_PATH))
    print("  history -> %s  (append-only)" % rel(LEDGER_PATH))
    return 0


if __name__ == "__main__":
    sys.exit(main())
