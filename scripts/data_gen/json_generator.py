"""
scripts/data_gen/json_generator.py
------------------------------------
Programmatically generates fully-populated valid supply chain
JSON configs for fine-tuning data generation.

Every key in the schema is always present — irrelevant fields
are populated with neutral defaults. Filtering happens later.

Usage:
    from data_gen.json_generator import generate_config
    config = generate_config(complexity="simple", seed=42)
"""

from __future__ import annotations

import random
import math
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# Name pools
# ============================================================

RAW_MATERIAL_NAMES = [
    "microprocessor", "memory_chip", "silicon_wafer", "copper_wire",
    "aluminum_sheet", "steel_rod", "plastic_pellet", "glass_fiber",
    "lithium_cell", "carbon_fiber", "resin", "solder_paste",
    "gold_wire", "ceramic_substrate", "epoxy_compound",
]

INTERMEDIATE_MATERIAL_NAMES = [
    "circuit_board", "heat_sink", "power_module", "sensor_array",
    "display_panel", "motor_assembly", "battery_pack", "lens_assembly",
    "frame_structure", "cooling_unit",
]

PRODUCT_NAMES = [
    "CPU", "GPU", "SoC", "FPGA", "smartphone", "laptop",
    "tablet", "router", "camera", "drone", "EV_battery",
    "solar_panel", "medical_device", "industrial_sensor",
]

SUPPLIER_NAMES = [
    "Process Go.", "Memory Star", "ChipCo", "SiliconWorks",
    "MetalFab Inc.", "PolymerSource", "GlassCore Ltd.",
    "BatteryTech", "CarbonSupply Co.", "NanoMaterials Inc.",
    "PrecisionParts", "GlobalComponents", "FastChip Corp.",
    "ElectroParts", "MegaSupply",
]

CUSTOMER_NAMES = [
    "Ross Associates", "TechCorp", "NXP Semiconductors",
    "Siemens Digital", "Broadcom Limited", "Infineon Technologies",
    "Samsung Electronics", "Apple Supply", "Dell Technologies",
    "HP Enterprise", "Lenovo Group", "Sony Electronics",
    "Bosch Automotive", "ABB Industrial", "Schneider Electric",
]

FACILITY_NAMES = [
    "Fab", "Plant A", "Assembly Hub", "Production Center",
    "Manufacturing Site", "Main Factory", "Pilot Line",
    "Advanced Fab", "Integration Center", "Processing Unit",
]

WAREHOUSE_NAMES = [
    "Warehouse", "Distribution Center", "Fulfillment Hub",
    "Storage Facility", "Logistics Center", "Regional Depot",
]

RESOURCE_NAMES = [
    "Assembly Line", "CNC Machine", "Laser Cutter", "Robot Arm",
    "Test Equipment", "Conveyor Belt", "Injection Molder",
    "Welding Station", "Quality Scanner", "Packaging Machine",
]

OPERATION_NAMES = [
    "assembly", "fabrication", "polishing", "testing",
    "welding", "molding", "cutting", "bonding",
    "coating", "inspection",
]

SCENARIO_NAMES = [
    "CPU Supply Chain", "Electronics Manufacturing",
    "Semiconductor Production", "Consumer Goods Supply",
    "Automotive Parts", "Medical Device Manufacturing",
    "Renewable Energy Supply", "Industrial Equipment",
    "Aerospace Components", "Pharmaceutical Supply",
]

TIME_UNITS = ["day", "hour", "week"]

# ============================================================
# Distribution helpers
# ============================================================

DISTRIBUTIONS = ["constant", "uniform", "normal", "exponential",
                 "triangular", "poisson"]

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


