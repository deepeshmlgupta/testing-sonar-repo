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
from defusedxml.ElementTree import parse as safe_parse
from collections import Counter, OrderedDict
KEY_SOLID = "solid"
KEY_DEFINITION = "Definition"
KEY_BASE = "base"
KEY_SUFFIX = "suffix"
KEY_OUTCOME = "outcome"
KEY_PD_INV = "pd_inventory"
KEY_PROVENANCE = "provenance"
KEY_NOTE = "Note"
KEY_COMMENT = "Comment"
KEY_TOTAL = "Total"
KEY_NOTES_XML = "Notes"
COLOR_FFC7CE = "FFC7CE"
EXT_XML = ".xml"
KEY_FIDELITY = "fidelity"
KEY_DETAIL_REPORT = "detail_report"
KEY_CRITICAL = "critical"
KEY_STAGE = "stage"
KEY_PROMOTED = "promoted"
KEY_STEP = "step"
KEY_PHASE = "phase"
KEY_STATUS = "status"
KEY_DETAIL = "detail"
KEY_EVIDENCE = "evidence"
KEY_WHEN = "when"
KEY_BYTES = "bytes"
KEY_ORIGIN = "origin"
KEY_STALE_HOURS = "stale_hours"
KEY_SIZE = "size"
KEY_SECONDS = "seconds"
KEY_ER_INV = "er_inventory"
KEY_NOT_APP = "N/A"
KEY_MODEL = "Model"

KEY_ENTITY_PROPS = "EntityProps"
KEY_ATTRIBUTE = "Attribute"
KEY_ATTR_PROPS = "AttributeProps"
KEY_REL_PROPS = "RelationshipProps"

ENC_UTF8 = "utf-8"
COLOR_E7E6E6 = "E7E6E6"
COLOR_C6EFCE = "C6EFCE"
DIR_SAPPDMODELS = "sappdmodels"
DIR_3_FINAL = "3_final"
DIR_REPORTING = "reporting"
EXT_CDM = ".cdm"
EXT_ERWIN = ".erwin"
MSG_GATE_FAIL = "Gate failure: validation not 100% or validation missing"

EXT_LDM = ".ldm"
EXT_PDM = ".pdm"
KIND_ENTITY = "Entity"
KIND_RELATIONSHIP = "Relationship"
KEY_NOTES_WHEN = "notes"
KEY_NOTES_SIZE = "notes_size"
KEY_VALIDATION = "validation"
DIR_ERWINMODELS = "erwinmodels"
KEY_ERWIN = "erwin"



from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# --------------------------------------------------------------------------
# Anchored to this file, so the Run button and the terminal behave alike.
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, os.pardir, os.pardir))

SAP_DIRS = {
    EXT_LDM: os.path.join(PROJECT_ROOT, DIR_SAPPDMODELS, "ldm"),
    EXT_CDM: os.path.join(PROJECT_ROOT, DIR_SAPPDMODELS, "cdm"),
    EXT_PDM: os.path.join(PROJECT_ROOT, DIR_SAPPDMODELS, "pdm"),
}
INITIAL_ERWIN = os.path.join(PROJECT_ROOT, DIR_ERWINMODELS, "1_initial", KEY_ERWIN)
INITIAL_XML = os.path.join(PROJECT_ROOT, DIR_ERWINMODELS, "1_initial", "xml")
PREPROCESSED_XML = os.path.join(PROJECT_ROOT, DIR_ERWINMODELS, "2_preprocessed", "xml")
FINAL_ERWIN = os.path.join(PROJECT_ROOT, DIR_ERWINMODELS, DIR_3_FINAL, "final_erwin")
FINAL_XML = os.path.join(PROJECT_ROOT, DIR_ERWINMODELS, DIR_3_FINAL, "final_xml")
REPORT_DIRS = {
    EXT_LDM: os.path.join(PROJECT_ROOT, "app", DIR_REPORTING, "ldm_reports"),
    EXT_CDM: os.path.join(PROJECT_ROOT, "app", DIR_REPORTING, "cdm_reports"),
    EXT_PDM: os.path.join(PROJECT_ROOT, "app", DIR_REPORTING, "pdm_reports"),
}

def field_map_report_for(suffix):
    """Each model type gets its own field-mapping workbook, beside its report."""
    return os.path.join(REPORT_DIRS.get(suffix, REPORT_DIRS[EXT_LDM]),
                        "field_mapping_report.xlsx")


