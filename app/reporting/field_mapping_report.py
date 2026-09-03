#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
field_mapping_report.py
=======================

Field-by-field reconciliation of the TEXT properties that carry business
meaning, reported in a SEPARATE SECTION PER FIELD PAIR.

WHY THIS EXISTS
---------------
The main reconciliation report (`ldm_validation_report.xlsx`) cannot answer
"which Comment became a Note, and which Description became a Definition",
because both parsers collapse every text property into ONE field before the
comparison happens:

    pd_ldm_parser.py    _description()  ->  first non-empty of
                                            Comment, Description, Annotation, Definition
    erwin_ldm_parser.py _definition()   ->  first non-empty of
                                            Definition, Comment, Note, Description, ...

So PD's *Comment* is silently compared against erwin's *Definition*. On the
SUBSURFACE AND WELLS model that is 307 Comments measured against 134
Definitions -- different fields, so the mismatch is guaranteed and the
resulting DEFINITION findings are not meaningful.

This script reads both XML files directly, keeps every text property
separate, and reports each mapping in its own worksheet.

THE MAPPINGS
------------
Configured in FIELD_MAPPINGS below. Edit that table if your migration
convention differs -- it is a business decision, not a technical one.

    PD Comment (General tab)   ->  erwin Note
    PD Definition tab (stored
    as Description, RTF)       ->  erwin Definition

Entities, attributes AND relationships are all covered, matching exactly what
pd_comment_to_erwin_note.py migrates.

USAGE
-----
    python app/reporting/field_mapping_report.py \\
        --ldm   "sappdmodels/ldm/MODEL.ldm" \\
        --erwin "erwinmodels/2_preprocessed/xml/MODEL_notes.xml"

Point --erwin at the *_notes.xml if you want the Comment->Note section to
show the migrated notes. Point it at 1_initial/xml to see the state before
the notes migration.

Output: app/reporting/ldm_reports/field_mapping_report.xlsx
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, OrderedDict

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# --------------------------------------------------------------------------
# WHICH PD FIELD IS EXPECTED TO LAND IN WHICH erwin FIELD
# --------------------------------------------------------------------------
# (sheet title, PD property, erwin property, short explanation)
#
# These pairings were derived empirically, by cross-tabulating every PD text
# field against every erwin text field on SUBSURFACE AND WELLS and counting
# exact matches:
#
#                   erwin Definition   erwin Comment   erwin Note
#   PD Comment                    70             691          691
#   PD Description                 0               0            0
#   PD Annotation                  0               0            0
#
# Conclusion: PD *Comment* is the single carrier of business meaning, and it
# feeds all three erwin properties. PD *Description* is an RTF-wrapped
# duplicate of Comment (same text, plus a font table), not an independent
# field. PD *Annotation* is modelling commentary that was never migrated.
#
FIELD_MAPPINGS = [
    ("COMMENT_to_NOTE", "Comment", "Note",
     "SAP PD Comment", "erwin Note",
     "PD Comment (General tab) -> erwin Note. Written by "
     "pd_comment_to_erwin_note.py migrate."),
    ("DEFINITION_to_DEFINITION", "Description", "Definition",
     "SAP PD Definition", "erwin Definition",
     "PD Definition tab (stored as Description, RTF) -> erwin Definition. "
     "Written by the same tool with --definitions."),
]

# A PHYSICAL model migrates its documentation differently: there is no
# Comment->Note preprocessing step in the PDM flow, and erwin's importer puts
# the PD Comment straight into both Definition and Comment. Reporting a .pdm
# against the Note convention showed every object as "missing in erwin" when
# the text had in fact crossed -- so each model type gets the mapping table
# that matches how it is actually migrated.
FIELD_MAPPINGS_PDM = [
    ("COMMENT_to_DEFINITION", "Comment", "Definition",
     "SAP PD Comment", "erwin Definition",
     "PD Comment (General tab) -> erwin Definition. Written by erwin's own "
     "importer when the physical model is imported."),
    ("COMMENT_to_COMMENT", "Comment", "Comment",
     "SAP PD Comment", "erwin Comment",
     "PD Comment -> erwin Comment. The same text, kept in erwin's Comment "
     "property as well as its Definition."),
]

