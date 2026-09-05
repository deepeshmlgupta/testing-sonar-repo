#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pd_comment_to_erwin_note.py
===========================

Migrate SAP PowerDesigner LDM **Comments** into erwin Data Modeler **Notes**,
operating purely on the two XML model files.

    SAP PD  <a:Comment>            -->     erwin  Note_List_Array/Note_List

Nothing else is touched.  `Definition`, `Comment`, `Name`, `Physical_Name`,
relationships, keys, diagrams, history and every other property are preserved
**byte for byte**.


WHY THIS SCRIPT DOES A BYTE-LEVEL SPLICE
----------------------------------------
An erwin XML export cannot be safely round-tripped through a DOM writer:

  1. The export carries ~2,187 literal CRLF sequences *inside* <Definition>
     and <Comment> text.  XML line-end normalisation (a hard requirement of
     the XML spec, applied by lxml, ElementTree, expat - everything) silently
     rewrites those to LF.  Re-serialising therefore MODIFIES Definition text,
     which this task explicitly forbids.
  2. erwin writes ~952 empty elements in long form (<Disposition></Disposition>).
     Every DOM writer collapses them to <Disposition/>.
  3. lxml reorders the xmlns declaration on <EMX:Model> ahead of id/name.

A naive parse+write of the supplied 22,576,946-byte export produces
22,550,616 bytes - 26,330 bytes of unrequested change.

So: the XML is *parsed* (with expat, which reports exact byte offsets) only to
LOCATE things, and the output is produced by splicing note fragments into the
original byte stream.  Everything outside the inserted regions is guaranteed
identical, and `verify` proves it.


THE erwin NOTE STRUCTURE (discovered, not assumed)
--------------------------------------------------
Derived by diffing the supplied original export against the export in which
Notes were added by hand in erwin and verified in the tool.  The *only*
property that differs between the two files, across all 781 Entity/Attribute
objects, is `Note_List_Array`.

    <Note_List_Array>
      <Note_List HandleNonPrintableChar="Y" index="0">...record...</Note_List>
    </Note_List_Array>

Placement: immediately after <User_Formatted_Physical_Name>, inside
<EntityProps> / <AttributeProps>.  (Anchor present on 781/781 objects.)

The record is a flat string of 12 fields joined by the six-character ASCII
literal  \\#x1F  - a backslash, then '#x1F'.  There are NO real 0x1F bytes in
the file; `HandleNonPrintableChar="Y"` declares that non-printable characters
are written in this \\#xNN form.

    field[ 0]  note number, 1-based        e.g. "1"
    field[ 1]  created timestamp           "YYYY-MM-DD HH:MM:SS"
    field[ 2]  creator
    field[ 3]  modified timestamp
    field[ 4]  modifier
    field[ 5]  (empty in the verified sample)
    field[ 6]  NOTE TEXT                   <-- the payload
    field[ 7]  flag                        "F"
    field[ 8]  (empty)
    field[ 9]  (empty)
    field[10]  note GUID                   "{XXXXXXXX-....}"
    field[11]  (empty - trailing separator)

Fields 5, 7, 8, 9 are reproduced exactly as erwin wrote them.


ONE ITEM TO CONFIRM IN erwin
-----------------------------
The hand-made sample note text was "asdfghjkl" - no newlines - so the file
gives no direct evidence of how erwin encodes a line break inside a note.
Since `HandleNonPrintableChar="Y"` is exactly the mechanism for non-printable
characters, the default here encodes LF as \\#x0A.  Use --newline-mode to
switch if erwin renders it differently:

    --newline-mode escaped-lf    (default)  LF -> \\#x0A
    --newline-mode escaped-crlf             LF -> \\#x0D\\#x0A
    --newline-mode raw                      literal newline in the record

Migrate a couple of entities first, open them in erwin, confirm the notes read
correctly, then run the full set.


DIRECTORIES
-----------
Three directories are configured in the CONFIGURED DIRECTORIES block below:

    ERWIN_INPUT_DIR   erwin XML export(s)          - input
    MODEL_INPUT_DIR   SAP PowerDesigner .ldm       - input
    OUTPUT_DIR        migrated XML + CSV report    - output

Because of these you can pass a bare filename, or omit the argument when the
directory holds exactly one candidate file.  An absolute path, or any path
containing a directory separator, is honoured as-is and bypasses them.


USAGE
-----
    # 1. show what is actually in the files (no assumptions, no writes)
    python pd_comment_to_erwin_note.py inspect

    # 1b. show the note structure learned from a reference file
    python pd_comment_to_erwin_note.py inspect \\
        --reference SUBSURFACE_AND_WELLS_updated_14_2026.xml

    # 2. migrate  ->  OUTPUT_DIR\\<erwin name>_notes.xml
    python pd_comment_to_erwin_note.py migrate --report

    # 3. prove only Notes changed
    python pd_comment_to_erwin_note.py verify \\
        --out SUBSURFACE_AND_WELLS_notes.xml

