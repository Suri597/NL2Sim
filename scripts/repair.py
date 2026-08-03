"""
scripts2/repair.py
--------------------
Corrective actions for issues found by verification_layer1/2/3.py.

IMPORTANT ARCHITECTURE NOTE: this file contains ONLY the repair actions
themselves -- pure functions that take a config + a ValidationIssue and
fix that ONE issue. It does NOT contain the loop that runs verification,
dispatches to repair, and re-verifies -- that orchestration lives in a
separate script (not yet built). Keeping these separate means the repair
actions stay simple, testable in isolation, and swappable without
touching the loop logic.

Built in the same sequence verification_layer1.py's conditions were
built, starting with the simplest case.

Implemented so far:
    1. Scalar missing/placeholder field repair: given a MISSING_REQUIRED_VALUE
       issue on a plain scalar field (str/num), insert the field at the
       correct location and prompt the user for a value, validating its
       type before accepting it.

NOT yet implemented (planned, in order):
    2. Dict-shaped missing fields (e.g. bom) -- insert {} then prompt for
       key/value pairs.
    3. List-shaped missing fields (e.g. inventory_managed, operation.input/
       output, nodes[0].supplier/facility/customer) -- insert [] then
       prompt for one or more entries.
    4. Distribution-object missing fields -- insert the {distribution,
       parameters} shape, prompt for distribution type first, THEN only
       prompt for the parameters that distribution type actually needs
       (reusing verification_layer1.py's DISTRIBUTION_PARAM_COUNTS).
    5. Nested multi-field entities with conditional requirements (e.g. a
       whole missing supplier[] entry, or resource.batching where
       batch_size is only asked for if enabled=True).
"""

from __future__ import annotations

import re
from typing import Optional

from issue_types import ValidationIssue, DefectType, Severity
from verification_layer2 import INVENTORY_TYPE_TO_SECTION
from verification_layer3 import _build_adjacency, _bfs_reachable_from
from verification_layer1 import (
    SECTION_SPECS, SIMULATION_FIELDS, is_required,
    DISTRIBUTION_PARAM_COUNTS, PARAM_KEYS,
    MATERIAL_TYPES, PROCUREMENT_SCHEME_TYPES, FACILITY_TYPES, SHORTAGE_POLICIES,
)


# ============================================================
# Natural-language description helpers
# (originally a separate humanize.py -- merged in here since every
# actual USE of these functions is from repair actions in this file)
# ============================================================

# ============================================================
# Entity resolution
# ============================================================

def _entry_at(config: dict, section: str, idx) -> Optional[dict]:
    entries = config.get(section, []) or []
    if not isinstance(idx, int) or idx < 0 or idx >= len(entries):
        return None
    entry = entries[idx]
    return entry if isinstance(entry, dict) else None


def describe_entity(config: dict, section: str, idx) -> str:
    """
    Produces a short, identifying, natural-language phrase for a
    specific entity -- its name plus whatever context makes it
    unambiguous and meaningful to a non-technical reader. Falls back to
    a plain "the Nth entry in <section>" if the entry or its name can't
    be resolved.
    """
    entry = _entry_at(config, section, idx)
    if entry is None:
        return f"entry #{idx} in {section.replace('_', ' ')}"

    name = entry.get("name")
    name_part = f"'{name}'" if isinstance(name, str) and name and name != "missing" else "(unnamed entry)"

    if section == "raw_materials":
        return f"the raw material {name_part}"
    if section == "intermediate_materials":
        return f"the intermediate material {name_part}"
    if section == "products":
        return f"the product {name_part}"
    if section == "inventory":
        inv_type = entry.get("type")
        label = {"raw_material": "raw material", "intermediate_material": "intermediate material", "product": "product"}.get(inv_type, "item")
        return f"inventory tracking for {label} {name_part}"
    if section == "supplier":
        material = entry.get("supply_material_name")
        suffix = f" (supplies '{material}')" if isinstance(material, str) and material else ""
        return f"the supplier {name_part}{suffix}"
    if section == "resource":
        return f"the resource {name_part}"
    if section == "facility":
        ftype = entry.get("type")
        output = (entry.get("operation") or {}).get("output") or []
        if ftype == "warehouse":
            return f"the warehouse {name_part}"
        suffix = f" (makes: {', '.join(output)})" if output else ""
        return f"the manufacturing facility {name_part}{suffix}"
    if section == "customer":
        product = entry.get("product")
        suffix = f" (orders '{product}')" if isinstance(product, str) and product else ""
        return f"the customer {name_part}{suffix}"

    return name_part


def _describe_edge(config: dict, idx: int) -> str:
    entry = _entry_at(config, "edges", idx)
    if entry is None:
        return f"edge #{idx}"
    src = entry.get("source")
    src = src if isinstance(src, str) and src != "missing" else "?"
    dst = entry.get("destination")
    dst = dst if isinstance(dst, str) and dst != "missing" else "?"
    mat = entry.get("material_name")
    label = f"the delivery from '{src}' to '{dst}'"
    if isinstance(mat, str) and mat and mat != "missing":
        label += f" (carrying '{mat}')"
    return label


# ============================================================
# Field-path -> question templates
# ============================================================
# Keyed by (section, field_path_within_entity) -- field_path is the
# location string with the "section[idx]." prefix stripped. Each
# template is a function(config, entry, extra) -> question string.
# "extra" carries any regex-captured group needed (e.g. distribution
# parameter letter, or which sub-object like customer_lead_time).

def _distribution_question(entry_desc: str, what: str) -> str:
    return (f"For {entry_desc}: {what} Is it always the same, or does it "
            f"vary according to some distribution?")


def _param_question(entry_desc: str, what_noun: str, param_letter: str) -> str:
    return f"For {entry_desc}: what number describes {what_noun} (this specific value, not the overall pattern)?"


# Static field -> question-fragment lookup for the most common leaf
# fields across the schema. Each value is the "what" clause plugged
# into _distribution_question, or a standalone full question for
# non-distribution fields.
_DISTRIBUTION_FIELD_LABELS = {
    "customer_lead_time": "how long does it usually take for this customer to receive their order once it ships?",
    "customer_payment_lead_time": "how long after delivery does this customer usually pay?",
    "arrival_time": "how often does this customer place an order?",
    "demand": "how many units does this customer order each time?",
    "supplier_lead_time": "how long does it usually take this supplier to deliver after an order is placed?",
    "supplier_payment_lead_time": "how long after delivery do you usually pay this supplier?",
    "operation_cycle": "how long does it take to produce one unit (or one batch)?",
    "service_time": "how long does this resource take to process one unit?",
    "transfer_time": "how long does this delivery usually take?",
    "procurement_arrival": "how often do periodic supply orders arrive?",
}

# Noun-phrase form of the same fields, for composing parameter
# sub-questions cleanly (avoids mangling a question into a clause).
_DISTRIBUTION_FIELD_NOUNS = {
    "customer_lead_time": "the delivery time to this customer",
    "customer_payment_lead_time": "this customer's payment delay",
    "arrival_time": "how often this customer orders",
    "demand": "the order quantity",
    "supplier_lead_time": "this supplier's delivery time",
    "supplier_payment_lead_time": "the payment delay to this supplier",
    "operation_cycle": "the production time per unit/batch",
    "service_time": "this resource's processing time per unit",
    "transfer_time": "this delivery's transfer time",
    "procurement_arrival": "how often supply orders arrive",
}

_SCALAR_FIELD_QUESTIONS = {
    "shortage_policy": "For {entity}: if there isn't enough stock to fill an order, what should happen -- should the sale be lost, backordered, or partially fulfilled?",
    "unit_selling_price": "For {entity}: what price is charged per unit sold?",
    "initial_inventory": "For {entity}: how many units are on hand at the very start of the simulation?",
    "name": "What should this entry be named?",
    "type": "For {entity}: what type is this?",
    "supply_material_name": "For {entity}: which raw material does this supplier provide?",
    "supplier_cost": "For {entity}: what does one unit cost from this supplier?",
    "supplier_capacity": "For {entity}: is there a maximum amount this supplier can deliver at once? (Leave as unlimited if not.)",
    "capacity": "For {entity}: how many units can this resource handle at the same time?",
    "resource_required": "For {entity}: does this operation need a specific resource (machine/worker) to run, or none at all?",
    "product": "For {entity}: which product does this customer order?",
}


def _procurement_scheme_type_question(entity_desc: str) -> str:
    return (
        f"For {entity_desc}: how is this material restocked -- "
        f"delivered on a regular schedule (periodic supply), "
        f"ordered only when stock runs low (inventory threshold, using a reorder point and a refill-up-to level), "
        f"or ordered directly in response to demand (demand-driven)?"
    )


def _procurement_threshold_param_question(entity_desc: str, param_letter: str) -> str:
    if param_letter == "a":
        return f"For {entity_desc}: at what stock level should a reorder be triggered (the reorder point, 's')?"
    if param_letter == "b":
        return f"For {entity_desc}: when reordering, what level should stock be refilled up to (the order-up-to level, 'S')?"
    return f"For {entity_desc}: what should parameter '{param_letter}' be?"


# ============================================================
# Breadcrumb-style description (simpler alternative to full questions)
# ============================================================

def describe_location(config: dict, location: str) -> str:
    """
    Converts a technical location string into a readable breadcrumb,
    matching the reference implementation's describe_finding() style --
    NOT a full question, just the path made human-readable: array
    indices replaced with the entry's name where one exists, dots/
    brackets replaced with ' -> ', section/field names title-cased.

    Examples:
      inventory[1].procurement_scheme.parameters.a
        -> Inventory -> 'lead frame' -> Procurement scheme -> Parameters -> a
      customer[0].shortage_policy
        -> Customer -> 'Ross Associates' -> Shortage policy
      edges[0].transfer_time.parameters.a
        -> Edges -> 'WaferSource Inc. -> Wafer Fab (silicon wafer)' -> Transfer time -> Parameters -> a

    Falls back to the raw location string on any unexpected shape --
    never raises.
    """
    try:
        parts = []
        node = config
        tokens = re.findall(r"[a-zA-Z_]\w*(?:\[\d+\])?", location)

        for token in tokens:
            m = re.match(r"^([a-zA-Z_]\w*)(\[(\d+)\])?$", token)
            if not m:
                parts.append(token.replace("_", " ").capitalize())
                continue

            key = m.group(1)
            idx_str = m.group(3)

            child = node.get(key) if isinstance(node, dict) else None
            parts.append(key.replace("_", " ").capitalize())
            node = child

            if idx_str is not None:
                idx = int(idx_str)
                entry = node[idx] if isinstance(node, list) and 0 <= idx < len(node) else None

                if key == "edges" and isinstance(entry, dict):
                    src = entry.get("source")
                    src = src if isinstance(src, str) and src != "missing" else "?"
                    dst = entry.get("destination")
                    dst = dst if isinstance(dst, str) and dst != "missing" else "?"
                    mat = entry.get("material_name")
                    edge_label = f"{src} -> {dst}"
                    if isinstance(mat, str) and mat and mat != "missing":
                        edge_label += f" ({mat})"
                    parts.append(f"'{edge_label}'")
                else:
                    name = entry.get("name") if isinstance(entry, dict) else None
                    if isinstance(name, str) and name and name != "missing":
                        parts.append(f"'{name}'")
                    else:
                        parts.append(f"[{idx}]")

                node = entry

        return " -> ".join(parts)

    except Exception:
        return location


# ============================================================
# Main entry point
# ============================================================

def humanize_question(config: dict, location: str) -> str:
    """
    The main entry point: given a ValidationIssue.location string and
    the config, returns a natural-language QUESTION. Falls back to a
    generic breadcrumb-based phrasing if no specific template exists
    for the field involved -- never raises, always returns something
    displayable.
    """
    try:
        return _humanize_question_inner(config, location)
    except Exception:
        return f"Please provide a value for '{location}'."


def _humanize_question_inner(config: dict, location: str) -> str:
    steps = _parse_location_steps(location)
    if not steps:
        return f"Please provide a value for '{location}'."

    section = steps[0]

    # ---- edges: distinct entity description (no simple "name") ----
    if section == "edges" and len(steps) >= 2 and isinstance(steps[1], int):
        idx = steps[1]
        entity_desc = _describe_edge(config, idx)
        remainder = ".".join(str(s) for s in steps[2:])
        if remainder.startswith("transfer_time"):
            what = _DISTRIBUTION_FIELD_LABELS["transfer_time"]
            pm = re.search(r"parameters\.([a-e])$", remainder)
            if pm:
                return _param_question(entity_desc, _DISTRIBUTION_FIELD_NOUNS["transfer_time"], pm.group(1))
            if remainder.endswith(".distribution") or remainder == "transfer_time":
                return _distribution_question(entity_desc, what)
        if remainder == "material_type":
            return f"For {entity_desc}: what category is this material -- raw material, intermediate material, or product?"
        if remainder == "material_name":
            return f"For {entity_desc}: what material is being delivered?"
        return f"For {entity_desc}: please provide '{remainder}'."

    # ---- sectioned entities with a numeric index ----
    if len(steps) >= 2 and isinstance(steps[1], int):
        idx = steps[1]
        entity_desc = describe_entity(config, section, idx)
        remainder_steps = steps[2:]
        remainder = ".".join(str(s) for s in remainder_steps)

        if not remainder:
            return f"Please provide details for {entity_desc}."

        # ---- procurement_scheme: special three-way structure ----
        if remainder.startswith("procurement_scheme"):
            if remainder == "procurement_scheme.type" or remainder == "procurement_scheme":
                return _procurement_scheme_type_question(entity_desc)
            pm = re.search(r"parameters\.([a-e])$", remainder)
            if pm:
                entry = _entry_at(config, section, idx)
                ps = (entry or {}).get("procurement_scheme") or {}
                if ps.get("type") == "inventory_threshold":
                    return _procurement_threshold_param_question(entity_desc, pm.group(1))
                return _param_question(entity_desc, "the amount of variability in the order quantity", pm.group(1))
            if remainder.endswith(".distribution"):
                return _distribution_question(entity_desc, "how much does the order quantity vary?")

        # ---- generic distribution-shaped fields ----
        for field_key, what in _DISTRIBUTION_FIELD_LABELS.items():
            if remainder == field_key or remainder.startswith(field_key + "."):
                pm = re.search(r"parameters\.([a-e])$", remainder)
                if pm:
                    return _param_question(entity_desc, _DISTRIBUTION_FIELD_NOUNS[field_key], pm.group(1))
                if remainder.endswith(".distribution") or remainder == field_key:
                    return _distribution_question(entity_desc, what)

        # ---- known scalar fields (TOP-LEVEL on the entity only -- a
        # nested field sharing a leaf name, e.g. operation.name, is a
        # different question and must not match here) ----
        if len(remainder_steps) == 1 and isinstance(remainder_steps[0], str):
            leaf = remainder_steps[0]
            if leaf in _SCALAR_FIELD_QUESTIONS:
                return _SCALAR_FIELD_QUESTIONS[leaf].format(entity=entity_desc)

        # ---- nested "name" fields (e.g. facility[1].operation.name) --
        # still get entity context, just phrased for the sub-object ----
        if remainder.endswith(".name") and len(remainder_steps) > 1:
            container = remainder_steps[-2]
            if isinstance(container, str):
                container_label = container.replace("_", " ")
                return f"For {entity_desc}: what should its {container_label} be named?"

        # ---- inventory_costs ----
        if remainder.startswith("inventory_costs"):
            cost_labels = {
                "holding_cost": "what does it cost to hold one unit in stock for one time period?",
                "shortage_cost": "what's the cost of being short one unit when it's needed?",
                "review_time": "how often is stock level reviewed?",
            }
            leaf2 = remainder.split(".")[-1]
            if leaf2 in cost_labels:
                return f"For {entity_desc}: {cost_labels[leaf2]}"

        # ---- fallback: generic, still names the entity ----
        field_label = remainder.replace("_", " ").replace(".", " ")
        return f"For {entity_desc}: please provide a value for {field_label}."

    # ---- bare section-level (e.g. "raw_materials", "products") ----
    if len(steps) == 1:
        label = section.replace("_", " ")
        return f"At least one {label.rstrip('s')} is needed -- what should it be?"

    # ---- top-level dict fields (e.g. "simulation.horizon", "nodes[0].facility") ----
    if section == "simulation":
        sim_labels = {
            "horizon": "How many time units should the simulation run for?",
            "warm_up": "Should any initial warm-up period be excluded from results? If so, how long?",
            "time_unit": "What time unit are you using (e.g. days, hours)?",
            "replications": "How many times should the simulation be repeated (for statistical confidence)?",
            "random_seed": "Any specific random seed to use for reproducibility? (Leave default if not.)",
        }
        leaf = steps[-1] if isinstance(steps[-1], str) else None
        if leaf in sim_labels:
            return sim_labels[leaf]

    if section == "nodes":
        return "Which entities should be registered here?"

    return f"Please provide a value for '{location}'."



