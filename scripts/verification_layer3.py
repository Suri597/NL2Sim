"""
scripts2/verification_layer3.py
---------------------------------
Layer3: supply chain FEASIBILITY checks.

Distinct from Layer1 (field presence/type) and Layer2 (referential
integrity -- does every reference point at something real): Layer3 asks
a different question entirely -- "even if every reference is valid and
every field is well-formed, can this supply chain actually EXECUTE?"

A config can pass Layer1 and Layer2 perfectly and still be infeasible --
e.g. a raw material with zero suppliers can never be replenished, so
every downstream process eventually stalls even though nothing is
"wrong" in the referential sense.

Conditions are added one at a time, by explicit instruction, and tested
before the next one is added.

Implemented so far:
    1. Every raw material must have at least one supplier.
    2. There must be at least one raw material declared.
    3. There must be at least one product declared.
    4. Every product must have at least one customer ordering it.
    5. Every intermediate material must be producible by some facility
       operation (appears in some operation.output).
    6. Every product must be producible by some facility operation
       (appears in some operation.output).
    7. Every raw material must actually be consumed somewhere (appears in
       some bom or some operation.input) -- the inverse concern of #1.
    8. Every supplier must have at least one outbound edge.
    9. Every facility must have at least one inbound AND at least one
       outbound edge.
    10. Every customer must have at least one inbound edge.
    11. For every product: its producing facility must be reachable (via
        the edges graph) from at least one supplier, AND must be able to
        reach at least one ordering customer. SCOPE NOTE: this checks
        structural graph connectivity only -- it does NOT trace specific
        material identity hop-by-hop through bom relationships (e.g. it
        doesn't verify the die specifically becomes the packaged IC). It
        catches disconnected sub-graphs, not "right shape, wrong material."
    12. (WARNING only, heuristic) simulation.horizon should comfortably
        exceed a rough lower-bound estimate of one full source-to-customer
        cycle time (supplier lead time + operation cycles + transfer
        times along one path), or the simulation may end before a single
        unit can complete the chain.
    13. A manufacturing facility's inventory_managed must span at least
        TWO distinct material stages (raw < intermediate < product) --
        otherwise there's no later-stage material for it to actually
        produce. A facility that manages only raw materials (or only
        intermediates, or only products) receives things but never
        makes anything -- structurally present, but functionally inert.
"""

from issue_types import ValidationIssue, DefectType, Severity

LAYER = "Layer3"


def _collect_names(config: dict, section: str) -> set:
    """Collect the set of 'name' values from a list-section, ignoring
    malformed entries (earlier layers' job to catch those, not ours)."""
    names = set()
    for entry in config.get(section, []) or []:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str):
                names.add(name)
    return names


