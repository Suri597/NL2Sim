"""
scripts/run_pipeline.py
------------------------
NL2Sim pipeline CLI — runs the full end-to-end pipeline.

Usage:
    python run_pipeline.py
    python run_pipeline.py --output-dir ../outputs/my_run
    python run_pipeline.py --no-simulate

"""

import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nl2sim.pipeline import Pipeline


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the full NL2Sim pipeline."
    )
    parser.add_argument(
        "--output-dir", "-o",
        default=None,
        help="Directory to save all outputs (default: outputs/run_{timestamp})",
    )
    parser.add_argument(
        "--no-simulate",
        action="store_true",
        help="Skip simulation step",
    )
    
    args = parser.parse_args()

    # ── Interactive input choice ───────────────────────────
    print("\n" + "=" * 60)
    print("NL2Sim Pipeline")
    print("=" * 60)
    print("\nHow would you like to provide the supply chain description?")
    print("  1) Load from a .txt file")
    print("  2) Type directly in the terminal")

    while True:
        choice = input("\nSelect option (1 or 2): ").strip()
        if choice in {"1", "2"}:
            break
        print("Invalid selection. Please enter 1 or 2.")

    # ── Option 1 — file ────────────────────────────────────
    if choice == "1":
        while True:
            filepath = input("Enter path to description file: ").strip()
            p = Path(filepath)
            if p.exists() and p.is_file():
                description = p.read_text(encoding="utf-8")
                print(f"  ✓ Loaded description from {filepath}")
                break
            else:
                print(f"  File not found: {filepath}. Please try again.")

    # ── Option 2 — direct text ─────────────────────────────
    else:
        print("\nEnter your supply chain description.")
        print("Press Enter on an empty line when done.\n")
        lines = []
        while True:
            line = input()
            if line == "":
                break
            lines.append(line)
        description = "\n".join(lines).strip()
        if not description:
            print("No description entered. Exiting.")
            sys.exit(1)
        print("  ✓ Description received.")

    # ── Ask about system instructions ─────────────────────
    print("\n" + "─" * 60)
    print("⚠️  WARNING: Including system instructions significantly")
    print("   increases token usage and API cost.")
    print("   Only recommended if JSON quality is poor without it.")
    print("─" * 60)
    ctx = input("Include system instructions? (yes/no) [default: no]: ").strip().lower()
    use_context = ctx in {"yes", "y"}
    print(f"  System instructions: {'included' if use_context else 'excluded'}")

    # ── Run pipeline ───────────────────────────────────────
    output_dir = Path(args.output_dir) if args.output_dir else None

    pipeline = Pipeline(
        description = description,
        output_dir  = output_dir,
        use_context = use_context,
        simulate    = not args.no_simulate,
    )

    pipeline.run()