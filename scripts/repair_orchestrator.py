"""
scripts/repair_orchestrator.py
--------------------------------
The verify -> repair -> re-verify LOOP. Kept separate from repair.py by
design: repair.py is a library of individual repair actions (pure
functions, no looping, no re-verification calls); this file owns the
control flow that runs verification, dispatches each found issue to
repair.py's dispatch_repair(), and re-verifies from scratch before moving
to the next issue.

Why re-verify after EVERY single repair, not once per batch: a single
verify-once-repair-all pass can act on a STALE issue list -- fixing one
issue can silently resolve (or newly reveal) another as a side effect
(e.g. registering a node in nodes[0] can un-block an edge reference that
was flagged only because the registration hadn't happened yet). Re-running
full verification after each individual repair keeps every decision based
on current, accurate state -- at some cost in repeated verification work,
which is a fine trade for correctness here.

[BATCH] Exception: Layer1 issues. When question_scheduler.BATCH_QUESTIONS
is True, Layer1 repairs that need no input are applied first (then
verification re-runs), and after that all Layer1 issues that don't depend on another open issue are
resolved together as one ROUND, then verification runs once. This is safe
because Layer1 conditions are strictly local (a field's requirement only
reads siblings in the same entry), and question_scheduler defers any issue
whose gate is still open. Within a round, question_scheduler.is_still_open
re-checks Layer1 before each repair, so nothing already fixed by an
earlier answer is asked again. Layer2/Layer3 issues keep the original
one-at-a-time path below, which runs once Layer1 has nothing left to
batch. Each round's questions are printed together first, then asked one
by one (the terminal can only take one answer at a time). See
question_scheduler.py for the full rationale.

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
from repair import dispatch_repair, set_repair_context   # [CLEANUP] duplicate import line removed
from repair import describe_location                      # [PLACEHOLDER] readable labels for stripped fields
import question_scheduler as qs                           # [BATCH] new import
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
#
# [BATCH] With BATCH_QUESTIONS on, all Layer1 rounds run before any
# Layer2/Layer3 repair, and this order is used WITHIN each round (and for
# the Layer2/Layer3 path afterwards). Layer1-first is deliberate: Layer2
# reference checks read names, so a name still set to "missing" produces
# spurious dangling-reference issues until Layer1 is clean.
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


def _attempt_repair(config: dict, issue, permanent_skip: set, temporary_skip: set, verbose: bool) -> bool:
    """
    [BATCH] Extracted from run_repair_loop so the batched Layer1 path and
    the original one-at-a-time path share exactly the same dispatch and
    skip-tracking behavior. The logic is unchanged from the original loop
    body except for the corrected message in the `raised` branch.
    """
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
                # [CLEANUP] message corrected: this branch DOES retry later
                print(f"  Repair for {issue.location} could not complete right now -- "
                      f"will retry after other progress.")   # [CLEANUP-2] names the issue
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
                print(f"  No repair action available for {issue.location} -- "
                      f"skipping, will not retry this one.")   # [CLEANUP-2] names the issue
            permanent_skip.add(_issue_signature(issue))

    return success


# ============================================================
# [PLACEHOLDER] Optional-field placeholder cleanup
# ============================================================
# An OPTIONAL field holding the "missing" placeholder is only a WARNING in
# verification_layer1, so the repair loop never touches it -- but the
# simulation engine can't always read the string "missing" (observed:
# float("missing") on resource.operating_cost_per_time crashed
# simulate.py). For fields where the engine already has its own default
# when the KEY IS ABSENT, the safe fix is to delete the placeholder key,
# so the engine's default applies and the engine itself stays untouched.
#
# ONLY fields confirmed in simulate.py to have an absent-key default
# belong here. The value is a short description of that default, shown to
# the user when the placeholder is removed. To add a field: find where
# simulate.py reads it, confirm it uses r.get(key, <default>) (or
# equivalent) and does NOT special-case the "missing" string, then add
# its normalized path (no list indices) here.
OPTIONAL_PLACEHOLDER_DEFAULTS = {
    # simulate.py _build_once: float(r.get("operating_cost_per_time", 0.0))
    # -- "missing" crashes float().
    "resource.operating_cost_per_time": "0 (no operating cost)",

    # [PLACEHOLDER-2] Confirmed against simulate.py _build_once /
    # normalize_config. For each, the absent key gives the default below,
    # while the "missing" string either crashes (.get on a str) or is read
    # as truthy (bool("missing") is True), silently switching the feature ON.
    # r.get("batching") or {"enabled": False}
    "resource.batching": "batching off",
    # bool(batch_spec.get("enabled", False))
    "resource.batching.enabled": "batching off",
    # r.get("failure", {}) or {}
    "resource.failure": "no failures modelled",
    # failure.get("enabled") -- "missing" would be read as enabled
    "resource.failure.enabled": "no failures modelled",
    # item.get("inventory_costs", {}) or {} -- then holding/shortage cost 0,
    # review_time 1 (normalize_config already handles "missing" INSIDE it)
    "inventory.inventory_costs": "no holding or shortage cost, review time 1",
}

MISSING_PLACEHOLDER = "missing"


def strip_optional_placeholders(config: dict, verbose: bool = True) -> list:
    """
    Deletes every key whose value is the "missing" placeholder AND whose
    normalized path is listed in OPTIONAL_PLACEHOLDER_DEFAULTS. Returns
    the list of locations removed. Never touches required fields (they
    are not in the table) and never raises on an unexpected shape.
    """
    removed = []

    def _walk(obj: dict, loc: str, normalized: str):
        for key in list(obj.keys()):
            value = obj[key]
            child_loc = f"{loc}.{key}"
            child_norm = f"{normalized}.{key}"
            if value == MISSING_PLACEHOLDER and child_norm in OPTIONAL_PLACEHOLDER_DEFAULTS:
                try:
                    label = describe_location(config, child_loc)
                except Exception:
                    label = child_loc
                del obj[key]
                removed.append(child_loc)
                if verbose:
                    print(f"  {label}: not given -- using the simulation default of "
                          f"{OPTIONAL_PLACEHOLDER_DEFAULTS[child_norm]}.")
            elif isinstance(value, dict):
                _walk(value, child_loc, child_norm)

    if not isinstance(config, dict):
        return removed

    for section, content in config.items():
        if isinstance(content, list):
            for idx, entry in enumerate(content):
                if isinstance(entry, dict):
                    _walk(entry, f"{section}[{idx}]", section)
        elif isinstance(content, dict):
            _walk(content, section, section)

    return removed


def run_repair_loop(config: dict, max_iterations: int = 200, verbose: bool = True, description: str = "") -> tuple:
    """
    The core loop: verify -> pick the first non-skipped BLOCKING issue ->
    repair it -> re-verify from scratch -> repeat. An issue that
    dispatch_repair can't handle is added to a skip set (so the loop
    doesn't retry it every pass) and the loop moves on to try other
    issues, maximizing how much gets fixed automatically.

    [BATCH] When question_scheduler.BATCH_QUESTIONS is True, each
    iteration first applies any Layer1 repairs that need no input (then
    re-verifies); if there are none, it checks for a Layer1 round (every Layer1 BLOCKING
    issue not waiting on another open issue). If there is one, the whole
    round is resolved, then the loop re-verifies once. Only when no Layer1
    round is available does the iteration fall through to the original
    one-issue path below. Each round uses one iteration of max_iterations.

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
    [PLACEHOLDER] Before returning, optional fields still holding the
    "missing" placeholder are removed where the simulation engine has its
    own default (see strip_optional_placeholders).
    Round statistics for the run are in question_scheduler.LAST_RUN_STATS.
    """

    set_repair_context(description)
    qs.apply_phrasing_mode()   # [BATCH] LLM phrasing on/off per qs.USE_LLM_PHRASING
    qs.reset_stats()           # [BATCH]

    permanent_skip = set()
    temporary_skip = set()

    for iteration in range(max_iterations):
        issues = run_all_blocking_verifications(config)

        # ---- [BATCH] automatic fixes, then Layer1 round --------------
        if qs.BATCH_QUESTIONS:
            skipped = permanent_skip | temporary_skip

            # [BATCH-2] Repairs that need no input run first, then the loop
            # re-verifies. Otherwise an auto-correctable value (e.g. an
            # inventory.type of "raw_materials") holds back its dependents
            # for a whole extra round although nobody had to answer anything.
            auto = qs.select_auto_fixes(config, issues, skipped)
            if auto:
                if verbose:
                    print(f"\n=== Automatic fixes (no input needed) ===")   # [BATCH-3] count shown in final stats
                applied = 0
                for auto_issue in auto:
                    # [BATCH-3] One repair can fix several flagged issues at
                    # once (edges[i].transfer_time.distribution and
                    # .parameters.a are fixed by the same repair), so skip
                    # any that an earlier fix in this pass already resolved.
                    if not qs.is_still_open(config, auto_issue):
                        continue
                    _attempt_repair(config, auto_issue, permanent_skip, temporary_skip, verbose)
                    applied += 1
                qs.LAST_RUN_STATS["auto_fixed"] += applied
                continue  # re-verify before building the round

            batch, deferred = qs.select_round(issues, skipped)
            if batch:
                batch.sort(key=_section_priority)
                qs.LAST_RUN_STATS["rounds"] += 1
                if verbose:
                    print(f"\n=== Round {qs.LAST_RUN_STATS['rounds']}: {len(batch)} item(s) to resolve, "
                          f"{len(deferred)} waiting on these answers ===")
                    # [BATCH-2] Show every question of the round together
                    # before asking them one by one. In a UI, the same list
                    # (qs.describe_round) is shown and answered at once.
                    print(qs.format_round_preview(config, batch))
                    print("\n  Answer them one at a time below:")

                dispatched = 0
                for number, batch_issue in enumerate(batch, start=1):
                    if not qs.is_still_open(config, batch_issue):
                        qs.LAST_RUN_STATS["already_fixed_skipped"] += 1
                        if verbose:
                            print(f"\n  [Q{number} of {len(batch)}] already answered by an earlier question -- skipped")
                        continue
                    if verbose:
                        print(f"\n  [Q{number} of {len(batch)}]")   # [BATCH-2] ties each prompt to the preview
                    _attempt_repair(config, batch_issue, permanent_skip, temporary_skip, verbose)
                    dispatched += 1

                qs.LAST_RUN_STATS["repairs_per_round"].append(dispatched)
                qs.LAST_RUN_STATS["deferred_per_round"].append(len(deferred))
                continue  # re-verify once, then build the next round
        # ---- end [BATCH] ---------------------------------------------

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
            strip_optional_placeholders(config, verbose)   # [PLACEHOLDER]
            return config, issues

        # Sort by schema section order (raw_materials -> ... -> simulation),
        # not by which verification layer happened to find it. Python's
        # sort is stable, so within the same section, the original
        # Layer1-then-Layer2-then-Layer3 order still breaks ties.
        actionable.sort(key=_section_priority)

        issue = actionable[0]
        # if verbose:
        #     print(f"\n--- Repairing ({iteration + 1}): {issue} ---")

        if qs.BATCH_QUESTIONS:
            qs.LAST_RUN_STATS["one_at_a_time_repairs"] += 1   # [BATCH]

        _attempt_repair(config, issue, permanent_skip, temporary_skip, verbose)   # [BATCH] was inline

    if verbose:
        print(f"\nStopping: reached max_iterations ({max_iterations}) without full resolution.")
    strip_optional_placeholders(config, verbose)   # [PLACEHOLDER]
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

    if qs.BATCH_QUESTIONS:                     # [BATCH] run summary for evaluation
        print()
        print(qs.format_stats())

    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"\nSaved to {path}")