# Kept for callers that still expect the old module-level constant.
FIELD_MAP_REPORT = os.path.join(PROJECT_ROOT, "app", DIR_REPORTING,
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
    (MANUAL, NA):         KEY_NOT_APP,
    (PIPELINE, DONE):     "Ran in the pipeline (py -m app.main)",
    (PIPELINE, PENDING):  "PIPELINE NOT RUN for this model",
    (PIPELINE, FAILED):   "PIPELINE STEP FAILED",
    (PIPELINE, NA):       KEY_NOT_APP,
    (SCRIPT, DONE):       "Ran as a separate script",
    (SCRIPT, PENDING):    "SEPARATE SCRIPT NOT RUN",
    (SCRIPT, FAILED):     "SEPARATE SCRIPT FAILED",
    (SCRIPT, NA):         KEY_NOT_APP,
    (NOT_IMPL, NA):       "Not implemented in the code",
}
STATUS_FILL = {
    "Done by a person":                    PatternFill(KEY_SOLID, fgColor="FFF2CC"),
    "WAITING ON A PERSON":                 PatternFill(KEY_SOLID, fgColor=COLOR_FFC7CE),
    "Person attempted, failed":            PatternFill(KEY_SOLID, fgColor=COLOR_FFC7CE),
    "Ran in the pipeline (py -m app.main)": PatternFill(KEY_SOLID, fgColor=COLOR_C6EFCE),
    "PIPELINE NOT RUN for this model":     PatternFill(KEY_SOLID, fgColor=COLOR_FFC7CE),
    "PIPELINE STEP FAILED":                PatternFill(KEY_SOLID, fgColor=COLOR_FFC7CE),
    "Ran as a separate script":            PatternFill(KEY_SOLID, fgColor="DDEBF7"),
    "SEPARATE SCRIPT NOT RUN":             PatternFill(KEY_SOLID, fgColor=COLOR_FFC7CE),
    "SEPARATE SCRIPT FAILED":              PatternFill(KEY_SOLID, fgColor=COLOR_FFC7CE),
    "Not implemented in the code": PatternFill(KEY_SOLID, fgColor=COLOR_E7E6E6),
    KEY_NOT_APP:              PatternFill(KEY_SOLID, fgColor=COLOR_E7E6E6),
}


def status_text(prov, outcome):
    return STATUS_TEXT.get((prov, outcome), "%s / %s" % (prov, outcome))

HDR_FILL = PatternFill(KEY_SOLID, fgColor="1F3864")
HDR_FONT = Font(bold=True, color="FFFFFF")
PROV_FILL = {
    PIPELINE: PatternFill(KEY_SOLID, fgColor=COLOR_C6EFCE),
    SCRIPT:    PatternFill(KEY_SOLID, fgColor="DDEBF7"),
    MANUAL:    PatternFill(KEY_SOLID, fgColor="FFF2CC"),
    NOT_IMPL:  PatternFill(KEY_SOLID, fgColor=COLOR_E7E6E6),
}
OUTCOME_FILL = {
    DONE:    PatternFill(KEY_SOLID, fgColor=COLOR_C6EFCE),
    PENDING: PatternFill(KEY_SOLID, fgColor=COLOR_FFC7CE),
    FAILED:  PatternFill(KEY_SOLID, fgColor=COLOR_FFC7CE),
    NA:      PatternFill(KEY_SOLID, fgColor=COLOR_E7E6E6),
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
    EXT_LDM: ((KIND_ENTITY,       KIND_ENTITY,           KEY_ENTITY_PROPS),
             (KEY_ATTRIBUTE,    "EntityAttribute",  KEY_ATTR_PROPS),
             (KIND_RELATIONSHIP, KIND_RELATIONSHIP,     KEY_REL_PROPS)),
    EXT_CDM: ((KIND_ENTITY,       KIND_ENTITY,           KEY_ENTITY_PROPS),
             (KEY_ATTRIBUTE,    "EntityAttribute",  KEY_ATTR_PROPS),
             (KIND_RELATIONSHIP, KIND_RELATIONSHIP,     KEY_REL_PROPS)),
    EXT_PDM: (("Table",        "Table",            KEY_ENTITY_PROPS),
             ("Column",       "Column",           KEY_ATTR_PROPS),
             ("Reference",    "Reference",        KEY_REL_PROPS)),
}

# Default for callers that predate the per-type split.
PD_KINDS = (KIND_ENTITY, KEY_ATTRIBUTE, KIND_RELATIONSHIP)
PD_TAG = {KIND_ENTITY: KIND_ENTITY, KEY_ATTRIBUTE: "EntityAttribute",
          KIND_RELATIONSHIP: KIND_RELATIONSHIP}


def kinds_for(suffix):
    """(kind, pd_tag, erwin_props) triples for one model type."""
    return KIND_SPECS.get(suffix, KIND_SPECS[EXT_LDM])


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
        with open(TIMINGS_PATH, "r", encoding=ENC_UTF8) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = json.loads(line)
    except Exception:
        return None
    return last


def pd_text_inventory(ldm_path, suffix=EXT_LDM):
    """
    Per-object-kind census of the SOURCE model.

    Returns {kind: {KEY_TOTAL: n, KEY_COMMENT: n, KEY_DEFINITION: n}}, where the
    kinds are Entity/Attribute/Relationship for a conceptual or logical model
    and Table/Column/Reference for a physical one.

    Per-kind rather than one aggregate, because "22 Comments" tells you
    nothing about WHERE the gap is: 22 entity comments with zero on the
    relationships is a completely different situation from an even spread,
    and only the breakdown distinguishes them.
    """
    specs = kinds_for(suffix)
    inv = OrderedDict((k, {KEY_TOTAL: 0, KEY_COMMENT: 0, KEY_DEFINITION: 0})
                      for k, _t, _p in specs)
    try:
        root = safe_parse(ldm_path).getroot()
    except Exception:
        return inv
    O = "{object}"
    for kind, pd_tag, _props in specs:
        for el in root.iter(O + pd_tag):
            if el.get("Id") is None:            # Ref= pointers are not objects
                continue
            inv[kind][KEY_TOTAL] += 1
            # PowerDesigner writes some scalars class-qualified
            # (<a:Table.Comment>), so both spellings have to be accepted or a
            # populated field reads as empty.
            if _pd_text(el, "Comment"):
                inv[kind][KEY_COMMENT] += 1
            if _pd_text(el, "Description") or _pd_text(el, "Annotation"):
                inv[kind][KEY_DEFINITION] += 1
    return inv


