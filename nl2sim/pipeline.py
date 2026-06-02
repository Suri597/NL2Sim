"""
nl2sim/pipeline.py
-------------------
NL2Sim pipeline library — importable by run_pipeline.py or any external code.

Usage (imported):
    from nl2sim.pipeline import Pipeline
    results = Pipeline(description).run()
"""

from __future__ import annotations

import json
import sys
import os
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional

# ── ensure scripts/ is on path ─────────────────────────────
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from nl_to_json        import generate_json
from iterative_repair  import InteractiveRepairRunner, canonicalize_config, deep_sort, resolve_missing_placeholders
from score_reliability import compute_reliability_score, print_score_report
from simulate          import run_simulation
from nl_to_whatif      import generate_whatif
from what_if_engine    import apply_what_if_config, WhatIfError


# ============================================================
# Pipeline
# ============================================================

class Pipeline:
    """
    Full NL2Sim pipeline:
      1. Generate JSON from natural language description
      2. Validate and repair JSON
      3. Compute reliability score
      4. Run simulation
      5. Optional: what-if analysis
    """

    def __init__(
        self,
        description:  str,
        output_dir:   Optional[Path] = None,
        use_context:  bool = True,
        simulate:     bool = True,
        use_azure:    bool = False,
    ):
        self.description = description
        self.use_context = use_context
        self.simulate    = simulate
        self.use_azure   = use_azure

        # ── output directory ───────────────────────────────
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base      = output_dir or (
            Path(__file__).resolve().parents[1] / "outputs"
        )
        self.output_dir = Path(base) / f"run_{timestamp}"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # ── state ──────────────────────────────────────────
        self.config:             Optional[Dict[str, Any]] = None
        self.reliability_result: Optional[Dict[str, Any]] = None
        self.simulation_result:  Optional[Dict[str, Any]] = None
        self.whatif_results:     list = []
        self.whatif_count:       int  = 0

    # ── Step 1 — Generate ──────────────────────────────────

    def generate(self) -> Dict[str, Any]:
        print("\nStep 1/4 — Generating JSON from description...")
        cfg = generate_json(
            self.description,
            use_context=self.use_context,
            use_azure=self.use_azure,
        )
        self._save("config_raw.json", cfg)
        print(f"  ✓ JSON generated → {self.output_dir / 'config_raw.json'}")
        return cfg

    # ── Step 2 — Validate + Repair ─────────────────────────

    def validate(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        print("\nStep 2/4 — Validating and repairing JSON...")

        # ── Pre-pass: resolve all "missing" placeholders ───────
        # Uses filter_config as oracle — only required fields are scanned
        # Full config is patched in-place before validators run
        resolve_missing_placeholders(cfg)

        # ── Full patched JSON → validation layers ──────────────
        runner = InteractiveRepairRunner(
            cfg,
            strict_layer0=True,
            max_passes_per_layer=20,
        )
        repaired = runner.run()
        repaired = deep_sort(canonicalize_config(repaired))
        self._save("config.json", repaired)
        print(f"  ✓ Config validated → {self.output_dir / 'config.json'}")
        return repaired

    # ── Step 3 — Reliability Score ─────────────────────────

    def score(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        print("\nStep 3/4 — Computing reliability score...")
        try:
            result = compute_reliability_score(
                self.description,
                cfg,
                use_azure=self.use_azure,
            )
            print_score_report(result)
            self._save("reliability_score.json", result)
            return result
        except Exception as e:
            print(f"\n  ✗ Reliability score failed: {e}")
            print("\n  How would you like to proceed?")
            print("  1) Skip score and continue")
            print("  2) Abort")
            while True:
                choice = input("  Select option #: ").strip()
                if choice == "1":
                    print("  Skipping reliability score.")
                    return {}
                elif choice == "2":
                    raise SystemExit("Aborted by user.")
                else:
                    print("  Invalid selection. Please enter 1 or 2.")

    # ── Step 4 — Simulate ──────────────────────────────────

    def simulate_run(
        self,
        cfg: Dict[str, Any],
        output_name: str = "simulation_results.json",
    ) -> Dict[str, Any]:
        print(f"\nStep 4/4 — Running simulation...")
        out_path = str(self.output_dir / output_name)
        result   = run_simulation(cfg, output_path=out_path)
        print(f"  ✓ Results saved → {out_path}")
        return result

    # ── Step 5 — What-if ───────────────────────────────────

    def run_whatif(
        self,
        cfg:          Dict[str, Any],
        instruction:  str,
        use_examples: bool = True,
    ) -> Dict[str, Any]:
        self.whatif_count += 1
        n = self.whatif_count

        print(f"\n  Generating what-if JSON...")
        whatif_json = generate_whatif(
            instruction, cfg, use_examples=use_examples)
        self._save(f"whatif_{n}_changes.json", whatif_json)

        print(f"  Applying changes...")
        from copy import deepcopy
        modified = apply_what_if_config(deepcopy(cfg), whatif_json)

        print(f"  Validating modified config...")
        runner   = InteractiveRepairRunner(
            modified, strict_layer0=True, max_passes_per_layer=20)
        repaired = deep_sort(canonicalize_config(runner.run()))
        self._save(f"whatif_{n}_config.json", repaired)
        print(f"  ✓ What-if config → {self.output_dir / f'whatif_{n}_config.json'}")

        print(f"  Running what-if simulation...")
        out_path = str(self.output_dir / f"whatif_{n}_results.json")
        result   = run_simulation(repaired, output_path=out_path)
        print(f"  ✓ What-if results → {out_path}")

        self.whatif_results.append({
            "instruction": instruction,
            "whatif_json": whatif_json,
            "result":      result,
        })

        return repaired, result

    # ── Full pipeline ──────────────────────────────────────

    def run(self) -> Dict[str, Any]:
        self._print_header()

        # Step 1 — Generate
        cfg = self.generate()

        # Step 2 — Validate
        cfg = self.validate(cfg)
        self.config = cfg

        # Step 3 — Score
        self.reliability_result = self.score(cfg)

        # ── Ask user to proceed or re-enter description ────
        while True:
            print("\n" + "─" * 60)
            print("How would you like to proceed?")
            print("  1) Proceed to simulation")
            print("  2) Re-enter description and regenerate")
            print("  3) Exit")
            print("─" * 60)
            choice = input("Select option: ").strip()

            if choice == "1":
                break

            elif choice == "2":
                print("\nEnter new supply chain description.")
                print("Press Enter on an empty line when done.\n")
                lines = []
                while True:
                    line = input()
                    if line == "":
                        break
                    lines.append(line)
                new_description = "\n".join(lines).strip()
                if not new_description:
                    print("No description entered — keeping original.")
                    break
                self.description = new_description
                print("  ✓ New description received. Regenerating...\n")

                # regenerate + revalidate + rescore
                cfg = self.generate()
                cfg = self.validate(cfg)
                self.config = cfg
                self.reliability_result = self.score(cfg)

            elif choice == "3":
                print("Exiting pipeline.")
                sys.exit(0)

            else:
                print("Invalid selection. Please enter 1, 2 or 3.")

        # Step 4 — Simulate
        if self.simulate:
            self.simulation_result = self.simulate_run(cfg)

        # Step 5 — What-if loop
        self._whatif_loop(cfg)

        # Save summary
        summary = self._build_summary()
        self._save("pipeline_summary.json", summary)

        self._print_footer()
        return summary

    # ── Validate + Simulate (skip LLM step) ───────────────

    def validate_and_simulate(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """
        Entry point when skipping LLM step (--from-config flag).
        Starts from an existing raw config JSON.
        """
        self._print_header()

        # Step 2 — Validate
        cfg = self.validate(cfg)
        self.config = cfg

        # Step 3 — Score
        self.reliability_result = self.score(cfg)

        # ── Ask user to proceed ────────────────────────────
        while True:
            print("\n" + "─" * 60)
            print("How would you like to proceed?")
            print("  1) Proceed to simulation")
            print("  2) Exit")
            print("─" * 60)
            choice = input("Select option: ").strip()

            if choice == "1":
                break
            elif choice == "2":
                print("Exiting pipeline.")
                sys.exit(0)
            else:
                print("Invalid selection. Please enter 1 or 2.")

        # Step 4 — Simulate
        if self.simulate:
            self.simulation_result = self.simulate_run(cfg)

        # Step 5 — What-if loop
        self._whatif_loop(cfg)

        # Save summary
        summary = self._build_summary()
        self._save("pipeline_summary.json", summary)

        self._print_footer()
        return summary

    # ── What-if interactive loop ───────────────────────────

    def _whatif_loop(self, cfg: Dict[str, Any]):
        current_cfg = cfg
        while True:
            print("\n" + "=" * 60)
            answer = input(
                "Would you like to run a what-if analysis? (yes/no): "
            ).strip().lower()

            if answer not in {"yes", "y", "1"}:
                break

            # ── ask about examples ─────────────────────────
            print("\n" + "─" * 60)
            print("Few-shot examples improve accuracy but use more tokens.")
            print("Recommended: Yes for complex changes, No for simple ones.")
            print("─" * 60)
            ex = input(
                "Include examples? (yes/no) [default: yes]: "
            ).strip().lower()
            use_examples = ex not in {"no", "n"}

            # ── get instruction ────────────────────────────
            print("\n" + "=" * 60)
            print("NL2Sim What-If Instruction")
            print("=" * 60)
            print("Describe the change you want to make.")
            print("⚠️  Be specific — mention exact entity names.")
            print("Press Enter on an empty line when done.\n")

            lines = []
            while True:
                line = input()
                if line == "":
                    break
                lines.append(line)

            instruction = "\n".join(lines).strip()
            if not instruction:
                print("No instruction entered — skipping.")
                continue

            # ── save instruction ───────────────────────────
            n         = self.whatif_count + 1
            instr_dir = self.output_dir / "whatif_instructions"
            instr_dir.mkdir(exist_ok=True)
            ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
            (instr_dir / f"instruction_{n}_{ts}.txt").write_text(
                f"Instruction {n}:\n{instruction}\n",
                encoding="utf-8",
            )

            # ── run what-if ────────────────────────────────
            try:
                current_cfg, _ = self.run_whatif(
                    current_cfg, instruction,
                    use_examples=use_examples,
                )
            except WhatIfError as e:
                print(f"  ✗ What-if failed: {e}")

    # ── Helpers ────────────────────────────────────────────

    def _save(self, filename: str, data: dict):
        path = self.output_dir / filename
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)

    def _build_summary(self) -> Dict[str, Any]:
        return {
            "description":        self.description[:500],
            "output_dir":         str(self.output_dir),
            "reliability_score":  self.reliability_result,
            "simulation_result":  self.simulation_result,
            "whatif_count":       self.whatif_count,
            "whatif_results": [
                {
                    "instruction": w["instruction"],
                    "whatif_json": w["whatif_json"],
                }
                for w in self.whatif_results
            ],
        }

    def _print_header(self):
        print("\n" + "=" * 60)
        print("NL2Sim Pipeline")
        print("=" * 60)

    def _print_footer(self):
        print("\n" + "=" * 60)
        print("Pipeline complete.")
        print(f"All outputs saved → {self.output_dir}")
        print("=" * 60)