# ----------------------------------------------------------------------
# Field type lookup
# ----------------------------------------------------------------------
# Derived directly from scripts/schema.py's structure. Kept local to this
# file rather than added to verification_layer1.py's FIELD() spec, per
# instruction -- container/type shape is inferred here at repair time
# from a lookup table mirroring the schema, not from a modified spec.
#
# Keyed by a NORMALIZED path: array indices stripped, section name kept
# singular-vs-plural exactly as it appears in the schema (e.g.
# "supplier.supplier_cost", not "supplier[2].supplier_cost").
#
# "str"  -> free-text string field
# "num"  -> numeric field (int or float)
# Fields not in this table are either dict/list-shaped (handled by later
# repair actions, not this one) or don't exist as scalars in the schema.

SCALAR_FIELD_TYPES = {
    "config_info.name": "name",
    "config_info.version": "str",

    "raw_materials.name": "name",

    "intermediate_materials.name": "name",

    "products.name": "name",

    "inventory.name": "name",
    "inventory.type": "str",
    "inventory.initial_inventory": "num",
    "inventory.inventory_costs.holding_cost": "num",
    "inventory.inventory_costs.shortage_cost": "num",
    "inventory.inventory_costs.review_time": "num",
    "inventory.procurement_scheme.type": "str",

    "supplier.name": "name",
    "supplier.supply_material_name": "name",
    "supplier.supplier_capacity": "num",
    "supplier.supplier_cost": "num",

    "resource.name": "name",
    "resource.capacity": "num",
    "resource.operating_cost_per_time": "num",
    "resource.batching.enabled": "bool",
    "resource.batching.batch_size": "num",
    "resource.batching.max_wait_time": "num",
    "resource.failure.enabled": "bool",

    "facility.name": "name",
    "facility.type": "str",
    "facility.operation.name": "name",
    "facility.operation.resource_required": "name",

    "customer.name": "name",
    "customer.product": "name",
    "customer.shortage_policy": "str",
    "customer.unit_selling_price": "num",

    "edges.source": "name",
    "edges.destination": "name",
    "edges.material_type": "str",
    "edges.material_name": "name",

    "simulation.time_unit": "str",
    "simulation.horizon": "num",
    "simulation.warm_up": "num",
    "simulation.replications": "num",
    "simulation.random_seed": "num",

    # Distribution object's own internal scalar fields (not the object
    # itself, which is dict-shaped -- see planned action #4).
    "*.distribution": "str",
    "*.parameters.a": "num",
    "*.parameters.b": "num",
    "*.parameters.c": "num",
    "*.parameters.d": "num",
    "*.parameters.e": "num",
}


def normalize_location(location: str) -> str:
    """
    Strips array indices from a ValidationIssue.location string so it can
    be matched against SCALAR_FIELD_TYPES.

    "supplier[0].supplier_cost"                              -> "supplier.supplier_cost"
    "inventory[2].procurement_scheme.parameters.a"            -> "inventory.procurement_scheme.parameters.a"
    "facility[0].operation.operation_cycle.parameters.a"      -> "facility.operation.operation_cycle.parameters.a"
    """
    return re.sub(r"\[\d+\]", "", location)


def _lookup_field_type(normalized_location: str) -> str | None:
    """
    Look up the expected scalar type for a normalized location. Falls
    back to matching the "*.distribution" / "*.parameters.X" wildcard
    entries for any distribution object, regardless of which field it's
    nested under (supplier_lead_time, transfer_time, operation_cycle,
    etc. all share the same internal shape).
    """
    if normalized_location in SCALAR_FIELD_TYPES:
        return SCALAR_FIELD_TYPES[normalized_location]

    # Wildcard fallback for distribution internals: anything ending in
    # ".distribution" or ".parameters.<key>" matches regardless of what
    # precedes it.
    for suffix in (".distribution", ".parameters.a", ".parameters.b",
                   ".parameters.c", ".parameters.d", ".parameters.e"):
        if normalized_location.endswith(suffix):
            wildcard_key = "*" + suffix
            if wildcard_key in SCALAR_FIELD_TYPES:
                return SCALAR_FIELD_TYPES[wildcard_key]

    return None


# ----------------------------------------------------------------------
# Enum constraints
# ----------------------------------------------------------------------
# Some scalar fields aren't just "a string" -- they're one of a fixed,
# known set of values. Keyed the same way as SCALAR_FIELD_TYPES
# (normalized path, with "*" wildcard support for fields that appear
# inside any distribution object regardless of which parent field it's
# nested under).
#
# When a field has an enum constraint, the prompt validates against this
# list instead of just checking the general "str" type -- any free-text
# string would pass the type check but could still be a nonsense value
# (e.g. "sometimes" for a distribution type), which is exactly the kind
# of error verification_layer1/2 can't catch after the fact since they
# only check presence/references, not value correctness.

DISTRIBUTION_TYPES = list(DISTRIBUTION_PARAM_COUNTS.keys())

# Fields whose distribution describes a QUANTITY (units), not a TIME
# duration/delay -- "Instant" (meaning "zero delay") is semantically
# meaningless for these ("the order quantity is instant" doesn't mean
# anything), so the Instant shortcut must never be offered here.
# procurement_scheme's own distribution is ALSO quantity-based but is
# handled entirely by its own dedicated repair action
# (repair_procurement_scheme_field), which never reaches the generic
# paths this set is checked against -- so it doesn't need to be listed
# here as well.
QUANTITY_BASED_DISTRIBUTION_FIELDS = {"customer.demand"}

ENUM_FIELD_VALUES = {
    "*.distribution": DISTRIBUTION_TYPES,
    "inventory.type": MATERIAL_TYPES,
    "edges.material_type": MATERIAL_TYPES,
    "facility.type": FACILITY_TYPES,
    "customer.shortage_policy": SHORTAGE_POLICIES,
}


def _lookup_enum_values(normalized_location: str) -> list | None:
    """Same lookup pattern as _lookup_field_type, but for enum constraints."""
    if normalized_location in ENUM_FIELD_VALUES:
        return ENUM_FIELD_VALUES[normalized_location]

    if normalized_location.endswith(".distribution"):
        return ENUM_FIELD_VALUES.get("*.distribution")

    return None


# ----------------------------------------------------------------------
# Path navigation
# ----------------------------------------------------------------------

def _parse_location_steps(location: str) -> list:
    """
    Parses a ValidationIssue.location string into a list of navigation
    steps. Each step is either a string (dict key) or an int (list index).

    "supplier[0].supplier_cost" -> ["supplier", 0, "supplier_cost"]

    NOTE: this single definition serves BOTH the natural-language
    description helpers above and the repair actions below -- when
    humanize.py was a separate file it had its own independent (lenient)
    copy to avoid a circular import; merged into this file, only one
    definition is needed. The repair actions genuinely depend on this
    raising ValueError on a malformed location (that's how a repair
    action's failure correctly propagates up to the orchestration loop's
    skip-tracking); the description helpers that also call this are
    already wrapped in their own outer exception handling, so they
    degrade gracefully to a fallback string either way.
    """
    steps = []
    for part in location.split("."):
        match = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)(\[(\d+)\])?$", part)
        if not match:
            raise ValueError(f"Could not parse location segment: '{part}' in '{location}'")
        key, _, index = match.groups()
        steps.append(key)
        if index is not None:
            steps.append(int(index))
    return steps


def _navigate_to_parent(config: dict, steps: list):
    """
    Walks all but the last step, returning (parent_container, last_step).
    Raises if an intermediate container doesn't exist -- this repair
    action only handles the LEAF field being missing, not missing
    intermediate containers (that's handled by earlier steps in the
    orchestrator, which repairs top-down).
    """
    current = config
    for step in steps[:-1]:
        if isinstance(step, int):
            current = current[step]
        else:
            current = current[step]
    return current, steps[-1]


# ----------------------------------------------------------------------
# Type-checked user prompting
# ----------------------------------------------------------------------

def _prompt_select_single(location: str, candidates: list, allow_none: bool = False, config: dict = None):
    """
    Presents a numbered menu for picking ONE value from a derived
    candidate list -- the single-value counterpart to _fill_list_select
    (which builds a whole list) and _fill_bom_by_selection (which builds
    a dict). Used for scalar reference fields whose valid values are
    fully determined by other data already in the config (e.g.
    supply_material_name must be a real raw material).

    allow_none=True adds an explicit "0) None" option that returns None
    -- used for fields that are legitimately optional (e.g.
    resource_required), where clearing the field is a valid outcome.

    config is OPTIONAL, matching _prompt_for_value's pattern -- when
    provided, the prompt header is generated via describe_location()
    instead of the raw technical location string. This matters
    particularly for edges.source/destination: describe_location's
    edge-specific handling already shows the OTHER known fields (the
    edge's source, its material_name) even when the field being asked
    about itself is missing -- e.g. "Edges -> 'ChipSource Inc. -> ?
    (silicon)' -> Destination" instead of a bare "edges[2].destination"
    with zero context about which edge is being discussed at all.
    """
    if not candidates and not allow_none:
        return None  # signal to caller: nothing to select from, fall back

    prompt_header = f"  Select a value for '{location}':"
    if config is not None:
        try:
            prompt_header = f"  {describe_location(config, location)}:"
        except Exception:
            pass

    print(prompt_header)
    if allow_none:
        print("    0) None (leave unset)")
    for i, name in enumerate(candidates, start=1):
        print(f"    {i}) {name}")

    while True:
        choice = input("  Enter number: ").strip()
        if allow_none and choice == "0":
            return None
        if not choice.isdigit() or not (1 <= int(choice) <= len(candidates)):
            print(f"    '{choice}' is not a valid selection. Try again.")
            continue
        return candidates[int(choice) - 1]


def _raw_material_candidates(config: dict, entry: dict = None) -> list:
    return sorted(_collect_names(config, "raw_materials"))


def _product_candidates(config: dict, entry: dict = None) -> list:
    return sorted(_collect_names(config, "products"))


def _resource_name_candidates(config: dict, entry: dict = None) -> list:
    return sorted(_collect_names(config, "resource"))


def _node_endpoint_candidates(config: dict, entry: dict = None) -> list:
    """Candidates for edges.source / edges.destination: any real supplier,
    facility, or customer name -- matches verification_layer2.py's
    check_edge_node_references."""
    return sorted(
        _collect_names(config, "supplier")
        | _collect_names(config, "facility")
        | _collect_names(config, "customer")
    )


def _inventory_name_candidates(config: dict, entry: dict) -> list:
    """
    CASCADING: inventory.name candidates depend on the sibling
    inventory.type already set on the same entry -- narrows to whichever
    material-category section that type points to.
    """
    inv_type = (entry or {}).get("type")
    section = INVENTORY_TYPE_TO_SECTION.get(inv_type)
    if section is None:
        return []  # type not set/recognized yet -- nothing to narrow by
    return sorted(_collect_names(config, section))


def _edge_material_name_candidates(config: dict, entry: dict) -> list:
    """
    CASCADING: edges.material_name candidates depend on the sibling
    edges.material_type already set on the same entry.
    """
    material_type = (entry or {}).get("material_type")
    section = INVENTORY_TYPE_TO_SECTION.get(material_type)
    if section is None:
        return []
    return sorted(_collect_names(config, section))


# Fields whose SINGLE value should be selected from a derived candidate
# set rather than free-typed, keyed by normalized location. Each maps to
# a function(config, entry) -> list[str], where entry is the owning dict
# (needed for cascading lookups like inventory.name / edges.material_name).
SCALAR_SELECT_CANDIDATE_FNS = {
    "supplier.supply_material_name": _raw_material_candidates,
    "customer.product": _product_candidates,
    "facility.operation.resource_required": _resource_name_candidates,
    "edges.source": _node_endpoint_candidates,
    "edges.destination": _node_endpoint_candidates,
    "inventory.name": _inventory_name_candidates,
    "edges.material_name": _edge_material_name_candidates,
}

# Fields where "no valid selection" is an acceptable outcome (clears/skips
# the field) rather than an error -- currently just resource_required,
# since it's legitimately optional.
SCALAR_SELECT_ALLOW_NONE = {"facility.operation.resource_required"}