def _sample_distribution(
    rng: random.Random,
    dist_pool: List[str],
    min_val: float = 0.1,
    max_val: float = 100.0,
    integer_only: bool = False,
) -> Dict[str, Any]:
    """
    Sample a random distribution spec with valid parameters.
    All 5 parameter slots always populated (full schema).
    """
    dist = rng.choice(dist_pool)
    params = {"a": 0.0, "b": 0.0, "c": 0.0, "d": 0.0, "e": 0.0}

    def rv(lo: float, hi: float) -> float:
        v = rng.uniform(lo, hi)
        return round(float(int(v)) if integer_only else round(v, 3), 3)

    if dist == "constant":
        params["a"] = rv(min_val, max_val)

    elif dist == "uniform":
        a = rv(min_val, max_val * 0.6)
        b = rv(a + 0.1, max_val)
        params["a"] = a
        params["b"] = b

    elif dist == "normal":
        mu = rv(min_val, max_val)
        sd = rv(0.1, mu * 0.3 + 0.1)
        params["a"] = mu
        params["b"] = sd

    elif dist == "exponential":
        params["a"] = rv(min_val, max_val)

    elif dist == "poisson":
        params["a"] = rv(max(1.0, min_val), max_val)

    elif dist == "triangular":
        a = rv(min_val, max_val * 0.4)
        c = rv(a + 1.0, max_val)
        b = rv(a, c)
        params["a"] = a
        params["b"] = b
        params["c"] = c

    return {"distribution": dist, "parameters": params}


def _constant(val: float) -> Dict[str, Any]:
    return {
        "distribution": "constant",
        "parameters": {"a": val, "b": 0.0, "c": 0.0, "d": 0.0, "e": 0.0}
    }


def _empty_dist() -> Dict[str, Any]:
    return {
        "distribution": "constant",
        "parameters": {"a": 0.0, "b": 0.0, "c": 0.0, "d": 0.0, "e": 0.0}
    }

# ============================================================
# Complexity profiles
# ============================================================

COMPLEXITY_PROFILES = {
    "simple": {
        "n_raw":          (1, 1),
        "n_intermediate": (0, 0),
        "n_products":     (1, 1),
        "n_suppliers":    (1, 1),
        "n_customers":    (1, 1),
        "n_resources":    (0, 0),
        "n_facilities":   1,
    },
    "medium": {
        "n_raw":          (2, 3),
        "n_intermediate": (0, 1),
        "n_products":     (1, 2),
        "n_suppliers":    (3, 3),
        "n_customers":    (1, 2),
        "n_resources":    (0, 1),
        "n_facilities":   1,
    },
    "complex": {
        "n_raw":          (2, 4),
        "n_intermediate": (1, 3),
        "n_products":     (1, 3),
        "n_suppliers":    (4, 8),
        "n_customers":    (1, 3),
        "n_resources":    (1, 2),
        "n_facilities":   1,
    },
}

SHORTAGE_POLICIES = [
    "backorder",
    "salelost",
    "backorder_partial",
    "salelost_partial",
]

PROCUREMENT_TYPES = [
    "periodic_supply",
    "inventory_threshold",
    "demand_driven",
]

# ============================================================
# Main generator
# ============================================================