def check_raw_material_has_supplier(config: dict) -> list[ValidationIssue]:
    """
    Every raw_materials[].name must be sourced by at least one
    supplier[].supply_material_name. A raw material with zero suppliers
    can never be replenished -- inventory starts at whatever
    initial_inventory says and only ever depletes, so the supply chain
    is guaranteed to stall out for that material.
    """
    issues: list[ValidationIssue] = []

    supplied_materials = set()
    for entry in config.get("supplier", []) or []:
        if isinstance(entry, dict):
            name = entry.get("supply_material_name")
            if isinstance(name, str):
                supplied_materials.add(name)

    for idx, entry in enumerate(config.get("raw_materials", []) or []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue

        if name not in supplied_materials:
            issues.append(ValidationIssue(
                layer=LAYER,
                location=f"raw_materials[{idx}]",
                defect_type=DefectType.INCONSISTENT_CROSS_FIELD,
                severity=Severity.BLOCKING,
                detail=f"raw material '{name}' has no supplier -- it can never be "
                       f"replenished once initial inventory is depleted.",
                context={"referenced_name": name, "entry_index": idx},
            ))

    return issues


def check_at_least_one_raw_material(config: dict) -> list[ValidationIssue]:
    """
    The supply chain must declare at least one raw material -- without
    any raw input, nothing can ever be produced.
    """
    issues: list[ValidationIssue] = []

    raw_materials = config.get("raw_materials", []) or []
    if len(raw_materials) == 0:
        issues.append(ValidationIssue(
            layer=LAYER,
            location="raw_materials",
            defect_type=DefectType.MISSING_REQUIRED_VALUE,
            severity=Severity.BLOCKING,
            detail="No raw materials declared -- a supply chain needs at least "
                   "one raw input to produce anything.",
        ))

    return issues


def check_at_least_one_product(config: dict) -> list[ValidationIssue]:
    """
    The supply chain must declare at least one finished product -- without
    a product, there is nothing for the supply chain to ultimately deliver.
    """
    issues: list[ValidationIssue] = []

    products = config.get("products", []) or []
    if len(products) == 0:
        issues.append(ValidationIssue(
            layer=LAYER,
            location="products",
            defect_type=DefectType.MISSING_REQUIRED_VALUE,
            severity=Severity.BLOCKING,
            detail="No products declared -- a supply chain needs at least one "
                   "finished product to deliver to customers.",
        ))

    return issues


def check_product_has_customer(config: dict) -> list[ValidationIssue]:
    """
    Every products[].name must be ordered by at least one customer[].product.
    A product with zero customers is produced for nothing -- it will
    accumulate in inventory forever with no demand to consume it, and the
    revenue/demand side of the simulation for that product is meaningless.
    """
    issues: list[ValidationIssue] = []

    ordered_products = set()
    for entry in config.get("customer", []) or []:
        if isinstance(entry, dict):
            product = entry.get("product")
            if isinstance(product, str):
                ordered_products.add(product)

    for idx, entry in enumerate(config.get("products", []) or []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue

        if name not in ordered_products:
            issues.append(ValidationIssue(
                layer=LAYER,
                location=f"products[{idx}]",
                defect_type=DefectType.INCONSISTENT_CROSS_FIELD,
                severity=Severity.BLOCKING,
                detail=f"product '{name}' has no customer ordering it -- it will "
                       f"be produced with no demand to consume it.",
                context={"referenced_name": name, "entry_index": idx},
            ))

    return issues


def _collect_operation_outputs(config: dict) -> set:
    """All material names that appear in SOME manufacturing facility's operation.output."""
    outputs = set()
    for entry in config.get("facility", []) or []:
        if not isinstance(entry, dict):
            continue
        operation = entry.get("operation")
        if not isinstance(operation, dict):
            continue
        for name in operation.get("output") or []:
            if isinstance(name, str):
                outputs.add(name)
    return outputs


def _collect_operation_inputs(config: dict) -> set:
    """All material names that appear in SOME manufacturing facility's operation.input."""
    inputs = set()
    for entry in config.get("facility", []) or []:
        if not isinstance(entry, dict):
            continue
        operation = entry.get("operation")
        if not isinstance(operation, dict):
            continue
        for name in operation.get("input") or []:
            if isinstance(name, str):
                inputs.add(name)
    return inputs


def _collect_all_bom_keys(config: dict) -> set:
    """All material names referenced as a key in ANY bom (intermediate or product)."""
    keys = set()
    for section in ("intermediate_materials", "products"):
        for entry in config.get(section, []) or []:
            if not isinstance(entry, dict):
                continue
            bom = entry.get("bom")
            if isinstance(bom, dict):
                keys |= set(k for k in bom.keys() if isinstance(k, str))
    return keys


def check_intermediate_material_is_producible(config: dict) -> list[ValidationIssue]:
    """
    Every intermediate_materials[].name must appear in some facility's
    operation.output -- otherwise it can never come into existence, and
    anything downstream that depends on it (via bom) is unbuildable.
    """
    issues: list[ValidationIssue] = []
    producible = _collect_operation_outputs(config)

    for idx, entry in enumerate(config.get("intermediate_materials", []) or []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue

        if name not in producible:
            issues.append(ValidationIssue(
                layer=LAYER,
                location=f"intermediate_materials[{idx}]",
                defect_type=DefectType.INCONSISTENT_CROSS_FIELD,
                severity=Severity.BLOCKING,
                detail=f"intermediate material '{name}' is not produced by any facility "
                       f"operation -- it can never come into existence.",
                context={"referenced_name": name, "entry_index": idx},
            ))

    return issues


def check_product_is_producible(config: dict) -> list[ValidationIssue]:
    """
    Every products[].name must appear in some facility's operation.output
    -- otherwise, even though check_product_has_customer confirms demand
    exists, that demand can never be fulfilled because nothing makes it.
    """
    issues: list[ValidationIssue] = []
    producible = _collect_operation_outputs(config)

    for idx, entry in enumerate(config.get("products", []) or []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue

        if name not in producible:
            issues.append(ValidationIssue(
                layer=LAYER,
                location=f"products[{idx}]",
                defect_type=DefectType.INCONSISTENT_CROSS_FIELD,
                severity=Severity.BLOCKING,
                detail=f"product '{name}' is not produced by any facility operation -- "
                       f"customer demand for it can never be fulfilled.",
                context={"referenced_name": name, "entry_index": idx},
            ))

    return issues


def check_raw_material_is_consumed(config: dict) -> list[ValidationIssue]:
    """
    Every raw_materials[].name should actually be consumed somewhere --
    either as a bom key (of an intermediate material or product) or as a
    facility operation.input. A raw material that's supplied but never
    consumed is procured for no reason (the inverse of check 1).
    """
    issues: list[ValidationIssue] = []

    consumed = _collect_all_bom_keys(config) | _collect_operation_inputs(config)

    for idx, entry in enumerate(config.get("raw_materials", []) or []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue

        if name not in consumed:
            issues.append(ValidationIssue(
                layer=LAYER,
                location=f"raw_materials[{idx}]",
                defect_type=DefectType.INCONSISTENT_CROSS_FIELD,
                severity=Severity.BLOCKING,
                detail=f"raw material '{name}' is never consumed (not in any bom or "
                       f"operation.input) -- it is procured for no reason.",
                context={"referenced_name": name, "entry_index": idx},
            ))

    return issues


def _edge_degree_sets(config: dict) -> tuple:
    """Returns (has_outbound, has_inbound) -- sets of names that appear as
    a source / destination respectively, at least once in edges[]."""
    has_outbound = set()
    has_inbound = set()
    for entry in config.get("edges", []) or []:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        destination = entry.get("destination")
        if isinstance(source, str):
            has_outbound.add(source)
        if isinstance(destination, str):
            has_inbound.add(destination)
    return has_outbound, has_inbound


def check_supplier_has_outbound_edge(config: dict) -> list[ValidationIssue]:
    """
    Every supplier[].name must appear as the source of at least one edge
    -- a supplier with zero outbound edges feeds nothing and can never
    actually deliver material anywhere.
    """
    issues: list[ValidationIssue] = []
    has_outbound, _ = _edge_degree_sets(config)

    for idx, entry in enumerate(config.get("supplier", []) or []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue

        if name not in has_outbound:
            issues.append(ValidationIssue(
                layer=LAYER,
                location=f"supplier[{idx}]",
                defect_type=DefectType.INCONSISTENT_CROSS_FIELD,
                severity=Severity.BLOCKING,
                detail=f"supplier '{name}' has no outbound edge -- it feeds nothing.",
                context={"referenced_name": name, "entry_index": idx},
            ))

    return issues


def check_facility_has_inbound_and_outbound_edge(config: dict) -> list[ValidationIssue]:
    """
    Every facility[].name must appear as BOTH a source and a destination
    of at least one edge -- a facility with no inbound edge receives no
    material to work with; a facility with no outbound edge has nowhere
    to send its output. Either way it can't participate in the chain.
    """
    issues: list[ValidationIssue] = []
    has_outbound, has_inbound = _edge_degree_sets(config)

    for idx, entry in enumerate(config.get("facility", []) or []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue

        if name not in has_inbound:
            issues.append(ValidationIssue(
                layer=LAYER,
                location=f"facility[{idx}]",
                defect_type=DefectType.INCONSISTENT_CROSS_FIELD,
                severity=Severity.BLOCKING,
                detail=f"facility '{name}' has no inbound edge -- it receives no material.",
                context={"referenced_name": name, "entry_index": idx},
            ))
        if name not in has_outbound:
            issues.append(ValidationIssue(
                layer=LAYER,
                location=f"facility[{idx}]",
                defect_type=DefectType.INCONSISTENT_CROSS_FIELD,
                severity=Severity.BLOCKING,
                detail=f"facility '{name}' has no outbound edge -- its output goes nowhere.",
                context={"referenced_name": name, "entry_index": idx},
            ))

    return issues


def check_customer_has_inbound_edge(config: dict) -> list[ValidationIssue]:
    """
    Every customer[].name must appear as the destination of at least one
    edge -- a customer with no inbound edge can never actually receive
    the product they order.
    """
    issues: list[ValidationIssue] = []
    _, has_inbound = _edge_degree_sets(config)

    for idx, entry in enumerate(config.get("customer", []) or []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue

        if name not in has_inbound:
            issues.append(ValidationIssue(
                layer=LAYER,
                location=f"customer[{idx}]",
                defect_type=DefectType.INCONSISTENT_CROSS_FIELD,
                severity=Severity.BLOCKING,
                detail=f"customer '{name}' has no inbound edge -- they can never "
                       f"receive their ordered product.",
                context={"referenced_name": name, "entry_index": idx},
            ))

    return issues


def _build_adjacency(config: dict) -> dict:
    """Directed adjacency list: name -> set of names it has an edge to."""
    adjacency: dict = {}
    for entry in config.get("edges", []) or []:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        destination = entry.get("destination")
        if isinstance(source, str) and isinstance(destination, str):
            adjacency.setdefault(source, set()).add(destination)
    return adjacency


def _bfs_reachable_from(start: str, adjacency: dict) -> set:
    """All nodes reachable FROM start, following directed edges."""
    visited = {start}
    queue = [start]
    while queue:
        current = queue.pop(0)
        for neighbor in adjacency.get(current, set()):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    return visited


def _bfs_can_reach(target: str, adjacency: dict) -> set:
    """All nodes that can reach target, following directed edges (reverse BFS)."""
    reverse_adjacency: dict = {}
    for source, destinations in adjacency.items():
        for dest in destinations:
            reverse_adjacency.setdefault(dest, set()).add(source)
    return _bfs_reachable_from(target, reverse_adjacency)


def check_product_end_to_end_path(config: dict) -> list[ValidationIssue]:
    """
    For every product: its producing facility (per operation.output) must
    be reachable, via the edges graph, from at least one supplier -- AND
    must be able to reach at least one customer who orders that product.

    SCOPE NOTE: this checks structural graph connectivity only. It does
    NOT trace specific material identity hop-by-hop through bom
    relationships -- it doesn't verify that the die specifically becomes
    the packaged IC, only that SOME directed path exists from SOME
    supplier to the producing facility, and from the producing facility
    to SOME customer of that product. It catches disconnected sub-graphs
    (a real, common what-if failure mode), not "right topology, wrong
    material routed through it" -- that would require full multi-level
    bom resolution, which is out of scope here.

    Skips products with no producing facility or no ordering customer --
    those are already caught by check_product_is_producible and
    check_product_has_customer respectively; this check assumes both
    exist and only asks whether they're actually CONNECTED.
    """
    issues: list[ValidationIssue] = []

    adjacency = _build_adjacency(config)
    supplier_names = _collect_names(config, "supplier")

    # Map: product name -> set of facility names that produce it
    producing_facilities: dict = {}
    for entry in config.get("facility", []) or []:
        if not isinstance(entry, dict):
            continue
        operation = entry.get("operation")
        if not isinstance(operation, dict):
            continue
        facility_name = entry.get("name")
        if not isinstance(facility_name, str):
            continue
        for output_name in operation.get("output") or []:
            if isinstance(output_name, str):
                producing_facilities.setdefault(output_name, set()).add(facility_name)

    # Map: product name -> set of customer names ordering it
    ordering_customers: dict = {}
    for entry in config.get("customer", []) or []:
        if not isinstance(entry, dict):
            continue
        customer_name = entry.get("name")
        product_name = entry.get("product")
        if isinstance(customer_name, str) and isinstance(product_name, str):
            ordering_customers.setdefault(product_name, set()).add(customer_name)

    for idx, entry in enumerate(config.get("products", []) or []):
        if not isinstance(entry, dict):
            continue
        product_name = entry.get("name")
        if not isinstance(product_name, str):
            continue

        facilities_for_product = producing_facilities.get(product_name, set())
        customers_for_product = ordering_customers.get(product_name, set())

        if not facilities_for_product or not customers_for_product:
            continue  # already flagged by checks 6 / 4 respectively

        # Upstream: is at least one producing facility reachable from at
        # least one supplier?
        upstream_ok = False
        for supplier_name in supplier_names:
            reachable = _bfs_reachable_from(supplier_name, adjacency)
            if reachable & facilities_for_product:
                upstream_ok = True
                break

        if not upstream_ok:
            issues.append(ValidationIssue(
                layer=LAYER,
                location=f"products[{idx}]",
                defect_type=DefectType.INCONSISTENT_CROSS_FIELD,
                severity=Severity.BLOCKING,
                detail=f"product '{product_name}' has a producing facility, but no "
                       f"supplier can reach it via the edges graph -- the production "
                       f"chain is disconnected from any material source.",
                context={"referenced_name": product_name, "entry_index": idx},
            ))

        # Downstream: can at least one producing facility reach at least
        # one ordering customer?
        downstream_ok = False
        for facility_name in facilities_for_product:
            reachable = _bfs_reachable_from(facility_name, adjacency)
            if reachable & customers_for_product:
                downstream_ok = True
                break

        if not downstream_ok:
            issues.append(ValidationIssue(
                layer=LAYER,
                location=f"products[{idx}]",
                defect_type=DefectType.INCONSISTENT_CROSS_FIELD,
                severity=Severity.BLOCKING,
                detail=f"product '{product_name}' has a producing facility, but it "
                       f"cannot reach any ordering customer via the edges graph -- "
                       f"the production chain is disconnected from demand.",
                context={"referenced_name": product_name, "entry_index": idx},
            ))

    return issues


def _dist_param_a(obj) -> float:
    """Best-effort extraction of parameters.a from a distribution object.
    Returns None if unavailable/non-numeric -- caller must handle that."""
    if not isinstance(obj, dict):
        return None
    params = obj.get("parameters")
    if not isinstance(params, dict):
        return None
    a = params.get("a")
    if isinstance(a, (int, float)) and not isinstance(a, bool):
        return a
    return None


def _material_stage(config: dict, material_name: str):
    """Returns 0 (raw), 1 (intermediate), 2 (product), or None if the
    material isn't found in any of the three category lists."""
    if material_name in _collect_names(config, "raw_materials"):
        return 0
    if material_name in _collect_names(config, "intermediate_materials"):
        return 1
    if material_name in _collect_names(config, "products"):
        return 2
    return None


def check_manufacturing_facility_material_stage_span(config: dict) -> list[ValidationIssue]:
    """
    A manufacturing facility's inventory_managed must span at least TWO
    distinct material stages (raw=0 < intermediate=1 < product=2).
    Spanning only one stage means there's nothing at a LATER stage for
    the facility to actually produce -- e.g. two raw materials and
    nothing else describes a facility that only ever receives inputs,
    never converts them into anything. This is a feasibility problem
    (the facility's operation is undefined/impossible), not a referential
    one -- every name involved may be perfectly valid on its own.
    """
    issues: list[ValidationIssue] = []

    for idx, entry in enumerate(config.get("facility", []) or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "manufacturing":
            continue

        managed = entry.get("inventory_managed")
        if not isinstance(managed, list):
            continue

        stages_present = set()
        for m in managed:
            if isinstance(m, str):
                stage = _material_stage(config, m)
                if stage is not None:
                    stages_present.add(stage)

        if len(stages_present) < 2:
            issues.append(ValidationIssue(
                layer=LAYER,
                location=f"facility[{idx}].inventory_managed",
                defect_type=DefectType.INCONSISTENT_CROSS_FIELD,
                severity=Severity.BLOCKING,
                detail=f"manufacturing facility '{entry.get('name')}' inventory_managed spans "
                       f"only {len(stages_present)} material stage(s) -- a manufacturing "
                       f"operation needs at least one input-stage material and one "
                       f"later-stage material to actually produce anything.",
                context={"entry_index": idx},
            ))

    return issues


def check_horizon_sanity(config: dict) -> list[ValidationIssue]:
    """
    HEURISTIC, WARNING ONLY. Estimates a rough lower bound on how long it
    takes one unit to travel from a supplier all the way to a customer
    (supplier lead time + operation cycle times + transfer times along
    one path through the edges graph), and compares that against
    simulation.horizon. If horizon doesn't comfortably exceed this
    estimate, the simulation may end before a single unit can complete
    the full chain even once.

    This is deliberately rough: it uses parameters.a as a representative
    scalar for each distribution (not a true expectation for every
    distribution shape), and it walks the FIRST path BFS finds rather
    than the worst-case (longest) path. Treat the result as a sanity
    check, not a rigorous bound -- hence WARNING, never BLOCKING.
    """
    issues: list[ValidationIssue] = []

    sim = config.get("simulation")
    if not isinstance(sim, dict):
        return issues
    horizon = sim.get("horizon")
    if not isinstance(horizon, (int, float)) or isinstance(horizon, bool):
        return issues  # malformed, not this check's concern

    adjacency = _build_adjacency(config)

    # Build lookup: name -> node "cost" (operation cycle time, if a
    # manufacturing facility with a recognizable distribution; else 0).
    facility_cycle_time: dict = {}
    for entry in config.get("facility", []) or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        operation = entry.get("operation")
        if isinstance(name, str) and isinstance(operation, dict):
            cycle_time = _dist_param_a(operation.get("operation_cycle"))
            if cycle_time is not None:
                facility_cycle_time[name] = cycle_time

    # Build lookup: (source, destination) -> transfer_time.parameters.a
    edge_transfer_time: dict = {}
    for entry in config.get("edges", []) or []:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        destination = entry.get("destination")
        if isinstance(source, str) and isinstance(destination, str):
            t = _dist_param_a(entry.get("transfer_time"))
            if t is not None:
                edge_transfer_time[(source, destination)] = t

    supplier_lead_times: dict = {}
    for entry in config.get("supplier", []) or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str):
            lt = _dist_param_a(entry.get("supplier_lead_time"))
            if lt is not None:
                supplier_lead_times[name] = lt

    customer_names = _collect_names(config, "customer")

    # For each supplier, BFS to find a path to ANY customer, tracking
    # cumulative estimated time along the way. Take the first path found
    # per supplier (not exhaustive shortest-time search -- see docstring).
    best_estimate = None
    for supplier_name, lead_time in supplier_lead_times.items():
        # BFS tracking (node, cumulative_time)
        visited = {supplier_name}
        queue = [(supplier_name, lead_time)]
        while queue:
            current, elapsed = queue.pop(0)
            if current in customer_names:
                if best_estimate is None or elapsed < best_estimate:
                    best_estimate = elapsed
                continue
            for neighbor in adjacency.get(current, set()):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                hop_cost = edge_transfer_time.get((current, neighbor), 0)
                node_cost = facility_cycle_time.get(neighbor, 0)
                queue.append((neighbor, elapsed + hop_cost + node_cost))

    if best_estimate is None:
        return issues  # no complete path found -- check 11 already covers that gap

    if horizon < best_estimate:
        issues.append(ValidationIssue(
            layer=LAYER,
            location="simulation.horizon",
            defect_type=DefectType.INVALID_VALUE,
            severity=Severity.WARNING,
            detail=f"simulation.horizon ({horizon}) is shorter than a rough estimate "
                   f"of one full supplier-to-customer cycle (~{best_estimate}) -- the "
                   f"simulation may end before a single unit completes the chain. "
                   f"This is a heuristic estimate, not a guaranteed bound.",
            context={"horizon": horizon, "estimated_cycle_time": best_estimate},
        ))

    return issues


# ----------------------------------------------------------------------
# CLI entry point for quick manual testing
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "test_config.json"
    with open(path) as f:
        cfg = json.load(f)

    found = (
        check_raw_material_has_supplier(cfg)
        + check_at_least_one_raw_material(cfg)
        + check_at_least_one_product(cfg)
        + check_product_has_customer(cfg)
        + check_intermediate_material_is_producible(cfg)
        + check_product_is_producible(cfg)
        + check_raw_material_is_consumed(cfg)
        + check_supplier_has_outbound_edge(cfg)
        + check_facility_has_inbound_and_outbound_edge(cfg)
        + check_customer_has_inbound_edge(cfg)
        + check_product_end_to_end_path(cfg)
        + check_horizon_sanity(cfg)
        + check_manufacturing_facility_material_stage_span(cfg)
    )

    if not found:
        print("Layer3: no issues found.")
    else:
        blocking = [i for i in found if i.severity == Severity.BLOCKING]
        warnings = [i for i in found if i.severity == Severity.WARNING]
        print(f"Layer3: {len(blocking)} blocking issue(s), {len(warnings)} warning(s)\n")
        for issue in found:
            print(issue)