def _pd_text(el, name):
    """Text of a PD scalar field, plain or class-qualified."""
    A = "{attribute}"
    suffix = "." + name
    for child in el:
        tag = child.tag
        if isinstance(tag, str) and tag.startswith(A):
            local = tag[len(A):]
            if local == name or local.endswith(suffix):
                if child.text and child.text.strip():
                    return child.text.strip()
    return ""


ER_PROPS = {KIND_ENTITY: KEY_ENTITY_PROPS, KEY_ATTRIBUTE: KEY_ATTR_PROPS,
            KIND_RELATIONSHIP: KEY_REL_PROPS}


def erwin_text_inventory(xml_path, suffix=EXT_LDM):
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
    inv = OrderedDict((k, {KEY_TOTAL: 0, KEY_NOTE: 0, KEY_DEFINITION: 0})
                      for k, _t, _p in specs)
    if not xml_path or not os.path.isfile(xml_path):
        return inv
    try:
        with open(xml_path, "r", encoding=ENC_UTF8, errors="replace") as fh:
            raw = fh.read()
    except Exception:
        return inv
    for kind, _pd_tag, props in specs:
        for block in re.findall(r"<%s>(.*?)</%s>" % (props, props), raw, re.S):
            inv[kind][KEY_TOTAL] += 1
            if re.search(r"<Note_List\b", block):
                inv[kind][KEY_NOTE] += 1
            if re.search(r"<Definition\b[^>]*>(?!\s*</Definition>)", block):
                inv[kind][KEY_DEFINITION] += 1
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
        with open(xml_path, "r", encoding=ENC_UTF8, errors="replace") as fh:
            head = fh.read(8000)
    except Exception:
        return ""
    m = re.search(r"<Locator>(.*?)</Locator>", head, re.S)
    return m.group(1).strip() if m else ""


def notes_stats(xml_path):
    """Count migrated Notes and Definitions in an erwin export."""
    try:
        with open(xml_path, "r", encoding=ENC_UTF8, errors="replace") as fh:
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
        return _extract_validation_record(wb["SUMMARY"], model_name)
    except Exception:
        return None

def _extract_validation_record(ws, model_name):
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
                return {KEY_STATUS: rec.get("Status"),
                        KEY_FIDELITY: rec.get("Fidelity %"),
                        KEY_CRITICAL: rec.get("CRITICAL"),
                        KEY_STAGE: rec.get("Stage"),
                        KEY_PROMOTED: rec.get("Promoted?")}
    return None


# --------------------------------------------------------------------------
# the step table -- this IS the audit definition
# --------------------------------------------------------------------------

def build_steps(ctx):
    S = []
    def add(step, phase, prov, outcome, detail, evidence="", when="", size=0):
        S.append({
            KEY_STEP: step, KEY_PHASE: phase, KEY_PROVENANCE: prov,
            KEY_OUTCOME: outcome, KEY_DETAIL: detail,
            KEY_EVIDENCE: rel(evidence) if evidence else "",
            KEY_WHEN: when, KEY_BYTES: size,
        })
    
    label_for = {EXT_PDM: "SAP PD physical model (.pdm)",
                 EXT_CDM: "SAP PD conceptual model (.cdm)"}.get(
                     ctx[KEY_SUFFIX], "SAP PD logical model (.ldm)")
                     
    _build_source_author_steps(ctx, add, label_for)
    _build_manual_erwin_steps(ctx, add)
    xml_ok = _build_phase_b_gate(ctx, add)
    
    if ctx[KEY_SUFFIX] == EXT_PDM:
        _pdm_steps(ctx, add, xml_ok)
    else:
        _build_non_pdm_steps(ctx, add, xml_ok)
        
    return S

def _build_source_author_steps(ctx, add, label_for):
    creators, modifiers = ctx["authors"]
    add("Author SAP PD model", "Source", MANUAL, DONE,
        "%s modelled by hand in PowerDesigner. Creator(s): %s. Modifier(s): %s"
        % (label_for, creators or "unknown", modifiers or "unknown"),
        ctx["ldm"], ctx["ldm_when"], ctx["ldm_size"])
        
    add("Phase A - extract UDPs", "A", NOT_IMPL, NA,
        "Removed from main.py ('completely removed per user request'). No UDP "
        "extraction happens, so custom properties are not carried across.")

def _build_manual_erwin_steps(ctx, add):
    for label, path, when, size, kind in (
        ("Import into erwin (.erwin)", ctx[KEY_ERWIN], ctx["erwin_when"],
         ctx["erwin_size"], KEY_ERWIN),
        ("Export erwin XML (.xml)", ctx["xml"], ctx["xml_when"],
         ctx["xml_size"], "xml"),
    ):
        if path and os.path.isfile(path):
            extra = ""
            if kind == "xml" and ctx[KEY_ORIGIN]:
                extra = " Saved from: %s" % ctx[KEY_ORIGIN]
            stale = ""
            if kind == "xml" and ctx[KEY_STALE_HOURS] is not None and ctx[KEY_STALE_HOURS] > 0:
                stale = (" WARNING: the .ldm is %.1f h NEWER than this export, "
                         "so any score computed from it is out of date."
                         % ctx[KEY_STALE_HOURS])
            add(label, "B", MANUAL, DONE,
                "Done by hand in the erwin UI - erwin's OEM licence blocks "
                "headless MIT Bridge automation.%s%s" % (extra, stale),
                path, when, size)
        else:
            add(label, "B", MANUAL, PENDING,
                "REQUIRED AND NOT DONE. A person must open erwin, import the "
                "%s and save this file. The pipeline cannot proceed past "
                "Phase B without it." % ctx[KEY_SUFFIX],
                ctx["expect_%s" % kind])