Explicit paths still work exactly as before:

    python pd_comment_to_erwin_note.py migrate \\
        --ldm  C:\\models\\SUBSURFACE_AND_WELLS.ldm \\
        --erwin C:\\in\\SUBSURFACE_AND_WELLS.xml \\
        --out  C:\\out\\SUBSURFACE_AND_WELLS_notes.xml \\
        --author PD_MIGRATION --report report.csv

Requires: lxml (for `verify` only).  `inspect` and `migrate` use the standard
library alone.
"""

from __future__ import annotations

KEY_ERWIN = "erwin"
EXT_XML = ".xml"
PROP_NAME = "Name"
PROP_COMMENT = "Comment"
KEY_KIND = "kind"
KEY_ENTITY = "entity"
KEY_PNAME = "physical_name"
ENC_UTF8 = "utf-8"
KEY_MIGRATE = "migrate"



import argparse
import csv
import os
import re
import sys
import uuid
import xml.parsers.expat as expat
from collections import Counter, OrderedDict
from datetime import datetime

STATUS_KEY = "status"
REASON_KEY = "reason"
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_SKIPPED = "SKIPPED"
STATUS_UNMATCHED = "UNMATCHED"
KIND_ENTITY = "Entity"
KIND_ATTRIBUTE = "Attribute"


# --------------------------------------------------------------------------
# erwin note record constants - all verified against the reference export
# --------------------------------------------------------------------------

SEP = r"\#x1F"                     # six literal chars: \ # x 1 F
NOTE_FIELD_COUNT = 12
IDX_NUMBER, IDX_CREATED, IDX_CREATOR = 0, 1, 2
IDX_MODIFIED, IDX_MODIFIER = 3, 4
IDX_TEXT = 6
IDX_FLAG = 7
IDX_GUID = 10

NOTE_FLAG_DEFAULT = "F"            # field[7] as erwin wrote it
ANCHOR_TAG = "User_Formatted_Physical_Name"
NOTE_ARRAY_TAG = "Note_List_Array"
PROPS_TAGS = ("EntityProps", "AttributeProps")

TS_FMT = "%Y-%m-%d %H:%M:%S"

# SAP PowerDesigner namespaces
PD_ATTR = "attribute"
PD_OBJ = "object"


# --------------------------------------------------------------------------
# CONFIGURED DIRECTORIES
# --------------------------------------------------------------------------
# Edit these three paths to match the machine the script runs on.
#
#   ERWIN_INPUT_DIR : where the erwin XML export(s) live
#   MODEL_INPUT_DIR : where the SAP PowerDesigner .ldm model(s) live
#   OUTPUT_DIR      : where the migrated XML / CSV report are written
#
# With these set you can pass a bare filename - or omit the argument entirely
# when the directory holds exactly one candidate file:
#
#   python pd_comment_to_erwin_note.py migrate
#   python pd_comment_to_erwin_note.py migrate --erwin SUBSURFACE_AND_WELLS.xml
#
# An argument containing a path separator, or an absolute path, is used as
# given and bypasses these directories entirely.
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ERWIN_INPUT_DIR = os.path.join(BASE_DIR, "Erwin_file")
MODEL_INPUT_DIR = os.path.join(BASE_DIR, "Model_dir")
OUTPUT_DIR = os.path.join(BASE_DIR, "Output_dir")

# suffix appended to the erwin input name when --out is not supplied
DEFAULT_OUTPUT_SUFFIX = "_notes"
DEFAULT_REPORT_NAME = "migration_report.csv"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def localname(tag: str) -> str:
    """Strip {namespace} and any ns: prefix."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _has_own_path(value: str) -> bool:
    """True when the user gave a path, not just a bare filename."""
    return bool(value) and (os.path.isabs(value) or os.path.dirname(value))


def resolve_input(value, base_dir, extensions, what, flag=None):
    if _has_own_path(value):
        return value
    if value:
        return os.path.join(base_dir, value)
    return _auto_discover_input(base_dir, extensions, what, flag)

def _auto_discover_input(base_dir, extensions, what, flag):
    if not os.path.isdir(base_dir):
        raise SystemExit(
            "ERROR: %s directory does not exist:\n    %s\n"
            "Set it at the top of this script, or pass the file explicitly."
            % (what, base_dir))

    found = sorted(f for f in os.listdir(base_dir)
                   if f.lower().endswith(extensions)
                   and os.path.isfile(os.path.join(base_dir, f)))
    if len(found) == 1:
        return os.path.join(base_dir, found[0])
    if not found:
        raise SystemExit(
            "ERROR: no %s file (%s) found in:\n    %s"
            % (what, "/".join(extensions), base_dir))
    raise SystemExit(
        "ERROR: %d %s files found in:\n    %s\n%s\n"
        "Pass the one you want, e.g. --%s %s"
        % (len(found), what, base_dir,
           "\n".join("      " + f for f in found),
           flag or KEY_ERWIN, found[0]))