def _prompt_for_value(location: str, expected_kind: str, enum_values: list = None, config: dict = None):
    """
    Prompts the user for a value at the given location, validating it
    against expected_kind ("str" / "num" / "bool") before accepting it.
    Re-prompts on a type mismatch rather than silently coercing.

    If enum_values is given, the value must be one of that list (checked
    on top of / instead of the general type check) -- a free-text string
    that happens to be a valid "str" isn't enough if the field is
    actually constrained to a known set (e.g. distribution type,
    procurement_scheme.type, material type).

    config is OPTIONAL and keyword-only-in-practice (appended at the end
    so every existing call site remains valid unchanged, defaulting to
    None). When provided, the prompt text is generated via
    humanize.describe_location() instead of the raw technical
    "Missing required field 'X.Y.Z'" text -- e.g. "Customer -> 'Ross
    Associates' -> Customer lead time -> Distribution:" instead of
    "Missing required field 'customer[0].customer_lead_time.distribution'".
    Falls back to the technical text if config is None (not yet wired
    at this call site) or if describe_location itself raises for any
    reason (never blocks the actual prompt from working).
    """
    prompt_prefix = f"  Missing required field '{location}'."
    if config is not None:
        try:
            prompt_prefix = f"  {describe_location(config, location)}:"
        except Exception:
            pass

    if enum_values is not None:
        print(f"{prompt_prefix} Select one:")
        for i, opt in enumerate(enum_values, start=1):
            print(f"    {i}) {opt}")
        while True:
            choice = input("  Enter number: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(enum_values):
                return enum_values[int(choice) - 1]
            print(f"    '{choice}' is not a valid selection. Try again.")

    while True:
        raw = input(f"{prompt_prefix} Enter value ({expected_kind}): ").strip()

        if expected_kind == "str":
            if raw == "":
                print("    Value cannot be blank for a string field. Try again.")
                continue
            return raw

        elif expected_kind == "name":
            if raw == "":
                print("    Value cannot be blank for a name field. Try again.")
                continue
            try:
                float(raw)
                # If float() succeeds, the value is purely numeric --
                # not acceptable for an identifier/name field (e.g. a
                # supplier or material name shouldn't be "12345").
                print(f"    '{raw}' is purely numeric -- names must contain "
                      f"non-numeric characters. Try again.")
                continue
            except ValueError:
                pass  # good -- not purely numeric
            return raw

        elif expected_kind == "num":
            try:
                if "." in raw:
                    return float(raw)
                return int(raw)
            except ValueError:
                print(f"    '{raw}' is not a valid number. Try again.")
                continue

        elif expected_kind == "bool":
            if raw.lower() in ("true", "yes", "y", "1"):
                return True
            elif raw.lower() in ("false", "no", "n", "0"):
                return False
            else:
                print(f"    '{raw}' is not a valid boolean (true/false). Try again.")
                continue

        else:
            # Unknown kind -- shouldn't happen if _lookup_field_type is
            # complete, but fail loudly rather than silently accepting
            # anything.
            raise ValueError(f"No type-checking rule for kind '{expected_kind}'")


def _prompt_for_distribution(location: str, config: dict = None, allow_instant: bool = True) -> tuple:
    """
    Prompts for a full distribution object (distribution type +
    parameters) in ONE combined step, with "Instant" offered as an
    additional FIRST menu option when allow_instant=True -- a common,
    useful shortcut for TIME-based distributions (lead times, transfer
    times, cycle times, etc.), where "instant" unambiguously means a
    fixed zero delay/duration.

    allow_instant MUST be False for QUANTITY-based distributions (e.g.
    procurement_scheme's own distribution, which describes order-
    quantity variability, or customer.demand) -- "instant" is
    semantically meaningless there ("the order quantity is instant"
    doesn't mean anything); offering it would just be a confusing,
    wrong-category shortcut. Callers are responsible for knowing which
    kind of field they're filling and passing this correctly.

    Returns (distribution_name, parameters_dict).
    """
    prompt_prefix = f"  Missing required field '{location}'."
    if config is not None:
        try:
            prompt_prefix = f"  {describe_location(config, location)}:"
        except Exception:
            pass

    if allow_instant:
        options = ["Instant (always exactly 0 -- no delay or variability)"] + list(DISTRIBUTION_TYPES)
    else:
        options = list(DISTRIBUTION_TYPES)
    print(f"{prompt_prefix} Select one:")
    for i, opt in enumerate(options, start=1):
        print(f"    {i}) {opt}")

    while True:
        choice = input("  Enter number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            break
        print(f"    '{choice}' is not a valid selection. Try again.")

    choice_num = int(choice)
    if allow_instant and choice_num == 1:
        return "constant", {"a": 0}

    dist_index = choice_num - 2 if allow_instant else choice_num - 1
    dist_value = DISTRIBUTION_TYPES[dist_index]
    param_count = DISTRIBUTION_PARAM_COUNTS.get(dist_value, 1)
    params = {}
    for i, pkey in enumerate(PARAM_KEYS):
        if i < param_count:
            params[pkey] = _prompt_for_value(f"{location}.parameters.{pkey}", "num", config=config)
    return dist_value, params


# ----------------------------------------------------------------------
# The repair action itself
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# Schema-driven recursive repair (missing containers, not just leaves)
# ----------------------------------------------------------------------
# Reuses SECTION_SPECS (imported from verification_layer1.py) as the
# single source of truth for structure -- once we know a field is
# missing, the spec tells us whether it's a distribution object, a
# dict-with-dynamic-keys (bom), a fixed-name nested object (batching,
# operation, procurement_scheme), or a plain scalar, and what its
# children are. We walk that tree and prompt for each REQUIRED field in
# turn, re-evaluating conditional requirements live as sibling values are
# entered (e.g. batch_size only gets asked if the user just answered
# enabled=True).

def find_spec_node(normalized_location: str):
    """
    Walks SECTION_SPECS (or SIMULATION_FIELDS for the simulation section)
    to find the FIELD spec dict describing the node at normalized_location.
    Raises if the path can't be resolved -- e.g. it crosses through a
    distribution/dict-values node that has no further "fields" to
    traverse (those are terminal from a structural-walk perspective; any
    issue located deeper than that is a leaf, handled by a different
    repair action).
    """
    parts = normalized_location.split(".")
    section = parts[0]

    if section in SECTION_SPECS:
        current_fields = SECTION_SPECS[section]["fields"]
    elif section == "simulation":
        current_fields = SIMULATION_FIELDS
    else:
        raise ValueError(
            f"Section '{section}' is not supported by this repair action "
            f"(e.g. 'nodes' has no fixed-field spec to walk)."
        )

    fspec = None
    remaining = parts[1:]
    for i, part in enumerate(remaining):
        if current_fields is None or part not in current_fields:
            raise ValueError(
                f"Could not resolve '{normalized_location}' against the schema "
                f"spec at segment '{part}'."
            )
        fspec = current_fields[part]
        is_last = (i == len(remaining) - 1)
        if not is_last:
            if "fields" in fspec:
                current_fields = fspec["fields"]
            elif fspec.get("is_distribution"):
                current_fields = fspec.get("extra_fields")
            else:
                current_fields = None

    return fspec


def _collect_names(config: dict, section: str) -> set:
    """Collect the set of 'name' values from a list-section."""
    names = set()
    for entry in config.get(section, []) or []:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str):
                names.add(name)
    return names


def _add_to_inventory_managed(entity: dict, material_name: str) -> None:
    """
    Shared helper for adding a real material to any entity's
    inventory_managed list. First strips out any literal "missing"
    placeholder entries (e.g. a facility whose inventory_managed was
    left as ["missing"] by the LLM) before appending the real material --
    otherwise repeated appends leave the stale placeholder behind
    forever, re-triggering a DANGLING_REFERENCE on every subsequent
    verification pass even after the real fix was applied.
    """
    current = entity.setdefault("inventory_managed", [])
    cleaned = [m for m in current if m != "missing"]
    if len(cleaned) != len(current):
        current[:] = cleaned
    if material_name not in current:
        current.append(material_name)


def _fill_bom_by_selection(config: dict, owning_entry: dict, location: str, section_name: str):
    """
    bom keys are chosen from a menu, not typed freely and not derived
    from operation.input. The candidate set depends on which section owns
    this bom -- matches verification_layer2.py's own reference rules:
      - intermediate_materials[].bom keys must come from raw_materials
      - products[].bom keys must come from raw_materials OR intermediate_materials
    The user picks a material by number, enters its quantity, and repeats
    (blank to finish) -- this guarantees every key is a real, valid
    reference by construction, rather than risking a typo'd free-typed name.
    """
    material_name = owning_entry.get("name")

    if section_name == "intermediate_materials":
        candidates = sorted(_collect_names(config, "raw_materials"))
    elif section_name == "products":
        candidates = sorted(_collect_names(config, "raw_materials") | _collect_names(config, "intermediate_materials"))
    else:
        candidates = []

    if not candidates:
        raise ValueError(
            f"No candidate materials available to build '{material_name}'s bom -- "
            f"declare raw_materials (and intermediate_materials, if this is a product) first."
        )

    bom = {}
    owning_entry["bom"] = bom

    print(f"  Select materials for '{material_name}'s bom (blank to finish, enter 0 for "
          f"a material's quantity to exclude it):")
    excluded = set()
    while True:
        remaining = [c for c in candidates if c not in bom and c not in excluded]
        if not remaining:
            print("    All candidate materials have been addressed.")
            break
        for i, name in enumerate(remaining, start=1):
            print(f"    {i}) {name}")
        choice = input("  Enter number (blank to finish): ").strip()
        if choice == "":
            break
        if not choice.isdigit() or not (1 <= int(choice) <= len(remaining)):
            print(f"    '{choice}' is not a valid selection. Try again.")
            continue
        selected = remaining[int(choice) - 1]
        qty = _prompt_for_value(f"{location}.{selected}", "num")
        if qty == 0:
            excluded.add(selected)
            print(f"    '{selected}' excluded (not part of this bom).")
        else:
            bom[selected] = qty


def _fill_list_select(owning_dict: dict, key: str, candidates: list, location: str):
    """
    Builds a list-shaped field by letting the user pick entries from a
    candidate list, by number, repeating until they choose to stop --
    same UX pattern as _fill_bom_by_selection, applied to plain lists of
    names instead of a dict of name->quantity.
    """
    if not candidates:
        raise ValueError(f"No candidates available for '{location}' -- nothing to select from.")

    selected = []
    owning_dict[key] = selected

    print(f"  Select entries for '{location}' (blank to finish):")
    while True:
        remaining = [c for c in candidates if c not in selected]
        if not remaining:
            print("    All candidates have been added.")
            break
        for i, name in enumerate(remaining, start=1):
            print(f"    {i}) {name}")
        choice = input("  Enter number (blank to finish): ").strip()
        if choice == "":
            break
        if not choice.isdigit() or not (1 <= int(choice) <= len(remaining)):
            print(f"    '{choice}' is not a valid selection. Try again.")
            continue
        selected.append(remaining[int(choice) - 1])


MATERIAL_STAGE_ORDER = {"raw_materials": 0, "intermediate_materials": 1, "products": 2}


def _material_stage(config: dict, material_name: str):
    """Returns 0 (raw), 1 (intermediate), 2 (product), or None if the
    material isn't found in any of the three category lists."""
    for section, stage in MATERIAL_STAGE_ORDER.items():
        if material_name in _collect_names(config, section):
            return stage
    return None


def _try_auto_derive_operation_list(config: dict, section_entry: dict, which: str):
    """
    Auto-derives operation.input or operation.output purely from the
    facility's own inventory_managed, using a stage ordering
    (raw=0 < intermediate=1 < product=2):
        raw + raw + intermediate  -> input=[raws], output=[intermediate]
        raw + product             -> input=[raw],  output=[product]
        intermediate + product    -> input=[intermediate], output=[product]
        raw + intermediate + product -> input=[raw, intermediate], output=[product]

    Returns None (meaning "fall back to manual selection") only when
    genuinely ambiguous: a single stage present, with nothing at any
    other stage to distinguish input from output (e.g. two raw materials
    and nothing else -- this also gets flagged separately as an
    infeasibility by verification_layer3.py's
    check_manufacturing_facility_material_stage_span).
    """
    managed = [m for m in (section_entry or {}).get("inventory_managed", []) if isinstance(m, str)]
    if not managed:
        return None

    staged: dict = {}
    for m in managed:
        stage = _material_stage(config, m)
        if stage is not None:
            staged.setdefault(stage, []).append(m)

    stages_present = sorted(staged.keys())

    if len(stages_present) < 2:
        return None  # single stage only -- nothing to split, fall back (Layer3 also flags this as infeasible)

    if len(stages_present) == 2:
        min_stage, max_stage = stages_present[0], stages_present[-1]
        return staged[min_stage] if which == "input" else staged[max_stage]

    # All three stages present: raw + intermediate -> input, product -> output.
    if which == "input":
        return staged.get(0, []) + staged.get(1, [])
    else:
        return staged.get(2, [])


def _all_material_names(config: dict, section_entry: dict = None) -> list:
    return sorted(
        _collect_names(config, "raw_materials")
        | _collect_names(config, "intermediate_materials")
        | _collect_names(config, "products")
    )


def _operation_input_candidates(config: dict, section_entry: dict = None) -> list:
    """
    operation.input candidates = (raw_material UNION intermediate_material)
    INTERSECTED with this facility's own inventory_managed -- an operation
    never consumes a finished product, AND its input should already be
    something this facility is set up to manage (matches
    verification_layer2.py's check_facility_operation_inventory_consistency
    by construction, rather than relying on that check to catch a
    mismatch after the fact).

    Falls back to the category-only set (no inventory_managed
    intersection) if inventory_managed isn't populated yet -- this can
    happen if operation is being filled before inventory_managed has a
    chance to be set.
    """
    category_set = _collect_names(config, "raw_materials") | _collect_names(config, "intermediate_materials")
    managed = set(
        m for m in ((section_entry or {}).get("inventory_managed") or []) if isinstance(m, str)
    )
    if not managed:
        return sorted(category_set)
    return sorted(category_set & managed)


def _operation_output_candidates(config: dict, section_entry: dict = None) -> list:
    """
    operation.output candidates = (intermediate_material UNION product)
    INTERSECTED with this facility's own inventory_managed -- an
    operation never produces a raw material, AND its output should
    already be something this facility is set up to manage.

    Same inventory_managed-not-yet-populated fallback as
    _operation_input_candidates.
    """
    category_set = _collect_names(config, "intermediate_materials") | _collect_names(config, "products")
    managed = set(
        m for m in ((section_entry or {}).get("inventory_managed") or []) if isinstance(m, str)
    )
    if not managed:
        return sorted(category_set)
    return sorted(category_set & managed)


# Fields whose value is a LIST selected from a derivable candidate set,
# keyed by normalized location. Each maps to a function(config, section_entry) -> list
# of valid candidate names. section_entry is the owning top-level list
# entry (e.g. the specific facility[idx] dict) -- needed by operation.input/
# output to intersect against that facility's own inventory_managed.
LIST_SELECT_CANDIDATE_FNS = {
    "facility.inventory_managed": _all_material_names,
    "facility.operation.input": _operation_input_candidates,
    "facility.operation.output": _operation_output_candidates,
}


def _fill_node(config: dict, parent: dict, key: str, fspec: dict, location: str, normalized: str, section_entry: dict = None):
    """
    Creates parent[key] and recursively fills in every REQUIRED field
    beneath it, per fspec's shape. This is the SINGLE recursive entry
    point -- special-case candidate-derived fields (bom, inventory_managed,
    operation.input/output) are checked FIRST, at every level of
    recursion, not just when this field happens to be the outermost
    missing field.

    section_entry: the owning top-level list entry (e.g. the specific
    facility[idx] dict), threaded through recursion unchanged -- needed
    so nested fields (like operation.input, two levels below the
    facility) can look up sibling data on their OWNING entry (like
    inventory_managed) rather than just global config state.

    Optional/silent fields are left out entirely -- matching Layer1's own
    rule that an absent optional field is not a problem.
    """
    # -- Special cases: candidate-derived fields, checked before anything else --
    if key == "bom" and fspec.get("is_dict_values"):
        section_name = normalized.split(".")[0]
        _fill_bom_by_selection(config, parent, location, section_name)
        return parent[key]

    if normalized in ("facility.operation.input", "facility.operation.output"):
        which = "input" if normalized.endswith(".input") else "output"
        derived = _try_auto_derive_operation_list(config, section_entry, which)
        if derived is not None:
            parent[key] = derived
            print(f"  '{location}' auto-derived from inventory_managed: {derived}")
            return derived
        # Ambiguous (single stage, or all three stages present) -- fall
        # back to manual selection, still narrowed by category + intersection.
        candidates = LIST_SELECT_CANDIDATE_FNS[normalized](config, section_entry)
        _fill_list_select(parent, key, candidates, location)
        return parent[key]

    if normalized in LIST_SELECT_CANDIDATE_FNS:
        candidates = LIST_SELECT_CANDIDATE_FNS[normalized](config, section_entry)
        _fill_list_select(parent, key, candidates, location)
        return parent[key]

    # -- Generic shape-driven handling --
    if fspec.get("is_distribution"):
        obj = {}
        parent[key] = obj

        # extra_fields (e.g. procurement_scheme.type) are asked FIRST --
        # "type" (periodic_supply/demand_driven/inventory_threshold) is
        # the more fundamental choice; what "distribution" even MEANS
        # here depends on it (e.g. for periodic_supply, distribution
        # describes order-quantity variability), so it reads more
        # naturally to settle that before asking about the distribution.
        extra_fields = fspec.get("extra_fields")
        if extra_fields:
            for fname, child_fspec in extra_fields.items():
                required_now = is_required(child_fspec["required"], obj)
                should_ask = required_now or child_fspec.get("always_ask", False)
                if should_ask:
                    _fill_node(config, obj, fname, child_fspec, f"{location}.{fname}", f"{normalized}.{fname}", section_entry)

        dist_value, params = _prompt_for_distribution(
            location, config=config,
            allow_instant=(normalized not in QUANTITY_BASED_DISTRIBUTION_FIELDS),
        )
        obj["distribution"] = dist_value
        obj["parameters"] = params

        return obj

    elif fspec.get("is_dict_values"):
        # Generic fallback for any FUTURE dict-values field that isn't
        # "bom" and has no dedicated candidate-selection action yet.
        obj = {}
        parent[key] = obj
        value_kind = fspec.get("value_kind", "num")

        print(f"  '{location}' is empty -- add entries below (blank name to finish).")
        while True:
            name = input(f"    Enter key name for '{location}' (blank to finish): ").strip()
            if name == "":
                break
            obj[name] = _prompt_for_value(f"{location}.{name}", value_kind, config=config)

        return obj

    elif "fields" in fspec:
        obj = {}
        parent[key] = obj

        for fname, child_fspec in fspec["fields"].items():
            required_now = is_required(child_fspec["required"], obj)
            should_ask = required_now or child_fspec.get("always_ask", False)
            if should_ask:
                _fill_node(config, obj, fname, child_fspec, f"{location}.{fname}", f"{normalized}.{fname}", section_entry)

        return obj

    else:
        # Plain scalar leaf -- fall back to the same lookup used by
        # repair_scalar_missing_field, for consistency.
        select_fn = SCALAR_SELECT_CANDIDATE_FNS.get(normalized)
        if select_fn is not None:
            candidates = select_fn(config, parent)
            allow_none = normalized in SCALAR_SELECT_ALLOW_NONE
            if candidates or allow_none:
                selected = _prompt_select_single(location, candidates, allow_none=allow_none, config=config)
                if selected is not None:
                    parent[key] = selected
                return selected

        kind = _lookup_field_type(normalized) or "str"
        enum_values = _lookup_enum_values(normalized)
        value = _prompt_for_value(location, kind, enum_values=enum_values, config=config)
        parent[key] = value
        return value


# ----------------------------------------------------------------------
# Building brand-new array entries (Layer3 feasibility repairs)
# ----------------------------------------------------------------------
# Layer1/Layer2 repairs above all fix something INSIDE an existing entry.
# Layer3 issues are different in kind: "raw material X has no supplier"
# means no supplier ENTRY exists at all -- the fix is to create one.
#
# _build_new_entry reuses _fill_node for this: the schema (SECTION_SPECS)
# already defines exactly what a new entry of any section needs, so
# rather than writing a bespoke prompt sequence per Layer3 check, we
# append an empty dict, pre-seed whatever fields the issue's own context
# already tells us (e.g. supply_material_name is already known -- it's
# the material that triggered the issue), and let the existing recursive
# filler ask for everything else that's actually required.

def _build_new_entry(config: dict, section_name: str, presets: dict, location_prefix: str) -> tuple:
    """
    Appends a new entry to config[section_name], pre-filling any fields
    given in `presets` (already known from the triggering issue's
    context), then asks for every other REQUIRED field via _fill_node.
    Returns (new_entry, index).
    """
    new_entry = dict(presets)
    config.setdefault(section_name, []).append(new_entry)
    idx = len(config[section_name]) - 1

    fields_spec = SECTION_SPECS[section_name]["fields"]
    for fname, child_fspec in fields_spec.items():
        if fname in new_entry:
            continue  # already preset from context -- nothing to ask
        required_now = is_required(child_fspec["required"], new_entry)
        should_ask = required_now or child_fspec.get("always_ask", False)
        if should_ask:
            child_location = f"{location_prefix}[{idx}].{fname}"
            child_normalized = f"{section_name}.{fname}"
            _fill_node(config, new_entry, fname, child_fspec, child_location, child_normalized, new_entry)

    return new_entry, idx


# ----------------------------------------------------------------------
# Cascading material deletion
# ----------------------------------------------------------------------
# Sometimes the right fix for a problematic material isn't to make it
# work (create a supplier, add it to a recipe, retarget an edge) -- it's
# to remove it from the config entirely, because it shouldn't have been
# there in the first place. This cascades the deletion through every
# place a material can be referenced, so it doesn't leave the config in
# an even more broken state (a dangling bom key, an orphaned supplier, a
# phantom edge) than before.

def _delete_material_and_associations(config: dict, material_name: str) -> list:
    """
    Removes material_name from everywhere it can appear:
      - its own entry in raw_materials / intermediate_materials / products
      - every bom that references it as an ingredient
      - its inventory[] entry, if any
      - every facility's inventory_managed / operation.input / operation.output
      - any supplier whose supply_material_name IS this material (a supplier
        only ever supplies one material, so the supplier itself is removed,
        along with its nodes[0] registration)
      - any edge referencing this material_name, or sourced from a supplier
        just removed

    Returns a list of human-readable strings describing what was removed,
    for confirmation/logging.
    """
    removed = []

    for section in ("raw_materials", "intermediate_materials", "products"):
        before = len(config.get(section, []) or [])
        config[section] = [
            e for e in config.get(section, []) or []
            if not (isinstance(e, dict) and e.get("name") == material_name)
        ]
        if len(config[section]) < before:
            removed.append(f"{section}[] entry")

    for section in ("intermediate_materials", "products"):
        for entry in config.get(section, []) or []:
            if isinstance(entry, dict) and isinstance(entry.get("bom"), dict):
                if material_name in entry["bom"]:
                    del entry["bom"][material_name]
                    removed.append(f"{section} '{entry.get('name')}'s bom entry")

    before = len(config.get("inventory", []) or [])
    config["inventory"] = [
        e for e in config.get("inventory", []) or []
        if not (isinstance(e, dict) and e.get("name") == material_name)
    ]
    if len(config["inventory"]) < before:
        removed.append("inventory[] entry")

    for f in config.get("facility", []) or []:
        if not isinstance(f, dict):
            continue
        if material_name in (f.get("inventory_managed") or []):
            f["inventory_managed"].remove(material_name)
            removed.append(f"facility '{f.get('name')}' inventory_managed entry")
        operation = f.get("operation")
        if isinstance(operation, dict):
            for key in ("input", "output"):
                if material_name in (operation.get(key) or []):
                    operation[key].remove(material_name)
                    removed.append(f"facility '{f.get('name')}' operation.{key} entry")

    suppliers_to_remove = [
        s.get("name") for s in config.get("supplier", []) or []
        if isinstance(s, dict) and s.get("supply_material_name") == material_name
    ]
    if suppliers_to_remove:
        config["supplier"] = [
            s for s in config.get("supplier", []) or [] if s.get("name") not in suppliers_to_remove
        ]
        removed.append(f"supplier(s): {', '.join(suppliers_to_remove)}")
        nodes = config.get("nodes")
        if isinstance(nodes, list) and len(nodes) > 0 and isinstance(nodes[0], dict):
            if isinstance(nodes[0].get("supplier"), list):
                nodes[0]["supplier"] = [n for n in nodes[0]["supplier"] if n not in suppliers_to_remove]

    def _edge_should_be_removed(e):
        if not isinstance(e, dict):
            return False
        if e.get("material_name") == material_name:
            return True
        if e.get("source") in suppliers_to_remove:
            return True
        return False

    before_edges = len(config.get("edges", []) or [])
    config["edges"] = [e for e in config.get("edges", []) or [] if not _edge_should_be_removed(e)]
    if len(config["edges"]) < before_edges:
        removed.append(f"{before_edges - len(config['edges'])} edge(s)")

    return removed


def _offer_delete_material_option(config: dict, material_name: str) -> bool:
    """
    Prompts for confirmation, then runs the cascading deletion if
    confirmed. Returns True if deletion happened (caller should stop and
    return immediately), False if declined (caller should proceed with
    its own normal repair flow).
    """
    confirm = input(
        f"  Type 'yes' to confirm deleting '{material_name}' and everything associated "
        f"with it (this cannot be undone), or press Enter to go back: "
    ).strip().lower()
    if confirm != "yes":
        return False

    removed = _delete_material_and_associations(config, material_name)
    print(f"  Deleted '{material_name}'. Removed: {', '.join(removed) if removed else '(nothing else referenced it)'}")
    return True


def repair_intermediate_material_not_producible(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer3 check 5: an intermediate material is never produced by any
    facility's operation.output. Three options:
      1) Add it to an existing manufacturing facility's operation.output
         (and inventory_managed, to stay consistent).
      2) Create a brand-new manufacturing facility for it.
      3) Delete the intermediate material entirely.

    For option 2, the new facility is built via the standard
    schema-driven filler (asking for name, inventory_managed selection,
    operation details, etc.) -- since the filler doesn't currently
    support pre-seeding a list-select field while still letting the user
    add MORE to it, this doesn't force-preselect the target material
    during inventory_managed's selection (the user is reminded to pick
    it). As a safety net, once the facility is built, this function
    force-appends the material into inventory_managed and
    operation.output if it isn't already there -- guaranteeing the fix
    actually works regardless of what the user picked during the
    interactive fill.
    """
    material_name = issue.context.get("referenced_name") if issue.context else None
    if not material_name:
        raise ValueError(f"Issue at '{issue.location}' has no referenced_name in its context.")

    producible = set()
    for f in config.get("facility", []) or []:
        if isinstance(f, dict):
            operation = f.get("operation")
            if isinstance(operation, dict):
                producible |= set(m for m in (operation.get("output") or []) if isinstance(m, str))
    if material_name in producible:
        raise ValueError(f"'{material_name}' is already producible -- issue may be stale.")

    manufacturing_facilities = [
        (i, f.get("name"), (f.get("operation") or {}).get("output") or [])
        for i, f in enumerate(config.get("facility", []) or [])
        if isinstance(f, dict) and f.get("type") == "manufacturing" and isinstance(f.get("name"), str)
    ]

    print(f"Intermediate material '{material_name}' is not produced by any facility. Choose a fix:")
    print("    0) Delete this intermediate material entirely")
    for i, (facility_idx, name, current_output) in enumerate(manufacturing_facilities, start=1):
        output_hint = f" (currently makes: {', '.join(current_output)})" if current_output else " (no output yet)"
        print(f"    {i}) Add it to existing facility '{name}'s operation.output{output_hint}")
    new_facility_option = len(manufacturing_facilities) + 1
    print(f"    {new_facility_option}) Create a new manufacturing facility for it")

    while True:
        choice = input("  Enter number: ").strip()
        if choice == "0":
            if _offer_delete_material_option(config, material_name):
                return config
            continue
        if not choice.isdigit() or not (1 <= int(choice) <= new_facility_option):
            print(f"    '{choice}' is not a valid selection. Try again.")
            continue
        break

    choice_num = int(choice)

    if choice_num == new_facility_option:
        print("Creating a new manufacturing facility.")
        print(f"  (Remember to include '{material_name}' when selecting inventory_managed below.)")
        new_entry, _ = _build_new_entry(config, "facility", {"type": "manufacturing"}, "facility")

        _add_to_inventory_managed(new_entry, material_name)
        operation = new_entry.setdefault("operation", {})
        if material_name not in (operation.get("output") or []):
            operation.setdefault("output", []).append(material_name)
        print(f"  Ensured '{material_name}' is in the new facility's inventory_managed and operation.output.")
    else:
        facility_idx, facility_name, _ = manufacturing_facilities[choice_num - 1]
        facility = config["facility"][facility_idx]
        _add_to_inventory_managed(facility, material_name)
        operation = facility.setdefault("operation", {})
        if material_name not in (operation.get("output") or []):
            operation.setdefault("output", []).append(material_name)
        print(f"  Added '{material_name}' to '{facility_name}'s inventory_managed and operation.output.")

    return config


def repair_product_end_to_end_path(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer3 check 11: a product's producing facility is disconnected --
    either no supplier can reach it (upstream), or it can't reach any
    ordering customer (downstream). Fires as TWO variants sharing
    identical location/defect_type/context (only detail text differs),
    same pattern as check 9 -- disambiguated here by re-checking live
    graph reachability directly, not by parsing detail text.

    Reuses check 9's exact _fix_facility_missing_inbound/
    _fix_facility_missing_outbound helpers on the producing facility --
    "connect this facility to a supplier/consumer" is the same mechanism
    whether triggered by "zero edges at all" (check 9) or "edges exist
    locally but don't trace back/forward to the full chain" (check 11).

    SCOPE NOTE: if multiple facilities produce this product, only ONE
    needs to end up connected for the check to pass (matches check 11's
    own "at least one" logic) -- this operates on a single chosen
    facility, not all of them.
    """
    product_name = issue.context.get("referenced_name") if issue.context else None
    if not product_name:
        raise ValueError(f"Issue at '{issue.location}' has no referenced_name in its context.")

    producing_facilities = set()
    for f in config.get("facility", []) or []:
        if isinstance(f, dict):
            operation = f.get("operation") or {}
            if product_name in (operation.get("output") or []) and isinstance(f.get("name"), str):
                producing_facilities.add(f["name"])

    ordering_customers = set(
        c.get("name") for c in config.get("customer", []) or []
        if isinstance(c, dict) and c.get("product") == product_name and isinstance(c.get("name"), str)
    )

    if not producing_facilities or not ordering_customers:
        raise ValueError(
            f"'{product_name}' has no producing facility or no ordering customer yet -- "
            f"this issue must be from check_product_is_producible or "
            f"check_product_has_customer instead. Not acting."
        )

    adjacency = _build_adjacency(config)
    supplier_names = _collect_names(config, "supplier")

    upstream_ok = any(
        _bfs_reachable_from(s, adjacency) & producing_facilities for s in supplier_names
    )
    downstream_ok = any(
        _bfs_reachable_from(f, adjacency) & ordering_customers for f in producing_facilities
    )

    if upstream_ok and downstream_ok:
        raise ValueError(f"'{product_name}' is already fully connected end-to-end -- issue may be stale.")

    facility_name = sorted(producing_facilities)[0]
    facility = next(
        f for f in config["facility"]
        if f.get("name") == facility_name and product_name in ((f.get("operation") or {}).get("output") or [])
    )

    made_progress = False
    errors = []

    if not upstream_ok:
        print(f"Product '{product_name}': producing facility '{facility_name}' is not "
              f"reachable from any supplier.")
        try:
            _fix_facility_missing_inbound(config, facility)
            made_progress = True
        except ValueError as e:
            errors.append(str(e))

    if not downstream_ok:
        print(f"Product '{product_name}': producing facility '{facility_name}' cannot "
              f"reach any customer ordering it.")
        try:
            _fix_facility_missing_outbound(config, facility)
            made_progress = True
        except ValueError as e:
            errors.append(str(e))

    if not made_progress:
        raise ValueError("; ".join(errors))

    return config