def _build_phase_b_gate(ctx, add):
    xml_ok = os.path.isfile(ctx["xml"] or "")
    both = xml_ok and os.path.isfile(ctx[KEY_ERWIN] or "")
    add("Phase B - import gate", "B", AUTOMATED, DONE if xml_ok else FAILED,
        "GATE, not an import: erwin_importer.py only checks that the "
        "hand-made files exist, then stands aside and lets the pipeline "
        "continue past Phase B. It imports nothing. %s"
        % ("Both files present, gate passed." if both else
           ("XML present, gate passed; the .erwin binary is absent so only "
            "the promotion copy is affected." if xml_ok else
            "XML missing, gate failed - nothing downstream can run.")))
    return xml_ok

def _build_non_pdm_steps(ctx, add, xml_ok):
    _enrichment_steps(ctx, add)
    
    add("Phase C - inject UDPs", "C", NOT_IMPL, NA,
        "Removed from main.py. UDP injection does not run.")
        
    v = ctx[KEY_VALIDATION]
    both = xml_ok and os.path.isfile(ctx[KEY_ERWIN] or "")
    
    if v:
        add("Phase D - reconciliation", "D", AUTOMATED, DONE,
            "Compared SAP PD against the erwin XML. Status %s, fidelity %s%%, "
            "%s critical finding(s)."
            % (v.get(KEY_STATUS), v.get(KEY_FIDELITY), v.get(KEY_CRITICAL)),
            ctx[KEY_DETAIL_REPORT], ctx["detail_when"], ctx["detail_size"])
    else:
        add("Phase D - reconciliation", "D", AUTOMATED,
            PENDING if both else NA,
            "No entry for this model in the detailed validation report."
            if both else
            MSG_GATE_FAIL,
            ctx[KEY_DETAIL_REPORT])
            
    _report_steps(ctx, add)
    _non_pdm_promotion_gate(ctx, add, v)

def _enrichment_steps(ctx, add):
    src_comments = totals(ctx[KEY_PD_INV], KEY_COMMENT)
    src_defs = totals(ctx[KEY_PD_INV], KEY_DEFINITION)

    via = ctx["notes_via"]
    ran_by = ("app/main.py's preprocessing orchestrator"
              if via == PIPELINE else "pd_comment_to_erwin_note.py, run by hand")

    if ctx[KEY_NOTES_XML]:
        _enrichment_with_notes_xml(ctx, add, src_comments, src_defs, via, ran_by)
    else:
        _enrichment_without_notes_xml(ctx, add, src_comments, src_defs)

def _enrichment_with_notes_xml(ctx, add, src_comments, src_defs, via, ran_by):
    _enrich_comments(ctx, add, src_comments, via, ran_by)
    _enrich_definitions(ctx, add, src_defs, via)

def _enrich_comments(ctx, add, src_comments, via, ran_by):
    n, _, author = ctx["notes_counts"]
    kinds_text = " or ".join(kind_names(ctx[KEY_SUFFIX]))
    census = ", ".join("%s %d objects" % (k, v[KEY_TOTAL]) for k, v in ctx[KEY_PD_INV].items())
    
    if src_comments == 0:
        msg = f"Nothing to migrate: no {kinds_text} in the SAP PD model carries a Comment (checked {census})."
        add("Comment -> Note migration", "C", via, NA, msg,
            ctx[KEY_NOTES_XML], ctx[KEY_NOTES_WHEN], ctx[KEY_NOTES_SIZE])
        return

    status = DONE if n else PENDING
    if n:
        pd_k = _per_kind(ctx[KEY_PD_INV], KEY_COMMENT)
        er_k = _per_kind(ctx[KEY_ER_INV], KEY_NOTE)
        msg = (f"{ran_by} wrote {n} Note record(s) from {src_comments} PD Comment(s). "
               f"Source by kind: {pd_k}. erwin now carries Notes on: {er_k}. "
               f"Note author recorded as '{author or 'unset'}'.")
    else:
        msg = f"The model has {src_comments} Comment(s) but the output has no Notes."
        
    add("Comment -> Note migration", "C", via, status, msg,
        ctx[KEY_NOTES_XML], ctx[KEY_NOTES_WHEN], ctx[KEY_NOTES_SIZE])

def _enrich_definitions(ctx, add, src_defs, via):
    _, d, _ = ctx["notes_counts"]
    kinds_text = " or ".join(kind_names(ctx[KEY_SUFFIX]))
    census = ", ".join("%s %d objects" % (k, v[KEY_TOTAL]) for k, v in ctx[KEY_PD_INV].items())

    if src_defs == 0:
        msg = f"Nothing to migrate: no {kinds_text} in the SAP PD model has text in its Definition tab (checked {census}). Fill the Definition tab in PowerDesigner if erwin Definitions are expected."
        add("Definition -> Definition migration", "C", via, NA, msg,
            ctx[KEY_NOTES_XML], ctx[KEY_NOTES_WHEN], ctx[KEY_NOTES_SIZE])
        return

    status = DONE if d else PENDING
    if d:
        pd_k = _per_kind(ctx[KEY_PD_INV], KEY_DEFINITION)
        er_k = _per_kind(ctx[KEY_ER_INV], KEY_DEFINITION)
        msg = f"Wrote {d} Definition(s) from {src_defs} in the source. Source by kind: {pd_k}. erwin now carries Definitions on: {er_k}."
    else:
        msg = f"The model has {src_defs} Definition(s) but none reached erwin - the script was run WITHOUT --definitions."
        
    add("Definition -> Definition migration", "C", via, status, msg,
        ctx[KEY_NOTES_XML], ctx[KEY_NOTES_WHEN], ctx[KEY_NOTES_SIZE])