def resolve_output(value, default_name):
    """Place an output file in OUTPUT_DIR unless the user gave a path."""
    if _has_own_path(value):
        target = value
    else:
        if not os.path.isdir(OUTPUT_DIR):
            os.makedirs(OUTPUT_DIR, exist_ok=True)
        target = os.path.join(OUTPUT_DIR, value or default_name)
    parent = os.path.dirname(os.path.abspath(target))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    return target


def default_output_name(erwin_path: str) -> str:
    stem, ext = os.path.splitext(os.path.basename(erwin_path))
    return "%s%s%s" % (stem, DEFAULT_OUTPUT_SUFFIX, ext or EXT_XML)


def xml_escape_text(s: str) -> str:
    """Escape for XML element content."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def encode_note_payload(text: str, newline_mode: str) -> str:
    """
    Turn a SAP PD comment into the field[6] payload.

    HandleNonPrintableChar="Y" means control characters travel as \\#xNN.
    """
    if newline_mode == "escaped-crlf":
        nl = r"\#x0D" + r"\#x0A"
    elif newline_mode == "raw":
        nl = "\n"
    else:                                    # escaped-lf (default)
        nl = r"\#x0A"

    # A literal "\#x" in the source would be re-read by erwin as an escape.
    # Encode the backslash so the text survives intact.
    text = text.replace("\\#x", r"\#x5C" + "#x")

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    out = []
    for ch in text:
        o = ord(ch)
        if ch == "\n":
            out.append(nl)
        elif ch == "\t":
            out.append(r"\#x09")
        elif o < 0x20 or o == 0x7F:
            out.append(r"\#x%02X" % o)
        else:
            out.append(ch)
    return "".join(out)


def build_note_record(text: str, author: str, newline_mode: str,
                      number: int = 1, guid: str | None = None,
                      created: str | None = None,
                      modified: str | None = None) -> str:
    """Assemble the 12-field \\#x1F-delimited erwin note record."""
    now = datetime.now().strftime(TS_FMT)
    fields = [""] * NOTE_FIELD_COUNT
    fields[IDX_NUMBER] = str(number)
    fields[IDX_CREATED] = created or now
    fields[IDX_CREATOR] = author
    fields[IDX_MODIFIED] = modified or now
    fields[IDX_MODIFIER] = author
    fields[IDX_TEXT] = encode_note_payload(text, newline_mode)
    fields[IDX_FLAG] = NOTE_FLAG_DEFAULT
    fields[IDX_GUID] = guid or ("{%s}" % str(uuid.uuid4()).upper())
    return SEP.join(fields)


def build_note_element(record: str, index: int = 0) -> str:
    return ('<Note_List HandleNonPrintableChar="Y" index="%d">%s</Note_List>'
            % (index, xml_escape_text(record)))


def build_note_array(records: list) -> str:
    inner = "".join(build_note_element(r, i) for i, r in enumerate(records))
    return "<%s>%s</%s>" % (NOTE_ARRAY_TAG, inner, NOTE_ARRAY_TAG)


# --------------------------------------------------------------------------
# SAP PowerDesigner .ldm reader
# --------------------------------------------------------------------------

class PDModel:
    """Entity / attribute Name, Code and Comment extracted from a PD .ldm."""

    def __init__(self):
        self.entities = OrderedDict()    # (name, code) -> comment
        self.attributes = OrderedDict()  # (ent_name, name, code) -> comment
        self.entity_codes = {}           # code -> [(name, code)]
        self.entity_names = {}           # name.lower() -> [(name, code)]

    @classmethod
    def load(cls, path: str) -> "PDModel":
        try:
            from lxml import etree as ET
            tree = ET.parse(path, ET.XMLParser(huge_tree=True, resolve_entities=False, no_network=True))
        except ImportError:
            import xml.etree.ElementTree as ET  # nosec B405
            from defusedxml.ElementTree import parse as safe_parse
            tree = safe_parse(path)

        m = cls()
        a = "{%s}" % PD_ATTR

        def val(el, name):
            child = el.find(a + name)
            return child.text if child is not None and child.text else ""

        for ent in tree.getroot().iter("{%s}Entity" % PD_OBJ):
            # <o:Entity Ref="oNNN"/> are pointers, not definitions - skip them
            if ent.get("Id") is None:
                continue
            ename, ecode = val(ent, PROP_NAME), val(ent, "Code")
            m.entities[(ename, ecode)] = val(ent, PROP_COMMENT)
            m.entity_codes.setdefault(ecode, []).append((ename, ecode))
            m.entity_names.setdefault(ename.lower(), []).append((ename, ecode))

            for att in ent.iter("{%s}EntityAttribute" % PD_OBJ):
                if att.get("Id") is None:
                    continue
                m.attributes[(ename, val(att, PROP_NAME), val(att, "Code"))] = \
                    val(att, PROP_COMMENT)
        return m


