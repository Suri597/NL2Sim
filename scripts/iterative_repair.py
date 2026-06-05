# interactive_repair.py

from __future__ import annotations

import os
import sys
from copy import deepcopy
from typing import Any, Dict, List
from pprint import pformat
from datetime import datetime
import json

# ------------------------------------------------------------
# Make sure local imports work when run as a script
# ------------------------------------------------------------
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.append(THIS_DIR)

# ------------------------------------------------------------
# Validators
# ------------------------------------------------------------
from validation_layer_a import LayerAValidator
from validation_layer_b import LayerBValidator
from validation_layer_c import LayerCValidator

# ------------------------------------------------------------
# Resolvers
# ------------------------------------------------------------
from resolvers import REGISTRY, set_at_path, describe_finding


# ============================================================
# Unified finding adapter
# ============================================================

class Finding:
    def __init__(self, layer: str, severity: str, path: str, message: str):
        self.layer = layer
        self.severity = severity
        self.path = path
        self.message = message

    def __str__(self) -> str:
        return f"[{self.layer}::{self.severity}] {self.path}: {self.message}"


def adapt_findings(report: Dict[str, List[Any]]) -> List[Finding]:
    """
    Converts validator-specific findings into a uniform Finding list.
    """
    out: List[Finding] = []
    for _, items in report.items():
        for f in items:
            out.append(
                Finding(
                    layer=getattr(f, "layer", "?"),
                    severity=getattr(f, "severity", "?"),
                    path=getattr(f, "path", ""),
                    message=getattr(f, "message", ""),
                )
            )
    return out


# ============================================================
# Interactive repair runner
# ============================================================

class InteractiveRepairRunner:
    def __init__(
        self,
        config: Dict[str, Any],
        *,
        strict_layer0: bool = True,
        max_passes_per_layer: int = 20,
    ):
        self.config = config
        self.strict_layer0 = strict_layer0
        self.max_passes_per_layer = max_passes_per_layer

    # -------------------------
    # Public API
    # -------------------------

    def run(self) -> Dict[str, Any]:
        self._run_layer(
            "Layer0",
            lambda: adapt_findings(
                LayerAValidator(self.config, strict=self.strict_layer0).validate()
            ),
        )

        self._run_layer(
            "LayerB",
            lambda: adapt_findings(
                LayerBValidator(self.config).validate()
            ),
        )

        self._run_layer(
            "LayerC",
            lambda: adapt_findings(
                LayerCValidator(self.config).validate()
            ),
        )

        print("\n✅ All validation layers completed successfully.")
        return self.config

    # -------------------------
    # Core loop
    # -------------------------

    def _run_layer(self, layer_name: str, validate_fn) -> None:
        print("\n==============================")
        print(f"REPAIRING {layer_name}")
        print("==============================")

        skipped_issues = set()

        for pass_i in range(1, self.max_passes_per_layer + 1):

            findings = validate_fn()
            layer_findings = [f for f in findings if f.layer == layer_name]

            errors = [
                f for f in layer_findings
                if f.severity in {"error", "missing_required"}
                and (f.path, f.message) not in skipped_issues
            ]
            warnings = [
                f for f in layer_findings
                if f.severity in {"warning", "missing_optional"}
            ]

            if not errors:
                if warnings:
                    print(f"\n{layer_name} passed with warnings:")
                    for w in warnings:
                        print(" ", w)
                else:
                    print(f"\n{layer_name} passed clean.")
                return

            f0 = errors[0]

            print(f"\n--- {layer_name} pass {pass_i} ---")
            print("Current issue:")
            print(f"  {describe_finding(self.config, f0.path)}")
            print(f"  (detail: {f0.message})")

            applied = REGISTRY.resolve(self.config, f0)

            if applied:
                continue

            print("\nNo resolver found for this issue.")
            print(f"  {describe_finding(self.config, f0.path)}")

            choice = self._fallback_decision()

            if choice == "abort":
                raise SystemExit("Aborted by user.")

            if choice == "skip_issue":
                skipped_issues.add((f0.path, f0.message))
                print(f"  Skipping: {describe_finding(self.config, f0.path)}")
                continue

            # else: retry

        raise RuntimeError(
            f"Exceeded max passes ({self.max_passes_per_layer}) for {layer_name}"
        )

    # -------------------------
    # Fallback prompt
    # -------------------------

    def _fallback_decision(self) -> str:
        print("\nHow do you want to proceed?")
        print("  1) Try again")
        print("  2) Skip this issue")
        print("  3) Abort")
        raw = input("Select option #: ").strip()
        if raw == "2":
            return "skip_issue"
        if raw == "3":
            return "abort"
        return "retry"


