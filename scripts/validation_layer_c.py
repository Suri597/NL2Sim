# layer_c_validator.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Set, Tuple, Optional, Union
from collections import defaultdict, deque
import json

# ============================================================
# Finding model
# ============================================================

@dataclass
class ValidationFinding:
    layer: str
    severity: str  # "error" | "warning"
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.layer}::{self.severity}] {self.path}: {self.message}"


# ============================================================
# Layer C: "Simulation-readiness" / logical consistency checks
# Assumption: Layer0/LayerA already ensured basic schema/types,
# and LayerB ensured "structural supply-chain" validity.
# LayerC focuses on deeper logical constraints needed to run
# a discrete-event simulation robustly.
# ============================================================

LAYER = "LayerC"

# You can tighten/loosen these depending on your engine.
POSITIVE_INT_FIELDS = {
    # path suffixes
    "bom": "BOM quantities must be positive integers",
}

# If your engine uses these policies strictly:
ALLOWED_SHORTAGE_POLICIES = {
    "backorder",
    "sale_lost",
    "backorder_partial",
    "sale_lost_partial",
}

ALLOWED_INVENTORY_TYPES = {"raw_materials", "intermediate_materials", "products"}


# ============================================================
# Helper utilities
# ============================================================

def _as_list_of_str(x: Any) -> List[str]:
    if isinstance(x, list):
        return [str(v) for v in x]
    if isinstance(x, str):
        return [x]
    return []


def _is_positive_int(v: Any) -> bool:
    return isinstance(v, int) and v > 0


def _canon(s: str) -> str:
    return str(s).strip()


def _remove_indices(path: str) -> str:
    # inventory[3].procurement_scheme.type -> inventory.procurement_scheme.type
    out = []
    for part in path.replace("]", "").split("."):
        if "[" in part:
            part = part.split("[", 1)[0]
        out.append(part)
    return ".".join(out)


# ============================================================
# Layer C validator
# ============================================================