# --------------------------------------------------------------------------
# erwin XML scanner - expat gives exact byte offsets
# --------------------------------------------------------------------------

class ErwinObject:
    __slots__ = (KEY_KIND, KEY_ENTITY, "name", KEY_PNAME,
                 "anchor_end", "props_close", "note_array_span",
                 "existing_notes")

    def __init__(self):
        self.kind = None
        self.entity = None
        self.name = None
        self.physical_name = None
        self.anchor_end = None       # byte offset just past </User_Formatted_Physical_Name>
        self.props_close = None      # byte offset of </EntityProps> or </AttributeProps>
        self.note_array_span = None  # (start, end) if Note_List_Array already present
        self.existing_notes = 0

    @property
    def key(self):
        if self.kind == KIND_ENTITY:
            return (self.name, self.physical_name)
        return (self.entity, self.name, self.physical_name)

    @property
    def label(self):
        if self.kind == KIND_ENTITY:
            return "Entity '%s'" % self.name
        return "Attribute '%s.%s'" % (self.entity, self.name)


def scan_erwin(raw: bytes):
    """Walk the erwin export, returning [ErwinObject] with byte offsets."""
    objects = []
    stack = []
    chars = []
    state = {"obj": None, "cur_entity": None,
             "nla_start": None, "in_note_array": False}

    parser = expat.ParserCreate()
    parser.buffer_text = True

    def start(name, attrs):
        n = localname(name)
        stack.append(n)
        del chars[:]

        if n in PROPS_TAGS:
            o = ErwinObject()
            o.kind = KIND_ENTITY if n == "EntityProps" else KIND_ATTRIBUTE
            o.entity = state["cur_entity"]
            state["obj"] = o

        elif n == NOTE_ARRAY_TAG and state["obj"] is not None:
            state["nla_start"] = parser.CurrentByteIndex
            state["in_note_array"] = True

        elif n == "Note_List" and state["obj"] is not None:
            state["obj"].existing_notes += 1

    def chardata(data):
        chars.append(data)

    def end(name):
        n = localname(name)
        text = "".join(chars)
        del chars[:]
        o = state["obj"]

        if o is not None:
            parent = stack[-2] if len(stack) >= 2 else None

            if parent in PROPS_TAGS:
                if n == PROP_NAME and o.name is None:
                    o.name = text
                elif n == "Physical_Name" and o.physical_name is None:
                    o.physical_name = text

            if n == ANCHOR_TAG and parent in PROPS_TAGS and o.anchor_end is None:
                o.anchor_end = parser.CurrentByteIndex + len("</%s>" % ANCHOR_TAG)

            if n == NOTE_ARRAY_TAG and state["in_note_array"]:
                idx = parser.CurrentByteIndex
                closing = ("</%s>" % NOTE_ARRAY_TAG).encode(ENC_UTF8)
                if raw[idx:idx + len(closing)] == closing:
                    nla_end = idx + len(closing)
                else:                                   # self-closing <X/>
                    nla_end = raw.index(b">", state["nla_start"]) + 1
                o.note_array_span = (state["nla_start"], nla_end)
                state["in_note_array"] = False
                state["nla_start"] = None

            if n in PROPS_TAGS:
                o.props_close = parser.CurrentByteIndex
                if o.kind == KIND_ENTITY:
                    state["cur_entity"] = o.name
                    o.entity = o.name
                objects.append(o)
                state["obj"] = None

        stack.pop()

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chardata
    parser.Parse(raw, True)
    return objects


def decode_note_record(record: str) -> dict:
    """Split a Note_List record into its named fields."""
    f = record.split(SEP)
    while len(f) < NOTE_FIELD_COUNT:
        f.append("")
    return {
        "number": f[IDX_NUMBER], "created": f[IDX_CREATED],
        "creator": f[IDX_CREATOR], "modified": f[IDX_MODIFIED],
        "modifier": f[IDX_MODIFIER], "text": f[IDX_TEXT],
        "flag": f[IDX_FLAG], "guid": f[IDX_GUID],
        "field_count": len(f), "raw_fields": f,
    }


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def match_objects(pd: PDModel, objects):
    matches = {}
    unmatched = []

    ent_by_code, ent_by_name = _build_entity_indices(pd)
    att_by_code, att_by_name = _build_attribute_indices(pd)

    for o in objects:
        hit = tier = None
        if o.kind == KIND_ENTITY:
            hit, tier = _match_entity(o, pd, ent_by_code, ent_by_name)
        else:
            hit, tier = _match_attribute(o, pd, att_by_code, att_by_name)

        if tier is None:
            unmatched.append(o)
        else:
            matches[id(o)] = (hit or "", tier)
    return matches, unmatched

def _build_entity_indices(pd):
    ent_by_code = {}
    ent_by_name = {}
    for (n, c) in pd.entities:
        ent_by_code.setdefault(c, []).append((n, c))
        ent_by_name.setdefault(n.strip().lower(), []).append((n, c))
    return ent_by_code, ent_by_name

