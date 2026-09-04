"""
scripts/graph_input/run_with_input_mode.py

Entry point that lets the user choose HOW to provide the initial scenario:
  1) Text only        -> existing NL2Sim text pipeline (Pipeline.run())
  2) Graph only        -> sketch/image -> topology JSON (no NL to merge against yet)
  3) Graph + Text      -> sketch/image + description -> merge.py reconciliation
                          -> feeds the reconciled config into the existing
                          pipeline from validate() onward (skipping generate()).

This intentionally stays OUTSIDE nl2sim/pipeline.py for now, since the
verification-layer sequencing for graph-derived configs (already-structured
vs. raw NL output) hasn't been finalized yet. Once that's settled, this
selection logic can move into Pipeline itself.

Usage:
    python run_with_input_mode.py
"""

import json
import os
import sys
from pathlib import Path

# ── ensure scripts/ and scripts/graph_input/ are on path ──────────────
THIS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = THIS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from capture_sketch import main as capture_sketch_main
from image_to_json import extract_topology
import merge as merge_module

# nl2sim/pipeline.py already puts scripts/ on sys.path when imported,
# and exposes Pipeline for the text-only and post-merge paths.
sys.path.insert(0, str(SCRIPTS_DIR.parent))
from nl2sim.pipeline import Pipeline


OUTPUT_DIR = SCRIPTS_DIR.parent / "output"


def _prompt_choice(prompt: str, options: dict) -> str:
    """options: {key: label}. Returns the chosen key."""
    while True:
        print(f"\n{prompt}")
        for key, label in options.items():
            print(f"  {key}) {label}")
        choice = input("Select option: ").strip()
        if choice in options:
            return choice
        print("Invalid selection.")


def _collect_description() -> str:
    print("\nEnter supply chain description.")
    print("Press Enter on an empty line when done.\n")
    lines = []
    while True:
        line = input()
        if line == "":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _collect_image_path() -> str:
    """Asks capture-live vs. select-existing, returns a path to the image."""
    mode = _prompt_choice(
        "How would you like to provide the sketch?",
        {"1": "Capture live (webcam)", "2": "Select an existing image"},
    )

    if mode == "1":
        captured = capture_sketch_main()
        if not captured:
            print("No image captured — cannot proceed with graph input.")
            sys.exit(1)
        # capture_sketch saves possibly multiple frames; use the last one.
        return captured[-1]

    else:
        filename = input("\nImage file name (path): ").strip()
        if not os.path.exists(filename):
            print(f"Error: image not found at {filename}", file=sys.stderr)
            sys.exit(1)
        return filename


def run_text_only():
    description = _collect_description()
    if not description:
        print("No description entered — aborting.")
        sys.exit(1)
    Pipeline(description).run()


def run_graph_only():
    image_path = _collect_image_path()
    print(f"\nExtracting topology from {image_path}...")
    topology = extract_topology(image_path)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = os.path.splitext(os.path.basename(image_path))[0]
    out_path = OUTPUT_DIR / f"{base}_topology.json"
    with open(out_path, "w") as f:
        json.dump(topology, f, indent=2)

    print(f"\nTopology extracted -> {out_path}")
    print(json.dumps(topology, indent=2))
    print(
        "\nGraph-only input has no description to merge against, so attribute "
        "fields (capacities, costs, distributions, etc.) remain 'missing'. "
        "This mode does not yet continue into verification/repair — resolve "
        "how graph-only configs should be completed before wiring that up."
    )


def run_graph_and_text():
    description = _collect_description()
    if not description:
        print("No description entered — aborting.")
        sys.exit(1)

    image_path = _collect_image_path()
    print(f"\nExtracting topology from {image_path}...")
    topology = extract_topology(image_path)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    graph_path = OUTPUT_DIR / "graph_topology.json"
    with open(graph_path, "w") as f:
        json.dump(topology, f, indent=2)
    print(f"  ✓ Topology saved -> {graph_path}")

    print("\nGenerating NL-derived config from description...")
    nl_json = merge_module.load_json  # placeholder alias avoided below
    from nl_to_json import generate_json
    nl_config = generate_json(description, use_context=True, use_azure=False)
    nl_path = OUTPUT_DIR / "nl_config.json"
    with open(nl_path, "w") as f:
        json.dump(nl_config, f, indent=2)
    print(f"  ✓ NL config saved -> {nl_path}")

    print("\nReconciling graph + NL sources (merge.py)...")
    # merge.py's main() is CLI/argparse-driven and heavily checkpointed;
    # invoke it as a subprocess against the two files we just wrote so its
    # existing dispute-dialogue flow (terminal input) runs unmodified.
    import subprocess
    merge_out = OUTPUT_DIR / "nl_fixed.json"
    merge_script = THIS_DIR / "merge.py"
    result = subprocess.run(
        [sys.executable, str(merge_script),
         "--graph", str(graph_path),
         "--nl", str(nl_path),
         "--out", str(merge_out)],
    )
    if result.returncode != 0:
        print("merge.py did not complete successfully — aborting.", file=sys.stderr)
        sys.exit(result.returncode)

    with open(merge_out, "r") as f:
        reconciled_cfg = json.load(f)

    print(f"\nReconciled config ready -> {merge_out}")
    print("Continuing into the existing pipeline from validation onward "
          "(skipping JSON generation, since the config already exists)...")

    Pipeline(description).validate_and_simulate(reconciled_cfg)


def main():
    choice = _prompt_choice(
        "How would you like to provide the scenario?",
        {"1": "Text only", "2": "Graph only", "3": "Graph + Text"},
    )

    if choice == "1":
        run_text_only()
    elif choice == "2":
        run_graph_only()
    else:
        run_graph_and_text()


if __name__ == "__main__":
    main()