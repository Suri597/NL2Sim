"""
scripts/orchestrator.py
--------------------------
The verify -> repair -> re-verify LOOP. Kept separate from repair.py by
design: repair.py is a library of individual repair actions (pure
functions, no looping, no re-verification calls); this file owns the
control flow that runs verification, dispatches each found issue to
repair.py's dispatch_repair(), and re-verifies from scratch before moving
to 3 next issue.

Why re-verify after EVERY single repair, not once per batch: a single
verify-once-repair-all pass can act on a STALE issue list -- fixing one
issue can silently resolve (or newly reveal) another as a side effect
(e.g. registering a node in nodes[0] can un-block an edge reference that
was flagged only because the registration hadn't happened yet). Re-running
full verification after each individual repair keeps every decision based
on current, accurate state -- at some cost in repeated verification work,
which is a fine trade for correctness here.

Container-shaped repairs (e.g. a whole missing supplier_lead_time,
resource.batching, or facility.operation) are still ONE atomic step in
this loop, not one step per sub-field -- that's exactly what
repair_missing_node/_fill_node already do internally (the schema tells us
every question that subtree needs up front, so they're all asked in one
interactive pass before the result is placed into the config). The loop
here doesn't re-verify mid-container-build; it re-verifies only after the
whole container has been placed.
"""

import re

from issue_types import Severity
from repair import dispatch_repair
from repair import dispatch_repair, set_repair_context
from verification_layer1 import check_field_requirements
from verification_layer2 import (
    check_intermediate_bom_references, check_product_bom_references,
    check_inventory_type_consistency, check_supplier_material_references,
    check_facility_inventory_managed_references, check_facility_operation_inventory_consistency,
    check_customer_product_references, check_nodes_registration_completeness,
    check_edge_material_references, check_operation_resource_references,
    check_edge_node_references, check_nodes_names_exist, check_material_name_uniqueness,
    check_operation_io_material_references, check_duplicate_names_within_sections,
    check_duplicate_edges, check_supplier_facility_name_collision,
    check_nodes_customer_registration, check_edge_destination_material_consistency,
    check_material_has_inventory_entry, check_facility_duplicate_names_disjoint_output,
    check_customer_duplicate_names_disjoint_product,
    check_no_self_loop_edges,
)
from verification_layer3 import (
    check_raw_material_has_supplier, check_at_least_one_raw_material,
    check_at_least_one_product, check_product_has_customer,
    check_intermediate_material_is_producible, check_product_is_producible,
    check_raw_material_is_consumed, check_supplier_has_outbound_edge,
    check_facility_has_inbound_and_outbound_edge, check_customer_has_inbound_edge,
    check_product_end_to_end_path, check_horizon_sanity,
    check_manufacturing_facility_material_stage_span,
)


def run_all_verifications(config: dict) -> list:
    """
    Runs every check across all three layers and returns ALL issues,
    both BLOCKING and WARNING. Callers decide how to use severity --
    the repair loop only acts on BLOCKING (warnings never block progress
    or a save), but WARNINGs must still reach the user somehow, or a
    real signal (e.g. check_horizon_sanity flagging an unreasonably
    short simulation.horizon) silently vanishes with no indication it
    was ever found.
    """
    issues = (
        check_field_requirements(config)
        + check_intermediate_bom_references(config)
        + check_product_bom_references(config)
        + check_inventory_type_consistency(config)
        + check_supplier_material_references(config)
        + check_facility_inventory_managed_references(config)
        + check_facility_operation_inventory_consistency(config)
        + check_customer_product_references(config)
        + check_nodes_registration_completeness(config)
        + check_edge_material_references(config)
        + check_operation_resource_references(config)
        + check_edge_node_references(config)
        + check_nodes_names_exist(config)
        + check_material_name_uniqueness(config)
        + check_operation_io_material_references(config)
        + check_duplicate_names_within_sections(config)
        + check_duplicate_edges(config)
        + check_supplier_facility_name_collision(config)
        + check_nodes_customer_registration(config)
        + check_edge_destination_material_consistency(config)
        + check_material_has_inventory_entry(config)
        + check_facility_duplicate_names_disjoint_output(config)
        + check_customer_duplicate_names_disjoint_product(config)
        + check_no_self_loop_edges(config)
        + check_raw_material_has_supplier(config)
        + check_at_least_one_raw_material(config)
        + check_at_least_one_product(config)
        + check_product_has_customer(config)
        + check_intermediate_material_is_producible(config)
        + check_product_is_producible(config)
        + check_raw_material_is_consumed(config)
        + check_supplier_has_outbound_edge(config)
        + check_facility_has_inbound_and_outbound_edge(config)
        + check_customer_has_inbound_edge(config)
        + check_product_end_to_end_path(config)
        + check_horizon_sanity(config)
        + check_manufacturing_facility_material_stage_span(config)
    )
    return issues


