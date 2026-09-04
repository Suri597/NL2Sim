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

# capture_sketch lives in scripts/graph_input, already added to sys.path
# by nl2sim.pipeline's module-level path setup.
from capture_sketch import main as capture_sketch_main


def ask_input_mode() -> str:
    """
    Asks how the scenario should be provided.
    Returns one of: "text", "graph", "both".
    """
    print("\n" + "─" * 60)
    print("How would you like to provide the scenario?")
    print("  1) Text only")
    print("  2) Graph only")
    print("  3) Graph + Text")
    print("─" * 60)

    while True:
        choice = input("Select option (1, 2 or 3): ").strip()
        if choice == "1":
            return "text"
        elif choice == "2":
            return "graph"
        elif choice == "3":
            return "both"
        else:
            print("  Invalid selection. Please enter 1, 2 or 3.")


def ask_image_path() -> str:
    """Asks capture-live vs. select-existing, returns a path to the image."""
    print("\n" + "─" * 60)
    print("How would you like to provide the sketch?")
    print("  1) Capture live (webcam)")
    print("  2) Select an existing image")
    print("─" * 60)

    while True:
        choice = input("Select option (1 or 2): ").strip()
        if choice == "1":
            captured = capture_sketch_main()
            if not captured:
                print("No image captured — cannot proceed with graph input.")
                sys.exit(1)
            return captured[-1]  # last captured frame
        elif choice == "2":
            while True:
                filepath = input("\nEnter path to image file: ").strip()
                p = Path(filepath)
                if p.exists() and p.is_file():
                    print(f"  ✓ Using image {filepath}")
                    return str(p)
                print(f"  File not found: {filepath}. Please try again.")
        else:
            print("  Invalid selection. Please enter 1 or 2.")


def ask_description() -> str:
    print("\nHow would you like to provide the supply chain description?")
    print("  1) Load from a .txt file")
    print("  2) Type directly in the terminal")

    while True:
        choice = input("\nSelect option (1 or 2): ").strip()
        if choice in {"1", "2"}:
            break
        print("Invalid selection. Please enter 1 or 2.")

    if choice == "1":
        while True:
            filepath = input("Enter path to description file: ").strip()
            p = Path(filepath)
            if p.exists() and p.is_file():
                description = p.read_text(encoding="utf-8")
                print(f"  ✓ Loaded description from {filepath}")
                return description
            print(f"  File not found: {filepath}. Please try again.")

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
    return description


def ask_use_context() -> bool:
    print("\n" + "─" * 60)
    print("⚠️  WARNING: Including system instructions significantly")
    print("   increases token usage and API cost.")
    print("   Only recommended if JSON quality is poor without it.")
    print("─" * 60)
    ctx = input("Include system instructions? (yes/no) [default: no]: ").strip().lower()
    use_context = ctx in {"yes", "y"}
    print(f"  System instructions: {'included' if use_context else 'excluded'}")
    return use_context


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

    # ── Normal flow: input mode → (image) → description → run ─
    print("\n" + "=" * 60)
    print("NL2Sim Pipeline")
    print("=" * 60)

    # OpenAI is the default and only backend asked about here.
    use_azure = False

    input_mode = ask_input_mode()

    image_path = None
    if input_mode in ("graph", "both"):
        image_path = ask_image_path()

    if input_mode == "graph":
        pipeline = Pipeline(
            description="",
            output_dir=output_dir,
            use_context=False,
            simulate=not args.no_simulate,
            use_azure=use_azure,
        )
        pipeline.run_with_graph_only(image_path)
        sys.exit(0)

    description = ask_description()
    use_context = ask_use_context()

    pipeline = Pipeline(
        description=description,
        output_dir=output_dir,
        use_context=use_context,
        simulate=not args.no_simulate,
        use_azure=use_azure,
    )

    if input_mode == "both":
        pipeline.run_with_graph(image_path)
    else:
        pipeline.run()