def _build_attribute_indices(pd):
    att_by_code = {}
    att_by_name = {}
    for (e, n, c) in pd.attributes:
        att_by_code.setdefault((e, c), []).append((e, n, c))
        att_by_name.setdefault((e, n.strip().lower()), []).append((e, n, c))
    return att_by_code, att_by_name

def _match_entity(o, pd, ent_by_code, ent_by_name):
    k = (o.name, o.physical_name)
    if k in pd.entities:
        return pd.entities[k], 1
    
    cand = ent_by_code.get(o.physical_name, [])
    if len(cand) == 1:
        return pd.entities[cand[0]], 2
        
    cand = ent_by_name.get((o.name or "").strip().lower(), [])
    if len(cand) == 1:
        return pd.entities[cand[0]], 3
        
    return None, None

def _match_attribute(o, pd, att_by_code, att_by_name):
    k = (o.entity, o.name, o.physical_name)
    if k in pd.attributes:
        return pd.attributes[k], 1
        
    cand = att_by_code.get((o.entity, o.physical_name), [])
    if len(cand) == 1:
        return pd.attributes[cand[0]], 2
        
    cand = att_by_name.get((o.entity, (o.name or "").strip().lower()), [])
    if len(cand) == 1:
        return pd.attributes[cand[0]], 3
        
    return None, None

def cmd_inspect(args):
    pd = _inspect_pd_model(args)
    raw, objs = _inspect_erwin_xml(args)
    _inspect_note_structure(args, raw)
    _inspect_match_preview(pd, objs, args)

def _inspect_pd_model(args):
    print("=" * 74)
    print("SAP PowerDesigner LDM :", args.ldm)
    print("=" * 74)
    pd = PDModel.load(args.ldm)
    ec = sum(1 for v in pd.entities.values() if v.strip())
    ac = sum(1 for v in pd.attributes.values() if v.strip())
    print("  entity definitions      : %d  (with Comment: %d)" % (len(pd.entities), ec))
    print("  attribute definitions   : %d  (with Comment: %d)" % (len(pd.attributes), ac))
    print("  Comment is stored as    : <a:Comment> child of <o:Entity> / <o:EntityAttribute>")
    for i, ((n, c), cm) in enumerate(pd.entities.items()):
        if cm.strip():
            print("  sample entity           : Name=%r Code=%r" % (n, c))
            print("     Comment[:90]         : %r" % cm[:90])
            break
    return pd

def _inspect_erwin_xml(args):
    raw = open(args.erwin, "rb").read()
    objs = scan_erwin(raw)
    ents = [o for o in objs if o.kind == KIND_ENTITY]
    atts = [o for o in objs if o.kind == KIND_ATTRIBUTE]
    print()
    print("=" * 74)
    print("erwin XML             :", args.erwin)
    print("=" * 74)
    print("  bytes                   : %d" % len(raw))
    print("  Entity objects          : %d" % len(ents))
    print("  Attribute objects       : %d" % len(atts))
    print("  with %-18s: %d / %d" % (ANCHOR_TAG, sum(1 for o in objs if o.anchor_end is not None), len(objs)))
    print("  already carrying %s : %d" % (NOTE_ARRAY_TAG, sum(1 for o in objs if o.note_array_span)))
    print("  existing Note_List recs : %d" % sum(o.existing_notes for o in objs))
    return raw, objs

def _inspect_note_structure(args, raw):
    ref = args.reference or args.erwin
    refraw = open(ref, "rb").read()
    print()
    print("=" * 74)
    print("erwin NOTE STRUCTURE  :", ref)
    print("=" * 74)
    found = re.search(rb"<Note_List\b[^>]*>(.*?)</Note_List>", refraw, re.S)
    if not found:
        print("  no populated <Note_List> in this file (pass --reference <file with verified notes>)")
        return

    rec = found.group(1).decode(ENC_UTF8)
    openttag = refraw[found.start():found.start() + found.group(0).find(b">") + 1]
    print("  element    : %s" % openttag.decode(ENC_UTF8))
    print("  separator  : %r  (literal 6 chars; real 0x1F bytes in file: %d)" % (SEP, refraw.count(b"\x1f")))
    
    d = decode_note_record(rec)
    print("  fields     : %d" % d["field_count"])
    names = {IDX_NUMBER: "number", IDX_CREATED: "created", IDX_CREATOR: "creator",
             IDX_MODIFIED: "modified", IDX_MODIFIER: "modifier",
             IDX_TEXT: "NOTE TEXT", IDX_FLAG: "flag", IDX_GUID: "guid"}
    for i, v in enumerate(d["raw_fields"]):
        print("     field[%2d] %-10s = %r" % (i, names.get(i, ""), v))

    m = re.search(rb"<Note_List_Array\b", refraw)
    if m:
        before = refraw[max(0, m.start() - 200):m.start()].decode(ENC_UTF8, "replace")
        after = refraw[m.start():m.start() + 200].decode(ENC_UTF8, "replace")
        prev = re.findall(r"</([A-Za-z_]+)>\s*$", before)
        nxt = re.findall(r"<([A-Za-z_]+)[ />]", after[after.find(">"):])
        print("  placement  : after </%s>, before <%s>" % (prev[-1] if prev else "?", nxt[0] if nxt else "?"))