def _per_kind(inv, key):
    res = ", ".join("%s %d" % (k, v[key]) for k, v in inv.items() if v[key])
    return res or "none"

def _enrichment_without_notes_xml(ctx, add, src_comments, src_defs):
    expected = os.path.join(PREPROCESSED_XML, ctx[KEY_BASE] + EXT_XML)
    add("Comment -> Note migration", "C", PIPELINE,
        NA if src_comments == 0 else PENDING,
        "Nothing to migrate: the model has no Comments."
        if src_comments == 0 else
        "Not run. The model has %d Comment(s) waiting to migrate. "
        "py -m app.main runs this as Phase C." % src_comments, expected)
    add("Definition -> Definition migration", "C", PIPELINE,
        NA if src_defs == 0 else PENDING,
        "Nothing to migrate: the model has no Definitions."
        if src_defs == 0 else
        "Not run. The model has %d Definition(s) waiting to migrate."
        % src_defs, expected)

def _non_pdm_promotion_gate(ctx, add, v):
    promoted_path = promoted_artefact(ctx[KEY_BASE])
    passed = bool(v and str(v.get(KEY_STATUS)).upper() == "PASS"
                  and str(v.get(KEY_CRITICAL)) in ("0", "0.0", "None"))
    add("Promote to 3_final", "Gate", AUTOMATED,
        DONE if promoted_path else (PENDING if passed else NA),
        "Copied into erwinmodels/3_final - this is the blessed output."
        if promoted_path else
        ("Eligible but not copied." if passed else
         "Not eligible. main.py copies a model into 3_final only when status is "
         "PASS with zero criticals; anything else stays put."),
        promoted_path or os.path.join(FINAL_XML, ctx[KEY_BASE] + EXT_XML))

def _report_steps(ctx, add):
    """
    The reporting artefacts, which are the same question for every model type
    but live in a different folder per type.
    """
    for label, path, prov in (
            ("Detailed validation report", ctx[KEY_DETAIL_REPORT], PIPELINE),
            ("Pass/Fail summary", SUMMARY_REPORT, PIPELINE),
            ("Field mapping report", field_map_report_for(ctx[KEY_SUFFIX]), SCRIPT)):
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
    v = ctx[KEY_VALIDATION] or {}
    promoted_flag = str(v.get("promoted") or "").strip().upper() == "YES"
    remediated = bool(ctx[KEY_NOTES_XML])
    
    _pdm_pass1_validate(ctx, add, xml_ok, v)
    _pdm_remediation(ctx, add, xml_ok, v, remediated)
    _report_steps(ctx, add)
    _pdm_promotion_gate(ctx, add, promoted_flag)

def _pdm_pass1_validate(ctx, add, xml_ok, v):
    if v:
        add("Phase D - validate 1_initial", "D", PIPELINE, DONE,
            "pdm_flow validated the raw erwin export against the .pdm. "
            "The report holds the FINAL result (status %s, fidelity %s%%, %s "
            "critical); pass-1 numbers are in the run log."
            % (v.get(KEY_STATUS), v.get(KEY_FIDELITY), v.get(KEY_CRITICAL)),
            ctx[KEY_DETAIL_REPORT], ctx["detail_when"], ctx["detail_size"])
    else:
        add("Phase D - validate 1_initial", "D", PIPELINE,
            PENDING if xml_ok else NA,
            "No entry for this model in the PDM validation report."
            if xml_ok else MSG_GATE_FAIL,
            ctx[KEY_DETAIL_REPORT])

def _pdm_remediation(ctx, add, xml_ok, v, remediated):
    if remediated:
        add("Remediation (preprocessing)", "C", PIPELINE, DONE,
            "pdm_preprocessor rewrote the erwin XML from the PowerDesigner "
            "source: missing columns restored, empty primary keys re-linked "
            "and broken foreign-key joins repaired. It never invents data to "
            "raise a score, so legitimate modelling differences and defects "
            "in the source model stay in the report.",
            ctx[KEY_NOTES_XML], ctx[KEY_NOTES_WHEN], ctx[KEY_NOTES_SIZE])
        add("Phase D - re-validate 2_preprocessed", "D", PIPELINE,
            DONE if v else PENDING,
            "The remediated XML was re-parsed from disk and scored again -- "
            "promotion follows a MEASURED result, never what preprocessing "
            "believes it changed. Final status %s, fidelity %s%%."
            % (v.get(KEY_STATUS), v.get(KEY_FIDELITY)) if v else
            "Remediated XML exists but no result was recorded.",
            ctx[KEY_NOTES_XML], ctx[KEY_NOTES_WHEN], ctx[KEY_NOTES_SIZE])
    else:
        expected = os.path.join(PREPROCESSED_XML, ctx[KEY_BASE] + EXT_XML)
        at_target = bool(v) and str(v.get(KEY_STAGE) or "") == DIR_3_FINAL
        add("Remediation (preprocessing)", "C", PIPELINE,
            NA if at_target else (PENDING if xml_ok else NA),
            "Not needed: the model met the fidelity target on import."
            if at_target else
            ("Not run. py -m app.main remediates a PDM that falls short of "
             "the target (see PDM_FIDELITY_TARGET in app/config/settings.py)."
             if xml_ok else MSG_GATE_FAIL),
            expected)
        add("Phase D - re-validate 2_preprocessed", "D", PIPELINE, NA,
            "Only runs when remediation runs.", expected)

