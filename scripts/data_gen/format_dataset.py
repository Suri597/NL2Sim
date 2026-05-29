"""
scripts/data_gen/format_dataset.py
------------------------------------
Converts dataset_builder.py output into OpenAI fine-tuning format.

Takes the raw JSONL (nl, full_config pairs) and formats them as:
{
    "messages": [
        {"role": "system",    "content": SYSTEM_INSTRUCTIONS},
        {"role": "user",      "content": nl_description},
        {"role": "assistant", "content": full_json_string}
    ]
}

Can also split into train/validation sets.

Usage:
    python format_dataset.py --input ../outputs/data_gen/dataset/dataset.jsonl
    python format_dataset.py --input dataset.jsonl --split 0.8
    python format_dataset.py --input dataset.jsonl --split 0.8 --output-dir ../outputs/data_gen/formatted
"""

from __future__ import annotations

import sys
import json
import random
import argparse
from pathlib import Path
from typing import List, Dict, Any

# ── ensure scripts/ is on path ─────────────────────────────
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from prompts import SYSTEM_INSTRUCTIONS


# ============================================================
# Formatter
# ============================================================

def format_record(
    nl:          str,
    full_config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Format a single (NL, JSON) pair into OpenAI fine-tuning format.
    """
    return {
        "messages": [
            {
                "role":    "system",
                "content": SYSTEM_INSTRUCTIONS,
            },
            {
                "role":    "user",
                "content": nl,
            },
            {
                "role":    "assistant",
                "content": json.dumps(full_config),
            },
        ]
    }


def load_dataset(input_path: Path) -> List[Dict[str, Any]]:
    """
    Load dataset from JSONL file.
    Handles two formats:
      1. Already formatted: {"messages": [...]}
      2. Raw pairs: {"nl": "...", "config": {...}}
    """
    records = []
    with open(input_path, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                records.append(record)
            except json.JSONDecodeError as e:
                print(f"  ✗ Skipping line {line_num}: {e}")
    return records


def format_dataset(
    records:    List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Ensure all records are in OpenAI fine-tuning format.
    If already formatted (has 'messages' key) — keep as is.
    If raw pair (has 'nl' and 'config' keys) — format them.
    """
    formatted = []
    for record in records:
        if "messages" in record:
            # already formatted — keep as is
            formatted.append(record)
        elif "nl" in record and "config" in record:
            # raw pair format — convert
            formatted.append(
                format_record(record["nl"], record["config"])
            )
        else:
            print(f"  ✗ Unknown record format — skipping: {list(record.keys())}")
    return formatted


def split_dataset(
    records:     List[Dict[str, Any]],
    train_ratio: float = 0.8,
    seed:        int   = 42,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Split dataset into train and validation sets.
    """
    shuffled = records.copy()
    random.Random(seed).shuffle(shuffled)

    split_idx   = int(len(shuffled) * train_ratio)
    train_set   = shuffled[:split_idx]
    val_set     = shuffled[split_idx:]

    return train_set, val_set


def save_jsonl(
    records:  List[Dict[str, Any]],
    path:     Path,
) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")
    print(f"  ✓ Saved {len(records)} records → {path}")


def validate_format(records: List[Dict[str, Any]]) -> bool:
    """
    Validate that all records match OpenAI fine-tuning format.
    """
    errors = 0
    for i, record in enumerate(records):
        if "messages" not in record:
            print(f"  ✗ Record {i}: missing 'messages' key")
            errors += 1
            continue

        messages = record["messages"]
        if len(messages) != 3:
            print(f"  ✗ Record {i}: expected 3 messages, got {len(messages)}")
            errors += 1
            continue

        roles = [m.get("role") for m in messages]
        if roles != ["system", "user", "assistant"]:
            print(f"  ✗ Record {i}: expected roles [system, user, assistant], got {roles}")
            errors += 1
            continue

        for m in messages:
            if not m.get("content"):
                print(f"  ✗ Record {i}: empty content in {m.get('role')} message")
                errors += 1

    if errors == 0:
        print(f"  ✓ All {len(records)} records valid")
        return True
    else:
        print(f"  ✗ {errors} validation errors found")
        return False


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Format dataset into OpenAI fine-tuning JSONL format."
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Path to input JSONL file from dataset_builder.py"
    )
    parser.add_argument(
        "--output-dir", "-o",
        default=None,
        help="Output directory (default: same as input file)"
    )
    parser.add_argument(
        "--split", "-s",
        type=float,
        default=0.8,
        help="Train/validation split ratio (default: 0.8 = 80%% train)"
    )
    parser.add_argument(
        "--no-split",
        action="store_true",
        help="Do not split — save single formatted file"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for split (default: 42)"
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate format before saving"
    )

    parser.add_argument(
        "--sample", type=int,
        default=None,
        help="Randomly sample N records before splitting (default: use all)"
    )
    parser.add_argument(
        "--sample-seed", type=int,
        default=42,
        help="Random seed for sampling (default: 42)"
    )

    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output_dir) if args.output_dir \
                 else input_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"Format Dataset")
    print(f"{'='*60}")
    print(f"  Input      : {input_path}")
    print(f"  Output dir : {output_dir}")
    print(f"  Split      : {'no split' if args.no_split else f'{args.split:.0%} train / {1-args.split:.0%} val'}")
    print(f"{'='*60}\n")

    # ── load ───────────────────────────────────────────────

    print("Loading dataset...")
    records = load_dataset(input_path)
    print(f"  Loaded {len(records)} records")

    # ── format ─────────────────────────────────────────────
    print("\nFormatting records...")
    formatted = format_dataset(records)
    print(f"  Formatted {len(formatted)} records")

    # ── random sample ──────────────────────────────────────
    if args.sample is not None:
        if args.sample > len(formatted):
            print(f"  ⚠️  Sample size {args.sample} > dataset size {len(formatted)} — using all")
        else:
            rng = random.Random(args.sample_seed)
            formatted = rng.sample(formatted, args.sample)
            print(f"  Sampled {len(formatted)} records (seed={args.sample_seed})")

    # ── validate ───────────────────────────────────────────
    if args.validate:
        print("\nValidating format...")
        validate_format(formatted)

    # ── save ───────────────────────────────────────────────
    print("\nSaving...")

    import uuid
    run_id = uuid.uuid4().hex[:8]  # unique 8-char identifier

    if args.no_split:
        n        = len(formatted)
        filename = f"dataset_{n}samples_{run_id}.jsonl"
        save_jsonl(formatted, output_dir / filename)
    else:
        train_set, val_set = split_dataset(
            formatted,
            train_ratio=args.split,
            seed=args.seed,
        )
        n_train = len(train_set)
        n_val   = len(val_set)

        train_file = f"{run_id}_train_{n_train}samples.jsonl"
        val_file   = f"{run_id}_val_{n_val}samples.jsonl"

        print(f"  Split: {n_train} train / {n_val} validation")
        print(f"  Run ID: {run_id}")
        save_jsonl(train_set, output_dir / train_file)
        save_jsonl(val_set,   output_dir / val_file)

    print(f"\n{'='*60}")
    print(f"Done.")
    print(f"{'='*60}\n")