def _inspect_match_preview(pd, objs, args):
    print()
    print("=" * 74)
    print("MATCH PREVIEW")
    print("=" * 74)
    matches, unmatched = match_objects(pd, objs)
    print("  PD objects       : %d" % (len(pd.entities) + len(pd.attributes)))
    print("  erwin objects    : %d" % len(objs))
    print("  matched          : %d" % len(matches))
    print("  unmatched erwin  : %d" % len(unmatched))
    if unmatched:
        print("  unmatched sample :")
        for u in unmatched[:5]:
            print("      %-9s Name=%r Code=%r" % (u.kind, u.name, u.physical_name))

def cmd_migrate(args):
    pd = PDModel.load(args.ldm)
    raw = open(args.erwin, "rb").read()
    objects = scan_erwin(raw)
    matches, unmatched = match_objects(pd, objects)

    only = set(args.only.split(",")) if args.only else {KIND_ENTITY, KIND_ATTRIBUTE}

    edits = []
    rows = []
    stats = Counter()

    for o in objects:
        _process_migrate_object(o, matches, only, args, raw, edits, rows, stats)

    print("=" * 74)
    print("MIGRATION EXECUTION")
    print("=" * 74)
    print("  SAP model               : %s" % args.ldm)
    print("  erwin XML               : %s" % args.erwin)
    print("  objects matched         : %d" % stats["matched"])
    print("  objects skipped         : %d" % stats["skipped"])
    print("  objects unmatched       : %d" % stats["unmatched"])
    
    _write_migrate_output(args, raw, edits)
    _write_migrate_csv(args, rows)

def _process_migrate_object(o, matches, only, args, raw, edits, rows, stats):
    row = {KEY_KIND: o.kind, KEY_ENTITY: o.entity or "", "name": o.name or "",
           KEY_PNAME: o.physical_name or "", STATUS_KEY: "",
           "match_tier": "", REASON_KEY: "", "comment_chars": 0}

    if id(o) not in matches:
        row[STATUS_KEY] = STATUS_UNMATCHED
        row[REASON_KEY] = "no PD object with this Name/Code"
        stats["unmatched"] += 1
        rows.append(row)
        return

    comment, tier = matches[id(o)]
    row["match_tier"] = tier
    row["comment_chars"] = len(comment)
    stats["matched"] += 1

    if o.kind not in only:
        row[STATUS_KEY] = STATUS_SKIPPED
        row[REASON_KEY] = "object kind excluded by --only"
        stats["skipped"] += 1
        rows.append(row)
        return

    if not comment.strip():
        row[STATUS_KEY] = STATUS_SKIPPED
        row[REASON_KEY] = "PD Comment empty"
        stats["skipped"] += 1
        rows.append(row)
        return

    if o.existing_notes and args.on_existing == "skip":
        row[STATUS_KEY] = STATUS_SKIPPED
        row[REASON_KEY] = "erwin Note already present (--on-existing skip)"
        stats["skipped"] += 1
        rows.append(row)
        return

    _apply_migrate_edit(o, comment, args, raw, edits, row)
    row[STATUS_KEY] = STATUS_PASS
    rows.append(row)

def _apply_migrate_edit(o, comment, args, raw, edits, row):
    if o.existing_notes and args.on_existing == "append":
        start, end = o.note_array_span
        existing = raw[start:end].decode(ENC_UTF8)
        recs = re.findall(r"<Note_List\b[^>]*>(.*?)</Note_List>", existing, re.S)
        recs = list(recs)
        new = build_note_record(comment, args.author, args.newline_mode, number=len(recs) + 1)
        payload = build_note_array([_unescape(r) for r in recs] + [new]).encode(ENC_UTF8)
        edits.append((start, end, payload))
        row[REASON_KEY] = "appended as note #%d" % (len(recs) + 1)
    elif o.note_array_span:
        start, end = o.note_array_span
        rec = build_note_record(comment, args.author, args.newline_mode)
        edits.append((start, end, build_note_array([rec]).encode(ENC_UTF8)))
        row[REASON_KEY] = "replaced existing Note_List_Array" if o.existing_notes else "filled empty Note_List_Array"
    else:
        start = o.anchor_end
        rec = build_note_record(comment, args.author, args.newline_mode)
        edits.append((start, start, build_note_array([rec]).encode(ENC_UTF8)))
        row[REASON_KEY] = "created new Note_List_Array"