def _pdm_promotion_gate(ctx, add, promoted_flag):
    promoted_path = promoted_artefact(ctx[KEY_BASE])
    if promoted_path or promoted_flag:
        add("Promote to 3_final", "Gate", PIPELINE, DONE,
            "Fidelity target met on a measured score; copied into "
            "erwinmodels/3_final - this is the blessed output.",
            promoted_path or os.path.join(FINAL_XML, ctx[KEY_BASE] + EXT_XML))

def promoted_artefact(base):
    """
    The promoted copy of a model, or "" when it was never promoted.

    3_final has been laid out two ways across versions of main.py
    (final_erwin/final_xml, and plain erwin/xml), so both are checked — a
    promotion reported as "not done" because the folder was renamed is worse
    than no report at all.
    """
    final_root = os.path.join(PROJECT_ROOT, DIR_ERWINMODELS, DIR_3_FINAL)
    for folder, ext in (("final_erwin", EXT_ERWIN), (KEY_ERWIN, EXT_ERWIN),
                        ("final_xml", EXT_XML), ("xml", EXT_XML)):
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

    erwin = os.path.join(INITIAL_ERWIN, base + EXT_ERWIN)
    xml = os.path.join(INITIAL_XML, base + EXT_XML)
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
    for cand, via in ((base + EXT_XML, PIPELINE),
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
        KEY_BASE: base, KEY_SUFFIX: suffix,
        "ldm": ldm_path, "ldm_when": ldm_when, "ldm_size": ldm_size,
        # Creator / Modifier are stamped by PowerDesigner on every model type,
        # so every type can name the people behind its MANUAL steps.
        "authors": pd_authors(ldm_path),
        KEY_ERWIN: erwin if erwin_ok else "", "erwin_when": erwin_when,
        "erwin_size": erwin_size, "expect_erwin": erwin,
        "xml": xml if xml_ok else "", "xml_when": xml_when,
        "xml_size": xml_size, "expect_xml": xml,
        KEY_ORIGIN: erwin_export_origin(xml) if xml_ok else "",
        KEY_STALE_HOURS: stale_hours,
        KEY_NOTES_XML: notes_xml, KEY_NOTES_WHEN: notes_when,
        KEY_NOTES_SIZE: notes_size, "notes_via": notes_via,
        "notes_counts": notes_stats(notes_xml) if notes_xml else (0, 0, ""),
        KEY_DETAIL_REPORT: detail_report, "detail_when": detail_when,
        "detail_size": detail_size,
        KEY_VALIDATION: read_validation(detail_report, base),
        KEY_PD_INV: pd_text_inventory(ldm_path, suffix),
        KEY_ER_INV: erwin_text_inventory(notes_xml or xml, suffix),
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

    _write_step_ledger(wb, scanned)
    _write_manual_actions(wb, scanned)
    _write_blockers(wb, scanned)
    _write_artefacts(wb, scanned)
    _write_content_inventory(wb, scanned)
    _write_timings(wb)
    _write_summary(wb, scanned, run_ts, note)

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    wb.save(REPORT_PATH)

def _write_step_ledger(wb, scanned):
    ws = wb.create_sheet("STEP_LEDGER")
    ws.append([KEY_MODEL, "Type", "Phase", "Step", "What happened",
               "Provenance", "Outcome",
               "Detail", "Evidence file", "Evidence timestamp", "Bytes"])
    for ctx, steps in scanned:
        for s in steps:
            ws.append([ctx[KEY_BASE], ctx[KEY_SUFFIX], s[KEY_PHASE], s[KEY_STEP],
                       status_text(s[KEY_PROVENANCE], s[KEY_OUTCOME]),
                       s[KEY_PROVENANCE], s[KEY_OUTCOME], s[KEY_DETAIL],
                       s[KEY_EVIDENCE], s[KEY_WHEN], s[KEY_BYTES]])
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

def _write_manual_actions(wb, scanned):
    ws = wb.create_sheet("MANUAL_ACTIONS")
    ws.append([KEY_MODEL, "Step", "What happened", "What the person must do / did",
               "File", "Timestamp"])
    for ctx, steps in scanned:
        for s in steps:
            if s[KEY_PROVENANCE] == MANUAL:
                ws.append([ctx[KEY_BASE], s[KEY_STEP],
                           status_text(s[KEY_PROVENANCE], s[KEY_OUTCOME]),
                           s[KEY_DETAIL], s[KEY_EVIDENCE], s[KEY_WHEN]])
    for row in ws.iter_rows(min_row=2, min_col=3, max_col=3):
        if row[0].value in STATUS_FILL:
            row[0].fill = STATUS_FILL[row[0].value]
    for row in ws.iter_rows(min_row=2, min_col=4, max_col=4):
        row[0].alignment = Alignment(vertical="top", wrap_text=True)
    style(ws, [30, 30, 26, 80, 46, 20])

def _write_blockers(wb, scanned):
    ws = wb.create_sheet("BLOCKERS")
    ws.append([KEY_MODEL, "Step", "Provenance", "Why it is blocking", "Expected file"])
    for ctx, steps in scanned:
        for s in steps:
            if s[KEY_OUTCOME] in (PENDING, FAILED):
                ws.append([ctx[KEY_BASE], s[KEY_STEP], s[KEY_PROVENANCE],
                           s[KEY_DETAIL], s[KEY_EVIDENCE]])
    for row in ws.iter_rows(min_row=2, min_col=4, max_col=4):
        row[0].alignment = Alignment(vertical="top", wrap_text=True)
    style(ws, [30, 32, 17, 86, 46])

def _write_artefacts(wb, scanned):
    ws = wb.create_sheet("ARTEFACTS")
    ws.append([KEY_MODEL, "Artefact", "Produced by", "Path", "Timestamp", "Bytes"])
    for ctx, steps in scanned:
        for s in steps:
            if s[KEY_EVIDENCE] and s[KEY_OUTCOME] == DONE:
                ws.append([ctx[KEY_BASE], s[KEY_STEP], s[KEY_PROVENANCE],
                           s[KEY_EVIDENCE], s[KEY_WHEN], s[KEY_BYTES]])
    style(ws, [30, 32, 17, 52, 20, 12])

def _write_content_inventory(wb, scanned):
    ws = wb.create_sheet("CONTENT_INVENTORY")
    ws.append([KEY_MODEL, "Type", "Object kind",
               "SAP PD objects", "SAP PD with Comment", "SAP PD with Definition",
               "erwin objects", "erwin with Note", "erwin with Definition",
               "Comment not carried over", "Definition not carried over"])
    for ctx, _steps in scanned:
        for kind in ctx["kinds"]:
            pdk = ctx[KEY_PD_INV][kind]
            erk = ctx[KEY_ER_INV][kind]
            carried = (max(erk[KEY_NOTE], erk[KEY_DEFINITION])
                       if ctx[KEY_SUFFIX] == EXT_PDM else erk[KEY_NOTE])
            ws.append([ctx[KEY_BASE], ctx[KEY_SUFFIX], kind,
                       pdk[KEY_TOTAL], pdk[KEY_COMMENT], pdk[KEY_DEFINITION],
                       erk[KEY_TOTAL], erk[KEY_NOTE], erk[KEY_DEFINITION],
                       max(0, pdk[KEY_COMMENT] - carried),
                       max(0, pdk[KEY_DEFINITION] - erk[KEY_DEFINITION])])
        pdt = ctx[KEY_PD_INV]
        ert = ctx[KEY_ER_INV]
        carried_total = (max(totals(ert, KEY_NOTE), totals(ert, KEY_DEFINITION))
                         if ctx[KEY_SUFFIX] == EXT_PDM else totals(ert, KEY_NOTE))
        ws.append([ctx[KEY_BASE], ctx[KEY_SUFFIX], "TOTAL",
                   totals(pdt, KEY_TOTAL), totals(pdt, KEY_COMMENT),
                   totals(pdt, KEY_DEFINITION),
                   totals(ert, KEY_TOTAL), totals(ert, KEY_NOTE),
                   totals(ert, KEY_DEFINITION),
                   max(0, totals(pdt, KEY_COMMENT) - carried_total),
                   max(0, totals(pdt, KEY_DEFINITION) - totals(ert, KEY_DEFINITION))])
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
    for row in ws.iter_rows(min_row=2, min_col=10, max_col=11):
        for c in row:
            if isinstance(c.value, int) and c.value > 0:
                c.fill = PatternFill(KEY_SOLID, fgColor=COLOR_FFC7CE)
    style(ws, [30, 8, 14, 15, 20, 22, 14, 16, 20, 20, 24])

def _write_timings(wb):
    timings = latest_timings()
    ws = wb.create_sheet("TIMINGS")
    if not timings:
        ws["A1"] = "No timing recorded yet."
        ws["A2"] = "Run:  py app\\reporting\\benchmark.py"
        ws.column_dimensions["A"].width = 60
        return
        
    scen = timings.get("scenarios", [])
    ws.append(["How long the framework takes"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["Measured", timings.get("run", "")])
    ws.append([])

    _write_timing_scenarios(ws, scen)
    _write_timing_details(ws, scen)

    for i, w in enumerate([34, 16, 18], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

def _write_timing_scenarios(ws, scen):
    ws.append(["Models", "Total seconds", "Seconds per model"])
    hdr = ws.max_row
    for r in scen:
        size = r.get(KEY_SIZE)
        distinct = r.get("distinct_models")
        if r.get("repeats_used") and distinct:
            size = "%d (%d real, %d reused)" % (size, distinct, size - distinct)
        ws.append([size, r.get("end_to_end_s"), r.get("per_model_avg_s")])
    for cell in ws[hdr]:
        cell.fill = HDR_FILL
        cell.font = HDR_FONT

    if len(scen) >= 2:
        one, many = scen[0], scen[-1]
        naive = round((one.get("end_to_end_s") or 0) * (many.get(KEY_SIZE) or 0), 1)
        ws.append([])
        ws.append(["%d models together" % many.get(KEY_SIZE), many.get("end_to_end_s"), KEY_SECONDS])
        ws.append(["%d models one at a time" % many.get(KEY_SIZE), naive, KEY_SECONDS])

def _write_timing_details(ws, scen):
    rows = [m for r in scen for m in r.get("per_model", [])]
    if rows:
        m = rows[0]
        ws.append([])
        ws.append(["Where the time goes, for one model"])
        ws[ws.max_row][0].font = Font(bold=True, size=12)
        ws.append(["Reading the SAP PD model", m.get("parse_pd_s"), KEY_SECONDS])
        ws.append(["Reading the erwin XML", m.get("parse_erwin_s"), KEY_SECONDS])
        ws.append(["Comparing them", m.get("compare_s"), KEY_SECONDS])

    if any(r.get("repeats_used") for r in scen):
        ws.append([])
        ws.append(["Note: not enough models available, so one model was reused to fill the batch."])

def _write_summary(wb, scanned, run_ts, note):
    ws = wb.create_sheet("SUMMARY", 0)
    ws.append(["Migration Provenance Audit"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["Generated", run_ts])
    ws.append(["Project", PROJECT_ROOT])
    if note:
        ws.append(["Run note", note])
    ws.append([])
    ws.append([KEY_MODEL, "Type", "Validation", "Fidelity %",
               "PD Comments in source", "PD Definitions in source",
               "Pipeline done", "Separate script done", "Manual done",
               "Manual PENDING", "Not implemented", "Blockers"])
    hdr = ws.max_row
    
    _populate_summary_rows(ws, scanned)
    _style_summary_sheet(ws, hdr)

def _populate_summary_rows(ws, scanned):
    for ctx, steps in scanned:
        c = Counter((s[KEY_PROVENANCE], s[KEY_OUTCOME]) for s in steps)
        blockers = sum(1 for s in steps if s[KEY_OUTCOME] in (PENDING, FAILED))
        v = ctx[KEY_VALIDATION] or {}
        ws.append([ctx[KEY_BASE], ctx[KEY_SUFFIX], v.get(KEY_STATUS, "-"),
                   v.get(KEY_FIDELITY, "-"),
                   totals(ctx[KEY_PD_INV], KEY_COMMENT),
                   totals(ctx[KEY_PD_INV], KEY_DEFINITION),
                   c[(AUTOMATED, DONE)], c[(SCRIPT, DONE)], c[(MANUAL, DONE)],
                   c[(MANUAL, PENDING)],
                   sum(n for (p, _o), n in c.items() if p == NOT_IMPL),
                   blockers])

def _style_summary_sheet(ws, hdr):
    for cell in ws[hdr]:
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
    for row in ws.iter_rows(min_row=hdr + 1, min_col=3, max_col=3):
        if row[0].value in STATUS_FILL:
            row[0].fill = STATUS_FILL[row[0].value]
    for row in ws.iter_rows(min_row=hdr + 1, min_col=12, max_col=12):
        if isinstance(row[0].value, int) and row[0].value > 0:
            row[0].fill = OUTCOME_FILL[FAILED]
    style(ws, [30, 8, 10, 10, 22, 23, 14, 20, 14, 17, 17, 9])

def append_ledger(scanned, run_ts, note):
    """Append-only history, so past state is never overwritten."""
    os.makedirs(AUDIT_DIR, exist_ok=True)
    with open(LEDGER_PATH, "a", encoding=ENC_UTF8) as fh:
        for ctx, steps in scanned:
            fh.write(json.dumps({
                "run": run_ts,
                KEY_NOTE: note,
                "model": ctx[KEY_BASE],
                "type": ctx[KEY_SUFFIX],
                KEY_VALIDATION: ctx[KEY_VALIDATION],
                "steps": [{k: s[k] for k in
                           (KEY_STEP, KEY_PHASE, KEY_PROVENANCE, KEY_OUTCOME,
                            KEY_EVIDENCE, KEY_WHEN)} for s in steps],
            }, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Provenance audit: what was automated, scripted or manual.")
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
    
    _print_audit_report(run_ts, scanned)

def _print_audit_report(run_ts, scanned):
    print("=" * 78)
    print("MIGRATION PROVENANCE AUDIT   %s" % run_ts)
    print("=" * 78)
    for ctx, steps in scanned:
        _print_model_audit(ctx, steps)
        
    t = latest_timings()
    if t and t.get("scenarios"):
        print()
        for r in t["scenarios"]:
            print("     %2d model(s) takes %.1f seconds" % (r.get(KEY_SIZE, 0), r.get("end_to_end_s", 0)))

    print("\n  report  -> %s" % rel(REPORT_PATH))
    print("  ledger  -> %s" % rel(LEDGER_PATH))

def _print_model_audit(ctx, steps):
    c = Counter((s[KEY_PROVENANCE], s[KEY_OUTCOME]) for s in steps)
    blockers = [s for s in steps if s[KEY_OUTCOME] in (PENDING, FAILED)]
    v = ctx[KEY_VALIDATION] or {}
    print("\n  %s  (%s)" % (ctx[KEY_BASE], ctx[KEY_SUFFIX]))
    print("     validation      : %s %s%%" % (v.get(KEY_STATUS, "-"), v.get(KEY_FIDELITY, "-")))
    print("     source content  :")
    for kind in ctx["kinds"]:
        pdk, erk = ctx[KEY_PD_INV][kind], ctx[KEY_ER_INV][kind]
        print("        %-13s PD %3d objects | Comment %3d -> Note %3d | "
              "Definition %3d -> Definition %3d"
              % (kind, pdk[KEY_TOTAL], pdk[KEY_COMMENT], erk[KEY_NOTE],
                 pdk[KEY_DEFINITION], erk[KEY_DEFINITION]))
    print("     pipeline done   : %d" % c[(PIPELINE, DONE)])
    print("     separate script : %d" % c[(SCRIPT, DONE)])
    print("     manual done     : %d" % c[(MANUAL, DONE)])
    print("     manual PENDING  : %d" % c[(MANUAL, PENDING)])
    print("     not implemented : %d" % sum(n for (p, _o), n in c.items() if p == NOT_IMPL))
    if blockers:
        print("     BLOCKERS:")
        for s in blockers:
            print("        [%s/%s] %s" % (s[KEY_PROVENANCE], s[KEY_OUTCOME], s[KEY_STEP]))