def repair_product_not_producible(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer3 check 6: a product is never produced by any facility's
    operation.output. Same three options as check 5's intermediate
    material version: extend an existing manufacturing facility, create
    a new one, or delete the product.

    SAFETY NOTE: shares "products[idx]" + INCONSISTENT_CROSS_FIELD with
    check 4 (product_has_customer) and check 11 (end_to_end_path, not
    yet built). Re-checks the live "is this genuinely not producible"
    condition before acting.
    """
    product_name = issue.context.get("referenced_name") if issue.context else None
    if not product_name:
        raise ValueError(f"Issue at '{issue.location}' has no referenced_name in its context.")

    producible = set()
    for f in config.get("facility", []) or []:
        if isinstance(f, dict):
            operation = f.get("operation")
            if isinstance(operation, dict):
                producible |= set(m for m in (operation.get("output") or []) if isinstance(m, str))
    if product_name in producible:
        raise ValueError(
            f"'{product_name}' is already producible -- this issue must be from a "
            f"different check (e.g. product_has_customer or end_to_end_path) sharing "
            f"the same location shape. Not acting."
        )

    manufacturing_facilities = [
        (i, f.get("name"), (f.get("operation") or {}).get("output") or [])
        for i, f in enumerate(config.get("facility", []) or [])
        if isinstance(f, dict) and f.get("type") == "manufacturing" and isinstance(f.get("name"), str)
    ]

    print(f"Product '{product_name}' is not produced by any facility. Choose a fix:")
    print("    0) Delete this product entirely")
    for i, (facility_idx, name, current_output) in enumerate(manufacturing_facilities, start=1):
        output_hint = f" (currently makes: {', '.join(current_output)})" if current_output else " (no output yet)"
        print(f"    {i}) Add it to existing facility '{name}'s operation.output{output_hint}")
    new_facility_option = len(manufacturing_facilities) + 1
    print(f"    {new_facility_option}) Create a new manufacturing facility for it")

    while True:
        choice = input("  Enter number: ").strip()
        if choice == "0":
            if _offer_delete_material_option(config, product_name):
                return config
            continue
        if not choice.isdigit() or not (1 <= int(choice) <= new_facility_option):
            print(f"    '{choice}' is not a valid selection. Try again.")
            continue
        break

    choice_num = int(choice)

    if choice_num == new_facility_option:
        print("Creating a new manufacturing facility.")
        print(f"  (Remember to include '{product_name}' when selecting inventory_managed below.)")
        new_entry, _ = _build_new_entry(config, "facility", {"type": "manufacturing"}, "facility")

        _add_to_inventory_managed(new_entry, product_name)
        operation = new_entry.setdefault("operation", {})
        if product_name not in (operation.get("output") or []):
            operation.setdefault("output", []).append(product_name)
        print(f"  Ensured '{product_name}' is in the new facility's inventory_managed and operation.output.")
    else:
        facility_idx, facility_name, _ = manufacturing_facilities[choice_num - 1]
        facility = config["facility"][facility_idx]
        _add_to_inventory_managed(facility, product_name)
        operation = facility.setdefault("operation", {})
        if product_name not in (operation.get("output") or []):
            operation.setdefault("output", []).append(product_name)
        print(f"  Added '{product_name}' to '{facility_name}'s inventory_managed and operation.output.")

    return config


def repair_product_missing_customer(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer3 check 4: a product has no customer ordering it. Creates a new
    customer entry with `product` pre-seeded to the known product name --
    everything else (arrival_time, demand, customer_lead_time,
    shortage_policy, unit_selling_price, customer_payment_lead_time) is
    asked via the standard schema-driven filler.

    SAFETY NOTE: location shape ("products[idx]", INCONSISTENT_CROSS_FIELD)
    is shared by THREE checks: check 4 (no customer, this one), check 6
    (not producible by any facility), and check 11 (end-to-end path
    disconnected) -- none of the latter two are built yet, but this guard
    is here now so it stays collision-safe once they are. Re-verifies the
    actual "does this product have zero customers right now" condition
    before acting.
    """
    product_name = issue.context.get("referenced_name") if issue.context else None
    if not product_name:
        raise ValueError(f"Issue at '{issue.location}' has no referenced_name in its context.")

    ordered_products = {
        c.get("product") for c in config.get("customer", []) or []
        if isinstance(c, dict)
    }
    if product_name in ordered_products:
        raise ValueError(
            f"'{product_name}' already has a customer -- this issue must be from a "
            f"different check (e.g. product_is_producible or end_to_end_path) sharing "
            f"the same location shape. Not acting."
        )

    print(f"Product '{product_name}' has no customer.")
    choice = input("  Type 'delete' to remove this product entirely, or press Enter to "
                    "create a customer for it: ").strip().lower()
    if choice == "delete":
        if _offer_delete_material_option(config, product_name):
            return config
        print("  Cancelled -- creating a customer instead.")

    _build_new_entry(config, "customer", {"product": product_name}, "customer")

    return config


def repair_at_least_one_raw_material(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer3 check 2: raw_materials is empty (or absent). Creates one new
    entry via the standard schema-driven filler -- raw_materials only
    needs a name, so this is a single prompt.

    NOTE: shares the exact location shape ("raw_materials", no index,
    MISSING_REQUIRED_VALUE) with Layer1's own "section entirely absent"
    check -- both fire when the key is missing outright, only check 2
    additionally fires when the key exists as an empty list. dispatch_repair
    routes bare "raw_materials"/"products" locations here directly, ahead
    of the generic scalar/find_spec_node path (which isn't designed for
    bare section-level creation and would raise).
    """
    print("No raw materials declared -- creating one.")
    _build_new_entry(config, "raw_materials", {}, "raw_materials")
    return config


def repair_at_least_one_product(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer3 check 3: products is empty (or absent). Creates one new entry
    via the standard schema-driven filler -- this naturally triggers the
    bom-selection flow too, since bom is required for products. If no
    raw_materials/intermediate_materials exist yet to choose from, the
    resulting bom will be left empty and Layer1's min_items=1 check will
    catch that on the next verification pass (graceful partial progress,
    not a crash) -- create raw materials first if this happens.
    """
    print("No products declared -- creating one.")
    _build_new_entry(config, "products", {}, "products")
    return config


def repair_material_missing_inventory_entry(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer2 check 20: a declared material has no corresponding inventory[]
    entry. Creates one, with 'name' and 'type' pre-seeded from the
    issue's own context (both already known -- never asked, never picked
    wrong) -- everything else (initial_inventory, and conditionally
    procurement_scheme/procurement_arrival if type=="raw_material") is
    asked via the standard schema-driven filler.

    SAFETY NOTE: fires at "raw_materials[idx]" / "intermediate_materials[idx]"
    / "products[idx]" -- shapes shared with several Layer3 checks. Verifies
    the material genuinely still lacks an inventory entry before acting.
    """
    name = issue.context.get("referenced_name") if issue.context else None
    expected_type = issue.context.get("expected_type") if issue.context else None
    if not name or not expected_type:
        raise ValueError(f"Issue at '{issue.location}' has no referenced_name/expected_type in its context.")

    if name in _collect_names(config, "inventory"):
        raise ValueError(
            f"'{name}' already has an inventory entry -- this issue must be from a "
            f"different check sharing the same location shape. Not acting."
        )

    print(f"'{name}' has no inventory entry -- creating one.")
    _build_new_entry(config, "inventory", {"name": name, "type": expected_type}, "inventory")

    return config


def repair_raw_material_missing_supplier(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer3 check 1: a raw material has no supplier. The material name is
    already known from the issue's own context -- pre-seed
    supply_material_name with it (never ask, never let it be picked
    wrong) and build the rest of a new supplier entry from the schema.

    SAFETY NOTE: check_raw_material_has_supplier (check 1) and
    check_raw_material_is_consumed (check 7) both emit the IDENTICAL
    location shape ("raw_materials[idx]") and defect_type
    (INCONSISTENT_CROSS_FIELD) -- the issue alone can't distinguish which
    check produced it. Rather than trust dispatch routing blindly, this
    re-checks the ACTUAL live condition (does this material really have
    zero suppliers right now?) before acting -- if it already has one,
    this issue must actually be from check 7, and raises rather than
    creating a redundant/wrong supplier.
    """
    material_name = issue.context.get("referenced_name") if issue.context else None
    if not material_name:
        raise ValueError(f"Issue at '{issue.location}' has no referenced_name in its context.")

    existing_suppliers = {
        s.get("supply_material_name") for s in config.get("supplier", []) or []
        if isinstance(s, dict)
    }
    if material_name in existing_suppliers:
        raise ValueError(
            f"'{material_name}' already has a supplier -- this issue must be from a "
            f"different check (e.g. check_raw_material_is_consumed) sharing the same "
            f"location shape, not check_raw_material_has_supplier. Not creating a "
            f"redundant supplier."
        )

    print(f"Raw material '{material_name}' has no supplier.")
    print(f"  1) Create a new supplier for it")
    print(f"  2) Delete this material entirely instead")
    choice = input("  Enter number: ").strip()
    if choice == "2":
        if _offer_delete_material_option(config, material_name):
            return config
        print("  Cancelled -- creating a supplier instead.")

    _build_new_entry(config, "supplier", {"supply_material_name": material_name}, "supplier")

    return config


def repair_raw_material_not_consumed(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer3 check 7: a raw material is never consumed (not in any bom, not
    in any facility's operation.input). Fix: add it as an ingredient to
    an existing intermediate_material or product's bom, chosen by menu,
    with a user-entered quantity.

    SAFETY NOTE: shares the identical location shape ("raw_materials[idx]")
    and defect_type with check 1 (raw_material_has_supplier) -- re-checks
    the actual live "is it consumed anywhere" condition before acting,
    rather than trusting which check produced the issue.
    """
    material_name = issue.context.get("referenced_name") if issue.context else None
    if not material_name:
        raise ValueError(f"Issue at '{issue.location}' has no referenced_name in its context.")

    consumed = set()
    for section in ("intermediate_materials", "products"):
        for entry in config.get(section, []) or []:
            if isinstance(entry, dict) and isinstance(entry.get("bom"), dict):
                consumed |= set(entry["bom"].keys())
    for facility in config.get("facility", []) or []:
        if isinstance(facility, dict):
            operation = facility.get("operation")
            if isinstance(operation, dict):
                consumed |= set(m for m in (operation.get("input") or []) if isinstance(m, str))

    if material_name in consumed:
        raise ValueError(
            f"'{material_name}' is already consumed somewhere -- this issue must be from "
            f"a different check (e.g. check_raw_material_has_supplier) sharing the same "
            f"location shape. Not acting."
        )

    candidates = []
    for section in ("intermediate_materials", "products"):
        for idx, entry in enumerate(config.get(section, []) or []):
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                candidates.append((section, idx, entry["name"]))

    if not candidates:
        raise ValueError(
            "No intermediate_materials or products exist yet for this raw material to "
            "be an ingredient of."
        )

    print(f"Raw material '{material_name}' is never consumed -- choose which recipe uses it:")
    print(f"    0) Delete this material entirely instead")
    for i, (section, idx, name) in enumerate(candidates, start=1):
        print(f"    {i}) {name} ({section})")
    while True:
        choice = input("  Enter number: ").strip()
        if choice == "0":
            if _offer_delete_material_option(config, material_name):
                return config
            continue  # cancelled -- redisplay the menu
        if not choice.isdigit() or not (1 <= int(choice) <= len(candidates)):
            print(f"    '{choice}' is not a valid selection. Try again.")
            continue
        break

    section, idx, name = candidates[int(choice) - 1]
    qty = _prompt_for_value(f"{section}[{idx}].bom.{material_name}", "num")
    config[section][idx].setdefault("bom", {})[material_name] = qty
    print(f"  Added '{material_name}' (qty {qty}) to {name}'s bom.")

    # Keep the recipe consistent with the physical operation: find the
    # facility that PRODUCES `name` (operation.output includes it) and
    # make sure it also receives/manages the new ingredient. Without
    # this, the bom says the ingredient is needed but no facility's
    # operation.input reflects that -- exactly the gap that let a
    # supplier's outbound edge get created to the WRONG facility in an
    # earlier real run (nothing referenced the material yet, so the
    # destination had to be guessed).
    producing_facility = None
    for f in config.get("facility", []) or []:
        if not isinstance(f, dict):
            continue
        operation = f.get("operation")
        if isinstance(operation, dict) and name in (operation.get("output") or []):
            producing_facility = f
            break

    if producing_facility is not None:
        operation = producing_facility["operation"]
        if material_name not in (operation.get("input") or []):
            operation.setdefault("input", []).append(material_name)
        _add_to_inventory_managed(producing_facility, material_name)
        print(f"  Also added '{material_name}' to '{producing_facility.get('name')}'s "
              f"operation.input and inventory_managed, to keep the recipe and the "
              f"physical operation consistent.")
    else:
        print(f"  NOTE: no facility currently produces '{name}' -- could not sync "
              f"'{material_name}' into any operation.input. This will need attention "
              f"once a producing facility exists.")

    return config


def repair_edge_phantom_delivery(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer2 check 19: an edge delivers a CATEGORY-VALID material to a
    facility that doesn't manage or consume it at all. Distinct from
    check 9 (material_name doesn't even belong to the right category) --
    here the material is a legitimate raw/intermediate/product, it's just
    not one this particular destination facility uses.

    Offers two fixes:
      1) Retarget the edge to a facility that already uses this material
         (if one exists).
      2) Keep the edge as-is, and sync the CURRENT destination facility's
         inventory_managed/operation.input to include the material.
    """
    steps = _parse_location_steps(issue.location)
    parent, _ = _navigate_to_parent(config, steps)  # parent = the edges[idx] dict itself
    material_name = parent.get("material_name")
    current_destination = parent.get("destination")

    if not material_name or not current_destination:
        raise ValueError(f"Edge at '{issue.location}' is missing material_name/destination.")

    matching_facilities = [
        (i, f) for i, f in enumerate(config.get("facility", []) or [])
        if isinstance(f, dict) and f.get("name") == current_destination
    ]
    if not matching_facilities:
        raise ValueError(
            f"Destination '{current_destination}' is not a facility -- this issue must be "
            f"from a different check."
        )

    if len(matching_facilities) == 1:
        current_facility_idx, current_facility = matching_facilities[0]
    else:
        # Multiple facilities share this name (allowed when their outputs
        # are disjoint) -- an edge's destination is just a bare name
        # string with no array index attached, so which one this edge
        # actually means is genuinely ambiguous from the edge alone.
        # Material context doesn't resolve it here either, since the
        # whole point of this repair is "the facility doesn't reference
        # this material yet" -- so ask directly instead of guessing.
        print(f"  Multiple facilities are named '{current_destination}'. Which one does this edge mean?")
        for i, (idx, f) in enumerate(matching_facilities, start=1):
            current_output = (f.get("operation") or {}).get("output") or []
            hint = f"makes: {', '.join(current_output)}" if current_output else "no output yet"
            print(f"    {i}) entry {idx} ({hint})")
        while True:
            choice = input("  Enter number: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(matching_facilities):
                break
            print(f"    '{choice}' is not a valid selection. Try again.")
        current_facility_idx, current_facility = matching_facilities[int(choice) - 1]

    managed = current_facility.get("inventory_managed") or []
    inputs = (current_facility.get("operation") or {}).get("input") or []
    if material_name in managed or material_name in inputs:
        raise ValueError(f"'{current_destination}' already references '{material_name}' -- issue may be stale.")

    edge_source = parent.get("source")

    alt_facilities = []
    for f in config.get("facility", []) or []:
        if not isinstance(f, dict):
            continue
        name = f.get("name")
        if name == current_destination or not isinstance(name, str):
            continue
        f_managed = f.get("inventory_managed") or []
        f_inputs = (f.get("operation") or {}).get("input") or []
        if material_name not in f_managed and material_name not in f_inputs:
            continue

        # Skip if retargeting here would create an exact duplicate edge
        # (same source, same destination, same material already exists
        # as a SEPARATE edge). This is the common, entirely legitimate
        # case of a multi-destination topology -- e.g. one factory
        # shipping the same product to several regional warehouses, each
        # via its own edge. In that case the current edge's destination
        # isn't wrong at all; retargeting would just silently duplicate
        # an edge that already exists elsewhere, and the correct fix is
        # always "sync" (this facility legitimately needs its own copy
        # of this delivery, its data was just incomplete).
        already_has_this_edge = any(
            isinstance(e, dict) and e.get("source") == edge_source
            and e.get("destination") == name and e.get("material_name") == material_name
            for e in config.get("edges", []) or []
        )
        if already_has_this_edge:
            continue

        alt_facilities.append(name)

    print(f"Edge delivers '{material_name}' to '{current_destination}', which doesn't use it. Choose a fix:")
    print(f"    0) Delete this material entirely instead")
    options = [("retarget", name) for name in alt_facilities]
    options.append(("sync", current_destination))

    for i, (kind, name) in enumerate(options, start=1):
        if kind == "retarget":
            print(f"    {i}) Retarget this edge's destination to '{name}' (already uses '{material_name}')")
        else:
            print(f"    {i}) Keep destination '{name}' (recommended if multiple destinations "
                  f"legitimately need this material -- e.g. separate regional warehouses), "
                  f"add '{material_name}' to its inventory_managed/operation.input")

    while True:
        choice = input("  Enter number: ").strip()
        if choice == "0":
            if _offer_delete_material_option(config, material_name):
                return config
            continue  # cancelled -- redisplay the menu
        if not choice.isdigit() or not (1 <= int(choice) <= len(options)):
            print(f"    '{choice}' is not a valid selection. Try again.")
            continue
        break

    kind, name = options[int(choice) - 1]
    if kind == "retarget":
        parent["destination"] = name
        print(f"  Retargeted edge destination to '{name}'.")
    else:
        operation = current_facility.setdefault("operation", {})
        if material_name not in (operation.get("input") or []):
            operation.setdefault("input", []).append(material_name)
        _add_to_inventory_managed(current_facility, material_name)
        print(f"  Added '{material_name}' to '{name}'s operation.input and inventory_managed.")

    return config


SECTION_TO_MATERIAL_TYPE = {v: k for k, v in INVENTORY_TYPE_TO_SECTION.items()}


def _material_type_for(config: dict, material_name: str):
    """Reverse lookup: given a material name, which category (raw_material/
    intermediate_material/product) does it belong to? None if not found."""
    for section, mtype in SECTION_TO_MATERIAL_TYPE.items():
        if material_name in _collect_names(config, section):
            return mtype
    return None


def _create_edge(config: dict, source: str, destination: str, material_name: str, material_type: str, location_hint: str):
    """
    Shared helper: appends a new edge and fills its transfer_time.

    HARD RULE: any edge whose destination is a customer always gets
    transfer_time forced to constant/a=0, never asked interactively --
    delivery lead time to a customer is already modeled by that
    customer's own customer_lead_time field; giving the edge a real
    transfer_time on top of that would double-count the delay. This is
    enforced here (not per call site) so every repair action that might
    create a customer-destined edge (check 9's outbound fix, check 10's
    direct-warehouse path) gets it automatically, with nothing to remember.
    """
    new_edge = {
        "source": source,
        "destination": destination,
        "material_type": material_type,
        "material_name": material_name,
    }
    config.setdefault("edges", []).append(new_edge)
    idx = len(config["edges"]) - 1

    is_customer_destination = destination in _collect_names(config, "customer")
    if is_customer_destination:
        new_edge["transfer_time"] = {"distribution": "constant", "parameters": {"a": 0}}
        print(f"  Created edge: {source} -> {destination} (material: {material_name}, "
              f"transfer_time forced to 0 -- delivery lead time is covered by "
              f"customer_lead_time, not edge transfer_time).")
    else:
        transfer_fspec = SECTION_SPECS["edges"]["fields"]["transfer_time"]
        _fill_node(config, new_edge, "transfer_time", transfer_fspec,
                   f"edges[{idx}].transfer_time", "edges.transfer_time", new_edge)
        print(f"  Created edge: {source} -> {destination} (material: {material_name}).")


def _fix_facility_missing_inbound(config: dict, facility: dict):
    """
    Finds candidate (source, material) pairs that could feed this
    facility: materials in its own inventory_managed that it does NOT
    produce itself (operation.output) -- things it needs to RECEIVE --
    matched against suppliers that supply them or other facilities that
    produce them.
    """
    facility_name = facility.get("name")
    managed = facility.get("inventory_managed") or []
    own_output = set((facility.get("operation") or {}).get("output") or [])
    receivable = [m for m in managed if m not in own_output]

    if not receivable:
        raise ValueError(
            f"'{facility_name}' has no inventory_managed items it doesn't already produce "
            f"itself -- cannot determine what it should receive (check inventory_managed "
            f"is complete)."
        )

    options = []
    for m in receivable:
        for s in config.get("supplier", []) or []:
            if isinstance(s, dict) and s.get("supply_material_name") == m and isinstance(s.get("name"), str):
                options.append((s["name"], m))
        for f in config.get("facility", []) or []:
            if isinstance(f, dict) and f.get("name") != facility_name:
                op = f.get("operation") or {}
                if m in (op.get("output") or []) and isinstance(f.get("name"), str):
                    options.append((f["name"], m))

    if not options:
        raise ValueError(
            f"No supplier or facility currently produces any of {receivable} for "
            f"'{facility_name}' to receive -- create a producer for one of these "
            f"materials first."
        )

    print(f"'{facility_name}' has no inbound edge. Choose a source:")
    for i, (src, mat) in enumerate(options, start=1):
        print(f"    {i}) {src} -> {facility_name}  (material: {mat})")
    while True:
        choice = input("  Enter number: ").strip()
        if not choice.isdigit() or not (1 <= int(choice) <= len(options)):
            print(f"    '{choice}' is not a valid selection. Try again.")
            continue
        break

    source, material = options[int(choice) - 1]
    material_type = _material_type_for(config, material)
    _create_edge(config, source, facility_name, material, material_type, "edges")


def _fix_facility_missing_outbound(config: dict, facility: dict):
    """
    Finds candidate (destination, material) pairs for this facility's
    outbound edge: materials it produces (operation.output) or, if it has
    no operation (e.g. a warehouse), everything it manages -- matched
    against other facilities that consume/manage them, or customers that
    order them (if the material is a finished product).
    """
    facility_name = facility.get("name")
    own_output = set((facility.get("operation") or {}).get("output") or [])
    sendable = list(own_output) if own_output else list(facility.get("inventory_managed") or [])

    if not sendable:
        raise ValueError(
            f"'{facility_name}' has nothing in operation.output or inventory_managed "
            f"to send anywhere."
        )

    options = []
    for m in sendable:
        for f in config.get("facility", []) or []:
            if isinstance(f, dict) and f.get("name") != facility_name:
                f_managed = f.get("inventory_managed") or []
                f_inputs = (f.get("operation") or {}).get("input") or []
                if (m in f_managed or m in f_inputs) and isinstance(f.get("name"), str):
                    options.append((f["name"], m))
        for c in config.get("customer", []) or []:
            if isinstance(c, dict) and c.get("product") == m and isinstance(c.get("name"), str):
                options.append((c["name"], m))

    if not options:
        raise ValueError(
            f"No facility or customer currently uses/orders any of {sendable} from "
            f"'{facility_name}' -- make sure a downstream consumer exists first."
        )

    print(f"'{facility_name}' has no outbound edge. Choose a destination:")
    for i, (dst, mat) in enumerate(options, start=1):
        print(f"    {i}) {facility_name} -> {dst}  (material: {mat})")
    while True:
        choice = input("  Enter number: ").strip()
        if not choice.isdigit() or not (1 <= int(choice) <= len(options)):
            print(f"    '{choice}' is not a valid selection. Try again.")
            continue
        break

    destination, material = options[int(choice) - 1]
    material_type = _material_type_for(config, material)
    _create_edge(config, facility_name, destination, material, material_type, "edges")


def repair_customer_missing_inbound_edge(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer3 check 10: a customer has no inbound edge. Two kinds of source,
    per instruction:
      1) An existing warehouse-type facility that already manages the
         ordered product -- a direct edge is created, asking for
         transfer_time normally (a genuine one-hop delivery).
      2) A manufacturing facility that produces the product directly --
         since manufacturing facilities don't ship straight to customers
         in this model, a new HYPOTHETICAL warehouse is created between
         them: manufacturing -> new warehouse -> customer, with ZERO
         transfer_time on both new edges (an idealized instant pass-through,
         not a real logistics delay).

    The new warehouse (if created) is NOT manually registered in
    nodes[0].facility here -- consistent with how new suppliers/facilities
    are handled elsewhere, that's left to the existing self-healing
    nodes-registration-gap check/repair on the orchestrator's next pass.
    """
    customer_name = issue.context.get("referenced_name") if issue.context else None
    entry_index = issue.context.get("entry_index") if issue.context else None
    if not customer_name or entry_index is None:
        raise ValueError(f"Issue at '{issue.location}' has no referenced_name/entry_index in its context.")

    customers = config.get("customer", []) or []
    if entry_index >= len(customers) or not isinstance(customers[entry_index], dict):
        raise ValueError(f"'{issue.location}' does not resolve to a real customer entry.")
    customer_entry = customers[entry_index]
    if customer_entry.get("name") != customer_name:
        raise ValueError(f"Customer at index {entry_index} no longer matches '{customer_name}' -- issue may be stale.")

    product_name = customer_entry.get("product")
    if not product_name:
        raise ValueError(f"Customer '{customer_name}' has no product set -- cannot determine what to deliver.")

    has_inbound = any(
        isinstance(e, dict) and e.get("destination") == customer_name and e.get("material_name") == product_name
        for e in config.get("edges", []) or []
    )
    if has_inbound:
        raise ValueError(f"'{customer_name}' (ordering '{product_name}') already has an inbound edge -- issue may be stale.")

    warehouse_options = []
    manufacturing_options = []
    for f in config.get("facility", []) or []:
        if not isinstance(f, dict) or not isinstance(f.get("name"), str):
            continue
        if f.get("type") == "warehouse" and product_name in (f.get("inventory_managed") or []):
            warehouse_options.append(f["name"])
        elif f.get("type") == "manufacturing" and product_name in ((f.get("operation") or {}).get("output") or []):
            manufacturing_options.append(f["name"])

    if not warehouse_options and not manufacturing_options:
        raise ValueError(
            f"No warehouse or manufacturing facility currently manages/produces "
            f"'{product_name}' -- cannot determine a delivery source yet."
        )

    print(f"Customer '{customer_name}' (orders '{product_name}') has no inbound edge. Choose a delivery source:")
    options = [("warehouse", name) for name in warehouse_options] + [("manufacturing", name) for name in manufacturing_options]
    for i, (kind, name) in enumerate(options, start=1):
        label = "Warehouse" if kind == "warehouse" else "Manufacturing facility (via new hypothetical warehouse)"
        print(f"    {i}) {name} ({label})")

    while True:
        choice = input("  Enter number: ").strip()
        if not choice.isdigit() or not (1 <= int(choice) <= len(options)):
            print(f"    '{choice}' is not a valid selection. Try again.")
            continue
        break

    kind, name = options[int(choice) - 1]
    material_type = _material_type_for(config, product_name)

    if kind == "warehouse":
        _create_edge(config, name, customer_name, product_name, material_type, "edges")
    else:
        default_name = f"{name} Warehouse"
        entered = input(f"  Enter a name for the new hypothetical warehouse (default: '{default_name}'): ").strip()
        warehouse_name = entered if entered else default_name

        new_warehouse = {
            "name": warehouse_name,
            "type": "warehouse",
            "inventory_managed": [product_name],
        }
        config.setdefault("facility", []).append(new_warehouse)

        config.setdefault("edges", []).append({
            "source": name, "destination": warehouse_name,
            "material_type": material_type, "material_name": product_name,
            "transfer_time": {"distribution": "constant", "parameters": {"a": 0}},
        })
        config["edges"].append({
            "source": warehouse_name, "destination": customer_name,
            "material_type": material_type, "material_name": product_name,
            "transfer_time": {"distribution": "constant", "parameters": {"a": 0}},
        })
        print(f"  Created hypothetical warehouse '{warehouse_name}' with zero-transfer-time "
              f"edges: {name} -> {warehouse_name} -> {customer_name}.")

    return config


def _delete_facility_and_associations(config: dict, facility_name: str) -> list:
    """
    Removes a facility entirely: the facility entry itself, its nodes[0]
    registration, and every edge touching it (as source or destination).
    Same cascading philosophy as _delete_material_and_associations.
    """
    removed = []

    before = len(config.get("facility", []) or [])
    config["facility"] = [
        f for f in config.get("facility", []) or []
        if not (isinstance(f, dict) and f.get("name") == facility_name)
    ]
    if len(config["facility"]) < before:
        removed.append("facility[] entry")

    nodes = config.get("nodes")
    if isinstance(nodes, list) and len(nodes) > 0 and isinstance(nodes[0], dict):
        if isinstance(nodes[0].get("facility"), list) and facility_name in nodes[0]["facility"]:
            nodes[0]["facility"] = [n for n in nodes[0]["facility"] if n != facility_name]
            removed.append("nodes[0].facility registration")

    before_edges = len(config.get("edges", []) or [])
    config["edges"] = [
        e for e in config.get("edges", []) or []
        if not (isinstance(e, dict) and (e.get("source") == facility_name or e.get("destination") == facility_name))
    ]
    if len(config["edges"]) < before_edges:
        removed.append(f"{before_edges - len(config['edges'])} edge(s)")

    return removed


def _prompt_unique_name(location: str, existing_names: set) -> str:
    """Shared helper: prompts for a name (blank/numeric rejected via the
    'name' kind) that isn't already in existing_names, re-prompting until
    a genuinely new name is given."""
    while True:
        candidate = _prompt_for_value(location, "name")
        if candidate in existing_names:
            print(f"    '{candidate}' is already used. Try again.")
            continue
        return candidate


def repair_material_category_collision(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer2 check 13: the same name is declared in two different material
    category lists (e.g. both raw_materials AND products). Offers a
    choice of which entry to rename.

    Only renames the entity's own 'name' field in its own list -- does
    NOT cascade-update every place that might reference the old name
    (bom keys, edges, customer.product, etc.), since some references are
    genuinely ambiguous (a bom key doesn't carry category info, so we
    can't always tell which of the two same-named entries it meant).
    Anything left pointing at the old name will surface as an ordinary
    DANGLING_REFERENCE on the next verification pass, which already has
    well-tested repair actions.
    """
    name = issue.context.get("referenced_name") if issue.context else None
    sections = issue.context.get("sections") if issue.context else None
    if not name or not sections or len(sections) != 2:
        raise ValueError(f"Issue at '{issue.location}' has no referenced_name/sections in its context.")

    names_by_section = {s: _collect_names(config, s) for s in sections}
    if name not in names_by_section[sections[0]] or name not in names_by_section[sections[1]]:
        raise ValueError(f"'{name}' no longer collides between {sections} -- issue may be stale.")

    print(f"'{name}' is declared in both {sections[0]} and {sections[1]}. Choose which to rename:")
    print(f"    1) Rename the {sections[0]} entry")
    print(f"    2) Rename the {sections[1]} entry")
    while True:
        choice = input("  Enter number: ").strip()
        if choice in ("1", "2"):
            break
        print(f"    '{choice}' is not a valid selection. Try again.")

    target_section = sections[0] if choice == "1" else sections[1]
    all_names = set()
    for s in ("raw_materials", "intermediate_materials", "products"):
        all_names |= _collect_names(config, s)

    new_name = _prompt_unique_name(f"{target_section}[].name (renaming '{name}')", all_names)

    for entry in config.get(target_section, []) or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            entry["name"] = new_name
            break

    print(f"  Renamed '{name}' -> '{new_name}' in {target_section}. NOTE: other references to "
          f"'{name}' elsewhere (bom keys, edges, customer.product, etc.) are NOT automatically "
          f"updated -- any that meant this entry will surface as a dangling reference on the "
          f"next check, which can then be resolved individually.")

    return config


def repair_duplicate_name_within_section(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer2 check 15: the same name appears more than once within a
    single section (e.g. two suppliers both named the same thing).
    Keeps the first occurrence as-is, renames every subsequent duplicate
    to a new, unique name (one at a time, re-prompting on collision).

    Same non-cascading caveat as repair_material_category_collision --
    only the entries' own 'name' fields are changed here.
    """
    name = issue.context.get("referenced_name") if issue.context else None
    section = issue.context.get("section") if issue.context else None
    if not name or not section:
        raise ValueError(f"Issue at '{issue.location}' has no referenced_name/section in its context.")

    entries = config.get(section, []) or []
    current_indices = [i for i, e in enumerate(entries) if isinstance(e, dict) and e.get("name") == name]
    if len(current_indices) < 2:
        raise ValueError(f"'{name}' is no longer duplicated in {section} -- issue may be stale.")

    print(f"'{name}' appears {len(current_indices)} times in {section} "
          f"(indices {current_indices}). Keeping index {current_indices[0]} as-is; renaming the rest:")

    existing_names = set(e.get("name") for e in entries if isinstance(e, dict) and isinstance(e.get("name"), str))

    for idx in current_indices[1:]:
        new_name = _prompt_unique_name(f"{section}[{idx}].name (currently '{name}')", existing_names)
        entries[idx]["name"] = new_name
        existing_names.add(new_name)
        print(f"    Renamed {section}[{idx}] -> '{new_name}'.")

    print(f"  NOTE: other references to '{name}' elsewhere may now be ambiguous for the renamed "
          f"entries -- these will surface as dangling references on the next check.")

    return config


def repair_duplicate_edges(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer2 check 16: the same (source, destination, material_name) triple
    appears more than once in edges[]. Keeps the first occurrence,
    deletes the rest (highest index first, to avoid index-shift bugs
    during deletion).
    """
    source = issue.context.get("source") if issue.context else None
    destination = issue.context.get("destination") if issue.context else None
    material_name = issue.context.get("material_name") if issue.context else None
    if not source or not destination or not material_name:
        raise ValueError(f"Issue at '{issue.location}' is missing source/destination/material_name in its context.")

    edges = config.get("edges", []) or []
    current_indices = [
        i for i, e in enumerate(edges)
        if isinstance(e, dict) and e.get("source") == source
        and e.get("destination") == destination and e.get("material_name") == material_name
    ]
    if len(current_indices) < 2:
        raise ValueError(
            f"Edge ({source} -> {destination}, {material_name}) is no longer duplicated -- issue may be stale."
        )

    print(f"Edge ({source} -> {destination}, material '{material_name}') appears "
          f"{len(current_indices)} times. Keeping the first (index {current_indices[0]}), "
          f"removing the rest.")
    for idx in sorted(current_indices[1:], reverse=True):
        del edges[idx]
        print(f"    Removed duplicate at edges[{idx}].")

    return config


def repair_self_loop_edge(config: dict, issue: ValidationIssue) -> dict:
    """
    Deletes an edge whose source and destination are the same node --
    structurally meaningless in this domain, so there's no ambiguity to
    resolve and nothing to ask the person about. Fully automatic, no
    prompting, matching how transfer_time defaults are handled
    elsewhere in this system: informs, doesn't ask.
    """
    match = re.match(r"^edges\[(\d+)\]$", issue.location)
    if not match:
        raise ValueError(f"'{issue.location}' does not look like an edges[N] entry.")
    idx = int(match.group(1))

    edges = config.get("edges", [])
    if not isinstance(edges, list) or idx >= len(edges):
        raise ValueError(f"No edge at index {idx}.")

    try:
        label = describe_location(config, f"edges[{idx}]")
    except Exception:
        label = issue.location

    removed = edges.pop(idx)

    material = removed.get("material_name")
    material_note = f" (material: '{material}')" if isinstance(material, str) and material and material != "missing" else ""
    print(f"  Removed self-loop edge at {label}{material_note} -- "
          f"a node cannot deliver to itself.")

    return config


def repair_supplier_facility_name_collision(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer2 check 17: the same name is used as both a supplier and a
    facility. Offers a choice of which side to rename.
    """
    name = issue.context.get("referenced_name") if issue.context else None
    if not name:
        raise ValueError(f"Issue at '{issue.location}' has no referenced_name in its context.")

    supplier_names = _collect_names(config, "supplier")
    facility_names = _collect_names(config, "facility")
    if name not in supplier_names or name not in facility_names:
        raise ValueError(f"'{name}' no longer collides between supplier and facility -- issue may be stale.")

    print(f"'{name}' is used as both a supplier name and a facility name. Choose which to rename:")
    print("    1) Rename the supplier")
    print("    2) Rename the facility")
    while True:
        choice = input("  Enter number: ").strip()
        if choice in ("1", "2"):
            break
        print(f"    '{choice}' is not a valid selection. Try again.")

    section_label = "supplier" if choice == "1" else "facility"
    target_list = config.get(section_label, []) or []
    all_names = supplier_names | facility_names

    new_name = _prompt_unique_name(f"{section_label}[].name (renaming '{name}')", all_names)

    for entry in target_list:
        if isinstance(entry, dict) and entry.get("name") == name:
            entry["name"] = new_name
            break

    print(f"  Renamed {section_label} '{name}' -> '{new_name}'. NOTE: nodes[0].{section_label} "
          f"and any edges referencing '{name}' will now be dangling -- these will surface as "
          f"dangling references on the next check, which can then be resolved individually.")

    return config


def repair_facility_material_stage_span(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer3 check 13: a manufacturing facility's inventory_managed spans
    only one material stage (e.g. two raw materials, nothing else) --
    nothing at a later stage for it to actually produce. Three options:
      1) Change its type to 'warehouse' (it doesn't convert anything;
         removes the stage-span requirement entirely, since that only
         applies to type=="manufacturing"). Also strips the now-stale
         'operation' field, since a warehouse doesn't have one.
      2) Add an existing material from a different stage to
         inventory_managed, then attempt to re-derive operation.input/
         output from the updated set (same stage-based auto-derivation
         used when a facility is first built).
      3) Delete the facility entirely.
    """
    entry_index = issue.context.get("entry_index") if issue.context else None
    if entry_index is None:
        raise ValueError(f"Issue at '{issue.location}' has no entry_index in its context.")

    facilities = config.get("facility", []) or []
    if entry_index >= len(facilities) or not isinstance(facilities[entry_index], dict):
        raise ValueError(f"'{issue.location}' does not resolve to a real facility entry.")
    facility = facilities[entry_index]
    facility_name = facility.get("name")

    managed = facility.get("inventory_managed") or []
    stages_present = set()
    for m in managed:
        if isinstance(m, str):
            stage = _material_stage(config, m)
            if stage is not None:
                stages_present.add(stage)

    if len(stages_present) >= 2:
        raise ValueError(f"'{facility_name}' already spans multiple stages -- issue may be stale.")

    print(f"Manufacturing facility '{facility_name}' only manages material at one stage. Choose a fix:")
    print("    0) Delete this facility entirely")
    print("    1) Change its type to 'warehouse' (it doesn't convert anything)")
    print("    2) Add an existing material from a different stage to its inventory_managed")

    while True:
        choice = input("  Enter number: ").strip()

        if choice == "0":
            confirm = input(
                f"  Type 'yes' to confirm deleting facility '{facility_name}' and its "
                f"edges/registration (cannot be undone), or press Enter to go back: "
            ).strip().lower()
            if confirm == "yes":
                removed = _delete_facility_and_associations(config, facility_name)
                print(f"  Deleted '{facility_name}'. Removed: {', '.join(removed) if removed else '(nothing else referenced it)'}")
                return config
            continue

        if choice == "1":
            facility["type"] = "warehouse"
            if "operation" in facility:
                del facility["operation"]
                print(f"  Removed stale 'operation' field (warehouses don't have one).")
            print(f"  Changed '{facility_name}' type to 'warehouse'.")
            return config

        if choice == "2":
            all_names = sorted(
                _collect_names(config, "raw_materials")
                | _collect_names(config, "intermediate_materials")
                | _collect_names(config, "products")
            )
            candidates = [m for m in all_names if m not in managed]
            if not candidates:
                print("  No other materials exist to add. Choose a different option.")
                continue

            stage_labels = {0: "raw", 1: "intermediate", 2: "product"}
            print("  Select a material to add:")
            for i, m in enumerate(candidates, start=1):
                label = stage_labels.get(_material_stage(config, m), "?")
                print(f"    {i}) {m} ({label})")

            while True:
                mchoice = input("  Enter number: ").strip()
                if not mchoice.isdigit() or not (1 <= int(mchoice) <= len(candidates)):
                    print(f"    '{mchoice}' is not a valid selection. Try again.")
                    continue
                break

            new_material = candidates[int(mchoice) - 1]
            _add_to_inventory_managed(facility, new_material)
            print(f"  Added '{new_material}' to '{facility_name}'s inventory_managed.")

            derived_input = _try_auto_derive_operation_list(config, facility, "input")
            derived_output = _try_auto_derive_operation_list(config, facility, "output")
            if derived_input is not None and derived_output is not None:
                operation = facility.setdefault("operation", {})
                operation["input"] = derived_input
                operation["output"] = derived_output
                print(f"  Re-derived operation.input={derived_input}, operation.output={derived_output}.")
            else:
                print(f"  NOTE: could not auto-derive operation.input/output from the updated "
                      f"inventory_managed (still ambiguous) -- update these manually if needed.")
            return config

        print(f"    '{choice}' is not a valid selection. Try again.")


def repair_horizon_sanity(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer3 check 12 (WARNING only, never blocks): simulation.horizon is
    shorter than a rough estimate of one full supplier-to-customer cycle.
    Bumps horizon to comfortably exceed that estimate (1.5x margin, since
    the estimate itself is a rough heuristic, not a guaranteed bound --
    see check_horizon_sanity's own docstring).
    """
    estimated = issue.context.get("estimated_cycle_time") if issue.context else None
    current = issue.context.get("horizon") if issue.context else None
    if estimated is None or current is None:
        raise ValueError(f"Issue at '{issue.location}' is missing horizon/estimated_cycle_time in its context.")

    new_horizon = max(current, int(estimated * 1.5) + 1)
    print(f"simulation.horizon ({current}) is shorter than the estimated cycle time (~{estimated}). "
          f"Updating to {new_horizon}.")
    config.setdefault("simulation", {})["horizon"] = new_horizon

    return config


def repair_facility_missing_edge(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer3 check 9: a facility has no inbound and/or no outbound edge.
    Fires as TWO separate issues per facility with IDENTICAL location/
    defect_type/context (only detail text differs: "no inbound" vs "no
    outbound") -- rather than parse that text, this checks live edge
    state directly and fixes whichever direction(s) are actually missing
    in one call (so if both were missing, one repair pass handles both).
    """
    facility_name = issue.context.get("referenced_name") if issue.context else None
    if not facility_name:
        raise ValueError(f"Issue at '{issue.location}' has no referenced_name in its context.")

    facility = next(
        (f for f in config.get("facility", []) or [] if isinstance(f, dict) and f.get("name") == facility_name),
        None,
    )
    if facility is None:
        raise ValueError(f"Could not find a facility entry named '{facility_name}'.")

    has_inbound = any(
        isinstance(e, dict) and e.get("destination") == facility_name for e in config.get("edges", []) or []
    )
    has_outbound = any(
        isinstance(e, dict) and e.get("source") == facility_name for e in config.get("edges", []) or []
    )

    if has_inbound and has_outbound:
        raise ValueError(f"'{facility_name}' already has both inbound and outbound edges -- issue may be stale.")

    made_progress = False
    errors = []

    if not has_inbound:
        try:
            _fix_facility_missing_inbound(config, facility)
            made_progress = True
        except ValueError as e:
            errors.append(str(e))

    if not has_outbound:
        try:
            _fix_facility_missing_outbound(config, facility)
            made_progress = True
        except ValueError as e:
            errors.append(str(e))

    if not made_progress:
        # Neither direction could be fixed -- propagate failure so the
        # orchestrator correctly marks this as unrepairable and moves on,
        # instead of retrying the identical issue forever.
        raise ValueError("; ".join(errors))

    return config


def repair_supplier_missing_outbound_edge(config: dict, issue: ValidationIssue) -> dict:
    """
    Layer3 check 8: a supplier has no outbound edge. Fix: create a new
    edge from this supplier to a facility. material_name and
    material_type are pre-seeded -- a supplier only ever supplies ONE
    material (its own supply_material_name, always a raw_material).

    Destination selection is narrowed to facilities that ACTUALLY
    reference this material (via inventory_managed or operation.input) --
    NOT a blind menu of every facility. Delivering material to a facility
    that doesn't manage or consume it is physically meaningless (this was
    a real bug: an earlier version let the user pick any facility, which
    produced an edge to a facility that neither managed, consumed, nor
    produced the material at all).

    If exactly one facility references it: auto-selected, no prompt.
    If multiple: menu narrowed to just those.
    If none yet: falls back to the full facility list, but prints an
    explicit warning that the material isn't used anywhere yet -- the
    person should make sure some facility's operation.input/
    inventory_managed is updated to actually consume it (adding it to a
    bom via check_raw_material_is_consumed's repair does NOT by itself
    update any facility's operation.input/inventory_managed -- that
    alignment isn't currently auto-repaired and should be checked
    separately).
    """
    supplier_name = issue.context.get("referenced_name") if issue.context else None
    if not supplier_name:
        raise ValueError(f"Issue at '{issue.location}' has no referenced_name in its context.")

    has_outbound = any(
        isinstance(e, dict) and e.get("source") == supplier_name
        for e in config.get("edges", []) or []
    )
    if has_outbound:
        raise ValueError(f"'{supplier_name}' already has an outbound edge -- issue may be stale.")

    supplier_entry = next(
        (s for s in config.get("supplier", []) or [] if isinstance(s, dict) and s.get("name") == supplier_name),
        None,
    )
    if supplier_entry is None:
        raise ValueError(f"Could not find a supplier entry named '{supplier_name}'.")
    material_name = supplier_entry.get("supply_material_name")

    print(f"Supplier '{supplier_name}' has no outbound edge.")
    choice = input("  Type 'delete' to remove this material (and this supplier) entirely, "
                    "or press Enter to create an edge: ").strip().lower()
    if choice == "delete":
        if _offer_delete_material_option(config, material_name):
            return config
        print("  Cancelled -- creating an edge instead.")

    all_facility_names = sorted(_collect_names(config, "facility"))
    if not all_facility_names:
        raise ValueError("No facilities exist yet to connect this supplier to.")

    referencing_facilities = []
    for f in config.get("facility", []) or []:
        if not isinstance(f, dict):
            continue
        name = f.get("name")
        managed = f.get("inventory_managed") or []
        operation = f.get("operation") or {}
        inputs = operation.get("input") or []
        if material_name in managed or material_name in inputs:
            if isinstance(name, str):
                referencing_facilities.append(name)

    if len(referencing_facilities) == 1:
        destination = referencing_facilities[0]
        print(f"  Destination auto-selected: '{destination}' (the only facility that "
              f"manages or consumes '{material_name}').")
    elif len(referencing_facilities) > 1:
        destination = _prompt_select_single(
            f"edges[].destination (facilities that use '{material_name}')", sorted(referencing_facilities),
            config=config,
        )
    else:
        print(f"  WARNING: no facility currently manages or consumes '{material_name}' -- "
              f"make sure a facility's inventory_managed/operation.input gets updated to "
              f"actually use it, or this delivery will go nowhere useful.")
        destination = _prompt_select_single(
            f"edges[].destination (for supplier '{supplier_name}')", all_facility_names, config=config
        )

    new_edge = {
        "source": supplier_name,
        "destination": destination,
        "material_type": "raw_material",  # a supplier only ever supplies a raw material
        "material_name": material_name,
    }
    config.setdefault("edges", []).append(new_edge)
    idx = len(config["edges"]) - 1

    transfer_fspec = SECTION_SPECS["edges"]["fields"]["transfer_time"]
    _fill_node(config, new_edge, "transfer_time", transfer_fspec,
               f"edges[{idx}].transfer_time", "edges.transfer_time", new_edge)

    return config


def repair_missing_node(config: dict, issue: ValidationIssue) -> dict:
    """
    Repairs a MISSING_REQUIRED_VALUE issue whose location points at a
    CONTAINER (a distribution object, bom-style dict, a list-select field
    like inventory_managed/operation.input/output, or a fixed-name nested
    object like batching/operation/procurement_scheme) rather than a
    plain scalar. Reconstructs the entire missing subtree via _fill_node,
    which checks candidate-derived special cases (bom, list-select) at
    EVERY level of recursion -- not just for the field named directly by
    this issue, but also for any nested field encountered while filling
    a larger missing container (e.g. filling a fully-missing "operation"
    object still correctly turns its "input" child into a material
    selection menu, not a generic string prompt).

    For a plain scalar field, this still works (falls through to the
    same scalar-prompt logic) -- but repair_scalar_missing_field remains
    the more direct action for that case.
    """
    if issue.defect_type != DefectType.MISSING_REQUIRED_VALUE:
        raise ValueError(
            f"repair_missing_node only handles MISSING_REQUIRED_VALUE, got {issue.defect_type}"
        )

    normalized = normalize_location(issue.location)
    fspec = find_spec_node(normalized)

    if fspec is None:
        raise ValueError(f"Could not find a schema spec node for '{issue.location}'.")

    steps = _parse_location_steps(issue.location)
    parent, last_key = _navigate_to_parent(config, steps)

    # The owning top-level list entry for this issue's section (e.g.
    # config["facility"][0]) -- section names are always followed by an
    # integer index in every location this schema produces.
    section_entry = None
    if len(steps) >= 2 and isinstance(steps[0], str) and isinstance(steps[1], int):
        section_entry = config.get(steps[0], [None])[steps[1]] if steps[1] < len(config.get(steps[0], [])) else None

    print(f"Field '{issue.location}' is missing -- reconstructing it.")
    _fill_node(config, parent, last_key, fspec, issue.location, normalized, section_entry)

    return config


def repair_procurement_scheme_field(config: dict, issue: ValidationIssue) -> dict:
    """
    Handles ANY issue whose location involves procurement_scheme -- the
    object itself missing, or any of its sub-fields (type, distribution,
    parameters, parameters.a/b). Since procurement_scheme's valid shape
    genuinely depends on its own "type" value (three different shapes --
    periodic_supply needs a real distribution; demand_driven needs
    nothing else; inventory_threshold needs exactly two fixed values s/S,
    no distribution at all), it's treated as ONE atomic editable unit
    here rather than patching individual sub-fields in isolation.
    Whatever specific sub-issue triggered this call, the whole object
    gets resolved to a consistent, complete state in one pass: type
    first (only asked if not already validly set), then whatever the
    resulting type requires (only asking for pieces that are actually
    still missing or placeholder -- doesn't re-ask for anything already
    correctly filled).
    """
    match = re.search(r"^(.*?procurement_scheme)(\..*)?$", issue.location)
    if not match:
        raise ValueError(f"'{issue.location}' does not contain a procurement_scheme segment.")
    ps_location = match.group(1)
    ps_steps = _parse_location_steps(ps_location)
    owning_entry, key = _navigate_to_parent(config, ps_steps)

    existing = owning_entry.get(key)
    obj = existing if isinstance(existing, dict) else {}
    owning_entry[key] = obj

    current_type = obj.get("type")
    if current_type in PROCUREMENT_SCHEME_TYPES:
        type_val = current_type
    else:
        type_val = _prompt_for_value(f"{ps_location}.type", "str", enum_values=PROCUREMENT_SCHEME_TYPES, config=config)
        obj["type"] = type_val

    if type_val == "periodic_supply":
        dist_value = obj.get("distribution")
        if dist_value not in DISTRIBUTION_PARAM_COUNTS:
            dist_value, params = _prompt_for_distribution(ps_location, config=config, allow_instant=False)
            obj["distribution"] = dist_value
            obj["parameters"] = params
        else:
            param_count = DISTRIBUTION_PARAM_COUNTS.get(dist_value, 1)
            params = obj.get("parameters")
            if not isinstance(params, dict):
                params = {}
            obj["parameters"] = params
            for i, pkey in enumerate(PARAM_KEYS):
                if i < param_count and (pkey not in params or params.get(pkey) == "missing"):
                    params[pkey] = _prompt_for_value(f"{ps_location}.parameters.{pkey}", "num", config=config)

    elif type_val == "demand_driven":
        pass  # nothing else needed -- distribution/parameters are irrelevant for this type

    elif type_val == "inventory_threshold":
        params = obj.get("parameters")
        if not isinstance(params, dict):
            params = {}
        obj["parameters"] = params
        if "a" not in params or params.get("a") == "missing":
            params["a"] = _prompt_for_value(f"{ps_location}.parameters.a", "num", config=config)
        if "b" not in params or params.get("b") == "missing":
            params["b"] = _prompt_for_value(f"{ps_location}.parameters.b", "num", config=config)

    return config


def repair_transfer_time_default_instant(config: dict, issue: ValidationIssue) -> dict:
    """
    Per explicit instruction: edges[].transfer_time is ALWAYS defaulted
    to instant (constant, a=0) automatically -- no prompt at all, in
    either direction (missing entirely, or missing just its distribution/
    parameters). Delivery/transfer between nodes is assumed
    instantaneous by default; the person is only informed this happened,
    not asked anything.

    There is no what-if feature in this codebase yet to override this
    per-edge -- the printed notice references it as a future capability,
    not something implemented here.
    """
    match = re.search(r"^(edges\[\d+\]\.transfer_time)(\..*)?$", issue.location)
    if not match:
        raise ValueError(f"'{issue.location}' does not look like an edges[].transfer_time field.")
    tt_location = match.group(1)
    tt_steps = _parse_location_steps(tt_location)
    owning_edge, key = _navigate_to_parent(config, tt_steps)
    owning_edge[key] = {"distribution": "constant", "parameters": {"a": 0}}

    try:
        label = describe_location(config, tt_location)
    except Exception:
        label = tt_location

    print(f"  {label} -> defaulted to instant (constant, 0). Transfer times are "
          f"assumed instantaneous by default; use the what-if feature later if "
          f"you need to model an actual delay for this edge.")

    return config


def repair_scalar_missing_field(config: dict, issue: ValidationIssue) -> dict:
    """
    Repairs a MISSING_REQUIRED_VALUE issue on a plain scalar field
    (str/num/bool -- NOT dict-shaped or list-shaped fields, those are
    separate planned actions).

    Only handles issues from Layer1 (field presence / placeholder
    detection). Assumes the issue's location resolves to a scalar field
    per SCALAR_FIELD_TYPES -- raises if it doesn't, rather than guessing.
    """
    if issue.defect_type != DefectType.MISSING_REQUIRED_VALUE:
        raise ValueError(
            f"repair_scalar_missing_field only handles MISSING_REQUIRED_VALUE, "
            f"got {issue.defect_type}"
        )

    normalized = normalize_location(issue.location)
    expected_kind = _lookup_field_type(normalized)

    if expected_kind is None:
        raise ValueError(
            f"'{issue.location}' (normalized: '{normalized}') is not a known scalar "
            f"field -- it may be dict/list-shaped (use a different repair action) "
            f"or missing from SCALAR_FIELD_TYPES."
        )

    enum_values = _lookup_enum_values(normalized)

    steps = _parse_location_steps(issue.location)
    parent, last_key = _navigate_to_parent(config, steps)

    # Special case: edges.source / edges.destination missing a value.
    # A missing endpoint often means the edge itself is spurious (e.g.
    # leftover from a deleted node, or something that was never really
    # needed) rather than something that just needs a destination filled
    # in -- offer deleting the whole edge as an explicit option
    # alongside the normal candidate list, rather than forcing a pick.
    if normalized in ("edges.destination", "edges.source"):
        edge_match = re.match(r"^edges\[(\d+)\]\.", issue.location)
        if edge_match:
            edge_idx = int(edge_match.group(1))
            candidates = (SCALAR_SELECT_CANDIDATE_FNS.get(normalized) or (lambda c, p: []))(config, parent)
            try:
                edge_label = describe_location(config, f"edges[{edge_idx}]")
            except Exception:
                edge_label = f"edges[{edge_idx}]"

            print(f"{edge_label}: Select a value, or delete this edge entirely.")
            print("    0) Delete this edge entirely")
            for i, name in enumerate(candidates, start=1):
                print(f"    {i}) {name}")

            while True:
                choice = input("  Enter number: ").strip()
                if choice == "0":
                    edges = config.get("edges", [])
                    removed = edges.pop(edge_idx)
                    mat = removed.get("material_name")
                    mat_note = f" (material: '{mat}')" if isinstance(mat, str) and mat and mat != "missing" else ""
                    print(f"  Deleted edge at {edge_label}{mat_note}.")
                    return config
                if choice.isdigit() and 1 <= int(choice) <= len(candidates):
                    parent[last_key] = candidates[int(choice) - 1]
                    return config
                print(f"    '{choice}' is not a valid selection. Try again.")

    select_fn = SCALAR_SELECT_CANDIDATE_FNS.get(normalized)
    if select_fn is not None:
        candidates = select_fn(config, parent)
        allow_none = normalized in SCALAR_SELECT_ALLOW_NONE
        if candidates or allow_none:
            print(f"Field '{issue.location}' is missing -- select its value below.")
            selected = _prompt_select_single(issue.location, candidates, allow_none=allow_none, config=config)
            if selected is None:
                return config  # "None" chosen for an allow-none field -- leave it unset
            parent[last_key] = selected
            return config
        # No candidates and none not allowed -- fall through to free-text
        # entry below (e.g. cascading field where the sibling type/material_type
        # hasn't been set yet, so nothing can be narrowed down).
        print(f"  No candidates available yet for '{issue.location}' -- falling back to manual entry.")

    # Special case: a "*.distribution" field being repaired here (the
    # object it lives in already exists, just this one sub-field is
    # missing/placeholder) is the MOST COMMON real-world shape -- LLM
    # output almost always has the full object present with individual
    # "missing" placeholders inside, not the whole object absent. This
    # needs the same "Instant" shortcut and one-step parameters
    # resolution as the container-building path in _fill_node, or the
    # shortcut would only ever apply to the rare "whole object missing"
    # case. If this field is a distribution enum position, use the
    # combined helper and also set "parameters" directly here (the
    # sibling parameters issue, if it exists separately, simply won't
    # re-fire on the next verification pass since it's already resolved).
    if normalized.endswith(".distribution") and enum_values == DISTRIBUTION_TYPES:
        dist_location = issue.location[: -len(".distribution")]
        dist_field = normalized[: -len(".distribution")]
        dist_value, params = _prompt_for_distribution(
            dist_location, config=config,
            allow_instant=(dist_field not in QUANTITY_BASED_DISTRIBUTION_FIELDS),
        )
        parent[last_key] = dist_value
        if isinstance(parent, dict):
            parent["parameters"] = params
        return config

    value = _prompt_for_value(issue.location, expected_kind, enum_values=enum_values, config=config)
    parent[last_key] = value

    return config


def repair_invalid_enum_value(config: dict, issue: ValidationIssue) -> dict:
    """
    Repairs an INVALID_VALUE issue on an enum-constrained field (e.g.
    distribution type, inventory.type, edges.material_type,
    procurement_scheme.type). The field already has a value -- it's just
    not one of the recognized options -- so this OVERWRITES the existing
    value rather than inserting a new key (that distinction is the main
    difference from repair_scalar_missing_field).

    Only handles fields with a known enum constraint (per
    ENUM_FIELD_VALUES) -- raises if the issue's location isn't one of
    those, rather than guessing at a generic "fix this string" action.
    """
    if issue.defect_type != DefectType.INVALID_VALUE:
        raise ValueError(
            f"repair_invalid_enum_value only handles INVALID_VALUE, got {issue.defect_type}"
        )

    normalized = normalize_location(issue.location)
    enum_values = _lookup_enum_values(normalized)

    if enum_values is None:
        raise ValueError(
            f"'{issue.location}' (normalized: '{normalized}') has no known enum "
            f"constraint -- this action only handles recognized enum fields "
            f"(distribution, procurement_scheme.type, inventory.type, "
            f"edges.material_type). A different INVALID_VALUE (e.g. a general "
            f"type mismatch) needs a different repair action."
        )

    steps = _parse_location_steps(issue.location)
    parent, last_key = _navigate_to_parent(config, steps)
    current_value = parent.get(last_key) if isinstance(parent, dict) else None

    print(f"  Field '{issue.location}' has an invalid value: {current_value!r}")

    # Safe, deterministic auto-correction: the LLM sometimes writes the
    # plural section name (e.g. "products", "raw_materials") instead of
    # the singular enum value (e.g. "product", "raw_material"). This is
    # not a genuine ambiguity requiring human judgment -- if stripping a
    # single trailing 's' produces an EXACT match to one of the valid
    # options, just apply it directly, no prompt.
    if isinstance(current_value, str) and current_value.endswith("s"):
        singular_candidate = current_value[:-1]
        if singular_candidate in enum_values:
            parent[last_key] = singular_candidate
            print(f"  Auto-corrected '{current_value}' -> '{singular_candidate}' "
                  f"(plural/singular mismatch, unambiguous).")
            return config

    value = _prompt_for_value(issue.location, "str", enum_values=enum_values, config=config)
    parent[last_key] = value

    return config


# ----------------------------------------------------------------------
# nodes[0] repairs
# ----------------------------------------------------------------------
# "nodes" has an irregular shape not covered by SECTION_SPECS (a single
# dict with supplier/facility/customer LIST keys), so it needs its own
# dedicated repair actions rather than going through find_spec_node.

NODES_ENTITY_SECTION = {"supplier": "supplier", "facility": "facility", "customer": "customer"}


def repair_missing_nodes_list(config: dict, issue: ValidationIssue) -> dict:
    """
    Repairs a MISSING_REQUIRED_VALUE issue at "nodes[0].supplier" /
    "nodes[0].facility" / "nodes[0].customer" -- the KEY itself is absent
    from nodes[0]. Rebuilds it as a list-select menu of real entity names
    from the corresponding section.
    """
    m = re.match(r"^nodes\[0\]\.(supplier|facility|customer)$", issue.location)
    if not m:
        raise ValueError(f"'{issue.location}' is not a recognized nodes[0] list field.")
    key = m.group(1)

    nodes = config.get("nodes")
    if not isinstance(nodes, list) or len(nodes) == 0 or not isinstance(nodes[0], dict):
        raise ValueError("config['nodes'][0] does not exist -- cannot repair a child key of it.")

    section = NODES_ENTITY_SECTION[key]
    candidates = sorted(_collect_names(config, section))

    print(f"Field '{issue.location}' is missing -- select its entries below.")
    _fill_list_select(nodes[0], key, candidates, issue.location)

    return config


def repair_nodes_registration_gap(config: dict, issue: ValidationIssue) -> dict:
    """
    Repairs an INCONSISTENT_CROSS_FIELD issue at "nodes[0].supplier" /
    "nodes[0].facility" / "nodes[0].customer" (NO index) -- a real entity
    exists but isn't registered in nodes[0]. The issue's own context
    already names exactly which entity is missing (context["referenced_name"]),
    so this is a deterministic append, no menu needed.
    """
    m = re.match(r"^nodes\[0\]\.(supplier|facility|customer)$", issue.location)
    if not m:
        raise ValueError(f"'{issue.location}' is not a recognized nodes[0] registration gap.")
    key = m.group(1)

    name = issue.context.get("referenced_name") if issue.context else None
    if not name:
        raise ValueError(f"Issue at '{issue.location}' has no referenced_name in its context.")

    nodes = config.get("nodes")
    if not isinstance(nodes, list) or len(nodes) == 0 or not isinstance(nodes[0], dict):
        raise ValueError("config['nodes'][0] does not exist -- cannot repair a child key of it.")

    existing = nodes[0].setdefault(key, [])
    if name not in existing:
        existing.append(name)
        print(f"  Added '{name}' to nodes[0].{key}.")

    return config


def repair_nodes_phantom_entry(config: dict, issue: ValidationIssue) -> dict:
    """
    Repairs a DANGLING_REFERENCE issue at "nodes[0].supplier[i]" /
    "nodes[0].facility[i]" / "nodes[0].customer[i]" -- a name IS present
    at that index but doesn't correspond to any real entity. Offers to
    either remove the phantom entry or replace it with a real,
    not-already-listed name from the corresponding section.
    """
    m = re.match(r"^nodes\[0\]\.(supplier|facility|customer)\[(\d+)\]$", issue.location)
    if not m:
        raise ValueError(f"'{issue.location}' is not a recognized nodes[0] phantom-entry issue.")
    key, idx = m.group(1), int(m.group(2))

    nodes = config.get("nodes")
    if not isinstance(nodes, list) or len(nodes) == 0 or not isinstance(nodes[0], dict):
        raise ValueError("config['nodes'][0] does not exist -- cannot repair a child entry of it.")

    entry_list = nodes[0].get(key)
    if not isinstance(entry_list, list) or idx >= len(entry_list):
        raise ValueError(f"'{issue.location}' does not resolve to a real list entry.")

    phantom_name = entry_list[idx]
    section = NODES_ENTITY_SECTION[key]
    real_candidates = sorted(_collect_names(config, section) - set(entry_list))

    print(f"  '{issue.location}' = '{phantom_name}' does not correspond to any real "
          f"{key}. Choose how to resolve it:")
    print("    0) Remove this entry")
    for i, name in enumerate(real_candidates, start=1):
        print(f"    {i}) Replace with: {name}")

    while True:
        choice = input("  Enter number: ").strip()
        if choice == "0":
            del entry_list[idx]
            print(f"    Removed '{phantom_name}'.")
            return config
        if not choice.isdigit() or not (1 <= int(choice) <= len(real_candidates)):
            print(f"    '{choice}' is not a valid selection. Try again.")
            continue
        entry_list[idx] = real_candidates[int(choice) - 1]
        print(f"    Replaced with '{entry_list[idx]}'.")
        return config


# ----------------------------------------------------------------------
# Dangling reference repairs (Layer2 issues on scalar reference fields)
# ----------------------------------------------------------------------

def repair_dangling_reference(config: dict, issue: ValidationIssue) -> dict:
    """
    Repairs a DANGLING_REFERENCE issue on a scalar field that already has
    a value -- just not a valid one (e.g. supply_material_name pointing
    at a nonexistent raw material). Reuses the same
    SCALAR_SELECT_CANDIDATE_FNS candidate derivation as the missing-field
    path, but OVERWRITES the existing bad value rather than inserting a
    new key.

    Only handles fields with a registered candidate function -- raises
    otherwise (nodes[0][...] phantom entries and registration gaps are
    handled by their own dedicated actions, not this one).
    """
    if issue.defect_type != DefectType.DANGLING_REFERENCE:
        raise ValueError(
            f"repair_dangling_reference only handles DANGLING_REFERENCE, got {issue.defect_type}"
        )

    normalized = normalize_location(issue.location)
    select_fn = SCALAR_SELECT_CANDIDATE_FNS.get(normalized)
    if select_fn is None:
        raise ValueError(
            f"'{issue.location}' (normalized: '{normalized}') has no registered candidate "
            f"function -- this action only handles known reference fields."
        )

    steps = _parse_location_steps(issue.location)
    parent, last_key = _navigate_to_parent(config, steps)
    current_value = parent.get(last_key) if isinstance(parent, dict) else None

    candidates = select_fn(config, parent)
    allow_none = normalized in SCALAR_SELECT_ALLOW_NONE

    if not candidates and not allow_none:
        raise ValueError(f"No valid candidates available to repair '{issue.location}'.")

    print(f"  Field '{issue.location}' has an invalid reference: {current_value!r}")
    selected = _prompt_select_single(issue.location, candidates, allow_none=allow_none, config=config)
    if selected is None:
        del parent[last_key]
        print(f"    Cleared '{issue.location}' (left unset).")
    else:
        parent[last_key] = selected

    return config


# ----------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------

def dispatch_repair(config: dict, issue: ValidationIssue) -> bool:
    """
    Attempts to repair a single issue by routing it to whichever action
    actually handles its shape. Returns True if repaired, False if no
    current action can handle it (caller should report it as unhandled,
    not silently ignore it).
    """
    if issue.defect_type == DefectType.MISSING_REQUIRED_VALUE:
        if re.match(r"^nodes\[0\]\.(supplier|facility|customer)$", issue.location):
            repair_missing_nodes_list(config, issue)
            return True

        if issue.location == "raw_materials":
            repair_at_least_one_raw_material(config, issue)
            return True

        if issue.location == "products":
            repair_at_least_one_product(config, issue)
            return True

        if "procurement_scheme" in issue.location:
            repair_procurement_scheme_field(config, issue)
            return True

        if re.match(r"^edges\[\d+\]\.transfer_time", issue.location):
            repair_transfer_time_default_instant(config, issue)
            return True

        normalized = normalize_location(issue.location)

        if _lookup_field_type(normalized) is not None:
            repair_scalar_missing_field(config, issue)
            return True

        try:
            find_spec_node(normalized)
        except ValueError:
            return False
        repair_missing_node(config, issue)
        return True

    elif issue.defect_type == DefectType.INVALID_VALUE:
        if issue.location == "simulation.horizon":
            repair_horizon_sanity(config, issue)
            return True

        if "procurement_scheme" in issue.location:
            repair_procurement_scheme_field(config, issue)
            return True

        if re.match(r"^edges\[\d+\]\.transfer_time", issue.location):
            repair_transfer_time_default_instant(config, issue)
            return True

        normalized = normalize_location(issue.location)
        if _lookup_enum_values(normalized) is not None:
            repair_invalid_enum_value(config, issue)
            return True
        return False

    elif issue.defect_type == DefectType.DANGLING_REFERENCE:
        if re.match(r"^nodes\[0\]\.(supplier|facility|customer)\[\d+\]$", issue.location):
            repair_nodes_phantom_entry(config, issue)
            return True

        normalized = normalize_location(issue.location)

        if normalized == "edges.material_name":
            # Two checks share this exact shape: check 9 (material_name's
            # category doesn't match material_type) and check 19 (category
            # is fine, but the destination facility doesn't use it). Test
            # the live category-match condition to route correctly.
            steps = _parse_location_steps(issue.location)
            edge_entry, _ = _navigate_to_parent(config, steps)
            material_type = edge_entry.get("material_type")
            section = INVENTORY_TYPE_TO_SECTION.get(material_type)
            category_valid = section is not None and edge_entry.get("material_name") in _collect_names(config, section)

            if not category_valid:
                repair_dangling_reference(config, issue)
            else:
                repair_edge_phantom_delivery(config, issue)
            return True

        if normalized in SCALAR_SELECT_CANDIDATE_FNS:
            repair_dangling_reference(config, issue)
            return True
        return False

    elif issue.defect_type == DefectType.INCONSISTENT_CROSS_FIELD:
        if re.match(r"^nodes\[0\]\.(supplier|facility|customer)$", issue.location):
            repair_nodes_registration_gap(config, issue)
            return True

        if re.match(r"^raw_materials\[\d+\]$", issue.location):
            # Two different checks share this exact location shape --
            # try each candidate action in turn; each has its own live
            # safety check and raises if its condition doesn't actually
            # hold, so this is safe rather than guessing.
            for action in (repair_raw_material_missing_supplier, repair_raw_material_not_consumed, repair_material_missing_inventory_entry):
                try:
                    action(config, issue)
                    return True
                except ValueError:
                    continue
            return False

        if re.match(r"^supplier\[\d+\]$", issue.location):
            repair_supplier_missing_outbound_edge(config, issue)
            return True

        if re.match(r"^products\[\d+\]$", issue.location):
            for action in (repair_product_missing_customer, repair_product_not_producible, repair_product_end_to_end_path, repair_material_missing_inventory_entry):
                try:
                    action(config, issue)
                    return True
                except ValueError:
                    continue
            return False

        if re.match(r"^intermediate_materials\[\d+\]$", issue.location):
            for action in (repair_intermediate_material_not_producible, repair_material_missing_inventory_entry):
                try:
                    action(config, issue)
                    return True
                except ValueError:
                    continue
            return False

        if re.match(r"^facility\[\d+\]$", issue.location):
            repair_facility_missing_edge(config, issue)
            return True

        if re.match(r"^customer\[\d+\]$", issue.location):
            repair_customer_missing_inbound_edge(config, issue)
            return True

        if re.match(r"^facility\[\d+\]\.inventory_managed$", issue.location):
            repair_facility_material_stage_span(config, issue)
            return True

        return False

    elif issue.defect_type == DefectType.DUPLICATE_ENTITY:
        if issue.location == "supplier/facility":
            repair_supplier_facility_name_collision(config, issue)
            return True

        if re.match(r"^(raw_materials|intermediate_materials|products)/(raw_materials|intermediate_materials|products)$", issue.location):
            repair_material_category_collision(config, issue)
            return True

        if re.match(r"^edges\[[\d,]+\]$", issue.location):
            repair_duplicate_edges(config, issue)
            return True

        if re.match(r"^\w+\[[\d,]+\]$", issue.location):
            repair_duplicate_name_within_section(config, issue)
            return True

    elif issue.defect_type == DefectType.MALFORMED_ENTRY:
        if re.match(r"^edges\[\d+\]$", issue.location):
            repair_self_loop_edge(config, issue)
            return True

        return False

    return False


# ----------------------------------------------------------------------
# CLI entry point for quick manual testing
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    from verification_layer1 import check_field_requirements
    from verification_layer2 import (
        check_intermediate_bom_references, check_product_bom_references,
        check_inventory_type_consistency, check_supplier_material_references,
        check_facility_inventory_managed_references, check_customer_product_references,
        check_nodes_registration_completeness, check_edge_material_references,
        check_operation_resource_references, check_edge_node_references,
        check_nodes_names_exist, check_material_name_uniqueness,
        check_operation_io_material_references, check_duplicate_names_within_sections,
        check_duplicate_edges, check_supplier_facility_name_collision,
        check_nodes_customer_registration,
    )
    from issue_types import Severity

    path = sys.argv[1] if len(sys.argv) > 1 else "test_config.json"
    with open(path) as f:
        cfg = json.load(f)

    issues = check_field_requirements(cfg) + (
        check_intermediate_bom_references(cfg)
        + check_product_bom_references(cfg)
        + check_inventory_type_consistency(cfg)
        + check_supplier_material_references(cfg)
        + check_facility_inventory_managed_references(cfg)
        + check_customer_product_references(cfg)
        + check_nodes_registration_completeness(cfg)
        + check_edge_material_references(cfg)
        + check_operation_resource_references(cfg)
        + check_edge_node_references(cfg)
        + check_nodes_names_exist(cfg)
        + check_material_name_uniqueness(cfg)
        + check_operation_io_material_references(cfg)
        + check_duplicate_names_within_sections(cfg)
        + check_duplicate_edges(cfg)
        + check_supplier_facility_name_collision(cfg)
        + check_nodes_customer_registration(cfg)
    )
    blocking = [i for i in issues if i.severity == Severity.BLOCKING]

    handled = []
    unhandled = []
    for issue in blocking:
        if dispatch_repair(cfg, issue):
            handled.append(issue)
        else:
            unhandled.append(issue)

    print(f"Repaired {len(handled)} issue(s).")
    print(f"({len(unhandled)} issue(s) need a different/future repair action.)\n")

    if unhandled:
        print("Unhandled issues:")
        for issue in unhandled:
            print("   ", issue)
        print()

    if not handled:
        print("Nothing was repaired -- config left unchanged.")
    else:
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)
        print(f"Saved repaired config to {path}")