class ConfigGenerator:

    def __init__(self, rng: random.Random):
        self.rng = rng

    def _pick(self, pool: List[str], n: int) -> List[str]:
        return self.rng.sample(pool, min(n, len(pool)))

    def _rng_int(self, lo: int, hi: int) -> int:
        return self.rng.randint(lo, hi)

    def _rng_float(self, lo: float, hi: float) -> float:
        return round(self.rng.uniform(lo, hi), 3)

    # ── Build sections ─────────────────────────────────────

    def _build_config_info(self) -> List[Dict]:
        return [{"name": self.rng.choice(SCENARIO_NAMES), "version": "1.0"}]

    def _build_raw_materials(self, names: List[str]) -> List[Dict]:
        return [{"name": n} for n in names]

    def _build_intermediate_materials(
        self,
        names: List[str],
        raw_names: List[str],
    ) -> List[Dict]:
        if not names:
            return []

        result    = []
        shuffled  = raw_names.copy()
        self.rng.shuffle(shuffled)
        n_inter   = len(names)

        # assign raws round-robin across intermediates
        inter_inputs: Dict[str, List[str]] = {n: [] for n in names}
        for i, raw in enumerate(shuffled):
            inter_inputs[names[i % n_inter]].append(raw)

        for name in names:
            base_inputs = inter_inputs[name]

            # optionally add extra raws
            extras = [r for r in raw_names if r not in base_inputs]
            if extras and self.rng.random() > 0.6:
                n_extra = min(self.rng.randint(1, 2), len(extras))
                base_inputs += self.rng.sample(extras, n_extra)

            bom = {inp: self._rng_int(1, 5) for inp in base_inputs}
            result.append({"name": name, "bom": bom})

        return result

    def _build_products(
        self,
        names: List[str],
        raw_names: List[str],
        inter_names: List[str],
    ) -> List[Dict]:
        all_inputs = raw_names + inter_names
        n_products = len(names)
        result     = []

        # ── distribute inputs across products ──────────────
        # shuffle inputs and assign at least one to each product
        # ensuring all inputs are covered across all products
        shuffled = all_inputs.copy()
        self.rng.shuffle(shuffled)

        # assign inputs round-robin to guarantee coverage
        product_inputs: Dict[str, List[str]] = {n: [] for n in names}
        for i, inp in enumerate(shuffled):
            product_inputs[names[i % n_products]].append(inp)

        # each product may also randomly pick extra inputs
        # from the full pool for variety
        for name in names:
            base_inputs = product_inputs[name]

            # optionally add 1-2 more random inputs
            extras = [
                x for x in all_inputs
                if x not in base_inputs
            ]
            if extras and self.rng.random() > 0.5:
                n_extra = min(self.rng.randint(1, 2), len(extras))
                base_inputs += self.rng.sample(extras, n_extra)

            bom = {inp: self._rng_int(1, 4) for inp in base_inputs}
            result.append({"name": name, "bom": bom})

        return result

    def _build_inventory_item(
        self,
        name: str,
        inv_type: str,
    ) -> Dict:
        is_raw     = inv_type == "raw_materials"
        proc_type  = self.rng.choice(PROCUREMENT_TYPES) if is_raw else "periodic_supply"

        # procurement scheme
        if is_raw:
            if proc_type == "periodic_supply":
                qty_dist = _sample_distribution(
                    self.rng,
                    ["uniform", "constant", "normal"],
                    min_val=100, max_val=100000,
                    integer_only=True,
                )
                arrival_dist = _sample_distribution(
                    self.rng,
                    ["constant", "uniform"],
                    min_val=1, max_val=30,
                    integer_only=True,
                )
            elif proc_type == "inventory_threshold":
                s = self._rng_float(100, 5000)
                S = self._rng_float(s + 100, s * 3 + 1000)
                qty_dist = {
                    "distribution": "uniform",
                    "parameters": {"a": round(s, 2), "b": round(S, 2),
                                   "c": 0.0, "d": 0.0, "e": 0.0}
                }
                arrival_dist = _constant(1.0)
            else:  # demand_driven
                qty_dist     = _empty_dist()
                arrival_dist = _empty_dist()

        procurement_scheme = {
            "type":         proc_type if is_raw else "",
            "distribution": qty_dist["distribution"] if is_raw else "",
            "parameters":   qty_dist["parameters"] if is_raw else
                            {"a": 0.0, "b": 0.0, "c": 0.0, "d": 0.0, "e": 0.0},
        }

        holding_cost  = self._rng_float(0, 10) if self.rng.random() > 0.4 else 0.0
        shortage_cost = self._rng_float(0, 50) if self.rng.random() > 0.4 else 0.0
        review_time   = self._rng_int(1, 30) if is_raw else 1
        initial_inv   = self._rng_int(0, 5000000) if not is_raw else 0

        return {
            "name": name,
            "type": inv_type,
            "procurement_scheme":  procurement_scheme,
            "procurement_arrival": arrival_dist if is_raw else _empty_dist(),
            "initial_inventory":   initial_inv,
            "inventory_costs": {
                "holding_cost":  round(holding_cost, 3),
                "shortage_cost": round(shortage_cost, 3),
                "review_time":   review_time,
            },
        }

    def _build_inventory(
        self,
        raw_names: List[str],
        inter_names: List[str],
        prod_names: List[str],
    ) -> List[Dict]:
        inv = []
        for n in raw_names:
            inv.append(self._build_inventory_item(n, "raw_materials"))
        for n in inter_names:
            inv.append(self._build_inventory_item(n, "intermediate_materials"))
        for n in prod_names:
            inv.append(self._build_inventory_item(n, "products"))
        return inv

    def _build_supplier(
        self,
        name: str,
        material: str,
    ) -> Dict:
        return {
            "name":                 name,
            "supply_material_name": material,
            "supplier_lead_time":   _sample_distribution(
                self.rng, ["constant", "uniform", "exponential"],
                min_val=1, max_val=30, integer_only=True),
            "supplier_capacity":    self.rng.choice([0,
                self._rng_int(1000, 1000000)]),
            "supplier_cost":        self._rng_float(1, 500),
            "supplier_payment_lead_time": _sample_distribution(
                self.rng, ["constant", "uniform"],
                min_val=7, max_val=90, integer_only=True),
        }

    def _build_resource(self, name: str) -> Dict:
        has_failure     = self.rng.random() > 0.6
        batching_on     = self.rng.random() > 0.7
        batch_size      = self._rng_int(5, 100) if batching_on else 0
        max_wait        = self._rng_float(1, 20) if batching_on else 0

        return {
            "name":     name,
            "capacity": self._rng_int(1, 5),
            "service_time": _sample_distribution(
                self.rng, ["constant", "uniform", "exponential"],
                min_val=0.1, max_val=10),
            "batching": {
                "enabled":       batching_on,
                "batch_size":    batch_size,
                "max_wait_time": max_wait,
            },
            "failure": {
                "enabled": has_failure,
                "uptime":  _sample_distribution(
                    self.rng, ["exponential", "constant"],
                    min_val=10, max_val=500)
                    if has_failure else _empty_dist(),
                "downtime": _sample_distribution(
                    self.rng, ["exponential", "constant"],
                    min_val=1, max_val=50)
                    if has_failure else _empty_dist(),
            },
            "operating_cost_per_time": self._rng_float(0, 20),
        }

    def _build_facility(
        self,
        name: str,
        fac_type: str,
        all_materials: List[str],
        output_material: str,
        input_materials: List[str],
        resource_name: Optional[str],
        operation_name: str,
    ) -> Dict:
        if fac_type == "warehouse":
            return {
                "name": name,
                "type": "warehouse",
                "inventory_managed": [output_material],
                "operation": {
                    "name": "",
                    "input": [],
                    "output": [],
                    "resource_required": "",
                    "operation_cycle": _empty_dist(),
                },
            }

        cycle = _sample_distribution(
            self.rng, ["constant", "uniform"],
            min_val=1, max_val=30, integer_only=True)

        return {
            "name": name,
            "type": "manufacturing",
            "inventory_managed": list(set(input_materials + [output_material])),
            "operation": {
                "name":              operation_name,
                "input":             input_materials,
                "output":            [output_material],
                "resource_required": resource_name or "",
                "operation_cycle":   cycle,
            },
        }

    def _build_customer(
        self,
        name: str,
        product: str,
    ) -> Dict:
        return {
            "name":    name,
            "product": product,
            "arrival_time": _sample_distribution(
                self.rng, ["constant", "uniform", "exponential"],
                min_val=7, max_val=60, integer_only=True),
            "demand": _sample_distribution(
                self.rng, ["uniform", "normal", "constant"],
                min_val=100, max_val=5000000, integer_only=True),
            "customer_lead_time": _sample_distribution(
                self.rng, ["constant", "uniform"],
                min_val=1, max_val=30, integer_only=True),
            "shortage_policy":       self.rng.choice(SHORTAGE_POLICIES),
            "unit_selling_price":    self._rng_float(10, 5000),
            "customer_payment_lead_time": _sample_distribution(
                self.rng, ["constant", "uniform"],
                min_val=7, max_val=90, integer_only=True),
        }

    def _build_nodes(
        self,
        supplier_names: List[str],
        facility_names: List[str],
    ) -> List[Dict]:
        return [{
            "supplier": supplier_names,
            "facility": facility_names,
        }]

    def _build_edges(
        self,
        suppliers: List[Dict],
        facilities: List[Dict],
        wh_name: str,
        prod_names: List[str],
    ) -> List[Dict]:
        edges = []

        # build a map of product → facility name
        prod_to_fac = {}
        for fac in facilities:
            if fac["type"] == "manufacturing":
                for out in fac["operation"].get("output", []):
                    prod_to_fac[out] = fac["name"]

        # all unique manufacturing facility names
        mfg_names = list(dict.fromkeys(
            fac["name"] for fac in facilities
            if fac["type"] == "manufacturing"
        ))

        # supplier → manufacturing facility
        # each supplier delivers to the first mfg facility
        # (in dedicated case suppliers still deliver to main mfg)
        for s in suppliers:
            mat = s["supply_material_name"]
            edges.append({
                "source":        s["name"],
                "destination":   mfg_names[0],
                "material_type": "raw_materials",
                "material_name": mat,
                "transfer_time": _sample_distribution(
                    self.rng, ["constant", "uniform"],
                    min_val=1, max_val=14, integer_only=True)
                    if self.rng.random() > 0.7 else _constant(0.0),
            })

        # manufacturing facility → warehouse for each product
        for prod in prod_names:
            fac_name = prod_to_fac.get(prod, mfg_names[0])
            edges.append({
                "source":        fac_name,
                "destination":   wh_name,
                "material_type": "products",
                "material_name": prod,
                "transfer_time": _constant(0.0),
            })

        return edges

    def _build_simulation(self) -> Dict:
        horizon = self.rng.choice([100, 200, 365, 500, 730])
        return {
            "time_unit":   self.rng.choice(TIME_UNITS),
            "horizon":     horizon,
            "warm_up":     self.rng.choice([0, 0, 0, 30, 50]),
            "replications": self.rng.choice([1, 5, 10, 10]),
            "random_seed": self.rng.randint(0, 99999),
        }

    # ── Main build method ──────────────────────────────────

    def build(self, complexity: str) -> Dict[str, Any]:
        profile = COMPLEXITY_PROFILES[complexity]
        rng     = self.rng

        # ── Sample counts ──────────────────────────────────
        n_raw   = rng.randint(*profile["n_raw"])
        n_inter = rng.randint(*profile["n_intermediate"])
        n_prod  = rng.randint(*profile["n_products"])
        n_sup   = rng.randint(*profile["n_suppliers"])
        n_cust = max(rng.randint(*profile["n_customers"]), n_prod)
        n_res   = rng.randint(*profile["n_resources"])

        # ── Sample names ───────────────────────────────────
        raw_names   = self._pick(RAW_MATERIAL_NAMES,   n_raw)
        inter_names = self._pick(INTERMEDIATE_MATERIAL_NAMES, n_inter)
        prod_names  = self._pick(PRODUCT_NAMES,        n_prod)
        sup_names   = self._pick(SUPPLIER_NAMES,       n_sup)
        cust_names  = self._pick(CUSTOMER_NAMES,       n_cust)
        res_names   = self._pick(RESOURCE_NAMES,       n_res)
        mfg_name    = rng.choice(FACILITY_NAMES)
        wh_name     = rng.choice(WAREHOUSE_NAMES)
        op_name     = rng.choice(OPERATION_NAMES)

        # ── Ensure each raw material has at least one supplier ──
        # n_sup guaranteed >= n_raw from profile
        n_sup_min = n_raw
        n_sup_max = max(n_raw, profile["n_suppliers"][1])
        n_sup     = rng.randint(n_sup_min, n_sup_max)
        sup_names = self._pick(SUPPLIER_NAMES, n_sup)

        assert len(sup_names) >= len(raw_names), (
            f"Not enough suppliers ({len(sup_names)}) "
            f"for raw materials ({len(raw_names)})"
        )

        suppliers = []

        # one supplier guaranteed per raw material
        for i, raw in enumerate(raw_names):
            suppliers.append(self._build_supplier(sup_names[i], raw))

        # remaining suppliers assigned based on multi_supplier_prob
        multi_prob = profile.get("multi_supplier_prob", 0.2)
        for sname in sup_names[n_raw:]:
            if rng.random() < multi_prob:
                mat = rng.choice(raw_names)
            else:
                coverage = {r: 0 for r in raw_names}
                for s in suppliers:
                    coverage[s["supply_material_name"]] = \
                        coverage.get(s["supply_material_name"], 0) + 1
                mat = min(coverage, key=coverage.get)
            suppliers.append(self._build_supplier(sname, mat))

        all_sup_names = [s["name"] for s in suppliers]

        # ── Resources ──────────────────────────────────────
        resources = [self._build_resource(n) for n in res_names]
        res_name  = res_names[0] if res_names else None

        # ── Operation inputs ───────────────────────────────
        # ── Build intermediates and products first ─────────
        intermediates = self._build_intermediate_materials(
            inter_names, raw_names)
        products = self._build_products(
            prod_names, raw_names, inter_names)

        # ── Operation inputs must match first product BOM ──
        first_bom = products[0]["bom"] if products else {}
        op_inputs = list(first_bom.keys())

        # ── for medium complexity randomly skip resource ───
        if complexity == "medium" and n_res > 0:
            if rng.random() > 0.5:
                n_res     = 0
                res_names = []
                res_name  = None

        # ── Facilities ─────────────────────────────────────
        # ── Facilities ─────────────────────────────────────
        # randomly decide: shared facility or dedicated per product
        shared_facility = rng.random() > 0.4  # 60% shared, 40% dedicated
        facilities      = []
        used_fac_names  = []

        for p_idx, prod in enumerate(prod_names):
            prod_bom    = products[p_idx]["bom"]
            prod_inputs = list(prod_bom.keys())

            if shared_facility:
                fac_name = mfg_name
            else:
                if p_idx == 0:
                    fac_name = mfg_name
                else:
                    other_names = [n for n in FACILITY_NAMES if n != mfg_name]
                    fac_name = rng.choice(other_names) if other_names else mfg_name

            # resource only assigned to first operation
            fac_res = res_name if p_idx == 0 else None

            fac_entry = self._build_facility(
                name=fac_name, fac_type="manufacturing",
                all_materials=raw_names + inter_names + prod_names,
                output_material=prod,
                input_materials=prod_inputs,
                resource_name=fac_res,
                operation_name=rng.choice(OPERATION_NAMES),
            )
            facilities.append(fac_entry)
            used_fac_names.append(fac_name)

        # warehouse
        wh_facility = self._build_facility(
            name=wh_name, fac_type="warehouse",
            all_materials=prod_names,
            output_material=prod_names[0],
            input_materials=[],
            resource_name=None,
            operation_name="",
        )
        facilities.append(wh_facility)

        # deduplicate facility names for nodes
        all_fac_names = list(dict.fromkeys(used_fac_names + [wh_name]))


        # ── Customers ──────────────────────────────────────
        # guarantee at least one customer per product
        customers = []

        # first assign one customer per product
        for i, prod in enumerate(prod_names):
            cname = cust_names[i % len(cust_names)]
            customers.append(self._build_customer(cname, prod))

        # assign remaining customers randomly to any product
        for cname in cust_names[len(prod_names):]:
            prod = rng.choice(prod_names)
            customers.append(self._build_customer(cname, prod))

        # ── Assemble full config ───────────────────────────
        return {
            "config_info":           self._build_config_info(),
            "raw_materials":         self._build_raw_materials(raw_names),
            "intermediate_materials": intermediates,
            "products":              products,
            "inventory":             self._build_inventory(
                raw_names, inter_names, prod_names),
            "supplier":              suppliers,
            "resource":              resources,
            "facility":              facilities,
            "customer":              customers,
            "nodes":                 self._build_nodes(
                all_sup_names,
                all_fac_names),
            "edges":                 self._build_edges(
                suppliers, facilities, wh_name, prod_names),
            "simulation":            self._build_simulation(),
        }