# ============================================================
# Persistence helpers
# ============================================================

def backup_and_write_config(config: Dict[str, Any], filepath: str) -> None:
    if os.path.exists(filepath):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = filepath.replace(".py", f"_backup_{ts}.py")
        os.rename(filepath, backup)
        print(f"Backed up previous config to: {backup}")

    with open(filepath, "w") as f:
        f.write("# Auto-generated configuration\n\n")
        f.write("config = ")
        f.write(pformat(config, width=120, sort_dicts=False))
        f.write("\n")

    print(f"Saved updated config to: {filepath}")


CANONICAL_TOP_LEVEL_ORDER = [
    "config_info",
    "raw_materials",
    "intermediate_materials",
    "products",
    "inventory",
    "supplier",
    "resource",
    "facility",
    "customer",
    "nodes",
    "edges",
]


def sort_section(section_name: str, items: list) -> list:
    if not isinstance(items, list):
        return items

    if section_name in {
        "raw_materials",
        "intermediate_materials",
        "products",
        "inventory",
        "supplier",
        "resource",
        "customer",
    }:
        return sorted(items, key=lambda x: x.get("name", ""))

    if section_name == "facility":
        return sorted(
            items,
            key=lambda x: (x.get("name", ""), x.get("operation", {}).get("name", ""))
        )

    if section_name == "edges":
        return sorted(
            items,
            key=lambda x: (
                x.get("source", ""),
                x.get("destination", ""),
                x.get("material_name", ""),
            )
        )

    return items


def _resolve_missing_generic(cfg: dict, path: str) -> None:
    """
    Generic fallback for missing required fields.
    Tries int → float → string in that order.
    """
    print(f"\n--- Required field missing ---")
    print(f"  {describe_finding(cfg, path)}")
    print(f"  (path: {path})")

    while True:
        raw = input("  Enter value: ").strip()
        if not raw:
            print("  Value is required — cannot skip.")
            continue

        try:
            val: Any = int(raw)
        except ValueError:
            try:
                val = float(raw)
            except ValueError:
                val = raw

        set_at_path(cfg, path, val)
        print(f"  ✓ Set → {val}")
        break