class LayerCValidator:
    """
    Layer C checks:
      1) BOM correctness:
         - quantities are positive integers
         - referenced materials exist
         - BOM graph is acyclic (no circular dependencies)
      2) Operation <-> BOM consistency:
         - facility operations produce things that are defined as intermediates/products
         - operation outputs have a BOM definition if they are intermediate/product (unless you allow "black-box" ops)
         - operation inputs match BOM inputs (optional: strict/loose mode)
      3) End-to-end producibility / reachability:
         - every demanded product is either (a) externally supplied, or (b) producible via operations from raw materials supplied
      4) Inventory alignment:
         - inventory entries align to the correct entity class (raw/intermediate/product)
         - every defined material/product has an inventory record if your engine requires it (toggle)
      5) Edges alignment to facility managed inventories (optional strictness)
    """

    def __init__(
        self,
        config: Dict[str, Any],
        *,
        strict_operation_bom_match: bool = False,
        require_inventory_for_all_items: bool = False,
        enforce_facility_inventory_managed: bool = False,
    ):
        self.config = config
        self.strict_operation_bom_match = strict_operation_bom_match
        self.require_inventory_for_all_items = require_inventory_for_all_items
        self.enforce_facility_inventory_managed = enforce_facility_inventory_managed

        self.findings: List[ValidationFinding] = []

        # caches
        self.raw_set: Set[str] = set()
        self.inter_set: Set[str] = set()
        self.prod_set: Set[str] = set()

        self.boms: Dict[str, Dict[str, int]] = {}  # item -> {component -> qty}
        self.inventory_by_name: Dict[str, Dict[str, Any]] = {}
        self.facilities: List[Dict[str, Any]] = []
        self.resources: Set[str] = set()
        self.suppliers: Dict[str, str] = {}  # supplier_name -> supply_material_name
        self.customers: List[Dict[str, Any]] = []
        self.edges: List[Dict[str, Any]] = []

        # operation index: output_item -> list of ops (for producibility)
        self.ops_by_output: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    # -------------------------
    # Public
    # -------------------------

    def validate(self) -> Dict[str, List[ValidationFinding]]:
        self._index_entities()
        self._check_boms()
        self._check_bom_cycles()
        self._index_operations()
        self._check_operation_consistency()
        self._check_inventory_alignment()
        self._check_producibility_for_customers()
        self._check_edges_vs_facility_inventory_managed()

        errors = [f for f in self.findings if f.severity == "error"]
        warnings = [f for f in self.findings if f.severity == "warning"]
        return {"errors": errors, "warnings": warnings}

    # -------------------------
    # Indexing
    # -------------------------

    def _index_entities(self) -> None:
        # raw
        for i, entry in enumerate(self.config.get("raw_materials", [])):
            # Your config can represent raw_materials as [{"name": ["a","b"]}] or [{"name":"a"},...]
            name = entry.get("name")
            if isinstance(name, list):
                for n in name:
                    self.raw_set.add(_canon(n))
            elif isinstance(name, str):
                self.raw_set.add(_canon(name))
            else:
                # Layer A should catch, but keep safe
                self._err(f"raw_materials[{i}].name", "Invalid raw_materials.name format")

        # intermediate
        for i, entry in enumerate(self.config.get("intermediate_materials", [])):
            n = entry.get("name")
            if isinstance(n, str):
                self.inter_set.add(_canon(n))
            bom = entry.get("bom")
            if isinstance(n, str) and isinstance(bom, dict):
                self.boms[_canon(n)] = bom

        # products
        for i, entry in enumerate(self.config.get("products", [])):
            n = entry.get("name")
            if isinstance(n, str):
                self.prod_set.add(_canon(n))
            bom = entry.get("bom")
            if isinstance(n, str) and isinstance(bom, dict):
                self.boms[_canon(n)] = bom

        # inventory
        for i, inv in enumerate(self.config.get("inventory", [])):
            name = inv.get("name")
            if isinstance(name, str):
                self.inventory_by_name[_canon(name)] = inv

        # facilities/resources/suppliers/customers/edges
        self.facilities = self.config.get("facility", []) or []
        self.customers = self.config.get("customer", []) or []
        self.edges = self.config.get("edges", []) or []

        for r in self.config.get("resource", []) or []:
            rn = r.get("name")
            if isinstance(rn, str):
                self.resources.add(_canon(rn))

        for s in self.config.get("supplier", []) or []:
            sn = s.get("name")
            mat = s.get("supply_material_name")
            if isinstance(sn, str) and isinstance(mat, str):
                self.suppliers[_canon(sn)] = _canon(mat)

    # -------------------------
    # BOM checks
    # -------------------------

    def _check_boms(self) -> None:
        """
        Validate:
          - BOM references only known items (raw/intermediate)
          - BOM quantities are positive integers
        """
        all_known_components = self.raw_set | self.inter_set | self.prod_set

        for item, bom in self.boms.items():
            if not isinstance(bom, dict):
                self._err(f"boms.{item}", "BOM must be a dict of component->qty")
                continue

            for comp, qty in bom.items():
                comp_s = _canon(comp)
                if comp_s not in all_known_components:
                    self._err(
                        f"bom[{item}].{comp_s}",
                        f"BOM component '{comp_s}' is not a known raw_material or intermediate_material",
                    )
                if not _is_positive_int(qty):
                    self._err(
                        f"bom[{item}].{comp_s}",
                        f"BOM quantity must be a positive integer (got {qty!r})",
                    )

    def _check_bom_cycles(self) -> None:
        """
        Detect cycles in the BOM graph among intermediate+products.
        A cycle will break feasibility in most manufacturing logic.
        """
        # Build dependency graph: item -> set(components that are themselves "produced items")
        produced = self.inter_set | self.prod_set
        deps: Dict[str, Set[str]] = {x: set() for x in produced}

        for item in produced:
            bom = self.boms.get(item, {})
            if isinstance(bom, dict):
                for comp in bom.keys():
                    comp_s = _canon(comp)
                    if comp_s in produced:
                        deps[item].add(comp_s)

        # DFS cycle detection
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in deps}

        def dfs(u: str, stack: List[str]) -> None:
            color[u] = GRAY
            stack.append(u)
            for v in deps[u]:
                if color[v] == WHITE:
                    dfs(v, stack)
                elif color[v] == GRAY:
                    # cycle found: v ... u -> v
                    if v in stack:
                        idx = stack.index(v)
                        cyc = stack[idx:] + [v]
                    else:
                        cyc = [v, u, v]
                    self._err(
                        f"bom_cycle.{u}",
                        f"Circular BOM dependency detected: {' -> '.join(cyc)}",
                    )
            stack.pop()
            color[u] = BLACK

        for node in deps:
            if color[node] == WHITE:
                dfs(node, [])

    # -------------------------
    # Operations checks
    # -------------------------

    def _index_operations(self) -> None:
        self.ops_by_output.clear()
        for i, fac in enumerate(self.facilities):
            op = fac.get("operation")
            if not isinstance(op, dict):
                continue
            outputs = op.get("output")
            for out_item in _as_list_of_str(outputs):
                self.ops_by_output[_canon(out_item)].append(
                    {"facility_index": i, "facility": fac, "operation": op}
                )

    def _check_operation_consistency(self) -> None:
        """
        Validate:
          - operation.resource_required exists in resources
          - operation.output items must be intermediates/products (not raw)
          - if output is produced, it should have a BOM (unless you allow black-box ops)
          - optional strict match: operation.input should match BOM keys for its output
        """
        for i, fac in enumerate(self.facilities):
            fac_name = fac.get("name", f"facility[{i}]")
            op = fac.get("operation")
            if not isinstance(op, dict):
                continue

            op_name = op.get("name", "?")
            base = f"facility[{i}]({fac_name}).operation({op_name})"

            # resource_required
            rr = op.get("resource_required")
            if isinstance(rr, str):
                # empty string or "none" means no resource required — skip
                if rr.strip() == "" or rr.strip().lower() == "none":
                    pass
                elif _canon(rr) not in self.resources:
                    self._err(
                        f"{base}.resource_required",
                        f"resource_required '{rr}' is not defined in resource[]",
                    )

            # outputs must not be raw materials
            outputs = _as_list_of_str(op.get("output"))
            for out_item in outputs:
                out_item = _canon(out_item)
                if out_item in self.raw_set:
                    self._err(
                        f"{base}.output",
                        f"Operation output '{out_item}' is a raw_material; outputs should be intermediate/product",
                    )
                if out_item not in self.inter_set and out_item not in self.prod_set:
                    self._warn(
                        f"{base}.output",
                        f"Operation output '{out_item}' is not declared in intermediate_materials/products (engine may still allow it, but it's risky).",
                    )

                # If output is a produced item, ensure it has a BOM (common DES requirement)
                if out_item in (self.inter_set | self.prod_set):
                    if out_item not in self.boms:
                        self._warn(
                            f"{base}.output",
                            f"Produced item '{out_item}' has no BOM definition (allowed if you model as black-box, otherwise add BOM).",
                        )

            # optional strict: operation inputs = BOM keys
            if self.strict_operation_bom_match:
                inputs = set(_canon(x) for x in _as_list_of_str(op.get("input")))
                for out_item in outputs:
                    out_item = _canon(out_item)
                    bom = self.boms.get(out_item)
                    if isinstance(bom, dict):
                        bom_inputs = set(_canon(k) for k in bom.keys())
                        if inputs != bom_inputs:
                            self._err(
                                f"{base}.input",
                                f"Operation inputs {sorted(inputs)} do not match BOM inputs for '{out_item}' ({sorted(bom_inputs)}).",
                            )

    # -------------------------
    # Inventory alignment checks
    # -------------------------

    def _check_inventory_alignment(self) -> None:
        """
        Validate:
          - inventory.type matches actual entity class (raw/intermediate/product)
          - (optional) require inventory for every defined item
        """
        for name, inv in self.inventory_by_name.items():
            inv_type = inv.get("type")
            p = f"inventory[name={name}].type"
            if not isinstance(inv_type, str):
                continue
            
            # ---------------------------------------------
            # inventory_threshold (s, S) policy validation
            # ---------------------------------------------
            ps = inv.get("procurement_scheme")
            if isinstance(ps, dict):
                ptype = ps.get("type")

                if ptype == "inventory_threshold" and inv_type=="raw_materials":
                    params = ps.get("parameters", {})
                    a = params.get("a")  # small s
                    b = params.get("b")  # big S

                    path = f"inventory[name={name}].procurement_scheme.parameters"

                    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
                        self._err(
                            path,
                            "(s, S) policy requires numeric parameters a (small s) and b (big S)",
                        )
                    elif b <= a:
                        self._err(
                            path,
                            f"Invalid (s, S) policy: big S (b={b}) must be greater than small s (a={a})",
                        )

            inv_type = _canon(inv_type)
            if inv_type not in ALLOWED_INVENTORY_TYPES:
                # LayerA/0 should catch, but keep safe
                self._err(p, f"Invalid inventory type '{inv_type}'")
                continue

            # check match
            if name in self.raw_set and inv_type != "raw_materials":
                self._err(p, f"Inventory type mismatch: '{name}' is raw_materials but inventory.type='{inv_type}'")
            if name in self.inter_set and inv_type != "intermediate_materials":
                self._err(p, f"Inventory type mismatch: '{name}' is intermediate_materials but inventory.type='{inv_type}'")
            if name in self.prod_set and inv_type != "products":
                self._err(p, f"Inventory type mismatch: '{name}' is products but inventory.type='{inv_type}'")

        if self.require_inventory_for_all_items:
            needed = self.raw_set | self.inter_set | self.prod_set
            missing_inv = sorted([x for x in needed if x not in self.inventory_by_name])
            for x in missing_inv:
                self._err(f"inventory", f"Missing inventory entry for '{x}' (require_inventory_for_all_items=True)")

    # -------------------------
    # Producibility / Reachability checks
    # -------------------------

    def _check_producibility_for_customers(self) -> None:
        """
        For each customer.product:
          Determine if product can be supplied via:
            - existing inventory (always possible at t=0), AND/OR
            - supplier provides it directly, OR
            - there exists an operation chain whose leaves are raw materials with suppliers.
        This is a *graph feasibility* check, not capacity/time.
        """
        # what can suppliers supply?
        supplier_materials = set(self.suppliers.values())

        # quick allow: if product itself is supplier supplied (rare, but possible)
        # otherwise check if there's an operation chain from supplier-supplied raws.
        for i, cust in enumerate(self.customers):
            prod = cust.get("product")
            if not isinstance(prod, str):
                continue
            prod = _canon(prod)
            base = f"customer[{i}].product({prod})"

            if prod not in self.prod_set:
                # LayerB might catch, but keep safe
                self._err(base, f"Customer demands '{prod}' but it is not declared in products[]")
                continue

            # If product is directly supplied, feasible.
            if prod in supplier_materials:
                continue

            # If producible via operations/BOM chain
            if not self._is_producible(prod, supplier_materials):
                self._err(
                    base,
                    f"Product '{prod}' does not appear producible from supplier-supplied raw materials via defined operations/BOMs",
                )

            # shortage policy sanity (engine-level)
            sp = cust.get("shortage_policy")
            if isinstance(sp, str):
                sp_norm = sp.lower().replace("_", "").replace("-", "").replace(" ", "")
                allowed_norm = {
                    p.lower().replace("_", "").replace("-", "").replace(" ", "")
                    for p in ALLOWED_SHORTAGE_POLICIES
                }
                if sp_norm not in allowed_norm:
                    self._warn(f"customer[{i}].shortage_policy",
                               f"Unknown shortage_policy '{sp}' (engine may not support it)")

    def _is_producible(self, item: str, supplier_materials: Set[str]) -> bool:
        """
        Conservative feasibility:
          - If item is a raw material: must be supplier supplied.
          - If item is intermediate/product: either
              a) has an operation producing it AND all its BOM components are producible, OR
              b) (fallback) has BOM and all components are producible (if you model production without explicit operations)
        """
        item = _canon(item)

        # memoization
        memo: Dict[str, bool] = {}
        visiting: Set[str] = set()

        def rec(x: str) -> bool:
            x = _canon(x)
            if x in memo:
                return memo[x]
            if x in visiting:
                # cycle already reported by BOM cycle check; treat as not producible
                memo[x] = False
                return False

            # raw material -> must be supplier supplied
            if x in self.raw_set:
                ok = x in supplier_materials
                memo[x] = ok
                return ok

            visiting.add(x)

            # if supplier supplies this intermediate/product directly, allow
            if x in supplier_materials:
                visiting.remove(x)
                memo[x] = True
                return True

            # must be producible
            bom = self.boms.get(x)
            comps = []
            if isinstance(bom, dict):
                comps = [_canon(k) for k in bom.keys()]

            # option 1: require an operation that outputs x
            if x in self.ops_by_output:
                # if any op chain works -> True
                for _opref in self.ops_by_output[x]:
                    if comps:
                        if all(rec(c) for c in comps):
                            visiting.remove(x)
                            memo[x] = True
                            return True
                    else:
                        # no BOM; allow operation to be black-box producing it
                        visiting.remove(x)
                        memo[x] = True
                        return True

            # option 2: BOM chain alone (if you want to allow “implicit production”)
            if comps and all(rec(c) for c in comps):
                visiting.remove(x)
                memo[x] = True
                return True

            visiting.remove(x)
            memo[x] = False
            return False

        return rec(item)

    # -------------------------
    # Optional strict: edges vs facility inventory_managed
    # -------------------------

    def _check_edges_vs_facility_inventory_managed(self) -> None:
        """
        If enforce_facility_inventory_managed=True:
          - any material_name transferred into/out of a facility must be in that facility's inventory_managed
        This is useful to prevent “phantom items” moving through facilities.
        """
        if not self.enforce_facility_inventory_managed:
            return

        # map facility name -> inventory_managed set
        inv_managed: Dict[str, Set[str]] = defaultdict(set)
        for i, fac in enumerate(self.facilities):
            fn = fac.get("name")
            if not isinstance(fn, str):
                continue
            managed = fac.get("inventory_managed")
            if isinstance(managed, list):
                inv_managed[_canon(fn)] |= set(_canon(x) for x in managed)

        for i, e in enumerate(self.edges):
            src = e.get("source")
            dst = e.get("destination")
            mat = e.get("material_name")
            if not (isinstance(src, str) and isinstance(dst, str) and isinstance(mat, str)):
                continue

            src = _canon(src)
            dst = _canon(dst)
            mat = _canon(mat)

            if src in inv_managed and mat not in inv_managed[src]:
                self._err(f"edges[{i}]", f"Edge moves '{mat}' from facility '{src}', but it's not in facility.inventory_managed")
            if dst in inv_managed and mat not in inv_managed[dst]:
                self._err(f"edges[{i}]", f"Edge moves '{mat}' into facility '{dst}', but it's not in facility.inventory_managed")

    # -------------------------
    # Finding helpers
    # -------------------------

    def _err(self, path: str, msg: str) -> None:
        self.findings.append(ValidationFinding(LAYER, "error", path, msg))

    def _warn(self, path: str, msg: str) -> None:
        self.findings.append(ValidationFinding(LAYER, "warning", path, msg))


# ============================================================
# Example usage
# ============================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python validation_layer_c.py <config.json>")
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        config = json.load(f)

    v = LayerCValidator(
        config,
        strict_operation_bom_match=False,
        require_inventory_for_all_items=False,
        enforce_facility_inventory_managed=False,
    )
    report = v.validate()

    print("\n--- LAYER C ERRORS ---")
    for f in report["errors"]:
        print(f)

    print("\n--- LAYER C WARNINGS ---")
    for f in report["warnings"]:
        print(f)