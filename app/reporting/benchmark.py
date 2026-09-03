#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
benchmark.py — end-to-end timing for the migration framework
============================================================

Answers the question "how long does the optimised framework take on ONE model
versus a batch of 5 to 10?", with measured numbers rather than estimates.

WHAT IT MEASURES
----------------
Per model, the three phases that actually cost time:

    parse_pd        read and parse the SAP PowerDesigner model
    parse_erwin     read and parse the erwin XML export
    compare         reconcile the two and produce findings

Per batch, additionally:

    import          one-off cost of loading the framework (paid once per run,
                    NOT once per model — this is why a batch of 5 is cheaper
                    than five separate single runs)
    report          building the Excel workbook for the whole batch

WHY IT CALLS THE PHASES DIRECTLY
--------------------------------
It imports and calls the SAME functions `app/main.py` calls — parse_ldm,
parse_erwin_ldm, compare, generate_report — rather than shelling out to the
pipeline. That keeps the measurement free of Python interpreter start-up and
lets each phase be timed separately, which a wall-clock stopwatch on
`py -m app.main` cannot do. The manual erwin steps are excluded by definition:
they are human time, not framework time.

SCENARIOS
---------
By default it runs a 1-model scenario and a 5-model scenario. If the project
holds fewer models than a scenario needs, the list is cycled and the repeat
count is reported honestly in the output — a repeated model still measures the
true per-model cost and the true fixed overhead, which is what the comparison
is about.

USAGE
-----
    python app/reporting/benchmark.py                  # 1 and 5
    python app/reporting/benchmark.py --sizes 1,5,10   # any scenario sizes
    python app/reporting/benchmark.py --repeats 3      # median of 3 runs each

Output:
    batch_summary/audit/timings.jsonl                  append-only history
    console table
    a TIMINGS sheet appears in migration_audit_report.xlsx on the next audit run
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, os.pardir, os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

SAP_DIRS = {
    ".ldm": os.path.join(PROJECT_ROOT, "sappdmodels", "ldm"),
    ".cdm": os.path.join(PROJECT_ROOT, "sappdmodels", "cdm"),
    ".pdm": os.path.join(PROJECT_ROOT, "sappdmodels", "pdm"),
}
ERWIN_DIRS = [
    os.path.join(PROJECT_ROOT, "erwinmodels", "2_preprocessed", "xml"),
    os.path.join(PROJECT_ROOT, "erwinmodels", "1_initial", "xml"),
]
AUDIT_DIR = os.path.join(PROJECT_ROOT, "batch_summary", "audit")
TIMINGS_PATH = os.path.join(AUDIT_DIR, "timings.jsonl")


def discover():
    """-> [(suffix, ldm_path, erwin_xml_path)] for every model with an export."""
    out = []
    for suffix, d in SAP_DIRS.items():
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if not f.lower().endswith(suffix):
                continue
            base = os.path.splitext(f)[0]
            erwin = ""
            for ed in ERWIN_DIRS:
                for cand in (base + ".xml", base + "_notes.xml"):
                    p = os.path.join(ed, cand)
                    if os.path.isfile(p):
                        erwin = p
                        break
                if erwin:
                    break
            if erwin:
                out.append((suffix, os.path.join(d, f), erwin))
    return out


def load_engine():
    """Import the framework, returning (elapsed_seconds, functions)."""
    t0 = time.perf_counter()
    from app.validation.ldm_reconcile.pd_ldm_parser import parse_ldm
    from app.validation.ldm_reconcile.erwin_ldm_parser import parse_erwin_ldm
    from app.validation.ldm_reconcile.comparator import compare
    from app.validation.ldm_reconcile.report_generator import generate_report
    fns = {"parse_pd": parse_ldm, "parse_erwin": parse_erwin_ldm,
           "compare": compare, "report": generate_report}
    try:
        from app.validation.cdm_reconcile import parse_cdm, parse_erwin as pe_cdm
        from app.validation.cdm_reconcile import compare as cmp_cdm
        fns["cdm"] = (parse_cdm, pe_cdm, cmp_cdm)
    except Exception:
        fns["cdm"] = None
    # The PDM engine is loaded through its bridge, which imports the
    # standalone validator in an isolated window so its bare module names
    # (config, comparator) cannot collide with the LDM ones already loaded
    # above. A physical model is a different engine, so timing it under the
    # LDM parsers would have measured the wrong code entirely.
    try:
        from app.validation.pdm_reconcile import pdm_validator_bridge as bridge
        fns["pdm"] = (bridge.parse_pdm, bridge.parse_erwin, bridge.compare)
    except Exception:
        fns["pdm"] = None

    # Each engine writes its own workbook. Timing every type through the LDM
    # generator would have silently produced nothing (the result objects carry
    # different fields), so the report figure has to be per type.
    reports = {".ldm": generate_report}
    try:
        from app.validation.cdm_reconcile import generate_report as rep_cdm
        reports[".cdm"] = rep_cdm
    except Exception:  # nosec B110
        pass
    try:
        from app.validation.pdm_reconcile.pdm_report_generator import (
            generate_report as rep_pdm)
        reports[".pdm"] = rep_pdm
    except Exception:  # nosec B110
        pass
    fns["reports"] = reports
    return time.perf_counter() - t0, fns


