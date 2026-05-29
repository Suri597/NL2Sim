"""
scripts/data_gen/config_populate.py
-------------------------------------
Generates N fully-populated valid supply chain JSON configs
and saves them to disk for the next pipeline step (filtering).

Usage:
    python config_populate.py --n 750 --output ../outputs/data_gen/configs
    python config_populate.py --n 100 --output ../outputs/data_gen/configs --seed 42
"""

from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path
from typing import Dict, Any

# ── ensure scripts/ is on path ─────────────────────────────
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from data_gen.json_generator import generate_config

# ============================================================
# Complexity mix
# ============================================================

COMPLEXITY_MIX = {
    "simple":  0.35,
    "medium":  0.45,
    "complex": 0.20,
}


def get_complexity_counts(n: int) -> Dict[str, int]:
    """
    Split n samples into complexity buckets based on COMPLEXITY_MIX.
    Remainder goes to medium to hit exact total.
    """
    counts = {
        c: int(n * frac)
        for c, frac in COMPLEXITY_MIX.items()
    }
    # distribute remainder to medium
    remainder = n - sum(counts.values())
    counts["medium"] += remainder
    return counts


# ============================================================
# Main populate function
# ============================================================

def populate_configs(
    n: int = 750,
    output_dir: str = "../outputs/data_gen/configs",
    base_seed: int = 0,
    overwrite: bool = False,
) -> Path:
    """
    Generate n fully-populated valid JSON configs and save to disk.

    Parameters
    ----------
    n : int
        Total number of configs to generate.
    output_dir : str
        Directory to save configs.
    base_seed : int
        Base random seed — each config gets seed = base_seed + index.
    overwrite : bool
        If False, skip configs that already exist on disk.

    Returns
    -------
    Path
        Output directory path.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    counts     = get_complexity_counts(n)
    total      = sum(counts.values())
    generated  = 0
    skipped    = 0
    errors     = 0

    print(f"\n{'='*60}")
    print(f"Config Populate")
    print(f"{'='*60}")
    print(f"  Total configs    : {total}")
    print(f"  Simple           : {counts['simple']} ({COMPLEXITY_MIX['simple']*100:.0f}%)")
    print(f"  Medium           : {counts['medium']} ({COMPLEXITY_MIX['medium']*100:.0f}%)")
    print(f"  Complex          : {counts['complex']} ({COMPLEXITY_MIX['complex']*100:.0f}%)")
    print(f"  Output directory : {out}")
    print(f"{'='*60}\n")

    # ── generate configs ───────────────────────────────────
    idx = 0
    for complexity, count in counts.items():
        print(f"Generating {count} {complexity} configs...")
        for i in range(count):
            seed      = base_seed + idx
            filename  = out / f"config_{idx:04d}_{complexity}_seed{seed}.json"

            # skip if exists and not overwriting
            if filename.exists() and not overwrite:
                skipped += 1
                idx     += 1
                continue

            try:
                cfg = generate_config(complexity=complexity, seed=seed)

                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)

                generated += 1

                if (i + 1) % 50 == 0 or (i + 1) == count:
                    print(f"  [{complexity}] {i+1}/{count} done")

            except Exception as e:
                print(f"  ✗ Error at index {idx} (seed={seed}): {e}")
                errors += 1

            idx += 1

    # ── summary ────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"Done.")
    print(f"  Generated : {generated}")
    print(f"  Skipped   : {skipped} (already existed)")
    print(f"  Errors    : {errors}")
    print(f"  Output    : {out}")
    print(f"{'='*60}\n")

    return out


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate N fully-populated supply chain JSON configs."
    )
    parser.add_argument(
        "--n", type=int, default=750,
        help="Total number of configs to generate (default: 750)"
    )
    parser.add_argument(
        "--output", type=str,
        default="../outputs/data_gen/configs",
        help="Output directory for generated configs"
    )
    parser.add_argument(
        "--seed", type=int, default=0,
        help="Base random seed (default: 0)"
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite existing configs"
    )
    args = parser.parse_args()

    populate_configs(
        n         = args.n,
        output_dir = args.output,
        base_seed  = args.seed,
        overwrite  = args.overwrite,
    )