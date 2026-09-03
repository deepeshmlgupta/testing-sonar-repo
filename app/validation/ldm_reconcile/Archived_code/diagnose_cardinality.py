"""
erwin Cardinality Diagnostic
----------------------------
Prints, for every relationship in an erwin XML export, the *raw* cardinality and
nullability strings as they appear in the file and how the framework resolves
them.  Use it whenever a cardinality finding looks wrong.

    python diagnose_cardinality.py MyModel.xml

Why this exists
---------------
A misread cardinality yields a structurally valid value ("0,1"), so the report
cannot distinguish it from a genuine one.  The only reliable way to tell is to
look at what the file actually says next to what the parser made of it.  This
prints both, side by side, plus a variance check: if every relationship resolves
identically, the value is not being read from the data.

Exit codes
----------
  0  raw values found and all resolved
  1  file unreadable, or no relationships found
  2  a suspected parse failure was detected (unresolved values, or total
     collapse of variance) — the detail is printed above the summary
"""

import collections
import os
import sys
import xml.etree.ElementTree as ET  # nosec B405
from defusedxml.ElementTree import parse as safe_parse, iterparse as safe_iterparse

import config
from cardinality import degree, normalize_cardinality
from erwin_ldm_parser import (_child_end_is_mandatory, _local, _name, _oid,
                              _relationship_is_identifying,
                              _relationship_is_many_to_many, _val,
                              resolve_erwin_cardinality)

CARDINALITY_FIELDS = ("Cardinality", "Relationship_Cardinality",
                      "Parent_Cardinality", "Child_Cardinality_Type")
NULL_FIELDS = ("Null_Option_Type", "Nulls_Allowed", "Null_Option", "Child_Nulls_Allowed")


def _rule(char="-", width=100):
    print(char * width)


def diagnose(path: str) -> int:
    try:
        root = safe_parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        print(f"ERROR: cannot read {path}: {exc}")
        return 1

    rels = [n for n in root.iter() if _local(n.tag) == "Relationship"]
    rels = [r for r in rels if _oid(r) or _name(r)]
    if not rels:
        print("No <Relationship> elements found. Is this an erwin XML export?")
        print("Tip: erwin writes these via File > Save As > XML.")
        return 1

    print(f"\nerwin LDM cardinality diagnostic — {os.path.basename(path)}")
    print(f"{len(rels)} relationship(s)\n")

    enum_map = getattr(config, "ERWIN_CARDINALITY_CODES", {}) or {}

    raw_seen = collections.Counter()
    null_seen = collections.Counter()
    resolved_pairs = collections.Counter()
    unresolved = []

    header = (f"{'Relationship':<22}{'raw Cardinality':<26}"
              f"{'raw Nulls':<20}{'parent':<9}{'child':<9}{'degree':<7}")
    print(header)
    _rule()

    for elem in rels:
        label = _name(elem) or _oid(elem) or "(unnamed)"
        raw_card = _val(elem, *CARDINALITY_FIELDS)
        raw_null = _val(elem, *NULL_FIELDS)

        raw_seen[raw_card or "(absent)"] += 1
        null_seen[raw_null or "(absent)"] += 1

        parent, recognised = resolve_erwin_cardinality(raw_card)

        identifying = _relationship_is_identifying(elem)
        child_required = _child_end_is_mandatory(elem, identifying)
        if _relationship_is_many_to_many(elem):
            child = "1,n" if child_required else "0,n"
        else:
            child = "1,1" if child_required else "0,1"

        deg = degree(parent, child)
        resolved_pairs[(parent, child)] += 1

        if raw_card and not recognised:
            unresolved.append((label, raw_card))

        shown_card = (raw_card or "(absent)")
        if raw_card.strip() in enum_map:
            shown_card = f"{raw_card} -> {enum_map[raw_card.strip()]}"

        print(f"{label[:21]:<22}{shown_card[:25]:<26}"
              f"{(raw_null or '(absent)')[:19]:<20}{parent:<9}{child:<9}{deg:<7}")

    _rule()
    print("\nRaw Cardinality values in this file:")
    for value, count in raw_seen.most_common():
        print(f"   {count:>4} x  {value!r}")

    print("\nRaw nullability values in this file:")
    for value, count in null_seen.most_common():
        print(f"   {count:>4} x  {value!r}")

    print("\nResolved (parent, child) pairs:")
    for (parent, child), count in resolved_pairs.most_common():
        print(f"   {count:>4} x  {parent} / {child}   -> {degree(parent, child)}")

    problems = False

    if unresolved:
        problems = True
        print("\n** UNRESOLVED VALUES — these fell back to the 0,n default **")
        for label, value in unresolved:
            print(f"   {label}: {value!r}")
        print("   Add each code to ERWIN_CARDINALITY_CODES in config.py,")
        print("   or to _CARDINALITY_ALIASES in cardinality.py if it is a phrase.")

    if len(resolved_pairs) == 1 and len(rels) >= 3:
        problems = True
        only = next(iter(resolved_pairs))
        print(f"\n** COLLAPSED CARDINALITY — all {len(rels)} relationships "
              f"resolved to {only[0]}/{only[1]} **")
        print("   Real models vary. A constant means the source value is not")
        print("   being read. Compare the raw values above against what the")
        print("   erwin Relationship Editor displays.")

    if "(absent)" in raw_seen:
        problems = True
        print(f"\n** {raw_seen['(absent)']} relationship(s) carry no cardinality "
              f"field at all **")
        print("   The parser searched: " + ", ".join(CARDINALITY_FIELDS))
        print("   Your export may name it differently. Grep the XML for")
        print("   'ardinalit' and add the real field name to _parse_relationship.")

    if not problems:
        print("\nNo cardinality problems detected: every raw value resolved, "
              "and the results vary across relationships as expected.")

    print()
    return 2 if problems else 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        print("Usage: python diagnose_cardinality.py <erwin-model>.xml")
        return 1
    worst = 0
    for path in argv:
        worst = max(worst, diagnose(path))
    return worst


if __name__ == "__main__":
    sys.exit(main())