def _write_migrate_output(args, raw, edits):
    edits.sort(key=lambda x: x[0], reverse=True)
    out = bytearray(raw)
    for start, end, payload in edits:
        out[start:end] = payload

    outpath = resolve_output(args.out, "migrated_" + os.path.basename(args.erwin))
    with open(outpath, "wb") as f:
        f.write(out)
    print("  edits applied           : %d" % len(edits))
    print("  output written to       : %s" % outpath)

def _write_migrate_csv(args, rows):
    if getattr(args, "csv", None):
        with open(args.csv, "w", newline="", encoding=ENC_UTF8) as f:
            writer = csv.DictWriter(f, fieldnames=[
                KEY_KIND, KEY_ENTITY, "name", KEY_PNAME, STATUS_KEY,
                "match_tier", REASON_KEY, "comment_chars"
            ])
            writer.writeheader()
            writer.writerows(rows)
        print("  report written to       : %s" % args.csv)

def _unescape(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))


# --------------------------------------------------------------------------
# command: verify
# --------------------------------------------------------------------------

NOTE_ARRAY_RE = re.compile(
    rb"<Note_List_Array\b[^>]*/>|<Note_List_Array\b[^>]*>.*?</Note_List_Array>",
    re.S)


def cmd_verify(args):
    a = open(args.erwin, "rb").read()
    b = open(args.out, "rb").read()

    print("=" * 74)
    print("VERIFICATION")
    print("=" * 74)
    ok = True

    ok &= _verify_byte_level(a, b)

    try:
        from lxml import etree
    except ImportError:
        print("  [SKIP] structural check needs lxml (pip install lxml)")
        return 0 if ok else 1
    
    p = etree.XMLParser(huge_tree=True, resolve_entities=False, no_network=True)
    dict_a = _collect_properties(args.erwin, p, etree)
    dict_b = _collect_properties(args.out, p, etree)
    
    ok &= _verify_same_keys(dict_a, dict_b)
    ok &= _verify_properties(dict_a, dict_b)
    ok &= _verify_protected_fields(dict_a, dict_b)
    ok &= _verify_payload_sanity(args.out, p, etree)

    print()
    print("  RESULT: %s" % ("PASS - only Notes were changed" if ok else STATUS_FAIL))
    return 0 if ok else 1

def _verify_byte_level(a, b):
    a_stripped = NOTE_ARRAY_RE.sub(b"", a)
    b_stripped = NOTE_ARRAY_RE.sub(b"", b)
    same = a_stripped == b_stripped
    print("  [%s] byte-level: everything outside Note_List_Array is identical" % (STATUS_PASS if same else STATUS_FAIL))
    print("        input  %d bytes -> %d with notes removed" % (len(a), len(a_stripped)))
    print("        output %d bytes -> %d with notes removed" % (len(b), len(b_stripped)))
    if not same:
        n = min(len(a_stripped), len(b_stripped))
        for i in range(n):
            if a_stripped[i] != b_stripped[i]:
                print("        first difference at byte %d" % i)
                print("        IN : %r" % a_stripped[max(0, i - 90):i + 90])
                print("        OUT: %r" % b_stripped[max(0, i - 90):i + 90])
                break
    return same

def _collect_properties(path, p, etree):
    tree = etree.parse(path, p)
    out = {}
    for el in tree.getroot().iter():
        if localname(el.tag) not in (KIND_ENTITY, KIND_ATTRIBUTE):
            continue
        props = None
        for ch in el:
            if localname(ch.tag) in PROPS_TAGS:
                props = ch
                break
        if props is None:
            continue
        d = {}
        for pr in props:
            d.setdefault(localname(pr.tag), []).append(etree.tostring(pr, encoding="unicode"))
        out[(localname(el.tag), el.get("id"))] = d
    return out

def _verify_same_keys(dict_a, dict_b):
    same_keys = set(dict_a) == set(dict_b)
    print("  [%s] same object set: %d objects in / %d out" % (STATUS_PASS if same_keys else STATUS_FAIL, len(dict_a), len(dict_b)))
    return same_keys

def _verify_properties(dict_a, dict_b):
    added, removed, changed = _compute_property_diffs(dict_a, dict_b)
    illegal_add = {k: v for k, v in added.items() if k != NOTE_ARRAY_TAG}
    illegal_chg = {k: v for k, v in changed.items() if k != NOTE_ARRAY_TAG}

    _print_diff_status("properties added", illegal_add, added)
    _print_diff_status("properties removed", removed, removed)
    _print_diff_status("properties changed", illegal_chg, changed)
    
    return not illegal_add and not removed and not illegal_chg

def _print_diff_status(label, check_dict, full_dict):
    status = STATUS_FAIL if check_dict else STATUS_PASS
    data_str = dict(full_dict) if full_dict else "{}"
    print("  [%s] %s : %s" % (status, label.ljust(18), data_str))

