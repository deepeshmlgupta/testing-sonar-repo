"""
erwin_prepare.py -- turn a pd_extract baseline into the two things erwin
                    needs before and during load:

    1. udp_schema.json / udp_schema.csv
       The UDP definitions to create in erwin BEFORE importing, so
       extension values have somewhere to land. Types and value lists
       are INFERRED FROM OBSERVED DATA and flagged as such -- the
       authoritative lists live in BIM-core.xem, which Shell has not
       supplied. Observed != permitted.

    2. property_manifest.csv / .json
       One row per entity per property. This is the payload erwin_load.py
       applies, and the artifact a reviewer can read without running code.

    python erwin_prepare.py --baseline baseline --outdir erwin_input
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

# erwin UDP names cannot contain dots or spaces.
SAFE = re.compile(r"[^A-Za-z0-9_]")

# Structural UDPs that must exist regardless of the source model.
STRUCTURAL = [
    {"udp": "PD_ObjectID", "type": "Text", "length": 40,
     "purpose": "PowerDesigner GUID. Permanent join key for reapply, "
                "reconciliation and any future re-run. Names change; GUIDs don't."},
    {"udp": "PD_SourceExtraction", "type": "Text", "length": 20,
     "purpose": "PowerDesigner repository extraction id. Proves which "
                "point-in-time extract this model came from."},
    {"udp": "PD_SourceModel", "type": "Text", "length": 120,
     "purpose": "Source model name, for traceability across waves."},
]

BOOLEANS = {"true", "false"}
DATE_RE = re.compile(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}")
GUID_RE = re.compile(r"^\{?[0-9A-Fa-f]{8}-")


def udp_name(path: str) -> str:
    """BIM-core.DataConfidentiality -> DataConfidentiality (dedup-safe)."""
    return SAFE.sub("_", path.split(".")[-1])


def infer_type(values: Counter) -> tuple[str, int | None, list[str] | None]:
    """
    Infer an erwin UDP type from observed values.

    Deliberately conservative: a small closed set becomes a List so erwin
    enforces it; anything else stays Text. We never infer Number from
    digit-looking strings -- a version like '2021.2' is not a number, and
    coercing it silently corrupts the value.
    """
    non_empty = [v for v in values if v.strip()]
    distinct = len(non_empty)
    if distinct and all(v.strip().lower() in BOOLEANS for v in non_empty):
        return "List", None, ["true", "false"]
    if distinct and all(GUID_RE.match(v.strip()) for v in non_empty):
        return "Text", 40, None
    if distinct and all(DATE_RE.match(v.strip()) for v in non_empty):
        # Dates in this estate are inconsistently formatted (1/3/2019,
        # 01-11-19, '19/08/2021 Pass 3'). Text preserves them exactly;
        # a Date UDP would reject or mangle them.
        return "Text", 60, None
    if 0 < distinct <= 12:
        return "List", None, sorted(non_empty)
    longest = max((len(v) for v in values), default=0)
    return "Text", max(60, min(1024, longest + 40)), None


def build_udp_schema(profile: dict) -> list[dict]:
    rows = list(STRUCTURAL)
    seen = {r["udp"] for r in rows}
    for path, info in profile.items():
        counts = Counter(info["observed_values"])
        kind, length, values = infer_type(counts)
        name = udp_name(path)
        # Same leaf name under two containers -> prefix to keep them distinct.
        if name in seen:
            name = SAFE.sub("_", path)
        seen.add(name)
        rows.append({
            "udp": name,
            "type": kind,
            "length": length,
            "value_list": values,
            "source_path": path,
            "coverage": info["coverage"],
            "distinct_observed": info["distinct_values"],
            "list_source": "OBSERVED - confirm against BIM-core.xem" if values else "",
            "purpose": f"PowerDesigner extension property {path}",
        })
    return rows


def build_manifest(entities: list[dict], schema: list[dict],
                   extraction: str, model_name: str) -> list[dict]:
    by_path = {r["source_path"]: r["udp"] for r in schema if r.get("source_path")}
    out = []
    for e in entities:
        base = {
            "pd_object_id": e["object_id"],
            "entity_name": e["name"],
            "entity_code": e["code"],
        }
        for udp, val in (("PD_ObjectID", e["object_id"]),
                         ("PD_SourceExtraction", extraction),
                         ("PD_SourceModel", model_name)):
            out.append({**base, "udp": udp, "value": val, "source_path": ""})
        for path, val in e["extended_attributes"].items():
            out.append({**base,
                        "udp": by_path.get(path, udp_name(path)),
                        "value": val,
                        "source_path": path})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", type=Path, default=Path("baseline"))
    ap.add_argument("--outdir", type=Path, default=Path("erwin_input"))
    ap.add_argument("--model_name", required=True)
    a = ap.parse_args()
    a.outdir.mkdir(parents=True, exist_ok=True)

    profile = json.loads((a.baseline / "extended_attributes.json").read_text())
    entities = json.loads((a.baseline / "entities.json").read_text())
    counts = json.loads((a.baseline / "counts.json").read_text())

    schema = build_udp_schema(profile)
    (a.outdir / "udp_schema.json").write_text(json.dumps(schema, indent=2))
    with (a.outdir / "udp_schema.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["udp", "type", "length", "value_list", "source_path",
                    "coverage", "distinct_observed", "list_source"])
        for r in schema:
            w.writerow([r["udp"], r["type"], r.get("length") or "",
                        " | ".join(r.get("value_list") or []),
                        r.get("source_path", ""), r.get("coverage", ""),
                        r.get("distinct_observed", ""), r.get("list_source", "")])

    manifest = build_manifest(entities, schema, "46603045", a.model_name)
    (a.outdir / "property_manifest.json").write_text(json.dumps(manifest, indent=2))
    with (a.outdir / "property_manifest.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["pd_object_id", "entity_name",
                                          "entity_code", "udp", "value",
                                          "source_path"])
        w.writeheader()
        w.writerows(manifest)

    lists = [r for r in schema if r["type"] == "List"]
    print(f"       Extracted {len(schema)} UDPs and {len(manifest)} property values.")


if __name__ == "__main__":
    raise SystemExit(main())