# ============================================================
# Public API
# ============================================================

def generate_config(
    complexity: str = "simple",
    seed: int = 0,
) -> Dict[str, Any]:
    """
    Generate a fully-populated valid supply chain JSON config.

    Parameters
    ----------
    complexity : str
        One of "simple", "medium", "complex"
    seed : int
        Random seed for reproducibility

    Returns
    -------
    dict
        Fully populated JSON config — all schema keys present
    """
    rng = random.Random(seed)
    gen = ConfigGenerator(rng)
    return gen.build(complexity)


if __name__ == "__main__":
    import json

    for complexity in ["simple", "medium", "complex"]:
        cfg = generate_config(complexity=complexity, seed=42)
        print(f"\n{'='*60}")
        print(f"Complexity: {complexity}")
        print(f"{'='*60}")
        print(f"  Raw materials    : {[m['name'] for m in cfg['raw_materials']]}")
        print(f"  Intermediates    : {[m['name'] for m in cfg['intermediate_materials']]}")
        print(f"  Products         : {[p['name'] for p in cfg['products']]}")
        print(f"  Suppliers        : {[s['name'] for s in cfg['supplier']]}")
        print(f"  Resources        : {[r['name'] for r in cfg['resource']]}")
        print(f"  Customers        : {[c['name'] for c in cfg['customer']]}")
        print(f"  Horizon          : {cfg['simulation']['horizon']}")
        print(f"  Replications     : {cfg['simulation']['replications']}")