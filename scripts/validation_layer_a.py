from __future__ import annotations

import re
import json
from dataclasses import dataclass
from typing import Any, Dict, List

# ============================================================
# Finding model
# ============================================================

@dataclass
class ValidationFinding:
    layer: str
    severity: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.layer}::{self.severity}] {self.path}: {self.message}"


# ============================================================
# Constants
# ============================================================

LAYER           = "Layer0"
MISSING_LITERAL = "missing"
ILLEGAL_TYPES   = (set, tuple, type(None))


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


ALLOWED_DISTRIBUTIONS = {
    "poisson", "exponential", "normal", "uniform",
    "weibull", "beta", "triangular", "constant",
}

ALLOWED_INVENTORY_TYPES = {
    "raw_materials", "intermediate_materials", "products",
}

ALLOWED_SHORTAGE_POLICIES = {
    "backorder",
    "sale_lost",
    "lost_sales",
    "backorder_partial",
    "backorder_partial_fulfillment",
    "sale_lost_partial",
    "sale_lost_partial_fulfillment",
    "Sale_lost_partial_fulfillment",
}

ALLOWED_PROCUREMENT_TYPES = {
    "demand_driven", "periodic_supply", "inventory_threshold",
}

DISTRIBUTION_PARAMS_REQUIRED = {
    "constant":    ["a"],
    "exponential": ["a"],
    "poisson":     ["a"],
    "uniform":     ["a", "b"],
    "normal":      ["a", "b"],
    "triangular":  ["a", "b", "c"],
    "weibull":     ["a", "b"],
    "beta":        ["a", "b"],
}

TOP_LEVEL_SCHEMA: Dict[str, type] = {
    "config_info":             list,
    "raw_materials":           list,
    "intermediate_materials":  list,
    "products":                list,
    "inventory":               list,
    "supplier":                list,
    "resource":                list,
    "facility":                list,
    "customer":                list,
    "nodes":                   list,
    "edges":                   list,
    "simulation":              dict,
}

RAW_MATERIAL_SCHEMA          = {"name": str}
INTERMEDIATE_MATERIAL_SCHEMA = {"name": str, "bom": dict}
PRODUCT_SCHEMA               = {"name": str, "bom": dict}

INVENTORY_SCHEMA = {
    "name":               str,
    "type":               str,
    "procurement_scheme": dict,
    "initial_inventory":  int,
    "inventory_costs":    dict,
}

SUPPLIER_SCHEMA = {
    "name":                       str,
    "supply_material_name":       str,
    "supplier_lead_time":         dict,
    "supplier_capacity":          int,
    "supplier_cost":              (int, float),
    "supplier_payment_lead_time": dict,
}

RESOURCE_SCHEMA = {
    "name":                    str,
    "capacity":                int,
    "service_time":            dict,
    "batching":                dict,
    "failure":                 dict,
    "operating_cost_per_time": (int, float),
}

FACILITY_SCHEMA = {"name": str, "type": str}

CUSTOMER_SCHEMA = {
    "name":                       str,
    "product":                    str,
    "arrival_time":               dict,
    "demand":                     dict,
    "shortage_policy":            str,
    "unit_selling_price":         (int, float),
    "customer_payment_lead_time": dict,
}

EDGE_SCHEMA = {
    "source":        str,
    "destination":   str,
    "material_type": str,
    "material_name": str,
    "transfer_time": dict,
}

