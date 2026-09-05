"""
PDM → ERwin Validation Batch Runner
-------------------------------------
Processes 1500+ model pairs in parallel, generates an Excel report, and logs
all errors so you can resume from where you left off.

Usage
-----
  python main.py                         # use paths from config.py
  python main.py --pd C:\PD --erwin C:\EW --out C:\Reports
  python main.py --workers 16            # override parallel workers
  python main.py --resume                # skip pairs already in last report
  python main.py --limit 50             # quick test: validate only 50 pairs

Dependencies
------------
  pip install openpyxl tqdm
"""

import argparse
import csv
import glob
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Tuple, Dict, Optional

from tqdm import tqdm

from app.config.validation_config import PDM_CONFIG as config
from pd_parser     import parse_pdm
from erwin_parser  import parse_erwin
from comparator    import compare, ValidationResult
from report_generator import generate_report

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("validation.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ─── PAIR DISCOVERY ───────────────────────────────────────────────────────────

def _collect_files(directory: str, extensions: List[str]) -> Dict[str, str]:
    """Return {normalised_basename → full_path} for all matching files in directory."""
    result: Dict[str, str] = {}
    for ext in extensions:
        pattern = os.path.join(directory, "**", f"*{ext}")
        for path in glob.glob(pattern, recursive=True):
            key = os.path.splitext(os.path.basename(path))[0].upper()
            if key in result:
                logger.warning("Duplicate key '%s': keeping %s, ignoring %s",
                               key, result[key], path)
            else:
                result[key] = path
    return result


def _match_by_filename(pd_dir: str, erwin_dir: str) -> List[Tuple[str, str]]:
    pd_files    = _collect_files(pd_dir,    config.PD_EXTENSIONS)
    erwin_files = _collect_files(erwin_dir, config.ERWIN_EXTENSIONS)

    pairs: List[Tuple[str, str]] = []
    unmatched_pd:    List[str] = []
    unmatched_erwin: List[str] = []

    for key, pd_path in pd_files.items():
        if key in erwin_files:
            pairs.append((pd_path, erwin_files[key]))
        else:
            unmatched_pd.append(pd_path)

    for key, ew_path in erwin_files.items():
        if key not in pd_files:
            unmatched_erwin.append(ew_path)

    if unmatched_pd:
        logger.warning("%d PD files have no matching ERwin file:", len(unmatched_pd))
        for f in unmatched_pd[:10]:
            logger.warning("  %s", f)
        if len(unmatched_pd) > 10:
            logger.warning("  … and %d more (see validation.log)", len(unmatched_pd)-10)

    if unmatched_erwin:
        logger.warning("%d ERwin files have no matching PD file:", len(unmatched_erwin))
        for f in unmatched_erwin[:10]:
            logger.warning("  %s", f)

    logger.info("Matched %d pairs out of %d PD / %d ERwin files.",
                len(pairs), len(pd_files), len(erwin_files))
    return pairs


def _match_by_csv(csv_path: str) -> List[Tuple[str, str]]:
    """Load explicit pd_file,erwin_file mapping from a CSV."""
    pairs: List[Tuple[str, str]] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pd_path    = row.get("pd_file",    "").strip()
            erwin_path = row.get("erwin_file", "").strip()
            if pd_path and erwin_path:
                pairs.append((pd_path, erwin_path))
    logger.info("Loaded %d pairs from CSV: %s", len(pairs), csv_path)
    return pairs


def discover_pairs(pd_dir: str, erwin_dir: str) -> List[Tuple[str, str]]:
    if config.MATCH_STRATEGY == "csv":
        return _match_by_csv(config.MAPPING_CSV)
    return _match_by_filename(pd_dir, erwin_dir)


# ─── WORKER ───────────────────────────────────────────────────────────────────

def validate_pair(pd_path: str, erwin_path: str) -> ValidationResult:
    """Validate a single PD/ERwin pair.  Always returns a result (errors are captured)."""
    try:
        pd_model    = parse_pdm(pd_path)
        erwin_model = parse_erwin(erwin_path)
        return compare(pd_model, erwin_model)
    except Exception as exc:
        logger.error("Unhandled error (%s vs %s): %s", pd_path, erwin_path, exc)
        r = ValidationResult(pd_file=pd_path, erwin_file=erwin_path, status="ERROR")
        from comparator import Finding
        r.add(Finding("EXCEPTION", "CRITICAL", message=str(exc)))
        return r


# ─── RESUME SUPPORT ───────────────────────────────────────────────────────────

def _already_validated(output_dir: str) -> set:
    """Return set of (pd_basename) that appear in an existing report CSV."""
    done_path = os.path.join(output_dir, "validated_pairs.csv")
    done: set = set()
    if os.path.exists(done_path):
        with open(done_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done.add(row.get("pd_file", "").strip())
    return done


def _save_checkpoint(results: List[ValidationResult], output_dir: str):
    """Append completed pairs to checkpoint CSV."""
    done_path = os.path.join(output_dir, "validated_pairs.csv")
    write_header = not os.path.exists(done_path)
    with open(done_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["pd_file", "erwin_file", "status"])
        if write_header:
            writer.writeheader()
        for r in results:
            writer.writerow({
                "pd_file":    r.pd_file,
                "erwin_file": r.erwin_file,
                "status":     r.status,
            })


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="PDM → ERwin Validation Batch Runner")
    p.add_argument("--pd",      default=config.PD_MODELS_DIR,    help="Folder with .pdm files")
    p.add_argument("--erwin",   default=config.ERWIN_MODELS_DIR,  help="Folder with ERwin XML files")
    p.add_argument("--out",     default=config.OUTPUT_DIR,        help="Output folder for report")
    p.add_argument("--workers", type=int, default=config.MAX_WORKERS, help="Parallel workers")
    p.add_argument("--limit",   type=int, default=0,  help="Process only first N pairs (0=all)")
    p.add_argument("--resume",  action="store_true",  help="Skip pairs in validated_pairs.csv")
    p.add_argument("--report-name", default=config.REPORT_FILENAME, help="Output Excel filename")
    return p.parse_args()


def main():
    args = parse_args()
    config.REPORT_FILENAME = args.report_name

    print("=" * 70)
    print("  PDM → ERwin Validation Framework")
    print(f"  PD folder    : {args.pd}")
    print(f"  ERwin folder : {args.erwin}")
    print(f"  Output       : {args.out}")
    print(f"  Workers      : {args.workers}")
    print("=" * 70)

    os.makedirs(args.out, exist_ok=True)

    # 1. Discover pairs
    pairs = discover_pairs(args.pd, args.erwin)
    if not pairs:
        print("❌  No matching pairs found. Check your folder paths and file extensions.")
        sys.exit(1)

    # 2. Resume filter
    if args.resume:
        already_done = _already_validated(args.out)
        pairs = [(pd, ew) for pd, ew in pairs
                 if os.path.basename(pd) not in already_done]
        print(f"▶  Resume mode: {len(pairs)} pairs remaining.")

    # 3. Limit for testing
    if args.limit > 0:
        pairs = pairs[:args.limit]
        print(f"▶  Limit mode: processing first {len(pairs)} pairs.")

    # 4. Parallel validation
    results: List[ValidationResult] = []
    start = time.time()

    print(f"\n▶  Starting validation of {len(pairs)} model pairs …\n")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {
            executor.submit(validate_pair, pd_path, ew_path): (pd_path, ew_path)
            for pd_path, ew_path in pairs
        }

        batch: List[ValidationResult] = []
        CHECKPOINT_EVERY = 100   # Save checkpoint every N completions

        with tqdm(total=len(pairs), unit="model",
                  bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]") as pbar:

            for future in as_completed(future_map):
                pd_path, ew_path = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logger.error("Future failed (%s): %s", pd_path, exc)
                    from comparator import Finding
                    result = ValidationResult(pd_file=pd_path, erwin_file=ew_path, status="ERROR")
                    result.add(Finding("EXCEPTION", "CRITICAL", message=str(exc)))

                results.append(result)
                batch.append(result)

                status_icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌", "ERROR": "💥"}.get(result.status, "?")  # nosec B105
                pbar.set_postfix({
                    "last": os.path.basename(pd_path)[:25],
                    "status": result.status,
                })
                pbar.update(1)

                # Checkpoint
                if len(batch) >= CHECKPOINT_EVERY:
                    _save_checkpoint(batch, args.out)
                    batch.clear()

        # Final checkpoint
        if batch:
            _save_checkpoint(batch, args.out)

    elapsed = time.time() - start

    # 5. Print summary
    pass_count  = sum(1 for r in results if r.status == "PASS")
    warn_count  = sum(1 for r in results if r.status == "WARN")
    fail_count  = sum(1 for r in results if r.status == "FAIL")
    error_count = sum(1 for r in results if r.status == "ERROR")

    print(f"\n{'='*70}")
    print(f"  VALIDATION COMPLETE  ({elapsed:.1f}s)")
    print(f"  Total models : {len(results)}")
    print(f"  ✅ PASS      : {pass_count}")
    print(f"  ⚠️  WARN      : {warn_count}")
    print(f"  ❌ FAIL      : {fail_count}")
    print(f"  💥 ERROR     : {error_count}")
    print(f"{'='*70}")

    # 6. Generate Excel report
    # Fix:
    # Dropped the f prefix on these two prints -- neither has a {placeholder}, so
    # the f was doing nothing. The "Report saved" line between them keeps its f
    # because it interpolates report_path. Text and escapes are unchanged.
    print("\n▶  Generating Excel report …")
    report_path = generate_report(results, args.out)
    print(f"✅  Report saved → {report_path}")
    print("✅  Log saved   → validation.log\n")


if __name__ == "__main__":
    main()