def run_all_blocking_verifications(config: dict) -> list:
    """BLOCKING-only subset -- what the repair loop actually acts on."""
    return [i for i in run_all_verifications(config) if i.severity == Severity.BLOCKING]


def get_current_warnings(config: dict) -> list:
    """WARNING-only subset -- surfaced to the user, never auto-repaired."""
    return [i for i in run_all_verifications(config) if i.severity == Severity.WARNING]


def _issue_signature(issue) -> tuple:
    """A stable key identifying 'the same issue' across verification
    passes, used to track which issues have been confirmed unrepairable
    so the loop doesn't retry them forever."""
    return (issue.location, issue.defect_type)


# Repair order follows the schema's own dependency chain -- raw materials
# before products before inventory before supplier before facility before
# customer before nodes/edges before simulation -- NOT the order issues
# happen to fall in Layer1-then-Layer2-then-Layer3 concatenation. Without
# this, a leaf-level Layer1 field (e.g. customer.shortage_policy) always
# got resolved before a foundational Layer3 issue (e.g. "no raw materials
# declared at all"), even though logically nothing about customers should
# be settled before the materials/products they're ordering even exist.
SECTION_PRIORITY_ORDER = [
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
    "simulation",
]


def _extract_section(location: str) -> str:
    """
    Extracts the leading section name from a ValidationIssue.location
    string. Handles cross-section locations (e.g. "raw_materials/products"
    from the material-category-collision check, or "supplier/facility"
    from the name-collision check) by taking the FIRST section named --
    a reasonable default since these checks always involve exactly two
    sections and the first one is as good an anchor as any.
    """
    first_part = location.split("/")[0]
    section = re.split(r"[.\[]", first_part)[0]
    return section


def _section_priority(issue) -> int:
    """Lower = higher priority. Unrecognized sections sort last, never
    block anything, never crash on an unexpected location shape."""
    section = _extract_section(issue.location)
    try:
        return SECTION_PRIORITY_ORDER.index(section)
    except ValueError:
        return len(SECTION_PRIORITY_ORDER)


def run_repair_loop(config: dict, max_iterations: int = 200, verbose: bool = True, description: str = "") -> tuple:
    """
    The core loop: verify -> pick the first non-skipped BLOCKING issue ->
    repair it -> re-verify from scratch -> repeat. An issue that
    dispatch_repair can't handle is added to a skip set (so the loop
    doesn't retry it every pass) and the loop moves on to try other
    issues, maximizing how much gets fixed automatically.

    IMPORTANT: the skip set is cleared every time ANY repair succeeds,
    not just at the end. This matters because "unrepairable right now"
    is often ORDER-DEPENDENT, not permanent -- e.g. products[0].bom can't
    be built while raw_materials is empty, gets skipped, but becomes
    fixable two steps later once check 2 creates a raw material. Without
    clearing the skip set on progress, that issue would be skipped for
    the rest of the run even after its actual blocker was resolved --
    exactly this happened in an earlier real run before this fix.

    Returns (config, unresolved_issues) -- unresolved_issues is empty if
    everything got fixed, otherwise it's whatever's left (each either
    unrepairable by any current action, or the loop hit max_iterations).
    """

    set_repair_context(description)

    permanent_skip = set()
    temporary_skip = set()

    for iteration in range(max_iterations):
        issues = run_all_blocking_verifications(config)

        actionable = [
            i for i in issues
            if _issue_signature(i) not in permanent_skip
            and _issue_signature(i) not in temporary_skip
        ]

        if not actionable:
            if verbose:
                if issues:
                    print(f"\nStopping: {len(issues)} issue(s) remain, none repairable by a current action.")
                else:
                    print("\nAll blocking issues resolved.")
            return config, issues

        # Sort by schema section order (raw_materials -> ... -> simulation),
        # not by which verification layer happened to find it. Python's
        # sort is stable, so within the same section, the original
        # Layer1-then-Layer2-then-Layer3 order still breaks ties.
        actionable.sort(key=_section_priority)

        issue = actionable[0]
        # if verbose:
        #     print(f"\n--- Repairing ({iteration + 1}): {issue} ---")

        try:
            success = dispatch_repair(config, issue)
            raised = False
        except Exception as e:
            if verbose:
                print(f"  Repair action raised an error: {e}")
            success = False
            raised = True

        if success:
            temporary_skip.clear()
        else:
            if raised:
                # A matching repair action EXISTS and was attempted, but
                # its own live-condition check failed (e.g. "no supplier
                # produces this material yet") -- this can genuinely
                # become fixable once something else in the config
                # changes, so it's worth retrying after any other
                # progress elsewhere.
                if verbose:
                    print(f"  No repair action available -- skipping, will not retry this one.")
                temporary_skip.add(_issue_signature(issue))
            else:
                # dispatch_repair returned False WITHOUT raising at all --
                # there is NO matching repair action registered for this
                # location/defect_type pattern, period. This can never
                # succeed no matter what else happens in the config, so
                # retrying it after every unrelated success (as the old
                # blanket "clear on any progress" logic did) just wastes
                # the iteration budget -- a real, observed failure mode:
                # one config burned 32 of its 60 iterations re-attempting
                # two structurally-unfixable issues, starving 33 other
                # issues that were never even attempted once.
                if verbose:
                    print(f"  No repair action available -- skipping, will not retry this one.")
                permanent_skip.add(_issue_signature(issue))

    if verbose:
        print(f"\nStopping: reached max_iterations ({max_iterations}) without full resolution.")
    return config, run_all_blocking_verifications(config)