def _compute_property_diffs(dict_a, dict_b):
    added, removed, changed = Counter(), Counter(), Counter()
    for k in set(dict_a) & set(dict_b):
        da, db = dict_a[k], dict_b[k]
        for prop in set(db) - set(da):
            added[prop] += 1
        for prop in set(da) - set(db):
            removed[prop] += 1
        for prop in set(da) & set(db):
            if da[prop] != db[prop]:
                changed[prop] += 1
    return added, removed, changed

def _verify_protected_fields(dict_a, dict_b):
    protected = ("Definition", PROP_COMMENT, PROP_NAME, "Physical_Name", "Type",
                 "Long_Id", "Owner_Path", "Logical_Data_Type",
                 "Physical_Data_Type", "Null_Option_Type", "Parent_Domain_Ref")
    bad = []
    for k in set(dict_a) & set(dict_b):
        for prop in protected:
            if dict_a[k].get(prop) != dict_b[k].get(prop):
                bad.append((k, prop))
    print("  [%s] protected fields unchanged (%s)" % (STATUS_PASS if not bad else STATUS_FAIL, ", ".join(protected[:5]) + ", ..."))
    for k, prop in bad[:10]:
        print("        CHANGED %s %s -> %s" % (k, prop, prop))
    return not bad

def _verify_payload_sanity(out_path, p, etree):
    notes = 0
    badrec = 0
    tree = etree.parse(out_path, p)
    for el in tree.getroot().iter():
        if localname(el.tag) != "Note_List":
            continue
        notes += 1
        d = decode_note_record(el.text or "")
        if d["field_count"] != NOTE_FIELD_COUNT:
            badrec += 1
    print("  [%s] note records well-formed: %d notes, %d malformed" % (STATUS_PASS if badrec == 0 else STATUS_FAIL, notes, badrec))
    return badrec == 0

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Migrate SAP PowerDesigner Comments into erwin Notes.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("inspect", help="report the real structures; writes nothing")
    s.add_argument("--ldm", help="PD .ldm (default: the one in MODEL_INPUT_DIR)")
    s.add_argument("--erwin", help="erwin XML (default: the one in ERWIN_INPUT_DIR)")
    s.add_argument("--reference", help="erwin XML that already contains verified Notes")
    s.set_defaults(func=cmd_inspect)

    s = sub.add_parser(KEY_MIGRATE, help="write PD Comments into erwin Notes")
    s.add_argument("--ldm", help="PD .ldm (default: the one in MODEL_INPUT_DIR)")
    s.add_argument("--erwin", help="erwin XML (default: the one in ERWIN_INPUT_DIR)")
    s.add_argument("--out", help="output XML (default: <erwin name>%s.xml in OUTPUT_DIR)"
                                 % DEFAULT_OUTPUT_SUFFIX)
    s.add_argument("--author", default="PD_MIGRATION",
                   help="creator/modifier recorded on each note")
    s.add_argument("--newline-mode", default="escaped-lf",
                   choices=["escaped-lf", "escaped-crlf", "raw"])
    s.add_argument("--on-existing", default="skip",
                   choices=["skip", "replace", "append"],
                   help="what to do when the object already has an erwin Note")
    s.add_argument("--only", help="restrict to Entity and/or Attribute, comma separated")
    s.add_argument("--report", nargs="?", const=DEFAULT_REPORT_NAME,
                   help="per-object CSV report (written to OUTPUT_DIR)")
    s.set_defaults(func=cmd_migrate)

    s = sub.add_parser("verify", help="prove only Notes differ between two files")
    s.add_argument("--erwin", help="original erwin XML (default: from ERWIN_INPUT_DIR)")
    s.add_argument("--out", help="migrated erwin XML (default: from OUTPUT_DIR)")
    s.set_defaults(func=cmd_verify)

    args = ap.parse_args(argv)

    # ---- resolve paths against the configured directories -----------------
    if args.cmd in ("inspect", KEY_MIGRATE):
        args.ldm = resolve_input(args.ldm, MODEL_INPUT_DIR,
                                 (".ldm",), "LDM model", "ldm")
        args.erwin = resolve_input(args.erwin, ERWIN_INPUT_DIR,
                                   (EXT_XML,), "erwin XML", KEY_ERWIN)
        if args.cmd == "inspect" and args.reference:
            args.reference = resolve_input(args.reference, ERWIN_INPUT_DIR,
                                           (EXT_XML,), "erwin XML", "reference")
        if args.cmd == KEY_MIGRATE:
            args.out = resolve_output(args.out, default_output_name(args.erwin))
            if args.report:
                args.report = resolve_output(args.report, DEFAULT_REPORT_NAME)
    elif args.cmd == "verify":
        args.erwin = resolve_input(args.erwin, ERWIN_INPUT_DIR,
                                   (EXT_XML,), "erwin XML", KEY_ERWIN)
        args.out = resolve_input(args.out, OUTPUT_DIR,
                                 (EXT_XML,), "migrated XML", "out")

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