MISSING_POLICY_REQUIRED = {
    "raw_materials.name",
    "products.name",
    "products.bom",
    "inventory.name",
    "inventory.type",
    "inventory.procurement_scheme.type",
    "inventory.procurement_scheme.distribution",
    "inventory.procurement_scheme.parameters.a",
    "inventory.procurement_scheme.parameters.b",
    "inventory.initial_inventory",
    "supplier.name",
    "supplier.supply_material_name",
    "supplier.supplier_lead_time.distribution",
    "supplier.supplier_lead_time.parameters.a",
    "supplier.supplier_payment_lead_time.distribution",
    "supplier.supplier_payment_lead_time.parameters.a",
    "customer.name",
    "customer.product",
    "customer.arrival_time.distribution",
    "customer.arrival_time.parameters.a",
    "customer.demand.distribution",
    "customer.demand.parameters.a",
    "customer.customer_lead_time.distribution",
    "customer.customer_lead_time.parameters.a",
    "customer.customer_payment_lead_time.distribution",
    "customer.customer_payment_lead_time.parameters.a",
    "customer.shortage_policy",
    "facility.name",
    "facility.type",
    "nodes.supplier",
    "nodes.facility",
    "edges.source",
    "edges.destination",
    "edges.material_type",
    "edges.material_name",
}

MISSING_POLICY_OPTIONAL = {
    "inventory.inventory_costs.holding_cost",
    "inventory.inventory_costs.shortage_cost",
    "inventory.inventory_costs.review_time",
    "supplier.supplier_cost",
    "supplier.supplier_capacity",
    "customer.unit_selling_price",
    "resource.operating_cost_per_time",
    "resource.batching.enabled",
}


# ============================================================
# Module-level helpers
# ============================================================

def _get_nested(obj: Any, path: str) -> Any:
    """Get a nested value using dot notation path."""
    parts = [p for p in re.split(r"[\.\[\]]", path) if p]
    for part in parts:
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return obj