FIELD_MAPPINGS_FOR = {".pdm": FIELD_MAPPINGS_PDM}


def mappings_for(path_or_suffix: str):
    """The field mapping table that matches this model type."""
    suffix = path_or_suffix if path_or_suffix.startswith(".") else \
        os.path.splitext(path_or_suffix)[1]
    return FIELD_MAPPINGS_FOR.get(suffix.lower(), FIELD_MAPPINGS)


PD_FIELDS = ["Comment", "Description"]
ERWIN_FIELDS = ["Note", "Definition", "Comment"]

SEP = r"\#x1F"
IDX_TEXT = 6

# --------------------------------------------------------------------------
# CONFIGURED DIRECTORIES
# --------------------------------------------------------------------------
# Anchored to THIS FILE, not to the current directory, so the script behaves
# identically whether you launch it from a terminal in the project root or by
# pressing VS Code's Run button (which starts in the workspace folder, often
# one level up). All arguments are optional when each folder holds exactly one
# candidate file.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, os.pardir, os.pardir))

# All three SAP model types live under sappdmodels/. CDM, LDM and PDM use the
# same XML shapes on both sides, so one reader serves all three.
MODEL_INPUT_DIRS = OrderedDict((
    (".ldm", os.path.join(PROJECT_ROOT, "sappdmodels", "ldm")),
    (".cdm", os.path.join(PROJECT_ROOT, "sappdmodels", "cdm")),
    (".pdm", os.path.join(PROJECT_ROOT, "sappdmodels", "pdm")),
))
# Prefer the notes-enriched export so COMMENT_to_NOTE has data; fall back to raw.
ERWIN_INPUT_DIRS = [
    os.path.join(PROJECT_ROOT, "erwinmodels", "2_preprocessed", "xml"),
    os.path.join(PROJECT_ROOT, "erwinmodels", "1_initial", "xml"),
]
REPORT_DIR_FOR = {
    ".ldm": os.path.join(PROJECT_ROOT, "app", "reporting", "ldm_reports"),
    ".cdm": os.path.join(PROJECT_ROOT, "app", "reporting", "cdm_reports"),
    ".pdm": os.path.join(PROJECT_ROOT, "app", "reporting", "pdm_reports"),
}


def discover_models():
    """Every SAP model present, any type. Multiple models is normal, not an error."""
    found = []
    for suffix, d in MODEL_INPUT_DIRS.items():
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(suffix) and os.path.isfile(os.path.join(d, f)):
                found.append((suffix, os.path.join(d, f)))
    return found


def erwin_for(base):
    """The erwin export matching this model's stem, preprocessed preferred."""
    for d in ERWIN_INPUT_DIRS:
        for cand in (base + "_notes.xml", base + ".xml"):
            p = os.path.join(d, cand)
            if os.path.isfile(p):
                return p
    return ""


def autodiscover(dirs, extensions, what, flag):
    """Find the single candidate file across `dirs`, or explain why not."""
    if isinstance(dirs, str):
        dirs = [dirs]
    for d in dirs:
        if not os.path.isdir(d):
            continue
        found = sorted(f for f in os.listdir(d)
                       if f.lower().endswith(extensions)
                       and os.path.isfile(os.path.join(d, f)))
        if len(found) == 1:
            return os.path.join(d, found[0])
        if len(found) > 1:
            raise SystemExit(
                "ERROR: %d %s files found in:\n    %s\n%s\n"
                "Pass the one you want with --%s"
                % (len(found), what, d,
                   "\n".join("      " + f for f in found), flag))
    raise SystemExit(
        "ERROR: no %s file found. Looked in:\n%s\n"
        "Pass it explicitly with --%s <path>"
        % (what, "\n".join("    " + d for d in dirs), flag))

HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(bold=True, color="FFFFFF")
STATUS_FILL = {
    "MATCHED":          PatternFill("solid", fgColor="C6EFCE"),
    "TEXT_DIFFERS":     PatternFill("solid", fgColor="FFEB9C"),
    "MISSING_IN_ERWIN": PatternFill("solid", fgColor="FFC7CE"),
    "EXTRA_IN_ERWIN":   PatternFill("solid", fgColor="DDEBF7"),
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def localname(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def norm(text: str) -> str:
    """Whitespace-insensitive comparison key. Case IS significant."""
    return re.sub(r"\s+", " ", (text or "")).strip()


RTF_RE = re.compile(r"^\s*\{\\rtf", re.I)

# Header tables carry no business text, only font/colour/style declarations.
# They are BRACE-NESTED, so a plain regex leaves residue behind -- which is
# how 'Futura Medium; Times New Roman; ; ;' ended up prefixed to real
# definition text. Remove them by matching braces properly.
RTF_TABLES = ("fonttbl", "colortbl", "stylesheet", "listtable",
              "listoverridetable", "rsidtbl", "generator", "info")


def _drop_rtf_tables(s: str) -> str:
    out, i, n = [], 0, len(s)
    while i < n:
        if s[i] == "{":
            head = s[i + 1:i + 24]
            if head.startswith("\\") and any(
                    head[1:].startswith(t) for t in RTF_TABLES):
                depth, j = 0, i
                while j < n:                       # skip the balanced group
                    if s[j] == "{":
                        depth += 1
                    elif s[j] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                i = j + 1
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def strip_rtf(text: str) -> str:
    """
    PD stores rich text as RTF. Reduce it to readable plain text.

    Deliberately conservative: if the result would be empty but the input was
    not, the raw value is returned instead, so a real definition can never be
    silently discarded. (Same failure mode documented in pd_ldm_parser.py.)
    """
    if not text or not RTF_RE.match(text):
        return text or ""
    s = _drop_rtf_tables(text)
    s = re.sub(r"\{\\\*?\\[^{}]*\}", " ", s)      # remaining control groups
    s = re.sub(r"\\par[d]?\b", "\n", s)
    s = re.sub(r"\\tab\b", "\t", s)
    s = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", s)     # remaining control words
    s = s.replace("{", " ").replace("}", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*", "\n", s).strip()
    return s if s else text


def decode_note_text(record: str) -> str:
    """Pull field[6] out of an erwin Note_List record and un-escape it."""
    fields = record.split(SEP)
    if len(fields) <= IDX_TEXT:
        return ""
    text = fields[IDX_TEXT]
    text = re.sub(r"\\#x([0-9A-Fa-f]{2})",
                  lambda m: chr(int(m.group(1), 16)), text)
    return text


# --------------------------------------------------------------------------
# SAP PowerDesigner reader -- every text field kept SEPARATE
# --------------------------------------------------------------------------

# A physical model is built from Tables, Columns and References; a conceptual
# or logical one from Entities, EntityAttributes and Relationships. erwin holds
# all three types in ONE physical metamodel (Entity / Attribute / Relationship),
# so the SAP tags are normalised onto those internal kind names here — that is
# what lets the pairing logic below stay single-purpose. Reading only the
# Entity tags meant every .pdm produced zero PD rows, and the report then
# showed the whole model as "EXTRA_IN_ERWIN".
PD_OBJECT_TAGS = {
    ".pdm": {"entity": "Table", "attribute": "Column",
             "relationship": "Reference"},
    "default": {"entity": "Entity", "attribute": "EntityAttribute",
                "relationship": "Relationship"},
}


def pd_tags_for(path: str):
    suffix = os.path.splitext(path)[1].lower()
    return PD_OBJECT_TAGS.get(suffix, PD_OBJECT_TAGS["default"])


def load_pd(path: str):
    """-> OrderedDict[key] = {kind, entity, name, code, <PD_FIELDS>...}"""
    try:
        from lxml import etree as ET
        tree = ET.parse(path, ET.XMLParser(huge_tree=True))
    except ImportError:
        import xml.etree.ElementTree as ET  # nosec B405
        from defusedxml.ElementTree import parse as safe_parse, iterparse as safe_iterparse
        tree = safe_parse(path)

    A, O = "{attribute}", "{object}"
    tags = pd_tags_for(path)
    out = OrderedDict()

    def txt(el, name):
        """
        A PD scalar, plain (<a:Comment>) or class-qualified
        (<a:Table.Comment>) — physical models use the qualified spelling for
        several fields, and reading only the plain one made them look empty.
        """
        ch = el.find(A + name)
        if ch is not None and ch.text and ch.text.strip():
            return strip_rtf(ch.text)
        needle = "." + name
        for child in el:
            tag = child.tag
            if isinstance(tag, str) and tag.startswith(A):
                local = tag[len(A):]
                if local == name or local.endswith(needle):
                    if child.text and child.text.strip():
                        return strip_rtf(child.text)
        return ""

    for ent in tree.getroot().iter(O + tags["entity"]):
        if ent.get("Id") is None:          # <o:Entity Ref=".."/> is a pointer
            continue
        ename, ecode = txt(ent, "Name"), txt(ent, "Code")
        rec = {"kind": "Entity", "entity": ename, "name": ename, "code": ecode}
        for f in PD_FIELDS:
            rec[f] = txt(ent, f)
        out[("Entity", ename, ecode)] = rec

        for att in ent.iter(O + tags["attribute"]):
            if att.get("Id") is None:
                continue
            aname, acode = txt(att, "Name"), txt(att, "Code")
            arec = {"kind": "Attribute", "entity": ename, "name": aname, "code": acode}
            for f in PD_FIELDS:
                arec[f] = txt(att, f)
            out[("Attribute", ename, aname, acode)] = arec

    # ---- relationships ---------------------------------------------------
    # PD names each end via <c:Object1><o:Entity Ref="oNNN"/> -- the COLLECTION
    # namespace. Resolve both ends to entity names so a relationship can be
    # paired with its erwin counterpart, whose name PD does not preserve.
    by_id = {}
    for ent in tree.getroot().iter(O + tags["entity"]):
        if ent.get("Id") is not None:
            by_id[ent.get("Id")] = txt(ent, "Name")

    def end_name(rel, role):
        for holder in rel:
            if holder.tag.rsplit("}", 1)[-1] != role:
                continue
            for child in holder:
                ref = child.get("Ref")
                if ref and ref in by_id:
                    return by_id[ref]
        return ""

    for rel in tree.getroot().iter(O + tags["relationship"]):
        if rel.get("Id") is None:
            continue
        rname, rcode = txt(rel, "Name"), txt(rel, "Code")
        # A conceptual/logical Relationship names its ends Object1/Object2;
        # a physical Reference names them ParentTable/ChildTable.
        ends = tuple(sorted(
            [end_name(rel, "Object1") or end_name(rel, "ParentTable"),
             end_name(rel, "Object2") or end_name(rel, "ChildTable")]))
        rrec = {"kind": "Relationship", "entity": " <-> ".join(ends),
                "name": rname, "code": rcode, "ends": ends}
        for f in PD_FIELDS:
            rrec[f] = txt(rel, f)
        out[("Relationship", rname, rcode) + ends] = rrec
    return out


# --------------------------------------------------------------------------
# erwin reader -- Definition, Comment and Note kept SEPARATE
# --------------------------------------------------------------------------

def load_erwin(path: str):
    """-> OrderedDict[key] = {kind, entity, name, physical_name, <ERWIN_FIELDS>...}"""
    from lxml import etree
    tree = etree.parse(path, etree.XMLParser(huge_tree=True))

    # erwin relationships reference their ends by id; build the lookup first.
    ent_by_id = {}
    for el in tree.getroot().iter():
        if localname(el.tag) == "Entity" and el.get("id"):
            ent_by_id[el.get("id")] = el.get("name") or ""

    out = OrderedDict()
    cur_entity = None

    for el in tree.getroot().iter():
        ln = localname(el.tag)
        if ln not in ("EntityProps", "AttributeProps", "RelationshipProps"):
            continue

        kind = {"EntityProps": "Entity", "AttributeProps": "Attribute",
                "RelationshipProps": "Relationship"}[ln]
        rec = {"kind": kind, "name": "", "physical_name": "",
               "Definition": "", "Comment": "", "Note": ""}

        notes = []
        parent_ref = child_ref = ""
        for ch in el:
            c = localname(ch.tag)
            if c == "Parent_Entity_Ref" and not parent_ref:
                parent_ref = ch.text or ""
            elif c == "Child_Entity_Ref" and not child_ref:
                child_ref = ch.text or ""
            elif c == "Name" and not rec["name"]:
                rec["name"] = ch.text or ""
            elif c == "Physical_Name" and not rec["physical_name"]:
                rec["physical_name"] = ch.text or ""
            elif c == "Definition" and not rec["Definition"]:
                rec["Definition"] = ch.text or ""
            elif c == "Comment" and not rec["Comment"]:
                rec["Comment"] = ch.text or ""
            elif c == "Note_List_Array":
                for nl in ch:
                    if localname(nl.tag) == "Note_List":
                        t = decode_note_text(nl.text or "")
                        if t.strip():
                            notes.append(t)
        rec["Note"] = "\n---\n".join(notes)

        if kind == "Entity":
            cur_entity = rec["name"]
            rec["entity"] = rec["name"]
            out[("Entity", rec["name"], rec["physical_name"])] = rec
        elif kind == "Relationship":
            ends = tuple(sorted([ent_by_id.get(parent_ref, ""),
                                 ent_by_id.get(child_ref, "")]))
            rec["ends"] = ends
            rec["entity"] = " <-> ".join(ends)
            out[("Relationship", rec["name"]) + ends] = rec
        else:
            rec["entity"] = cur_entity
            out[("Attribute", cur_entity, rec["name"], rec["physical_name"])] = rec
    return out


# --------------------------------------------------------------------------
# pairing
# --------------------------------------------------------------------------

def pair(pd_objs, er_objs):
    """
    Tiered match, strongest first:
      1  Name + Code == Name + Physical_Name
      2  Code == Physical_Name        (business name drifted)
      3  Name == Name, case-folded    (technical code drifted)
    Returns (pairs, pd_only, erwin_only). pairs = [(pd_rec, er_rec, tier)]
    """
    pairs, used = [], set()

    er_by_code, er_by_name = {}, {}
    # Relationships are keyed by the pair of entities they join, refined by
    # name -- PD leaves most relationship names auto-generated while erwin
    # renames them, so name alone is not an identity.
    er_rel_by_pair_name, er_rel_by_pair = {}, {}
    for k, r in er_objs.items():
        if r["kind"] == "Relationship":
            pair = r.get("ends", ("", ""))
            er_rel_by_pair.setdefault(pair, []).append(k)
            er_rel_by_pair_name.setdefault(
                (pair, (r["name"] or "").strip().lower()), []).append(k)
            continue
        if r["kind"] == "Entity":
            er_by_code.setdefault(("Entity", r["physical_name"]), []).append(k)
            er_by_name.setdefault(("Entity", (r["name"] or "").lower()), []).append(k)
        else:
            er_by_code.setdefault(("Attribute", r["entity"], r["physical_name"]), []).append(k)
            er_by_name.setdefault(("Attribute", r["entity"], (r["name"] or "").lower()), []).append(k)

    for k, p in pd_objs.items():
        hit = tier = None
        if p["kind"] == "Relationship":
            pair = p.get("ends", ("", ""))
            c = er_rel_by_pair_name.get(
                (pair, (p["name"] or "").strip().lower()), [])
            if len(c) == 1:
                hit, tier = c[0], 1
            else:
                c = [x for x in er_rel_by_pair.get(pair, []) if x not in used]
                if len(c) == 1:
                    hit, tier = c[0], 2
        elif k in er_objs:
            hit, tier = k, 1
        else:
            if p["kind"] == "Entity":
                c = er_by_code.get(("Entity", p["code"]), [])
                n = er_by_name.get(("Entity", (p["name"] or "").lower()), [])
            else:
                c = er_by_code.get(("Attribute", p["entity"], p["code"]), [])
                n = er_by_name.get(("Attribute", p["entity"], (p["name"] or "").lower()), [])
            if len(c) == 1:
                hit, tier = c[0], 2
            elif len(n) == 1:
                hit, tier = n[0], 3

        if hit is not None and hit not in used:
            used.add(hit)
            pairs.append((p, er_objs[hit], tier))
        else:
            pairs.append((p, None, None))

    erwin_only = [er_objs[k] for k in er_objs if k not in used]
    return pairs, erwin_only


def classify(pd_text: str, er_text: str) -> str:
    p, e = norm(pd_text), norm(er_text)
    if not p and not e:
        return "BOTH_EMPTY"
    if p and not e:
        return "MISSING_IN_ERWIN"
    if e and not p:
        return "EXTRA_IN_ERWIN"
    return "MATCHED" if p == e else "TEXT_DIFFERS"


# --------------------------------------------------------------------------
# workbook
# --------------------------------------------------------------------------

def style_header(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for cell in ws[1]:
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def clip(s, n=32000):
    s = s or ""
    return s if len(s) <= n else s[:n] + " ...[truncated]"


def build_workbook(pairs, erwin_only, pd_objs, er_objs, ldm, erwin, out_path,
                   mappings=None):
    wb = Workbook()
    wb.remove(wb.active)

    mappings = mappings or mappings_for(ldm)
    per_map_stats = OrderedDict()

    # ---- one sheet per field mapping ------------------------------------
    for title, pd_field, er_field, pd_label, er_label, blurb in mappings:
        ws = wb.create_sheet(title[:31])
        ws.append(["Object Type", "Entity", "Object Name", "Code / Physical Name",
                   "Match Tier", "Status",
                   pd_label, er_label,
                   "PD chars", "erwin chars"])
        stats = Counter()
        by_kind = Counter()          # (kind, status) -> n, for the summary

        for p, e, tier in pairs:
            pd_text = p.get(pd_field, "")
            er_text = e.get(er_field, "") if e else ""
            status = "UNMATCHED_OBJECT" if e is None else classify(pd_text, er_text)
            stats[status] += 1
            by_kind[(p["kind"], status)] += 1
            if status == "BOTH_EMPTY":
                continue                       # nothing to report on this row
            ws.append([
                p["kind"], p["entity"], p["name"], p["code"],
                tier or "", status,
                clip(pd_text), clip(er_text),
                len(pd_text or ""), len(er_text or ""),
            ])

        for row in ws.iter_rows(min_row=2, min_col=6, max_col=6):
            f = STATUS_FILL.get(row[0].value)
            if f:
                row[0].fill = f
        for row in ws.iter_rows(min_row=2, min_col=7, max_col=8):
            for c in row:
                c.alignment = Alignment(vertical="top", wrap_text=True)
        style_header(ws, [12, 26, 30, 26, 10, 18, 70, 70, 10, 11])
        per_map_stats[title] = (pd_label, er_label, blurb, stats, by_kind)

    # ---- unmatched objects ----------------------------------------------
    # A bare list of unmatched names invites the reader to assume something
    # broke. Most "erwin only" attributes are foreign keys erwin propagated
    # along a relationship -- they have no PowerDesigner counterpart by design,
    # because PD models the relationship and leaves the FK implicit. Anything
    # NOT explained that way is genuine drift and worth a look.
    rel_partners = {}
    for r in pd_objs.values():
        if r["kind"] == "Relationship":
            a, b = r.get("ends", ("", ""))
            rel_partners.setdefault(a, set()).add(b)
            rel_partners.setdefault(b, set()).add(a)

    def why_erwin_only(rec):
        """
        Explain an erwin-only object.

        Matching is deliberately tiered so a confident claim is never made on
        weak evidence: a FULL entity-name prefix is stated as fact, a mere
        stem match is hedged. Without this, DISTRIBUTOR.Customer_Master_ID got
        credited to CUSTOMER_GROUP purely because both start with "CUSTOMER".
        """
        if rec["kind"] != "Attribute":
            return "No SAP PD counterpart"
        owner = rec.get("entity") or ""
        code = norm(rec.get("physical_name", "")).upper()
        name = norm(rec.get("name", "")).upper().replace(" ", "_")
        partners = sorted(rel_partners.get(owner, ()), key=len, reverse=True)

        for partner in partners:                     # tier 1: full token
            token = norm(partner).upper().replace(" ", "_")
            if token and (code.startswith(token) or name.startswith(token)):
                return ("Foreign key erwin propagated from %s (expected - PD "
                        "leaves FKs implicit in the relationship)" % partner)

        for partner in partners:                     # tier 2: stem only
            stem = norm(partner).upper().replace(" ", "_").split("_")[0]
            if len(stem) >= 4 and (code.startswith(stem) or name.startswith(stem)):
                return ("Possibly a foreign key from %s - only the name stem "
                        "'%s' matches, so confirm before assuming" % (partner, stem))

        return ("erwin-only attribute - not explained by any relationship. "
                "Either added directly in erwin, or removed from SAP PD.")

    ws = wb.create_sheet("UNMATCHED")
    ws.append(["Side", "Object Type", "Entity", "Object Name",
               "Code / Physical Name", "Likely reason"])
    for p, e, _ in pairs:
        if e is None:
            ws.append(["SAP PD only", p["kind"], p["entity"], p["name"], p["code"],
                       "Present in SAP PD but not found in the erwin export"])
    for r in erwin_only:
        ws.append(["erwin only", r["kind"], r.get("entity", ""), r["name"],
                   r["physical_name"], why_erwin_only(r)])
    for row in ws.iter_rows(min_row=2, min_col=6, max_col=6):
        row[0].alignment = Alignment(vertical="top", wrap_text=True)
    style_header(ws, [14, 12, 26, 30, 26, 66])

    # ---- field coverage --------------------------------------------------
    ws = wb.create_sheet("FIELD_COVERAGE")
    ws.append(["Side", "Field", "Objects carrying text", "Total objects", "% populated"])
    for f in PD_FIELDS:
        n = sum(1 for r in pd_objs.values() if norm(r.get(f, "")))
        ws.append(["SAP PD", f, n, len(pd_objs),
                   round(100.0 * n / max(1, len(pd_objs)), 2)])
    for f in ERWIN_FIELDS:
        n = sum(1 for r in er_objs.values() if norm(r.get(f, "")))
        ws.append(["erwin", f, n, len(er_objs),
                   round(100.0 * n / max(1, len(er_objs)), 2)])
    style_header(ws, [12, 22, 24, 16, 14])

    # ---- summary (first sheet) ------------------------------------------
    ws = wb.create_sheet("SUMMARY", 0)
    ws.append(["Text-Property Mapping Report"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(["SAP PD model", ldm])
    ws.append(["erwin XML", erwin])
    ws.append(["PD objects", len(pd_objs)])
    ws.append(["erwin objects", len(er_objs)])
    ws.append(["matched", sum(1 for _, e, _ in pairs if e is not None)])
    ws.append([])

    ws.append(["Mapping", "SAP PD field", "erwin field", "Matched",
               "Text differs", "Missing in erwin", "Extra in erwin",
               "Both empty", "Notes"])
    hdr_row = ws.max_row
    for title, (pd_f, er_f, blurb, st, _bk) in per_map_stats.items():
        ws.append([title, pd_f, er_f,
                   st["MATCHED"], st["TEXT_DIFFERS"], st["MISSING_IN_ERWIN"],
                   st["EXTRA_IN_ERWIN"], st["BOTH_EMPTY"], blurb])
    for cell in ws[hdr_row]:
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for i, w in enumerate([30, 16, 16, 11, 13, 18, 16, 12, 62], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # ---- breakdown by object kind ---------------------------------------
    # Both mappings cover Entity, Attribute AND Relationship. When a section
    # looks empty for one kind it is because the source model has no text in
    # that field for that kind -- not because the kind was skipped. This table
    # makes the difference visible.
    ws.append([])
    ws.append(["Breakdown by object kind"])
    ws[ws.max_row][0].font = Font(bold=True, size=12)
    ws.append(["Mapping", "Object kind", "Matched", "Text differs",
               "Missing in erwin", "Extra in erwin", "Nothing on either side"])
    hdr2 = ws.max_row
    for title, (_pd_f, _er_f, _b, _st, bk) in per_map_stats.items():
        for kind in ("Entity", "Attribute", "Relationship"):
            ws.append([title, kind,
                       bk[(kind, "MATCHED")], bk[(kind, "TEXT_DIFFERS")],
                       bk[(kind, "MISSING_IN_ERWIN")], bk[(kind, "EXTRA_IN_ERWIN")],
                       bk[(kind, "BOTH_EMPTY")]])
    for cell in ws[hdr2]:
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    wb.save(out_path)
    return per_map_stats


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Comment->Note and Definition->Definition report. "
                    "Processes EVERY model found unless --ldm names one.")
    ap.add_argument("--ldm", help="one model file; omit to do all of them")
    ap.add_argument("--erwin", help="erwin XML for that one model")
    ap.add_argument("--out", help="output .xlsx for that one model")
    args = ap.parse_args(argv)

    if args.ldm:
        suffix = os.path.splitext(args.ldm)[1].lower()
        base = os.path.splitext(os.path.basename(args.ldm))[0]
        jobs = [(suffix, args.ldm, args.erwin or erwin_for(base))]
    else:
        jobs = [(sfx, p, erwin_for(os.path.splitext(os.path.basename(p))[0]))
                for sfx, p in discover_models()]
        if not jobs:
            raise SystemExit("No SAP model found under sappdmodels/")

    done, skipped = 0, []
    for suffix, ldm_path, erwin_path in jobs:
        base = os.path.splitext(os.path.basename(ldm_path))[0]
        if not erwin_path:
            skipped.append(base)
            continue

        out = args.out or os.path.join(
            REPORT_DIR_FOR.get(suffix, REPORT_DIR_FOR[".ldm"]),
            f"{base}_field_mapping.xlsx")

        pd_objs = load_pd(ldm_path)
        er_objs = load_erwin(erwin_path)
        pairs, erwin_only = pair(pd_objs, er_objs)
        stats = build_workbook(pairs, erwin_only, pd_objs, er_objs,
                               ldm_path, erwin_path, out)
        done += 1

        print()
        print("  %s" % base)
        for title, (_pd, _er, _b, st, bk) in stats.items():
            print("     %-26s matched %-5d differs %-4d missing %d"
                  % (title, st["MATCHED"], st["TEXT_DIFFERS"],
                     st["MISSING_IN_ERWIN"]))
        print("     -> %s" % os.path.relpath(out, PROJECT_ROOT))

    print()
    print("  %d report(s) written." % done)
    if skipped:
        print("  %d model(s) skipped, no erwin XML export yet:" % len(skipped))
        for b in skipped:
            print("     %s" % b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
