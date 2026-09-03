"""
sow_classify.py -- classify a PowerDesigner model into the SOW's
                   Level B1-B4 complexity bands and model automation
                   coverage.

    python sow_classify.py SUBSURFACE_AND_WELLS.ldm

Bands, characteristics, automation ranges and effort multipliers are
taken verbatim from the SOW "Complexity Classification Spectrum". They
are contractual -- do not tune them to make a model look cheaper.

    B1 Very Simple   Clean physical models, standard DBMS, minimal
                     custom domains, no macros            85-95%   1.0x
    B2 Simple        Logical + physical alignment needed, limited
                     domain remapping, minor DBMS variance 70-85%  1.5x
    B3 Medium        Subtype/supertype handling, partial metadata
                     gaps, naming inconsistencies          50-70%  2.5x
    B4 High/V.High   Heavy domain rebuild, validation rule
                     recreation, macros, vendor-specific
                     configs, inheritance restructuring    20-50%  4.0x
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from pd_extract import Model, NS

BANDS = {
    "B1": {"label": "Very Simple",     "automation": (85, 95), "multiplier": 1.0},
    "B2": {"label": "Simple",          "automation": (70, 85), "multiplier": 1.5},
    "B3": {"label": "Medium",          "automation": (50, 70), "multiplier": 2.5},
    "B4": {"label": "High / Very High","automation": (20, 50), "multiplier": 4.0},
}


def model_options(raw: str) -> dict:
    """Parse the [ModelOptions] INI blob out of ModelOptionsText."""
    m = re.search(r"<a:ModelOptionsText>(.*?)</a:ModelOptionsText>", raw, re.S)
    if not m:
        return {}
    opts = {}
    for line in m.group(1).splitlines():
        if "=" in line and not line.strip().startswith("["):
            k, _, v = line.partition("=")
            opts[k.strip()] = v.strip()
    return opts


def classify(model_path: Path, outdir: Path = None) -> dict:
    raw = model_path.read_text(encoding="utf-8", errors="replace")
    m = Model(model_path)
    data = m.extract(outdir=outdir)
    c = data["counts"]
    opts = model_options(raw)
    ea = data["extended_attribute_profile"]

    # ---- measurable signals, mapped to the SOW's stated characteristics ----
    signals = {
        # B4 drivers
        "extension_model_definitions": len(
            [e for e in data["external_models"] if str(e["url"]).endswith(".xem")]
        ),
        "extension_containers": len({k.split(".")[0] for k in ea}),
        "extension_properties": len(ea),
        "extension_values": c["extended_attribute_values"],
        "gtl_templates": sum(
            1 for k in ("RlshAsstTmpl", "FKNameTemplate", "EntyAsstTmpl")
            if opts.get(k)
        ),
        "validation_rules": len(m._defs("Rule")),
        "inheritances": c["Inheritance"],
        "inheritance_links": c["InheritanceLink"],
        "mutually_exclusive_inheritances": c["inheritances_mutually_exclusive"],
        # B3 drivers
        "entities_without_definition": c["Entity"] - c["entities_with_comment"],
        "entities_without_identifier": c["entities_without_identifier"],
        "attributes_untyped": c["attributes_untyped"],
        "primary_identifiers": c["identifiers_primary"],
        "case_sensitive_naming": opts.get("CaseSensitive") == "Yes",
        # B2 drivers
        "glossary_enforced": opts.get("UseTerm") == "Yes",
        "glossary_shortcuts": c["Shortcut"],
        "external_glossary_models": len(
            [e for e in data["external_models"] if str(e["url"]).endswith(".glm")]
        ),
        # scale / structure
        "domains": len(m._defs("Domain")),
        "entities": c["Entity"],
        "attributes": c["EntityAttribute"],
        "relationships": c["Relationship"],
        "diagrams": c["LogicalDiagram"],
        "model_type": m.header.get("signature", "?"),
        "has_physical_layer": "PDM" in m.header.get("signature", ""),
    }

    # ---- knockouts: SOW characteristics that force a band regardless of size ----
    knockouts = []
    if signals["extension_model_definitions"]:
        knockouts.append((
            "B4",
            f"{signals['extension_model_definitions']} custom extended model "
            f"definition(s) (.xem) in active use, carrying "
            f"{signals['extension_values']} property values across "
            f"{signals['extension_properties']} properties. SOW B4: "
            f"'heavy domain rebuild'. No erwin bridge target exists.",
        ))
    if signals["gtl_templates"]:
        knockouts.append((
            "B4",
            f"{signals['gtl_templates']} custom GTL template(s) in model options "
            f"(relationship assertion / FK naming). SOW B4: 'macros'. No erwin "
            f"equivalent.",
        ))
    if signals["mutually_exclusive_inheritances"]:
        knockouts.append((
            "B4",
            f"{signals['mutually_exclusive_inheritances']} mutually-exclusive "
            f"inheritance structures require exclusivity flags to be re-set "
            f"post-import. SOW B4: 'inheritance restructuring'.",
        ))
    if signals["validation_rules"]:
        knockouts.append((
            "B4",
            f"{signals['validation_rules']} validation rules present. SOW B4: "
            f"'validation rule recreation'.",
        ))
    if signals["glossary_enforced"] and signals["external_glossary_models"]:
        knockouts.append((
            "B3",
            f"Naming governed by an external PowerDesigner glossary "
            f"(UseTerm=Yes) with {signals['glossary_shortcuts']} term shortcuts. "
            f"SOW B3: 'naming inconsistencies'. Glossary does not migrate.",
        ))
    if signals["inheritances"]:
        knockouts.append((
            "B3",
            f"{signals['inheritances']} subtype/supertype structures "
            f"({signals['inheritance_links']} links). SOW B3: "
            f"'subtype/supertype handling'.",
        ))
    pct_no_def = (
        100 * signals["entities_without_definition"] / max(signals["entities"], 1)
    )
    if pct_no_def > 10:
        knockouts.append((
            "B3",
            f"{signals['entities_without_definition']} of {signals['entities']} "
            f"entities ({pct_no_def:.0f}%) lack a definition. SOW B3: "
            f"'partial metadata gaps'.",
        ))
    if not signals["has_physical_layer"]:
        knockouts.append((
            "B2",
            "Logical model with no paired physical model. erwin auto-derives a "
            "physical layer that was never authored in the source. SOW B2: "
            "'logical + physical alignment needed'.",
        ))

    band = max((k for k, _ in knockouts), default="B1")

    # ---- automation coverage, under an explicit definition ----
    # The SOW never defines "automation coverage". We state ours so the
    # number is arguable rather than assertable.
    facts_bridge = (
        c["Entity"] + c["EntityAttribute"] + c["Relationship"]
        + c["RelationshipJoin"] + c["Identifier"] + c["Inheritance"]
        + c["LogicalDiagram"] + c["entities_with_comment"]
        + c["attributes_with_comment"]
    )
    facts_automation = c["extended_attribute_values"] + c["inheritances_mutually_exclusive"]
    facts_manual = signals["gtl_templates"] + (
        1 if signals["entities_without_identifier"] else 0
    )
    facts_lost = c["Shortcut"]          # glossary bindings: no erwin equivalent
    total = facts_bridge + facts_automation + facts_manual + facts_lost

    coverage = {
        "definition": (
            "automation coverage = (source facts delivered by the erwin bridge + "
            "source facts reapplied programmatically) / total source facts. "
            "A 'fact' is one object or one populated property."
        ),
        "bridge_delivered": facts_bridge,
        "automation_reapplied": facts_automation,
        "manual_required": facts_manual,
        "not_recoverable": facts_lost,
        "total_source_facts": total,
        "bridge_only_pct": round(100 * facts_bridge / total, 1),
        "with_automation_pct": round(100 * (facts_bridge + facts_automation) / total, 1),
    }

    return {
        "model": model_path.name,
        "model_name": m.header.get("Name"),
        "pd_version": m.header.get("version"),
        "extraction_id": m.header.get("ExtractionId"),
        "band": band,
        "band_label": BANDS[band]["label"],
        "sow_automation_range": BANDS[band]["automation"],
        "sow_effort_multiplier": BANDS[band]["multiplier"],
        "knockouts": [{"forces": b, "evidence": e} for b, e in knockouts],
        "signals": signals,
        "automation_coverage": coverage,
        "model_options_of_interest": {
            k: opts.get(k) for k in
            ("CaseSensitive", "UseTerm", "EnableFullShortcut", "FKNameTemplate",
             "FKNameTemplateUsage", "DefaultDttp", "RlshMigrateExtd")
        },
        "non_equivalence_register": build_register(signals, c),
    }


def build_register(s: dict, c: dict) -> list[dict]:
    """Per-construct migration disposition, for the SOW non-equivalence table."""
    reg = [
        {"construct": "Entities, attributes, relationships, keys",
         "volume": f"{c['Entity']} / {c['EntityAttribute']} / {c['Relationship']} / {c['Identifier']}",
         "bridge": "Delivered", "disposition": "In scope - bridge"},
        {"construct": "Definitions (Comment)",
         "volume": f"{c['entities_with_comment']} entities, {c['attributes_with_comment']} attributes",
         "bridge": "Delivered (verify mapping to erwin Definition)",
         "disposition": "In scope - bridge + verify"},
        {"construct": "Diagrams / subject areas",
         "volume": str(c["LogicalDiagram"]),
         "bridge": "Partial layout fidelity",
         "disposition": "In scope - membership preserved, layout cosmetic"},
        {"construct": "Extended model definitions (.xem) and extension properties",
         "volume": f"{s['extension_properties']} properties, {s['extension_values']} values",
         "bridge": "NOT delivered - no target construct",
         "disposition": "AUTOMATION - reapply as erwin UDPs. NOT currently listed "
                        "in SOW non-equivalence table; scope by omission."},
        {"construct": "Inheritance exclusivity / completeness flags",
         "volume": str(s["mutually_exclusive_inheritances"]),
         "bridge": "Commonly dropped (SOW: 'may flatten or alter')",
         "disposition": "AUTOMATION - re-set post-import"},
        {"construct": "Glossary term bindings (UseTerm)",
         "volume": f"{s['glossary_shortcuts']} term shortcuts",
         "bridge": "NOT delivered - external .glm has no erwin equivalent",
         "disposition": "NOT RECOVERABLE as-is. SOW covers under 'naming standards, "
                        "macros, templates - not automatically migrated'. Re-establish "
                        "in erwin Naming Standards or relocate to Collibra."},
        {"construct": "GTL templates (relationship assertion, FK naming)",
         "volume": str(s["gtl_templates"]),
         "bridge": "NOT delivered",
         "disposition": "NOT RECOVERABLE. SOW covers under 'model macros - manual "
                        "rewrite required'."},
        {"construct": "Repository version history / branch",
         "volume": f"extraction {c.get('ExtractionId','n/a')}",
         "bridge": "NOT delivered - point-in-time extract only",
         "disposition": "OUT OF SCOPE per SOW: 'version history & audit lineage lost'. "
                        "Archive PD repository read-only."},
        {"construct": "Primary identifiers",
         "volume": f"{s['primary_identifiers']} of {c['Identifier']} flagged primary; "
                   f"{s['entities_without_identifier']} entities have none",
         "bridge": "Delivered as-is",
         "disposition": "SOURCE CONDITION - do not invent keys. Requires Shell Data "
                        "Architect confirmation."},
        {"construct": "Untyped attributes",
         "volume": str(s["attributes_untyped"]),
         "bridge": "Delivered untyped",
         "disposition": "SOURCE CONDITION - carry through untyped, do not infer."},
    ]
    if s["domains"] == 0:
        reg.append({"construct": "Domains",
                    "volume": "0",
                    "bridge": "n/a",
                    "disposition": "Not applicable - no domains defined in this model. "
                                   "SOW domain-transport risk does not apply here."})
    return reg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--out", default="classification.json")
    a = ap.parse_args()

    out_path = Path(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    r = classify(Path(a.model), outdir=out_path.parent)
    out_path.write_text(json.dumps(r, indent=2))
    
    # Supressed SOW metrics printing for cleaner UDP logs
    # print(f"model            : {r['model_name']}  ({r['model']})")
    # ...
    # print(f"written to {a.out}")


if __name__ == "__main__":
    sys.exit(main())