def _is_conditionally_irrelevant(config: Dict[str, Any], path: str) -> bool:
    """
    Returns True if 'missing' at this path is expected and should
    be silently ignored. Mirrors filter_config.py conditions exactly.
    """

    # ── config_info always irrelevant ─────────────────────
    if path.startswith("config_info"):
        return True

    # ── simulation fields ──────────────────────────────────
    if path in ("simulation.warm_up", "simulation.random_seed"):
        return True

    # ── inventory procurement scheme ──────────────────────
    m = re.match(r"inventory\[(\d+)\]\.procurement_scheme", path)
    if m:
        idx = int(m.group(1))
        inv = config.get("inventory", [])
        if idx < len(inv):
            item      = inv[idx]
            inv_type  = item.get("type", "")
            ps        = item.get("procurement_scheme", {}) or {}
            proc_type = ps.get("type", "")

            if inv_type != "raw_materials":
                return True

            if proc_type == "demand_driven":
                if "distribution" in path or "parameters" in path:
                    return True

            if proc_type == "inventory_threshold":
                if re.search(r"\.distribution$", path):
                    return True
                pm = re.search(r"parameters\.([a-e])$", path)
                if pm and pm.group(1) in ["c", "d", "e"]:
                    return True

    # ── procurement_arrival ────────────────────────────────
    m = re.match(r"inventory\[(\d+)\]\.procurement_arrival", path)
    if m:
        idx = int(m.group(1))
        inv = config.get("inventory", [])
        if idx < len(inv):
            item      = inv[idx]
            inv_type  = item.get("type", "")
            ps        = item.get("procurement_scheme", {}) or {}
            proc_type = ps.get("type", "")
            if inv_type != "raw_materials" or proc_type != "periodic_supply":
                return True

    # ── inventory costs ────────────────────────────────────
    m = re.match(
        r"inventory\[(\d+)\]\.inventory_costs\.(holding_cost|shortage_cost|review_time)$",
        path)
    if m:
        idx   = int(m.group(1))
        field = m.group(2)
        inv   = config.get("inventory", [])
        if idx < len(inv):
            costs    = inv[idx].get("inventory_costs", {}) or {}
            holding  = costs.get("holding_cost",  0)
            shortage = costs.get("shortage_cost", 0)
            try:
                h = float(holding)  if holding  not in ("missing", None) else 0.0
                s = float(shortage) if shortage not in ("missing", None) else 0.0
            except (ValueError, TypeError):
                h = s = 0.0
            if field in ("holding_cost", "shortage_cost"):
                return True
            if field == "review_time" and h <= 0 and s <= 0:
                return True

    # ── supplier capacity ──────────────────────────────────
    if re.match(r"supplier\[(\d+)\]\.supplier_capacity$", path):
        return True

    # ── resource operating cost ────────────────────────────
    if re.match(r"resource\[(\d+)\]\.operating_cost_per_time$", path):
        return True

    # ── resource batching ──────────────────────────────────
    resources = config.get("resource", [])
    m = re.match(r"resource\[(\d+)\]\.batching\.(batch_size|max_wait_time)$", path)
    if m:
        idx = int(m.group(1))
        if idx < len(resources):
            batching = resources[idx].get("batching", {}) or {}
            if not batching.get("enabled", False):
                return True

    # ── resource failure ───────────────────────────────────
    m = re.match(r"resource\[(\d+)\]\.failure\.(uptime|downtime)", path)
    if m:
        idx = int(m.group(1))
        if idx < len(resources):
            failure = resources[idx].get("failure", {}) or {}
            if not failure.get("enabled", False):
                return True

    # ── warehouse operation fields ─────────────────────────
    m = re.match(r"facility\[(\d+)\]\.operation", path)
    if m:
        idx        = int(m.group(1))
        facilities = config.get("facility", [])
        if idx < len(facilities):
            if facilities[idx].get("type", "") == "warehouse":
                return True

    # ── resource_required when no resource defined ─────────
    has_resource = len(resources) > 0
    if re.match(r"facility\[(\d+)\]\.operation\.resource_required$", path):
        if not has_resource:
            return True

    # ── transfer time when constant(0) ────────────────────
    m = re.match(r"edges\[(\d+)\]\.transfer_time", path)
    if m:
        idx   = int(m.group(1))
        edges = config.get("edges", [])
        if idx < len(edges):
            tt   = edges[idx].get("transfer_time", {}) or {}
            dist = tt.get("distribution", "")
            a    = tt.get("parameters", {}).get("a", 0)
            if not dist or dist == "missing" or \
               (dist == "constant" and (a == "missing" or a == 0)):
                return True

    # ── distribution parameters not required by dist type ──
    pm = re.search(r"\.parameters\.([a-e])$", path)
    if pm:
        param_key   = pm.group(1)
        parent_path = path[:path.rfind(".parameters.")]
        dist        = _get_nested(config, parent_path + ".distribution")
        if dist and dist != "missing":
            required = DISTRIBUTION_PARAMS_REQUIRED.get(dist, ["a"])
            if param_key not in required:
                return True

    return False


# ============================================================
# Layer A Validator
# ============================================================

