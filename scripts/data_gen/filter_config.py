"""
scripts/data_gen/filter_config.py
-----------------------------------
Field relevance rules and filter function.

Takes a fully-populated JSON config and returns a filtered version
containing only relevant fields based on conditions.

The filtered JSON is used for NL generation only.
The full JSON is always used as the training target.

Usage:
    from data_gen.filter_config import filter_config
    filtered = filter_config(full_config)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from copy import deepcopy

# ============================================================
# Distribution parameter relevance
# ============================================================

DISTRIBUTION_PARAMS = {
    "constant":    ["a"],
    "exponential": ["a"],
    "poisson":     ["a"],
    "uniform":     ["a", "b"],
    "normal":      ["a", "b"],
    "triangular":  ["a", "b", "c"],
    "weibull":     ["a", "b"],
    "beta":        ["a", "b"],
}


def filter_distribution(block: Dict[str, Any]) -> Dict[str, Any]:
    """
    Keep only required parameters for the given distribution type.
    d and e are never included.
    """
    if not isinstance(block, dict):
        return block

    dist   = block.get("distribution", "constant")
    params = block.get("parameters", {})

    required = DISTRIBUTION_PARAMS.get(dist, ["a"])
    filtered_params = {
        k: v for k, v in params.items()
        if k in required
    }

    return {
        "distribution": dist,
        "parameters":   filtered_params,
    }


def is_zero_constant(block: Dict[str, Any]) -> bool:
    """
    Returns True if block is constant distribution with a=0.
    Used to decide whether transfer_time is relevant.
    """
    if not isinstance(block, dict):
        return True
    dist = block.get("distribution", "constant")
    a    = block.get("parameters", {}).get("a", 0)
    return dist == "constant" and a == 0


# ============================================================
# Section filters
# ============================================================

def filter_config_info(cfg: Dict) -> None:
    """config_info is NEVER included."""
    cfg.pop("config_info", None)


def filter_raw_materials(raw: List[Dict]) -> List[Dict]:
    """Only name is always relevant."""
    return [{"name": r["name"]} for r in raw]


def filter_intermediate_materials(
    inter: List[Dict],
    has_intermediate: bool,
) -> List[Dict]:
    """Entire section conditional on has_intermediate."""
    if not has_intermediate:
        return []
    return [{"name": i["name"], "bom": i["bom"]} for i in inter]


def filter_products(products: List[Dict]) -> List[Dict]:
    """name and bom always included."""
    return [{"name": p["name"], "bom": p["bom"]} for p in products]


def filter_inventory(inventory: List[Dict]) -> List[Dict]:
    """
    Conditions:
    - procurement_scheme     → is_raw_material
    - procurement_arrival    → is_raw_material AND is_periodic_supply
    - initial_inventory      → initial_inventory > 0
    - holding_cost           → holding_cost > 0
    - shortage_cost          → shortage_cost > 0
    - review_time            → holding_cost > 0 OR shortage_cost > 0
    """
    result = []
    for item in inventory:
        inv_type  = item.get("type", "")
        is_raw    = inv_type == "raw_materials"
        costs     = item.get("inventory_costs", {})
        holding   = costs.get("holding_cost", 0)
        shortage  = costs.get("shortage_cost", 0)
        review    = costs.get("review_time", 0)
        init_inv  = item.get("initial_inventory", 0)

        filtered: Dict[str, Any] = {"name": item["name"], "type": inv_type}

        # ── procurement scheme ─────────────────────────────
        if is_raw:
            ps        = item.get("procurement_scheme", {})
            proc_type = ps.get("type", "")

            if proc_type == "demand_driven":
                filtered["procurement_scheme"] = {
                    "type": proc_type,
                }

            elif proc_type == "inventory_threshold":
                # (s, S) policy — a=small s, b=large S
                # no distribution needed, only a and b parameters
                params = ps.get("parameters", {})
                filtered["procurement_scheme"] = {
                    "type": proc_type,
                    "parameters": {
                        "a": params.get("a", 0),  # small s
                        "b": params.get("b", 0),  # large S
                    },
                }

            else:  # periodic_supply
                filtered["procurement_scheme"] = {
                    "type":         proc_type,
                    "distribution": ps.get("distribution", ""),
                    "parameters":   filter_distribution({
                        "distribution": ps.get("distribution", "constant"),
                        "parameters":   ps.get("parameters", {}),
                    })["parameters"],
                }

                # procurement_arrival only if periodic_supply
                pa = item.get("procurement_arrival", {})
                filtered["procurement_arrival"] = filter_distribution(pa)

        # ── initial inventory ──────────────────────────────
        if init_inv > 0:
            filtered["initial_inventory"] = init_inv

        # ── inventory costs ────────────────────────────────
        inv_costs: Dict[str, Any] = {}
        if holding > 0:
            inv_costs["holding_cost"] = holding
        if shortage > 0:
            inv_costs["shortage_cost"] = shortage
        if (holding > 0 or shortage > 0) and review > 0:
            inv_costs["review_time"] = review

        if inv_costs:
            filtered["inventory_costs"] = inv_costs

        result.append(filtered)
    return result


def filter_suppliers(suppliers: List[Dict]) -> List[Dict]:
    """
    Conditions:
    - supplier_capacity          → supplier_capacity > 0
    - supplier_cost              → ALWAYS
    - supplier_payment_lead_time → ALWAYS
    """
    result = []
    for s in suppliers:
        cap = s.get("supplier_capacity", 0)

        filtered: Dict[str, Any] = {
            "name":                 s["name"],
            "supply_material_name": s["supply_material_name"],
            "supplier_lead_time":   filter_distribution(
                s.get("supplier_lead_time", {})),
            "supplier_cost":        s.get("supplier_cost", 0),
            "supplier_payment_lead_time": filter_distribution(
                s.get("supplier_payment_lead_time", {})),
        }

        if cap > 0:
            filtered["supplier_capacity"] = cap

        result.append(filtered)
    return result


def filter_resources(
    resources: List[Dict],
    has_resource: bool,
) -> List[Dict]:
    """
    Entire section conditional on has_resource.
    batching   → has_resource AND batching.enabled
    failure    → has_resource AND failure.enabled
    op_cost    → has_resource AND operating_cost_per_time > 0
    capacity   → ALWAYS if has_resource
    """
    if not has_resource:
        return []

    result = []
    for r in resources:
        batching    = r.get("batching", {})
        failure     = r.get("failure", {})
        op_cost     = r.get("operating_cost_per_time", 0)
        batching_on = batching.get("enabled", False)
        failure_on  = failure.get("enabled", False)

        filtered: Dict[str, Any] = {
            "name":         r["name"],
            "capacity":     r.get("capacity", 1),
            "service_time": filter_distribution(r.get("service_time", {})),
        }

        if batching_on:
            filtered["batching"] = {
                "enabled":       True,
                "batch_size":    batching.get("batch_size", 0),
                "max_wait_time": batching.get("max_wait_time", 0),
            }

        if failure_on:
            filtered["failure"] = {
                "enabled":  True,
                "uptime":   filter_distribution(failure.get("uptime", {})),
                "downtime": filter_distribution(failure.get("downtime", {})),
            }

        if op_cost > 0:
            filtered["operating_cost_per_time"] = op_cost

        result.append(filtered)
    return result


def filter_facilities(
    facilities: List[Dict],
    has_resource: bool,
) -> List[Dict]:
    """
    facility.name and facility.type ALWAYS included.
    Operation fields only if manufacturing.
    resource_required only if has_resource.
    """
    result = []
    for fac in facilities:
        ftype = fac.get("type", "")

        filtered: Dict[str, Any] = {
            "name": fac["name"],
            "type": ftype,
        }

        if ftype == "manufacturing":
            op = fac.get("operation", {})

            filtered["inventory_managed"] = fac.get(
                "inventory_managed", [])

            operation: Dict[str, Any] = {
                "name":            op.get("name", ""),
                "input":           op.get("input", []),
                "output":          op.get("output", []),
                "operation_cycle": filter_distribution(
                    op.get("operation_cycle", {})),
            }

            if has_resource:
                operation["resource_required"] = op.get(
                    "resource_required", "")

            filtered["operation"] = operation

        result.append(filtered)
    return result


def filter_customers(customers: List[Dict]) -> List[Dict]:
    """All customer fields ALWAYS included."""
    result = []
    for c in customers:
        result.append({
            "name":             c["name"],
            "product":          c["product"],
            "arrival_time":     filter_distribution(c.get("arrival_time", {})),
            "demand":           filter_distribution(c.get("demand", {})),
            "customer_lead_time": filter_distribution(
                c.get("customer_lead_time", {})),
            "shortage_policy":  c.get("shortage_policy", ""),
            "unit_selling_price": c.get("unit_selling_price", 0),
            "customer_payment_lead_time": filter_distribution(
                c.get("customer_payment_lead_time", {})),
        })
    return result


def filter_nodes(nodes: List[Dict]) -> List[Dict]:
    """nodes ALWAYS included."""
    return deepcopy(nodes)


def filter_edges(edges: List[Dict]) -> List[Dict]:
    """
    transfer_time only if NOT (constant AND a==0).
    """
    result = []
    for e in edges:
        tt = e.get("transfer_time", {})

        filtered: Dict[str, Any] = {
            "source":        e["source"],
            "destination":   e["destination"],
            "material_type": e["material_type"],
            "material_name": e["material_name"],
        }

        if not is_zero_constant(tt):
            filtered["transfer_time"] = filter_distribution(tt)

        result.append(filtered)
    return result


def filter_simulation(sim: Dict) -> Dict:
    """
    warm_up only if > 0.
    random_seed NEVER included.
    """
    filtered: Dict[str, Any] = {
        "time_unit":   sim.get("time_unit", "day"),
        "horizon":     sim.get("horizon", 365),
        "replications": sim.get("replications", 10),
    }

    warm_up = sim.get("warm_up", 0)
    if warm_up > 0:
        filtered["warm_up"] = warm_up

    return filtered


# ============================================================
# Main filter function
# ============================================================

def filter_config(full_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Takes a fully-populated JSON config and returns a filtered
    version containing only relevant fields.

    The filtered config is used for NL generation only.
    The full config is always used as the training target.

    Parameters
    ----------
    full_config : dict
        Fully populated JSON config from json_generator.py

    Returns
    -------
    dict
        Filtered config with only relevant fields
    """
    cfg = deepcopy(full_config)

    # ── evaluate conditions ────────────────────────────────
    has_resource     = len(cfg.get("resource", [])) > 0
    has_intermediate = len(cfg.get("intermediate_materials", [])) > 0

    # ── build filtered config ──────────────────────────────
    filtered: Dict[str, Any] = {}

    # config_info — NEVER
    # filtered["config_info"] — omitted

    filtered["raw_materials"] = filter_raw_materials(
        cfg.get("raw_materials", []))

    filtered["intermediate_materials"] = filter_intermediate_materials(
        cfg.get("intermediate_materials", []),
        has_intermediate=has_intermediate,
    )

    filtered["products"] = filter_products(
        cfg.get("products", []))

    filtered["inventory"] = filter_inventory(
        cfg.get("inventory", []))

    filtered["supplier"] = filter_suppliers(
        cfg.get("supplier", []))

    filtered["resource"] = filter_resources(
        cfg.get("resource", []),
        has_resource=has_resource,
    )

    filtered["facility"] = filter_facilities(
        cfg.get("facility", []),
        has_resource=has_resource,
    )

    filtered["customer"] = filter_customers(
        cfg.get("customer", []))

    filtered["nodes"] = filter_nodes(
        cfg.get("nodes", []))

    filtered["edges"] = filter_edges(
        cfg.get("edges", []))

    filtered["simulation"] = filter_simulation(
        cfg.get("simulation", {}))

    return filtered


# ============================================================
# CLI — test filter on a single config
# ============================================================

if __name__ == "__main__":
    import json
    import argparse

    parser = argparse.ArgumentParser(
        description="Filter a fully-populated JSON config."
    )
    parser.add_argument(
        "input_file",
        help="Path to fully-populated JSON config file"
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Path to save filtered config (default: auto-saved next to input file)"
    )
    args = parser.parse_args()

    input_path = Path(args.input_file)

    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    # handle both raw config and config_populate.py output format
    cfg = data.get("config", data)

    filtered = filter_config(cfg)

    # ── auto-save next to input file ──────────────────────
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.parent / (input_path.stem + "_filtered.json")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(filtered, f, indent=2)

    # ── always print to terminal ───────────────────────────
    print(json.dumps(filtered, indent=2))
    print(f"\n  ✓ Filtered config saved → {output_path}")