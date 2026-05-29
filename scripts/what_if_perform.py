import json
from copy import deepcopy

from what_if_engine import apply_what_if_config
# from config_v2 import config as base_config
from interactive_repair import InteractiveRepairRunner


# ------------------------------------------------------------
# Load what-if instructions
# ------------------------------------------------------------
with open("what_if_config.json") as f:
    what_if_config = json.load(f)

 # from config_v2 import config
    
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
runner = InteractiveRepairRunner(
    candidate,
    strict_layer0=True,
    max_passes_per_layer=20,
)

final_config = runner.run()

print("##################")
print("FINAL CONFIG (after interactive repair)")
print(final_config)
print("##################")