class LayerAValidator:

    def __init__(self, config: Dict[str, Any], strict: bool = True):
        self.config   = config
        self.strict   = strict
        self.findings: List[ValidationFinding] = []

    def validate(self) -> Dict[str, List[ValidationFinding]]:
        self._forbid_illegal_types(self.config)
        self._detect_missing_literals(self.config)
        self._validate_top_level()
        self._validate_sections()
        self._validate_distribution_parameters(self.config)

        errors           = [f for f in self.findings if f.severity == "error"]
        warnings         = [f for f in self.findings if f.severity == "warning"]
        missing_required = [f for f in self.findings if f.severity == "missing_required"]
        missing_optional = [f for f in self.findings if f.severity == "missing_optional"]

        return {
            "errors":           errors,
            "warnings":         warnings,
            "missing_required": missing_required,
            "missing_optional": missing_optional,
        }

    # ── illegal types ──────────────────────────────────────

    def _forbid_illegal_types(self, obj: Any, path: str = "") -> None:
        if isinstance(obj, ILLEGAL_TYPES):
            self._err(path, f"Illegal data structure: {type(obj).__name__}")
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                self._forbid_illegal_types(v, f"{path}.{k}" if path else str(k))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                self._forbid_illegal_types(v, f"{path}[{i}]")

    # ── missing literal detection ──────────────────────────

    def _detect_missing_literals(self, obj: Any, path: str = "") -> None:
        if isinstance(obj, str) and obj.strip().lower() == MISSING_LITERAL:
            # check if conditionally irrelevant — if so silently ignore
            if _is_conditionally_irrelevant(self.config, path):
                return

            section_field = self._canonical_policy_key(path)
            if section_field in MISSING_POLICY_REQUIRED:
                self._missing_required(path, "Found placeholder 'missing' in required field")
            elif section_field in MISSING_POLICY_OPTIONAL:
                self._missing_optional(path, "Found placeholder 'missing' in optional field")
            else:
                self._missing_optional(path,
                    "Found placeholder 'missing' (field not classified; default optional)")
            return

        if isinstance(obj, dict):
            for k, v in obj.items():
                self._detect_missing_literals(v, f"{path}.{k}" if path else str(k))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                self._detect_missing_literals(v, f"{path}[{i}]")

    # ── distribution parameter type enforcement ────────────

    def _validate_distribution_parameters(self, obj: Any, path: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                child_path = f"{path}.{k}" if path else k
                if k == "parameters" and isinstance(v, dict):
                    for pk, pv in v.items():
                        param_path = f"{child_path}.{pk}"
                        if isinstance(pv, str) and pv.strip().lower() == MISSING_LITERAL:
                            continue
                        if not _is_number(pv):
                            self._err(param_path,
                                f"Distribution parameter must be int or float, got {type(pv).__name__}")
                else:
                    self._validate_distribution_parameters(v, child_path)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                self._validate_distribution_parameters(v, f"{path}[{i}]")

    # ── top level ──────────────────────────────────────────

    def _validate_top_level(self) -> None:
        if not isinstance(self.config, dict):
            self._err("", "Config must be a dictionary")
            return

        for key, expected_type in TOP_LEVEL_SCHEMA.items():
            if key == "config_info":
                if key in self.config and not isinstance(self.config[key], expected_type):
                    self._err(key, f"Expected type {expected_type.__name__}")
                continue
            if key not in self.config:
                self._err(key, "Missing top-level section")
            elif not isinstance(self.config[key], expected_type):
                self._err(key, f"Expected type {expected_type.__name__}")

        if self.strict:
            for key in self.config.keys():
                if key not in TOP_LEVEL_SCHEMA:
                    self._warn(key, "Unknown top-level section")

    # ── sections ───────────────────────────────────────────

    def _validate_sections(self) -> None:
        self._validate_section("raw_materials",          RAW_MATERIAL_SCHEMA)
        self._validate_section("intermediate_materials", INTERMEDIATE_MATERIAL_SCHEMA)
        self._validate_section("products",               PRODUCT_SCHEMA)
        self._validate_inventory()
        self._validate_section("supplier",               SUPPLIER_SCHEMA)
        self._validate_supplier()
        self._validate_resource()
        self._validate_facility()
        self._validate_customer()
        self._validate_section("edges",                  EDGE_SCHEMA)
        self._validate_simulation()

    def _validate_simulation(self) -> None:
        sim = self.config.get("simulation")
        if not isinstance(sim, dict):
            self._err("simulation", "Must be a dictionary")
            return
        required = {
            "time_unit":   str,
            "horizon":     int,
            "warm_up":     int,
            "replications": int,
            "random_seed": int,
        }
        for k, t in required.items():
            if k not in sim:
                self._err(f"simulation.{k}", "Missing required field")
            elif sim[k] == MISSING_LITERAL:
                pass  # handled by _detect_missing_literals
            elif not isinstance(sim[k], t):
                self._err(f"simulation.{k}", f"Expected {t.__name__}")

    def _validate_section(self, section_name: str, schema: Dict[str, Any]) -> None:
        entries = self.config.get(section_name, [])
        if not isinstance(entries, list):
            self._err(section_name, "Section must be a list")
            return
        for idx, item in enumerate(entries):
            base = f"{section_name}[{idx}]"
            if not isinstance(item, dict):
                self._err(base, "Entry must be a dictionary")
                continue
            for field, expected_type in schema.items():
                p = f"{base}.{field}"
                if field not in item:
                    self._err(p, "Missing required field")
                    continue
                val = item[field]
                if val == MISSING_LITERAL:
                    continue  # handled by _detect_missing_literals
                if not isinstance(val, expected_type):
                    self._err(p, f"Expected {expected_type}")

    def _validate_inventory(self) -> None:
        entries = self.config.get("inventory", [])
        if not isinstance(entries, list):
            self._err("inventory", "Section must be a list")
            return
        for idx, item in enumerate(entries):
            base = f"inventory[{idx}]"
            if not isinstance(item, dict):
                self._err(base, "Entry must be a dictionary")
                continue

            for field, expected in INVENTORY_SCHEMA.items():
                p   = f"{base}.{field}"
                val = item.get(field)
                if val is None:
                    self._err(p, "Missing required field")
                    continue
                if val == MISSING_LITERAL:
                    continue
                if field == "initial_inventory":
                    if not isinstance(val, int) or val < 0:
                        self._err(p, "Must be a non-negative integer")
                elif field == "type":
                    if val not in ALLOWED_INVENTORY_TYPES:
                        self._err(p, f"Invalid inventory type: {val}")
                else:
                    if not isinstance(val, expected):
                        self._err(p, f"Expected {expected}")

            inv_type = item.get("type", "")
            if inv_type == "raw_materials":
                ps = item.get("procurement_scheme", {}) or {}
                if isinstance(ps, dict):
                    t = ps.get("type")
                    if t and t != MISSING_LITERAL and \
                       t not in ALLOWED_PROCUREMENT_TYPES:
                        self._err(f"{base}.procurement_scheme.type",
                                  f"Invalid procurement type: {t}")
                    dist = ps.get("distribution")
                    if dist and dist != MISSING_LITERAL and \
                       dist not in ALLOWED_DISTRIBUTIONS:
                        self._err(f"{base}.procurement_scheme.distribution",
                                  f"Invalid distribution: {dist}")

    def _validate_supplier(self) -> None:
        entries = self.config.get("supplier", [])
        if not isinstance(entries, list):
            return
        for idx, item in enumerate(entries):
            base = f"supplier[{idx}]"
            if not isinstance(item, dict):
                continue
            for dist_field in ["supplier_lead_time", "supplier_payment_lead_time"]:
                block = item.get(dist_field, {})
                if isinstance(block, dict):
                    dist = block.get("distribution")
                    if dist and dist != MISSING_LITERAL and \
                       dist not in ALLOWED_DISTRIBUTIONS:
                        self._err(f"{base}.{dist_field}.distribution",
                                  f"Invalid distribution: {dist}")

    def _validate_resource(self) -> None:
        entries = self.config.get("resource", [])
        if not isinstance(entries, list):
            return
        for idx, item in enumerate(entries):
            base = f"resource[{idx}]"
            if not isinstance(item, dict):
                continue
            for field, expected in RESOURCE_SCHEMA.items():
                p   = f"{base}.{field}"
                val = item.get(field)
                if val is None:
                    self._err(p, "Missing required field")
                    continue
                if val == MISSING_LITERAL:
                    continue
                if not isinstance(val, expected):
                    self._err(p, f"Expected {expected}")
            st = item.get("service_time", {})
            if isinstance(st, dict):
                dist = st.get("distribution")
                if dist and dist != MISSING_LITERAL and \
                   dist not in ALLOWED_DISTRIBUTIONS:
                    self._err(f"{base}.service_time.distribution",
                              f"Invalid distribution: {dist}")

    def _validate_facility(self) -> None:
        entries = self.config.get("facility", [])
        if not isinstance(entries, list):
            return
        for idx, item in enumerate(entries):
            base  = f"facility[{idx}]"
            ftype = item.get("type")
            if not ftype or ftype == MISSING_LITERAL:
                self._err(f"{base}.type", "Missing required field")
                continue
            if ftype not in ("manufacturing", "warehouse"):
                self._err(f"{base}.type", f"Unknown facility type: {ftype}")
                continue
            if ftype == "manufacturing":
                if "inventory_managed" not in item:
                    self._err(f"{base}.inventory_managed",
                              "Missing required field for manufacturing facility")
                if "operation" not in item:
                    self._err(f"{base}.operation",
                              "Missing required field for manufacturing facility")

    def _validate_customer(self) -> None:
        entries = self.config.get("customer", [])
        if not isinstance(entries, list):
            return
        for idx, item in enumerate(entries):
            base = f"customer[{idx}]"
            if not isinstance(item, dict):
                continue
            for field, expected in CUSTOMER_SCHEMA.items():
                p   = f"{base}.{field}"
                val = item.get(field)
                if val is None:
                    self._err(p, "Missing required field")
                    continue
                if val == MISSING_LITERAL:
                    continue
                if not isinstance(val, expected):
                    self._err(p, f"Expected {expected}")

            sp = item.get("shortage_policy")
            if sp and sp != MISSING_LITERAL:
                sp_norm = sp.lower().replace("_","").replace("-","").replace(" ","")
                allowed_norm = {
                    p.lower().replace("_","").replace("-","").replace(" ","")
                    for p in ALLOWED_SHORTAGE_POLICIES
                }
                if sp_norm not in allowed_norm:
                    self._err(f"{base}.shortage_policy",
                              f"Invalid shortage_policy: {sp}")

            for sub in ["arrival_time", "demand", "customer_lead_time",
                        "customer_payment_lead_time"]:
                node = item.get(sub, {})
                if isinstance(node, dict):
                    dist = node.get("distribution")
                    if dist and dist != MISSING_LITERAL and \
                       dist not in ALLOWED_DISTRIBUTIONS:
                        self._err(f"{base}.{sub}.distribution",
                                  f"Invalid distribution: {dist}")

            if "customer_lead_time" not in item:
                self._err(f"{base}.customer_lead_time",
                          "Missing customer_lead_time")

    # ── missing literal detection ──────────────────────────

    def _canonical_policy_key(self, path: str) -> str:
        out = []
        for part in path.replace("]", "").split("."):
            if "[" in part:
                part = part.split("[", 1)[0]
            out.append(part)
        return ".".join(out)

    # ── finding helpers ────────────────────────────────────

    def _err(self, path: str, msg: str) -> None:
        self.findings.append(ValidationFinding(LAYER, "error", path, msg))

    def _warn(self, path: str, msg: str) -> None:
        self.findings.append(ValidationFinding(LAYER, "warning", path, msg))

    def _missing_required(self, path: str, msg: str) -> None:
        self.findings.append(ValidationFinding(LAYER, "missing_required", path, msg))

    def _missing_optional(self, path: str, msg: str) -> None:
        self.findings.append(ValidationFinding(LAYER, "missing_optional", path, msg))


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python validation_layer_a.py <config.json>")
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        config = json.load(f)

    v      = LayerAValidator(config, strict=True)
    report = v.validate()

    print("\n--- ERRORS ---")
    for f in report["errors"]:
        print(f)

    print("\n--- MISSING (REQUIRED) ---")
    for f in report["missing_required"]:
        print(f)

    print("\n--- MISSING (OPTIONAL) ---")
    for f in report["missing_optional"]:
        print(f)

    print("\n--- WARNINGS ---")
    for f in report["warnings"]:
        print(f)