def resolve_missing_placeholders(cfg: dict) -> None:
    """
    Pre-validation pass:
    1. Filter the full config to find only required fields
    2. Scan filtered config for "missing" placeholders
    3. For each one, trigger a resolver against the FULL config
    4. Full config is patched in-place — no "missing" left for validators
    """
    from pathlib import Path
    SCRIPTS_DIR = Path(__file__).resolve().parent
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    from data_gen.filter_config import filter_config

    filtered = filter_config(cfg)

    missing_paths = []

    def _scan(obj: Any, path: str = "") -> None:
        if isinstance(obj, str) and obj.strip().lower() == "missing":
            missing_paths.append(path)
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                _scan(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _scan(v, f"{path}[{i}]")

    _scan(filtered)

    if not missing_paths:
        print("  ✓ No required fields missing.")
        return

    print(f"\n  ⚠️  {len(missing_paths)} required field(s) need input:\n")

    for path in missing_paths:
        finding = Finding(
            layer="Layer0",
            severity="missing_required",
            path=path,
            message="Found placeholder 'missing' in required field",
        )

        # Show human-readable breadcrumb instead of raw path
        print(f"  → {describe_finding(cfg, path)}")

        applied = REGISTRY.resolve(cfg, finding)

        if not applied:
            _resolve_missing_generic(cfg, path)


def clean_missing_placeholders(cfg: dict, required_paths: set = None) -> dict:
    """
    Replace 'missing' placeholder strings with empty string ""
    for all fields except those in required_paths.
    """
    import copy
    from validation_layer_a import MISSING_POLICY_REQUIRED

    if required_paths is None:
        required_paths = MISSING_POLICY_REQUIRED

    cfg = copy.deepcopy(cfg)

    def _canonical(path: str) -> str:
        out = []
        for part in path.replace("]", "").split("."):
            if "[" in part:
                part = part.split("[", 1)[0]
            out.append(part)
        return ".".join(out)

    def _clean(obj: Any, path: str = "") -> Any:
        if isinstance(obj, str):
            if obj.strip().lower() == "missing":
                canon = _canonical(path)
                if canon in required_paths:
                    return obj
                return ""
            return obj

        if isinstance(obj, dict):
            return {
                k: _clean(v, f"{path}.{k}" if path else k)
                for k, v in obj.items()
            }

        if isinstance(obj, list):
            return [
                _clean(v, f"{path}[{i}]")
                for i, v in enumerate(obj)
            ]

        return obj

    return _clean(cfg)


def canonicalize_config(config: dict) -> dict:
    """
    Reorders the config deterministically for human readability.
    Semantics are unchanged.
    """
    new_cfg = {}

    for key in CANONICAL_TOP_LEVEL_ORDER:
        if key not in config:
            continue

        val = config[key]

        if isinstance(val, list):
            new_cfg[key] = sort_section(key, val)
        else:
            new_cfg[key] = val

    for key, val in config.items():
        if key not in new_cfg:
            new_cfg[key] = val

    return new_cfg


def deep_sort(obj, *, _is_root=True):
    """
    Recursively sort config for readability.
    - Root dict preserves key order (canonical order)
    - Nested dicts are sorted alphabetically
    - Lists of dicts sorted by name
    - Lists of strings sorted alphabetically
    """
    if isinstance(obj, dict):
        items = obj.items()

        if not _is_root:
            items = sorted(items, key=lambda kv: kv[0])

        return {
            k: deep_sort(v, _is_root=False)
            for k, v in items
        }

    if isinstance(obj, list):
        if all(isinstance(x, dict) and "name" in x for x in obj):
            return [
                deep_sort(x, _is_root=False)
                for x in sorted(obj, key=lambda d: d.get("name", ""))
            ]

        if all(isinstance(x, str) for x in obj):
            return sorted(obj)

        return [deep_sort(x, _is_root=False) for x in obj]

    return obj


# ============================================================
# Entry point
# ============================================================

def interactive_repair(config: dict) -> dict:
    runner = InteractiveRepairRunner(
        config,
        strict_layer0=True,
        max_passes_per_layer=20,
    )
    return runner.run()


if __name__ == "__main__":
    import sys
    from copy import deepcopy
    from pprint import pformat

    if len(sys.argv) < 2:
        print("Usage: python iterative_repair.py <config.json> [output.json]")
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        config = json.load(f)

    working = deepcopy(config)

    runner = InteractiveRepairRunner(
        working,
        strict_layer0=True,
        max_passes_per_layer=20,
    )

    raw_config   = runner.run()
    final_config = deep_sort(canonicalize_config(raw_config))

    print("\n==============================")
    print("FINAL CONFIG PREVIEW")
    print("==============================")
    print(pformat(final_config, width=220))

    print("\nDo you want to save this configuration?")
    print("  1) Yes — save to outputs folder")
    print("  2) No")
    choice = input("Select option #: ").strip().lower()

    if choice in {"1", "yes", "y", ""}:
        if len(sys.argv) > 2:
            output_path = sys.argv[2]
        else:
            input_name = os.path.basename(sys.argv[1]).replace(".json", "")
            output_path = os.path.join(
                os.path.dirname(sys.argv[1]),
                f"{input_name}_repaired.json"
            )

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(final_config, f, indent=2)
        print(f"Saved → {output_path}")
    else:
        print("Config not saved.")