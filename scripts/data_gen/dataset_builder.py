"""
scripts/data_gen/dataset_builder.py
-------------------------------------
Assembles final fine-tuning JSONL from already-generated files:
  - dataset/nl/                  → NL descriptions
  - dataset/config_placeholders/ → configs with missing placeholders

Prerequisites (run these first):
  1. config_populate.py   → generates dataset/configs/
  2. filter_config.py     → generates dataset/filtered/
  3. nl_generator.py      → generates dataset/nl/
  4. process_configs.py   → generates dataset/config_placeholders/

Usage:
    python dataset_builder.py
    python dataset_builder.py --dataset-dir ../outputs/data_gen/dataset
    python dataset_builder.py --dataset-dir ../outputs/data_gen/dataset --output my_dataset.jsonl
"""

from __future__ import annotations

import sys
import json
import argparse
import random
from pathlib import Path
from typing import Any, Dict
from copy import deepcopy

# ── ensure scripts/ is on path ─────────────────────────────
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from schema import SCHEMA_EXAMPLE

# ============================================================
# Instruction prefixes
# ============================================================

INSTRUCTION_PREFIXES = [
    "Generate a structured JSON configuration for the following supply chain description. A JSON schema is provided below for reference:\n",
    "Convert this supply chain description into a structured simulation configuration. Use the schema provided below as a guide:\n",
    "Create a simulation configuration from the following supply chain scenario. The expected JSON schema follows the description:\n",
    "Parse the following supply chain description into a structured JSON format. Refer to the schema provided below:\n",
    "Extract the supply chain parameters from this description and return a structured configuration. A reference schema is included below:\n",
    "Build a supply chain simulation configuration based on the following description. The JSON schema is provided below for reference:\n",
    "Transform the following supply chain scenario into a structured JSON configuration. A schema template follows the description:\n",
    "Return a structured configuration for the supply chain described below. The expected JSON schema is provided after the description:\n",
    "Given the following supply chain description, generate the corresponding JSON configuration. A reference schema is included below:\n",
    "Produce a structured supply chain configuration from the description provided below. The JSON schema follows for reference:\n",
]


def add_instruction_prefix(nl: str, seed: int) -> str:
    rng    = random.Random(seed)
    prefix = rng.choice(INSTRUCTION_PREFIXES)
    return f"{prefix}\n\n{nl}"


# ============================================================
# Main builder
# ============================================================

def build_dataset(
    dataset_dir: str  = "../outputs/data_gen/dataset",
    output_name: str  = "dataset.jsonl",
    sample:      int  = None,
) -> Path:
    """
    Assembles JSONL from already-generated files in:
      - nl/                  → NL descriptions
      - config_placeholders/ → configs with missing placeholders

    Parameters
    ----------
    dataset_dir : str
        Base directory containing nl/ and config_placeholders/ folders.
    output_name : str
        Output JSONL filename.

    Returns
    -------
    Path
        Path to the output JSONL file.
    """
    base             = Path(dataset_dir)
    nl_dir           = base / "nl"
    placeholders_dir = base / "config_placeholders"
    out_path         = base / output_name

    # ── validate directories exist ─────────────────────────
    if not nl_dir.exists():
        raise FileNotFoundError(
            f"NL directory not found: {nl_dir}\n"
            f"Run nl_generator.py first."
        )
    if not placeholders_dir.exists():
        raise FileNotFoundError(
            f"Placeholders directory not found: {placeholders_dir}\n"
            f"Run process_configs.py first."
        )

    # ── find all placeholder files ─────────────────────────
    placeholder_files = sorted(
        placeholders_dir.glob("*_with_missing.json")
    )

    placeholder_files = sorted(
        placeholders_dir.glob("*_with_missing.json")
    )

    # ── limit to sample size if specified ─────────────────
    if sample is not None:
        placeholder_files = placeholder_files[:sample]
        print(f"  Sample mode: processing first {sample} files only")

    if not placeholder_files:
        raise FileNotFoundError(
            f"No *_with_missing.json files found in {placeholders_dir}\n"
            f"Run process_configs.py first."
        )

    print(f"\n{'='*60}")
    print(f"Dataset Builder")
    print(f"{'='*60}")
    print(f"  NL dir            : {nl_dir}")
    print(f"  Placeholders dir  : {placeholders_dir}")
    print(f"  Total found       : {len(placeholder_files)}")
    print(f"  Output            : {out_path}")
    print(f"{'='*60}\n")

    assembled = 0
    skipped   = 0
    errors    = 0

    with open(out_path, "w", encoding="utf-8") as out_file:
        for placeholder_file in placeholder_files:

            # ── parse stem ────────────────────────────────
            # stem format: config_0000_simple_with_missing
            stem  = placeholder_file.stem.replace("_with_missing", "")
            parts = stem.split("_")

            if len(parts) < 3:
                print(f"  ✗ Unexpected filename format: {placeholder_file.name}")
                skipped += 1
                continue

            idx        = int(parts[1])
            complexity = parts[2]

            # ── find matching NL file ──────────────────────
            nl_file = nl_dir / f"nl_{parts[1]}_{complexity}.txt"

            if not nl_file.exists():
                print(f"  ✗ Skipping {stem} — NL file not found: {nl_file.name}")
                skipped += 1
                continue

            try:
                # ── load placeholder config ────────────────
                with open(placeholder_file, encoding="utf-8") as f:
                    config_with_missing = json.load(f)

                # ── load NL description ────────────────────
                nl = nl_file.read_text(encoding="utf-8").strip()

                # ── add instruction prefix ─────────────────
                nl_with_prefix = add_instruction_prefix(nl, seed=idx)

                # ── build training record ──────────────────
                record = {
                    "messages": [
                        {
                            "role":    "user",
                            "content": f"{nl_with_prefix}\n\n{SCHEMA_EXAMPLE}",
                        },
                        {
                            "role":    "assistant",
                            "content": json.dumps(config_with_missing),
                        },
                    ]
                }

                out_file.write(json.dumps(record) + "\n")
                assembled += 1

                if assembled % 100 == 0:
                    print(f"  Assembled {assembled}/{len(placeholder_files)}")

            except Exception as e:
                print(f"  ✗ Error at {stem}: {e}")
                errors += 1

    # ── final summary ──────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"Dataset Build Complete")
    print(f"{'='*60}")
    print(f"  Assembled : {assembled}")
    print(f"  Skipped   : {skipped}")
    print(f"  Errors    : {errors}")
    print(f"  Output    : {out_path}")
    print(f"{'='*60}\n")

    return out_path


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build NL2Sim fine-tuning dataset from existing files."
    )
    parser.add_argument(
        "--dataset-dir", type=str,
        default="../outputs/data_gen/dataset",
        help="Base dataset directory containing nl/ and config_placeholders/ (default: ../outputs/data_gen/dataset)"
    )
    parser.add_argument(
        "--output", type=str,
        default="dataset.jsonl",
        help="Output JSONL filename (default: dataset.jsonl)"
    )

    parser.add_argument(
        "--sample", type=int,
        default=None,
        help="Only process first N samples (default: all)"
    )

    args = parser.parse_args()

    build_dataset(
        dataset_dir = args.dataset_dir,
        output_name = args.output,
        sample      = args.sample,
    )