"""
scripts/run_pipeline.py
------------------------
NL2Sim pipeline CLI — runs the full end-to-end pipeline.

Usage:
    python run_pipeline.py
    python run_pipeline.py --output-dir ../outputs/my_run
    python run_pipeline.py --no-simulate
    python run_pipeline.py --from-config ../outputs/run_xyz/config_raw.json
    python run_pipeline.py --from-config ../outputs/run_xyz/config_raw.json --no-simulate

"""

import sys
import json
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nl2sim.pipeline import Pipeline


def ask_model_choice() -> bool:
    """
    Interactively ask the user which model backend to use.
    Returns True if Azure, False if OpenAI.
    """
    import os
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    openai_model = os.environ.get("OPENAI_MODEL", "ft:gpt-4.1-2025-04-14:personal:nl2sim-ft:Dkz8vnRw")
    azure_model  = os.environ.get("AZURE_FINETUNED_MODEL") or os.environ.get("AZURE_BASE_MODEL", "Azure fine-tuned model")

    print("\n" + "─" * 60)
    print("Which model would you like to use?")
    print(f"  1) OpenAI  — {openai_model}")
    print(f"  2) Azure   — {azure_model} (Microsoft Foundry)")
    print("─" * 60)

    while True:
        choice = input("Select option (1 or 2): ").strip()
        if choice == "1":
            print("  ✓ Using OpenAI fine-tuned model.")
            return False
        elif choice == "2":
            print("  ✓ Using Azure fine-tuned model.")
            return True
        else:
            print("  Invalid selection. Please enter 1 or 2.")


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
    parser.add_argument(
        "--from-config",
        default=None,
        help="Skip LLM step and start from an existing raw config JSON file",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else None

    # ── Short-circuit: skip LLM, jump straight to validation ──
    if args.from_config:
        config_path = Path(args.from_config)
        if not config_path.exists():
            print(f"[ERROR] Config file not found: {config_path}")
            sys.exit(1)

        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)

        print(f"\n  ✓ Loaded config from {config_path} — skipping LLM step")

       

        pipeline = Pipeline(
            description="",
            output_dir=output_dir,
            use_context=False,
            simulate=not args.no_simulate,
            use_azure=False,
        )

        pipeline.validate_and_simulate(cfg)
        sys.exit(0)

    # ── Normal flow: LLM → validate → simulate ────────────
    print("\n" + "=" * 60)
    print("NL2Sim Pipeline")
    print("=" * 60)

    # ── Ask model choice first ─────────────────────────────
    use_azure = ask_model_choice()

    # ── Ask for description ────────────────────────────────
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
    pipeline = Pipeline(
        description=description,
        output_dir=output_dir,
        use_context=use_context,
        simulate=not args.no_simulate,
        use_azure=use_azure,
    )

    pipeline.run()