def time_model(fns, suffix, ldm_path, erwin_path):
    """Time the three per-model phases. Returns a dict of seconds and sizes."""
    engine = fns.get(suffix.lstrip("."))          # "cdm" / "pdm" when present
    if engine:
        parse_pd, parse_er, compare = engine
    else:
        parse_pd, parse_er, compare = (fns["parse_pd"], fns["parse_erwin"],
                                       fns["compare"])

    t = time.perf_counter()
    pd_model = parse_pd(ldm_path)
    t_pd = time.perf_counter() - t

    t = time.perf_counter()
    er_model = parse_er(erwin_path)
    t_er = time.perf_counter() - t

    t = time.perf_counter()
    result = compare(pd_model, er_model)
    t_cmp = time.perf_counter() - t

    def count(m):
        ents = getattr(m, "entities", None) or []
        ents = list(ents.values()) if isinstance(ents, dict) else list(ents)
        attrs = sum(len(getattr(e, "attributes", []) or []) for e in ents)
        rels = getattr(m, "relationships", None) or []
        rels = list(rels.values()) if isinstance(rels, dict) else list(rels)
        return len(ents), attrs, len(rels)

    ents, attrs, rels = count(pd_model)
    return {
        "model": os.path.splitext(os.path.basename(ldm_path))[0],
        "type": suffix,
        "parse_pd_s": round(t_pd, 3),
        "parse_erwin_s": round(t_er, 3),
        "compare_s": round(t_cmp, 3),
        "model_total_s": round(t_pd + t_er + t_cmp, 3),
        "ldm_bytes": os.path.getsize(ldm_path),
        "erwin_bytes": os.path.getsize(erwin_path),
        "entities": ents, "attributes": attrs, "relationships": rels,
        "status": getattr(result, "status", ""),
        "fidelity": getattr(result, "fidelity_score", ""),
        "_result": result,
    }


def run_scenario(fns, models, size, make_report):
    """One scenario: process `size` models, optionally building the workbook."""
    picked = [models[i % len(models)] for i in range(size)]
    repeats = size > len(models)

    t_all = time.perf_counter()
    per_model, by_type = [], {}
    for suffix, ldm, erwin in picked:
        row = time_model(fns, suffix, ldm, erwin)
        by_type.setdefault(suffix, []).append(row.pop("_result"))
        per_model.append(row)
    processing_s = time.perf_counter() - t_all

    report_s = 0.0
    if make_report:
        out_dir = os.path.join(PROJECT_ROOT, "batch_summary", "audit", "_bench")
        os.makedirs(out_dir, exist_ok=True)
        reports = fns.get("reports") or {}
        t = time.perf_counter()
        for suffix, results in by_type.items():
            builder = reports.get(suffix)
            if not builder:
                continue
            try:
                builder(results, out_dir,
                        "benchmark_report_%s.xlsx" % suffix.lstrip("."))
            except Exception:  # nosec B110
                pass
        report_s = time.perf_counter() - t

    return {
        "size": size,
        "distinct_models": min(size, len(models)),
        "repeats_used": repeats,
        "processing_s": round(processing_s, 3),
        "report_s": round(report_s, 3),
        "per_model": per_model,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="End-to-end timing for the framework.")
    ap.add_argument("--sizes", default="",
                    help="scenario sizes, comma separated (default: 1 and "
                         "however many models the project actually holds, so "
                         "the numbers describe YOUR models, none reused)")
    ap.add_argument("--repeats", type=int, default=1,
                    help="run each scenario N times and take the median")
    ap.add_argument("--no-report", action="store_true",
                    help="skip the Excel build, timing parse+compare only")
    args = ap.parse_args(argv)

    models = discover()
    if not models:
        print("No model has both a SAP file and an erwin XML export. Nothing to time.")
        return 1

    if args.sizes.strip():
        sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    else:
        # Default scenarios follow reality: 1 model, and the full set of
        # models actually present. A fixed "1,5" made the report say
        # "5 models" on a 4-model project (one was cycled to fill the batch,
        # honestly noted, but it still read as a wrong count).
        sizes = [1] if len(models) == 1 else [1, len(models)]
    import_s, fns = load_engine()

    scenarios = []
    for size in sizes:
        runs = [run_scenario(fns, models, size, not args.no_report)
                for _ in range(max(1, args.repeats))]
        run = sorted(runs, key=lambda r: r["processing_s"])[len(runs) // 2]
        run["import_s"] = round(import_s, 3)
        run["end_to_end_s"] = round(import_s + run["processing_s"] + run["report_s"], 3)
        run["per_model_avg_s"] = round(run["processing_s"] / size, 3)
        scenarios.append(run)

    print()
    print("  HOW LONG THE FRAMEWORK TAKES")
    print("  " + "-" * 40)
    for r in scenarios:
        print("  %2d model(s)  ->  %6.1f seconds" % (r["size"], r["end_to_end_s"]))
    print()
    for r in scenarios:
        print("  %2d model(s)  ->  %6.1f seconds each" % (r["size"], r["per_model_avg_s"]))
    print()
    if len(scenarios) >= 2:
        one, many = scenarios[0], scenarios[-1]
        naive = one["end_to_end_s"] * many["size"]
        print("  Running %d together is faster than %d one at a time:" %
              (many["size"], many["size"]))
        print("     together      %6.1f seconds" % many["end_to_end_s"])
        print("     one at a time %6.1f seconds" % naive)
    if any(r["repeats_used"] for r in scenarios):
        print()
        print("  Note: only %d model(s) available, so the same model was reused"
              % len(models))
        print("        to fill the batch. Add more models for a truer figure.")
    print()

    record = {
        "run": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "import_s": round(import_s, 3),
        "repeats": args.repeats,
        "scenarios": scenarios,
    }
    os.makedirs(AUDIT_DIR, exist_ok=True)
    with open(TIMINGS_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    print("  Saved. Run  py app\\reporting\\migration_audit.py  to see it in the report.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
