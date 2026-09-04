import json
from copy import deepcopy

from what_if_engine import apply_what_if_config
from orchestrator import run_repair_loop, canonicalize_config, deep_sort


# ------------------------------------------------------------
# Load what-if instructions
# ------------------------------------------------------------
with open("what_if_config.json") as f:
    what_if_config = json.load(f)

with open("active_config.json") as f:
    config = json.load(f)

# ------------------------------------------------------------
# Apply deterministic what-if changes
# ------------------------------------------------------------
candidate = apply_what_if_config(
    deepcopy(config),
    what_if_config
)

print("##################")
print("OLD CONFIG")
print(config)
print("##################")
print("CANDIDATE CONFIG (after what-if)")
print(candidate)
print("##################")


# ------------------------------------------------------------
# Interactive validation + repair
# ------------------------------------------------------------
final_config, remaining = run_repair_loop(candidate, max_iterations=60, verbose=True)

if remaining:
    print(f"\n{len(remaining)} issue(s) could not be fully resolved:")
    for issue in remaining:
        print("   ", issue)

final_config = deep_sort(canonicalize_config(final_config))

with open("active_config_whatif.json", "w") as f:
    json.dump(final_config, f, indent=2)

print("##################")
print("FINAL CONFIG (after interactive repair)")
print(final_config)
print("##################")