# ============================================================
# Output formatting -- deterministic ordering for readability.
# Semantics are unchanged; this is purely presentational, applied
# after run_repair_loop finishes, before saving the final config.
# ============================================================

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


def _sort_section(section_name: str, items: list) -> list:
    if not isinstance(items, list):
        return items

    if section_name in {
        "raw_materials", "intermediate_materials", "products",
        "inventory", "supplier", "resource", "customer",
    }:
        return sorted(items, key=lambda x: x.get("name", "") if isinstance(x, dict) else "")

    if section_name == "facility":
        return sorted(
            items,
            key=lambda x: (
                x.get("name", ""), (x.get("operation") or {}).get("name", "")
            ) if isinstance(x, dict) else ("", "")
        )

    if section_name == "edges":
        return sorted(
            items,
            key=lambda x: (
                x.get("source", ""), x.get("destination", ""), x.get("material_name", "")
            ) if isinstance(x, dict) else ("", "", "")
        )

    return items


def canonicalize_config(config: dict) -> dict:
    """
    Reorders the config's top-level keys into a fixed, deterministic
    order and sorts each section's entries. Semantics are unchanged;
    this is purely presentational.
    """
    new_cfg = {}

    for key in CANONICAL_TOP_LEVEL_ORDER:
        if key not in config:
            continue
        val = config[key]
        if isinstance(val, list):
            new_cfg[key] = _sort_section(key, val)
        else:
            new_cfg[key] = val

    for key, val in config.items():
        if key not in new_cfg:
            new_cfg[key] = val

    return new_cfg


def deep_sort(obj, *, _is_root=True):
    """
    Recursively sorts the config for readability:
      - Root dict preserves key order (expected to already be
        canonicalize_config's output)
      - Nested dicts sorted alphabetically by key
      - Lists of dicts (all having a "name" key) sorted by name
      - Lists of plain strings sorted alphabetically
      - Everything else left as-is
    """
    if isinstance(obj, dict):
        items = obj.items()
        if not _is_root:
            items = sorted(items, key=lambda kv: kv[0])
        return {k: deep_sort(v, _is_root=False) for k, v in items}

    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) and "name" in x for x in obj):
            return [
                deep_sort(x, _is_root=False)
                for x in sorted(obj, key=lambda d: d.get("name", ""))
            ]
        if obj and all(isinstance(x, str) for x in obj):
            return sorted(obj)
        return [deep_sort(x, _is_root=False) for x in obj]

    return obj


if __name__ == "__main__":
    import json
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "test_config.json"
    with open(path) as f:
        cfg = json.load(f)

    initial_issues = run_all_blocking_verifications(cfg)
    print(f"Starting: {len(initial_issues)} blocking issue(s) found.")

    cfg, remaining = run_repair_loop(cfg, verbose=True)

    print()
    if remaining:
        print(f"{len(remaining)} blocking issue(s) could not be fully resolved:")
        for issue in remaining:
            print("   ", issue)
    else:
        print("Config is fully clean of blocking issues across all three verification layers.")

    warnings = get_current_warnings(cfg)
    print()
    if warnings:
        print(f"{len(warnings)} warning(s) found (not auto-fixed -- review and decide manually):")
        for w in warnings:
            print("   ", w)
    else:
        print("No warnings.")

    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"\nSaved to {path}")    