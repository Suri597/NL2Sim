"""
scripts/data_gen/process_configs.py
-------------------------------------
Processes already-generated configs through:
  1. filter_config  → filtered version
  2. apply_missing_placeholders → version with "missing" placeholders

Reads from:   ../outputs/data_gen/configs/
Writes to:    ../outputs/data_gen/filtered/
              ../outputs/data_gen/configs/ (with_missing files)

Usage:
    python process_configs.py
    python process_configs.py --configs-dir ../outputs/data_gen/configs
    python process_configs.py --resume  ← skip already processed
"""

from __future__ import annotations

import sys
import json
import argparse
from pathlib import Path
from copy import deepcopy
from typing import Any, Dict

# ── ensure scripts/ is on path ─────────────────────────────
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from data_gen.filter_config  import filter_config
from data_gen.dataset_builder import apply_missing_placeholders


def process_configs(
    configs_dir: str = "../outputs/data_gen/configs",
    resume:      bool = True,
) -> None:

    configs_path      = Path(configs_dir)
    filtered_path     = configs_path.parent / "filtered"
    placeholders_path = configs_path.parent / "config_placeholders"
    filtered_path.mkdir(parents=True, exist_ok=True)
    placeholders_path.mkdir(parents=True, exist_ok=True)

    # ── clean up any with_missing files accidentally saved in configs ──
    stale = list(configs_path.glob("*_with_missing.json"))
    if stale:
        print(f"  Cleaning up {len(stale)} stale with_missing files from configs/...")
        for f in stale:
            f.unlink()
        print(f"  ✓ Deleted {len(stale)} stale files\n")
        
    # find all config files — exclude already processed ones
    all_configs = sorted([
        f for f in configs_path.glob("config_*.json")
        if "_with_missing" not in f.name
        and "_filtered"    not in f.name
    ])

    print(f"\n{'='*60}")
    print(f"Process Configs")
    print(f"{'='*60}")
    print(f"  Configs dir  : {configs_path}")
    print(f"  Filtered dir : {filtered_path}")
    print(f"  Total found  : {len(all_configs)}")
    print(f"  Resume mode  : {resume}")
    print(f"{'='*60}\n")

    processed = 0
    skipped   = 0
    errors    = 0

    for config_file in all_configs:
        stem = config_file.stem  # e.g. config_0000_simple

        # output paths
        filtered_out = filtered_path / f"{stem}_filtered.json"
        missing_out  = placeholders_path / f"{stem}_with_missing.json"

        # skip if both already exist and resume mode
        if resume and filtered_out.exists() and missing_out.exists():
            skipped += 1
            continue

        try:
            # load full config
            with open(config_file, encoding="utf-8") as f:
                full_config = json.load(f)

            # Step 1 — filter
            filtered = filter_config(full_config)
            filtered_out.write_text(
                json.dumps(filtered, indent=2), encoding="utf-8")

            # Step 2 — apply missing placeholders
            config_with_missing = apply_missing_placeholders(
                full_config, filtered)
            missing_out.write_text(
                json.dumps(config_with_missing, indent=2), encoding="utf-8")

            processed += 1

            if processed % 50 == 0:
                print(f"  Processed {processed}/{len(all_configs)}")

        except Exception as e:
            print(f"  ✗ Error processing {config_file.name}: {e}")
            errors += 1

    print(f"\n{'='*60}")
    print(f"Done.")
    print(f"  Processed : {processed}")
    print(f"  Skipped   : {skipped} (already done)")
    print(f"  Errors    : {errors}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Filter configs and apply missing placeholders."
    )
    parser.add_argument(
        "--configs-dir", type=str,
        default="../outputs/data_gen/configs",
        help="Directory containing generated configs"
    )
    parser.add_argument(
        "--no-resume", action="store_true",
        help="Reprocess all configs even if already done"
    )
    args = parser.parse_args()

    process_configs(
        configs_dir = args.configs_dir,
        resume      = not args.no_resume,
    )