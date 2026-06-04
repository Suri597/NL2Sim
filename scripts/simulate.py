"""
scripts/simulate.py
--------------------
NL2Sim Discrete-Event Simulation Engine (SimPy-based)

Reads a validated supply chain JSON config and runs a full DES simulation.

Usage (standalone):
    python simulate.py config.json
    python simulate.py config.json --output ../outputs/results.json

Usage (imported):
    from simulate import SupplyChainEngine, run_simulation
    results = run_simulation(config)

Assumptions communicated to user at runtime — see ASSUMPTIONS block below.
"""

from __future__ import annotations

import json
import math
import random
import warnings
import bisect
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import simpy

try:
    import scipy.stats as sps
except ImportError:
    sps = None

# ============================================================
# Assumptions
# ============================================================

ASSUMPTIONS = """
============================================================
NL2Sim Simulation Engine — Assumptions
============================================================
  1. Products are stored at the warehouse facility.
     Raw and intermediate materials are stored at
     manufacturing facilities.
  2. inventory_threshold: parameter a = reorder point (s),
     parameter b = order-up-to level (S).
  3. periodic_supply: distribution defines order quantity,
     delivery triggered every review_time periods.
  4. demand_driven: procurement order placed when a
     shortage occurs during production or customer demand.
  5. If multiple suppliers exist for the same raw material,
     the lowest cost supplier is always selected.
  6. Poisson arrival times are converted to exponential
     inter-arrival times with rate = parameter a.
  7. batching.enabled=true with batch_size=-1 means flush
     all available inputs every operation cycle.
  8. operation_cycle defines time between production runs.
     service_time defines per-unit processing time within
     a run. If operation_cycle is absent, service_time
     is used as the cycle time.
  9. Supplier payment is delayed by supplier_payment_lead_time.
     Customer revenue is received after customer_payment_lead_time.
 10. Warm-up period data is excluded from all KPI calculations.
 11. All metrics are reported as mean with 90%, 95%, and 99%
     confidence intervals across replications.
 12. If a resource has service_time = constant(0), it is
     treated as instantaneous (no delay, no failure modelled).
 13. Warehouse facilities are treated as pure storage nodes.
     No operations are executed at warehouse facilities.
 14. If supplier_capacity is absent or 'missing', it is treated
     as unlimited (inf). Only explicit numeric values cap supply.
 15. If transfer_time is absent or distribution is 'missing',
     transfer is instantaneous (delay = 0).
 16. If warm_up or random_seed is 'missing', defaults of 0
     and 12345 are used respectively.
============================================================
"""


# ============================================================
# Distributions
# ============================================================

def _lower(x: Any) -> Any:
    return x.lower().strip() if isinstance(x, str) else x


def _param_values(params: Dict[str, Any]) -> List[float]:
    out = []
    for v in (params or {}).values():
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


def sample_distribution(spec: Optional[Dict[str, Any]],
                         rng: random.Random) -> float:
    if not spec:
        return 0.0

    d    = _lower(spec.get("distribution", "constant"))
    vals = _param_values(spec.get("parameters", {}))

    def getn(n: int, default: float = 0.0) -> List[float]:
        padded = vals + [default] * max(0, n - len(vals))
        return padded[:n]

    if d in ("constant", "deterministic"):
        (a,) = getn(1)
        return max(0.0, a)

    if d in ("exponential", "exp"):
        (mean,) = getn(1)
        return rng.expovariate(1.0 / mean) if mean > 0 else 0.0

    if d == "poisson":
        (lam,) = getn(1)
        if lam <= 0:
            return 0.0
        if sps is not None:
            return float(sps.poisson.rvs(
                mu=lam, random_state=rng.randint(1, 2**31 - 1)))
        L, k, p = math.exp(-lam), 0, 1.0
        while p > L:
            k += 1
            p *= rng.random()
        return float(k - 1)

    if d == "uniform":
        a, b = getn(2)
        lo, hi = (a, b) if a <= b else (b, a)
        return max(0.0, rng.uniform(lo, hi))

    if d == "normal":
        mu, sd = getn(2)
        return max(0.0, rng.gauss(mu, abs(sd)))

    if d == "weibull":
        k, lam = getn(2, 1.0)
        if k <= 0 or lam <= 0:
            return 0.0
        return max(0.0, rng.weibullvariate(lam, k))

    if d == "lognormal":
        mean, sd = getn(2)
        if mean <= 0:
            return 0.0
        var    = sd * sd
        sig2   = math.log(1.0 + var / (mean * mean)) if var > 0 else 0.0
        sig    = math.sqrt(sig2)
        mu     = math.log(mean) - 0.5 * sig2
        return max(0.0, rng.lognormvariate(mu, sig) if sig > 0 else mean)

    if d in ("triangular", "triangle"):
        lo, hi, mode = getn(3)
        if lo > hi:
            lo, hi = hi, lo
        mode = min(max(mode, lo), hi)
        return max(0.0, rng.triangular(lo, hi, mode))

    if d == "beta":
        alpha, beta_p = getn(2, 1.0)
        lo, hi = 0.0, 1.0
        if len(vals) >= 4:
            lo, hi = float(vals[2]), float(vals[3])
            if lo > hi:
                lo, hi = hi, lo
        if alpha <= 0 or beta_p <= 0:
            return 0.0
        return max(0.0, lo + rng.betavariate(alpha, beta_p) * (hi - lo))

    raise ValueError(f"Unsupported distribution: '{d}'")


def interarrival_time(spec: Optional[Dict[str, Any]],
                       rng: random.Random) -> float:
    if not spec:
        return 1.0
    d    = _lower(spec.get("distribution", "exponential"))
    vals = _param_values(spec.get("parameters", {}))
    if d == "poisson":
        rate = float(vals[0]) if vals else 0.0
        return rng.expovariate(rate) if rate > 0 else 1.0
    return max(1e-9, sample_distribution(spec, rng))


def _is_zero_constant(spec: Optional[Dict[str, Any]]) -> bool:
    if not spec:
        return True
    d    = _lower(spec.get("distribution", "constant"))
    vals = _param_values(spec.get("parameters", {}))
    return d in ("constant", "deterministic") and (not vals or float(vals[0]) <= 0)


# ============================================================
# Config normalizer — strips "missing" and applies defaults
# ============================================================

MISSING_STR = "missing"


def _is_missing(val: Any) -> bool:
    """Returns True if value is None, absent, or the 'missing' placeholder."""
    return val is None or (
        isinstance(val, str) and val.strip().lower() == MISSING_STR)


def _clean_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Replace 'missing' parameter values with 0.0."""
    return {
        k: (0.0 if _is_missing(v) else float(v))
        for k, v in (params or {}).items()
    }


def _clean_dist_block(block: Any) -> Dict[str, Any]:
    """
    Normalize a distribution block.
    If distribution is missing or absent → constant(0) = instantaneous.
    """
    if not isinstance(block, dict):
        return {"distribution": "constant", "parameters": {"a": 0.0}}

    dist = block.get("distribution")
    if _is_missing(dist):
        return {"distribution": "constant", "parameters": {"a": 0.0}}

    return {
        "distribution": str(dist).strip().lower(),
        "parameters":   _clean_params(block.get("parameters", {})),
    }


def normalize_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a config dict before passing to the simulation engine.

    Decisions:
    - 'missing' placeholder   → sensible default for each field
    - absent optional field   → sensible default
    - transfer_time missing   → instantaneous (constant 0)
    - supplier_capacity miss  → float('inf') = unlimited
    - warm_up missing         → 0
    - random_seed missing     → 12345
    - holding/shortage cost   → 0
    - review_time missing     → 1
    - operation_cycle missing → constant(1)
    - resource_required miss  → "" (no resource)
    - warehouse operation     → cleared entirely
    """
    cfg = deepcopy(cfg)

    # ── simulation ─────────────────────────────────────────
    sim = cfg.get("simulation", {}) or {}
    if _is_missing(sim.get("warm_up")):
        sim["warm_up"] = 0
    if _is_missing(sim.get("random_seed")):
        sim["random_seed"] = 12345
    sim["horizon"]      = float(sim.get("horizon", 365))
    sim["replications"] = int(sim.get("replications", 1))
    cfg["simulation"]   = sim

    # ── inventory ──────────────────────────────────────────
    for item in cfg.get("inventory", []) or []:
        # initial inventory
        if _is_missing(item.get("initial_inventory")):
            item["initial_inventory"] = 0

        # inventory costs
        costs = item.get("inventory_costs", {}) or {}
        for f in ("holding_cost", "shortage_cost"):
            if _is_missing(costs.get(f)):
                costs[f] = 0.0
        if _is_missing(costs.get("review_time")):
            costs["review_time"] = 1.0
        item["inventory_costs"] = costs

        inv_type = item.get("type", "")
        ps       = item.get("procurement_scheme", {}) or {}
        ps_type  = ps.get("type", "")

        if inv_type == "raw_materials" and not _is_missing(ps_type):
            if ps_type == "periodic_supply":
                if _is_missing(ps.get("distribution")):
                    ps["distribution"] = "constant"
                ps["parameters"] = _clean_params(ps.get("parameters", {}))
            elif ps_type == "inventory_threshold":
                ps["parameters"] = _clean_params(ps.get("parameters", {}))
            elif ps_type == "demand_driven":
                pass  # no distribution needed
            item["procurement_scheme"] = ps

            # procurement arrival
            pa = item.get("procurement_arrival", {})
            item["procurement_arrival"] = _clean_dist_block(pa)

        else:
            # product / intermediate / missing type → no procurement
            item["procurement_scheme"]  = {"type": "none"}
            item["procurement_arrival"] = {
                "distribution": "constant", "parameters": {"a": 0.0}}

    # ── supplier ───────────────────────────────────────────
    for s in cfg.get("supplier", []) or []:
        # unlimited capacity if missing
        cap = s.get("supplier_capacity")
        if _is_missing(cap):
            s["supplier_capacity"] = float("inf")
        else:
            try:
                s["supplier_capacity"] = float(cap)
            except (TypeError, ValueError):
                s["supplier_capacity"] = float("inf")

        s["supplier_lead_time"] = _clean_dist_block(
            s.get("supplier_lead_time"))
        s["supplier_payment_lead_time"] = _clean_dist_block(
            s.get("supplier_payment_lead_time"))

        if _is_missing(s.get("supplier_cost")):
            s["supplier_cost"] = 0.0

    # ── facility ───────────────────────────────────────────
    for fac in cfg.get("facility", []) or []:
        ftype = _lower(fac.get("type", ""))

        # clean inventory_managed — remove "missing" entries
        fac["inventory_managed"] = [
            m for m in (fac.get("inventory_managed") or [])
            if not _is_missing(m)
        ]

        if ftype == "warehouse":
            # warehouses are pure storage — clear operation entirely
            fac["operation"] = {
                "name": "storage", "input": [], "output": [],
                "resource_required": "",
                "operation_cycle": {
                    "distribution": "constant", "parameters": {"a": 0.0}},
            }
            continue

        # manufacturing facility
        op = fac.get("operation", {}) or {}

        # resource_required
        rr = op.get("resource_required")
        if _is_missing(rr):
            op["resource_required"] = ""

        # operation_cycle — default to constant(1) if missing
        cycle = _clean_dist_block(op.get("operation_cycle"))
        if _is_zero_constant(cycle):
            cycle = {"distribution": "constant", "parameters": {"a": 1.0}}
        op["operation_cycle"] = cycle

        fac["operation"] = op

    # ── customer ───────────────────────────────────────────
    for c in cfg.get("customer", []) or []:
        for f in ("arrival_time", "demand", "customer_lead_time",
                  "customer_payment_lead_time"):
            c[f] = _clean_dist_block(c.get(f))

        if _is_missing(c.get("unit_selling_price")):
            c["unit_selling_price"] = 0.0

        sp = c.get("shortage_policy", "")
        if _is_missing(sp):
            c["shortage_policy"] = "backorder"

    # ── edges ──────────────────────────────────────────────
    for e in cfg.get("edges", []) or []:
        e["transfer_time"] = _clean_dist_block(e.get("transfer_time"))

    return cfg


# ============================================================
# Statistics helpers
# ============================================================

def _ci(values: List[float], confidence: float) -> Tuple[float, float]:
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    m  = sum(values) / n
    if n == 1 or sps is None:
        return m, m
    sd   = math.sqrt(sum((x - m) ** 2 for x in values) / (n - 1))
    t    = float(sps.t.ppf((1 + confidence) / 2, df=n - 1))
    half = t * sd / math.sqrt(n)
    return round(m - half, 6), round(m + half, 6)


def mean_ci(values: List[float]) -> Dict[str, Any]:
    n = len(values)
    if n == 0:
        return {"mean": 0.0, "ci90": [0.0, 0.0],
                "ci95": [0.0, 0.0], "ci99": [0.0, 0.0], "n": 0}
    m = round(sum(values) / n, 6)
    return {
        "mean": m,
        "ci90": list(_ci(values, 0.90)),
        "ci95": list(_ci(values, 0.95)),
        "ci99": list(_ci(values, 0.99)),
        "n":    n,
    }


# ============================================================
# Data classes
# ============================================================

@dataclass
class Inventory:
    name:          str
    level:         float
    holding_cost:  float
    shortage_cost: float
    review_time:   float


@dataclass
class Backorder:
    customer:     str
    product:      str
    units:        int
    unit_price:   float
    policy:       str
    created_time: float
    remaining:    int


# ============================================================
# Logger
# ============================================================

class Logger:
    def __init__(self, env: simpy.Environment, warm_up: float):
        self.env      = env
        self.warm_up  = float(warm_up)

        self.revenue           = 0.0
        self.procurement_cost  = 0.0
        self.operating_cost    = 0.0
        self.holding_cost      = 0.0
        self.shortage_cost     = 0.0
        self.cash_balance      = 0.0

        self.orders              = 0
        self.units_demanded      = 0
        self.units_delivered     = 0
        self.units_lost          = 0
        self.backorder_units_end = 0

        self.order_wait_times   = []
        self.partial_wait_times = []

        self._inv_last_t:     Dict[str, float] = {}
        self._inv_last_level: Dict[str, float] = {}
        self._inv_area:       Dict[str, float] = defaultdict(float)

        self._res_last_t:    Dict[str, float] = defaultdict(float)
        self._res_last_busy: Dict[str, float] = defaultdict(float)
        self._res_busy_area: Dict[str, float] = defaultdict(float)
        self.res_queue_waits: Dict[str, List[float]] = defaultdict(list)

        self._cash_last_t:     float = 0.0
        self._cash_last_level: float = 0.0
        self._cash_area:       float = 0.0

    @property
    def profit(self) -> float:
        return self.revenue - (
            self.procurement_cost + self.operating_cost +
            self.holding_cost    + self.shortage_cost
        )

    def add_revenue(self, x: float):
        self.revenue += float(x)

    def add_procurement_cost(self, x: float):
        self.procurement_cost += float(x)

    def add_operating_cost(self, x: float):
        self.operating_cost += float(x)

    def add_holding_cost(self, x: float):
        self.holding_cost += float(x)

    def add_shortage_cost(self, x: float):
        self.shortage_cost += float(x)

    def record_order(self, units: int):
        self.orders         += 1
        self.units_demanded += int(units)

    def record_delivery(self, units: int):
        self.units_delivered += int(units)

    def record_lost(self, units: int):
        self.units_lost += int(units)

    def inv_register(self, inv: Inventory):
        self._inv_last_t[inv.name]     = self.env.now
        self._inv_last_level[inv.name] = inv.level

    def inv_update(self, inv: Inventory, new_level: float):
        name   = inv.name
        t      = self.env.now
        last_t = self._inv_last_t.get(name, t)
        last_l = self._inv_last_level.get(name, inv.level)
        start  = max(last_t, self.warm_up)
        end    = max(t,      self.warm_up)
        if end > start:
            self._inv_area[name] += last_l * (end - start)
        self._inv_last_t[name]     = t
        self._inv_last_level[name] = new_level

    def inv_finalize(self):
        t = self.env.now
        for name in list(self._inv_last_t):
            last_t = self._inv_last_t[name]
            last_l = self._inv_last_level[name]
            start  = max(last_t, self.warm_up)
            end    = max(t,      self.warm_up)
            if end > start:
                self._inv_area[name] += last_l * (end - start)
            self._inv_last_t[name] = t

    def inv_time_avg(self, horizon: float) -> Dict[str, float]:
        denom = max(1e-9, horizon - self.warm_up)
        return {k: v / denom for k, v in self._inv_area.items()}

    def res_register(self, name: str):
        self._res_last_t[name]    = self.env.now
        self._res_last_busy[name] = 0.0

    def res_update_busy(self, name: str, new_busy: float):
        t      = self.env.now
        last_t = self._res_last_t.get(name, t)
        last_b = self._res_last_busy.get(name, 0.0)
        start  = max(last_t, self.warm_up)
        end    = max(t,      self.warm_up)
        if end > start:
            self._res_busy_area[name] += last_b * (end - start)
        self._res_last_t[name]    = t
        self._res_last_busy[name] = float(new_busy)

    def res_finalize(self):
        t = self.env.now
        for name in list(self._res_last_t):
            last_t = self._res_last_t[name]
            last_b = self._res_last_busy[name]
            start  = max(last_t, self.warm_up)
            end    = max(t,      self.warm_up)
            if end > start:
                self._res_busy_area[name] += last_b * (end - start)
            self._res_last_t[name] = t

    def res_utilization(self, horizon: float,
                         caps: Dict[str, int]) -> Dict[str, float]:
        denom = max(1e-9, horizon - self.warm_up)
        return {
            r: area / (max(1, caps.get(r, 1)) * denom)
            for r, area in self._res_busy_area.items()
        }

    def cash_update(self, new_balance: float):
        t      = self.env.now
        last_t = self._cash_last_t
        last_l = self._cash_last_level
        start  = max(last_t, self.warm_up)
        end    = max(t,      self.warm_up)
        if end > start:
            self._cash_area += last_l * (end - start)
        self._cash_last_t     = t
        self._cash_last_level = float(new_balance)
        self.cash_balance     = float(new_balance)

    def cash_finalize(self):
        t      = self.env.now
        last_t = self._cash_last_t
        last_l = self._cash_last_level
        start  = max(last_t, self.warm_up)
        end    = max(t,      self.warm_up)
        if end > start:
            self._cash_area += last_l * (end - start)
        self._cash_last_t = t

    def cash_time_avg(self, horizon: float) -> float:
        denom = max(1e-9, horizon - self.warm_up)
        return self._cash_area / denom


# ============================================================
# Failable Resource
# ============================================================

class FailableResource:
    def __init__(self, env: simpy.Environment, name: str,
                 capacity: int,
                 uptime_spec:   Optional[Dict[str, Any]],
                 downtime_spec: Optional[Dict[str, Any]],
                 rng: random.Random,
                 logger: Logger):
        self.env           = env
        self.name          = name
        self.capacity      = int(capacity)
        self.resource      = simpy.Resource(env, capacity=self.capacity)
        self.uptime_spec   = uptime_spec
        self.downtime_spec = downtime_spec
        self.rng           = rng
        self.logger        = logger
        self.is_up         = True
        self._state_ev     = simpy.Event(env)
        self._busy         = 0
        logger.res_register(name)

        if uptime_spec and downtime_spec:
            env.process(self._failure_loop())

    def _toggle(self, up: bool):
        self.is_up = up
        if not self._state_ev.triggered:
            self._state_ev.succeed()
        self._state_ev = simpy.Event(self.env)

    def _failure_loop(self):
        while True:
            yield self.env.timeout(
                sample_distribution(self.uptime_spec, self.rng))
            self._toggle(False)
            yield self.env.timeout(
                sample_distribution(self.downtime_spec, self.rng))
            self._toggle(True)

    def _wait_until_up(self):
        while not self.is_up:
            yield self._state_ev

    def _set_busy(self, n: int):
        self.logger.res_update_busy(self.name, n)
        self._busy = n

    def acquire(self):
        start = self.env.now
        req   = self.resource.request()
        yield req
        wait  = self.env.now - start
        if self.env.now >= self.logger.warm_up:
            self.logger.res_queue_waits[self.name].append(wait)
        yield from self._wait_until_up()
        self._set_busy(self._busy + 1)
        return req

    def release(self, req):
        self._set_busy(self._busy - 1)
        self.resource.release(req)


# ============================================================
# Main Engine
# ============================================================

class SupplyChainEngine:
    """
    JSON-driven discrete-event simulation engine for supply chain.
    Accepts both raw configs (with 'missing' placeholders) and
    filtered configs (with absent optional fields).
    normalize_config() is called automatically on init.
    """

    def __init__(self, config: Dict[str, Any]):
        self.cfg  = normalize_config(config)
        sim       = self.cfg.get("simulation", {}) or {}
        self.horizon      = float(sim.get("horizon",      365))
        self.warm_up      = float(sim.get("warm_up",        0))
        self.replications = int(sim.get("replications",     1))
        self.base_seed    = int(sim.get("random_seed",  12345))

    # ── config helpers ────────────────────────────────────

    def _edge_transfer(self, src: str, dst: str,
                        mat: str) -> Dict[str, Any]:
        for e in self.cfg.get("edges", []) or []:
            if (e.get("source")        == src and
                    e.get("destination")   == dst and
                    e.get("material_name") == mat):
                tt = e.get("transfer_time")
                return tt if tt else {
                    "distribution": "constant", "parameters": {"a": 0}}
        return {"distribution": "constant", "parameters": {"a": 0}}

    def _best_supplier(self, material: str) -> Dict[str, Any]:
        candidates = [
            s for s in (self.cfg.get("supplier", []) or [])
            if s.get("supply_material_name") == material
        ]
        if not candidates:
            raise ValueError(f"No supplier for raw material '{material}'")
        return min(candidates,
                   key=lambda s: float(s.get("supplier_cost", 0.0)))

    @staticmethod
    def _sS_params(scheme: Dict[str, Any]) -> Tuple[float, float]:
        vals = _param_values(scheme.get("parameters", {}))
        s = float(vals[0]) if len(vals) >= 1 else 0.0
        S = float(vals[1]) if len(vals) >= 2 else s
        return s, S

    # ── build one replication ──────────────────────────────

    def _build_once(self, seed: int):
        rng    = random.Random(seed)
        env    = simpy.Environment()
        logger = Logger(env, warm_up=self.warm_up)

        # ── classify materials ─────────────────────────────
        raw_names   = {m["name"] for m in
                       (self.cfg.get("raw_materials", []) or [])}
        inter_names = {m["name"] for m in
                       (self.cfg.get("intermediate_materials", []) or [])}
        prod_names  = {p["name"] for p in
                       (self.cfg.get("products", []) or [])}

        # ── identify facilities ────────────────────────────
        fac_cfg        = self.cfg.get("facility", []) or []
        mfg_facilities = [f for f in fac_cfg
                          if _lower(f.get("type", "")) == "manufacturing"]
        wh_facilities  = [f for f in fac_cfg
                          if _lower(f.get("type", "")) == "warehouse"]

        mfg_name = mfg_facilities[0]["name"] if mfg_facilities else "Manufacturing"
        wh_name  = wh_facilities[0]["name"]  if wh_facilities  else "Warehouse"

        # ── BOMs ───────────────────────────────────────────
        bom_inter = {
            m["name"]: {k: float(v) for k, v in (m.get("bom") or {}).items()}
            for m in (self.cfg.get("intermediate_materials", []) or [])
        }
        bom_prod = {
            p["name"]: {k: float(v) for k, v in (p.get("bom") or {}).items()}
            for p in (self.cfg.get("products", []) or [])
        }

        def bom_for(item: str) -> Dict[str, float]:
            return bom_inter.get(item, bom_prod.get(item, {}))

        # ── inventories ────────────────────────────────────
        inv_cfg = self.cfg.get("inventory", []) or []
        inv: Dict[str, Inventory] = {}
        for it in inv_cfg:
            c = it.get("inventory_costs", {}) or {}
            obj = Inventory(
                name          = it["name"],
                level         = float(it.get("initial_inventory", 0)),
                holding_cost  = float(c.get("holding_cost",  0)),
                shortage_cost = float(c.get("shortage_cost", 0)),
                review_time   = float(c.get("review_time", 1)) or 1.0,
            )
            inv[obj.name] = obj
            logger.inv_register(obj)

        # ── resources ──────────────────────────────────────
        res_cfg    = self.cfg.get("resource", []) or []
        resources:  Dict[str, FailableResource] = {}
        res_caps:   Dict[str, int]              = {}
        res_svc:    Dict[str, Dict]             = {}
        res_batch:  Dict[str, Dict]             = {}
        res_opcost: Dict[str, float]            = {}

        for r in res_cfg:
            svc     = r.get("service_time") or \
                      {"distribution": "constant", "parameters": {"a": 0}}
            zero    = _is_zero_constant(svc)
            failure = r.get("failure", {}) or {}
            up_spec = None if zero else (
                failure.get("uptime") if failure.get("enabled") else None)
            dn_spec = None if zero else (
                failure.get("downtime") if failure.get("enabled") else None)

            fr = FailableResource(
                env=env, name=r["name"],
                capacity=int(r.get("capacity", 1)),
                uptime_spec=up_spec, downtime_spec=dn_spec,
                rng=rng, logger=logger,
            )
            resources[r["name"]]  = fr
            res_caps[r["name"]]   = fr.capacity
            res_svc[r["name"]]    = svc
            res_batch[r["name"]]  = r.get("batching") or {"enabled": False}
            res_opcost[r["name"]] = float(r.get("operating_cost_per_time", 0.0))

        # ── backorder queues ───────────────────────────────
        backorders: Dict[str, deque] = {p: deque() for p in prod_names}

        # ── payment queues ─────────────────────────────────
        pending_payables:    List[Tuple[float, float]] = []
        pending_receivables: List[Tuple[float, float]] = []

        # ── on-order tracking ──────────────────────────────
        on_order: Dict[str, float] = defaultdict(float)

        # ── pending deliveries / shipments ─────────────────
        pending_deliveries: List[Tuple[float, str, float]] = []
        pending_shipments:  List[Tuple[float, str, float]] = []

        # ── work queue (production requests) ──────────────
        work_q: Dict[str, float] = defaultdict(float)

        # ── procurement scheme lookup ──────────────────────
        raw_schemes = {
            it["name"]: (it.get("procurement_scheme") or {})
            for it in inv_cfg if it["name"] in raw_names
        }
        all_schemes = {
            it["name"]: (it.get("procurement_scheme") or {})
            for it in inv_cfg
        }

        # ==========================================================
        # Helper functions
        # ==========================================================

        def schedule_delivery(t: float, mat: str, qty: float):
            bisect.insort(pending_deliveries, (t, mat, qty))

        def schedule_shipment(t: float, prod: str, qty: float):
            bisect.insort(pending_shipments, (t, prod, qty))

        def request_production(item: str, units: float):
            if units > 0:
                work_q[item] += units

        def can_consume(bom: Dict[str, float], units: float) -> bool:
            return all(
                inv[m].level >= q * units
                for m, q in bom.items() if m in inv
            )

        def consume_inputs(bom: Dict[str, float], units: float):
            for mat, q in bom.items():
                if mat in inv:
                    new_lvl = inv[mat].level - q * units
                    logger.inv_update(inv[mat], new_lvl)
                    inv[mat].level = new_lvl

        def add_to_inventory(item: str, qty: float):
            if item not in inv:
                inv[item] = Inventory(item, 0, 0, 0, 1)
                logger.inv_register(inv[item])
            new_lvl = inv[item].level + qty
            logger.inv_update(inv[item], new_lvl)
            inv[item].level = new_lvl

        def place_supplier_order(raw_mat: str, qty: float):
            if qty <= 0:
                return
            supplier  = self._best_supplier(raw_mat)
            unit_cost = float(supplier.get("supplier_cost", 0.0))

            # ── capacity check — inf means unlimited ───────
            raw_cap = supplier.get("supplier_capacity", float("inf"))
            try:
                raw_cap = float(raw_cap)
            except (TypeError, ValueError):
                raw_cap = float("inf")

            if raw_cap > 0 and not math.isinf(raw_cap):
                qty = min(qty, raw_cap)

            lead_spec = supplier.get("supplier_lead_time") or \
                        {"distribution": "constant", "parameters": {"a": 0}}
            lead     = sample_distribution(lead_spec, rng)
            transfer = sample_distribution(
                self._edge_transfer(supplier["name"], mfg_name, raw_mat), rng)

            arrival = env.now + lead + transfer
            if arrival <= env.now:
                add_to_inventory(raw_mat, qty)
            else:
                schedule_delivery(arrival, raw_mat, qty)
                on_order[raw_mat] += qty

            pay_spec = supplier.get("supplier_payment_lead_time")
            pay_t    = env.now + lead + (
                sample_distribution(pay_spec, rng) if pay_spec else 0.0)
            bisect.insort(pending_payables, (pay_t, unit_cost * qty))

        def fulfill_backorders(product: str):
            q = backorders.get(product, deque())
            while q and inv.get(product) and inv[product].level > 0:
                bo: Backorder = q[0]
                take = int(min(inv[product].level, bo.remaining))
                if take <= 0:
                    break
                new_lvl = inv[product].level - take
                logger.inv_update(inv[product], new_lvl)
                inv[product].level = new_lvl
                logger.record_delivery(take)

                recv_t = env.now
                bisect.insort(pending_receivables,
                              (recv_t, bo.unit_price * take))

                bo.remaining -= take
                if env.now >= self.warm_up:
                    logger.partial_wait_times.append(
                        env.now - bo.created_time)
                if bo.remaining <= 0:
                    if env.now >= self.warm_up:
                        logger.order_wait_times.append(
                            env.now - bo.created_time)
                    q.popleft()
                else:
                    break

        # ==========================================================
        # SimPy processes
        # ==========================================================

        # ── payment processor ──────────────────────────────
        def payment_loop():
            while True:
                t = env.now
                while pending_payables and pending_payables[0][0] <= t:
                    _, amt = pending_payables.pop(0)
                    logger.add_procurement_cost(amt)
                    logger.cash_update(logger.cash_balance - amt)
                while pending_receivables and pending_receivables[0][0] <= t:
                    _, amt = pending_receivables.pop(0)
                    logger.add_revenue(amt)
                    logger.cash_update(logger.cash_balance + amt)
                yield env.timeout(0.5)

        env.process(payment_loop())

        # ── delivery processor ─────────────────────────────
        def delivery_loop():
            while True:
                t = env.now
                while pending_deliveries and pending_deliveries[0][0] <= t:
                    _, mat, qty = pending_deliveries.pop(0)
                    add_to_inventory(mat, qty)
                    on_order[mat] = max(0.0, on_order[mat] - qty)
                yield env.timeout(1.0)

        env.process(delivery_loop())

        # ── shipment processor ─────────────────────────────
        def shipment_loop():
            while True:
                t = env.now
                while pending_shipments and pending_shipments[0][0] <= t:
                    _, prod, qty = pending_shipments.pop(0)
                    add_to_inventory(prod, qty)
                    fulfill_backorders(prod)
                yield env.timeout(1.0)

        env.process(shipment_loop())

        # ── inventory costing loop ─────────────────────────
        def costing_loop():
            tick = min(
                (max(1.0, inv[i].review_time)
                 for i in inv if inv[i].review_time > 0),
                default=1.0
            )
            while True:
                if env.now >= self.warm_up:
                    for item in inv.values():
                        if item.level > 0:
                            logger.add_holding_cost(
                                item.holding_cost * item.level * tick)
                        elif item.level < 0:
                            logger.add_shortage_cost(
                                item.shortage_cost * abs(item.level) * tick)
                yield env.timeout(tick)

        env.process(costing_loop())

        # ── daily inventory sampler ────────────────────────
        inv_daily_samples: Dict[str, List[float]] = defaultdict(list)

        def daily_sampler():
            while True:
                yield env.timeout(1.0)
                yield env.timeout(1e-6)
                if env.now >= self.warm_up:
                    for name, item in inv.items():
                        inv_daily_samples[name].append(item.level)

        env.process(daily_sampler())

        # ── raw material procurement ───────────────────────
        def raw_procurement_loop(raw_mat: str):
            scheme      = raw_schemes.get(raw_mat, {})
            scheme_type = _lower(scheme.get("type", "inventory_threshold"))
            arrival_spec = next(
                (it.get("procurement_arrival") for it in inv_cfg
                 if it["name"] == raw_mat), None)

            if arrival_spec and not _is_zero_constant(arrival_spec):
                review = max(1.0, sample_distribution(arrival_spec, rng))
            else:
                review = max(1.0, inv[raw_mat].review_time
                             if raw_mat in inv else 1.0)

            next_periodic = env.now

            while True:
                if scheme_type == "inventory_threshold":
                    s, S = self._sS_params(scheme)
                    pos  = inv[raw_mat].level + on_order[raw_mat] \
                           if raw_mat in inv else 0.0
                    if pos <= s:
                        qty = max(0.0, S - pos)
                        if qty > 0:
                            place_supplier_order(raw_mat, qty)

                elif scheme_type == "periodic_supply":
                    if env.now >= next_periodic:
                        dist_spec = {
                            "distribution": scheme.get("distribution",
                                                        "constant"),
                            "parameters":   scheme.get("parameters",
                                                        {"a": 0})
                        }
                        qty = int(max(0, math.floor(
                            sample_distribution(dist_spec, rng))))
                        if qty > 0:
                            place_supplier_order(raw_mat, qty)
                        next_periodic = env.now + review

                yield env.timeout(review)

        for rm in raw_names:
            if rm in inv:
                env.process(raw_procurement_loop(rm))

        # ── threshold controllers for inter + products ─────
        def threshold_controller(item: str):
            scheme      = all_schemes.get(item, {})
            scheme_type = _lower(scheme.get("type", ""))
            if scheme_type != "inventory_threshold":
                return
            s, S   = self._sS_params(scheme)
            review = max(1e-6, inv[item].review_time
                         if item in inv else 1.0)
            while True:
                if item in inv:
                    lvl = inv[item].level
                    if lvl <= s:
                        qty = int(max(0, math.floor(S - lvl)))
                        if qty > 0:
                            request_production(item, qty)
                yield env.timeout(review)

        for name in list(inter_names | prod_names):
            if name in inv:
                env.process(threshold_controller(name))

        # ── facility operations ────────────────────────────
        def operation_process(fac: Dict[str, Any]):
            if _lower(fac.get("type", "")) == "warehouse":
                return

            op       = fac.get("operation", {}) or {}
            outputs  = op.get("output", []) or []
            if len(outputs) != 1:
                return
            out_item = outputs[0]
            res_name = op.get("resource_required", "")

            no_resource = (
                not res_name or
                res_name.strip() == "" or
                _lower(res_name) == "none" or
                res_name not in resources
            )

            cycle_spec = op.get("operation_cycle")
            if not cycle_spec or _is_zero_constant(cycle_spec):
                cycle_spec = res_svc.get(res_name,
                    {"distribution": "constant", "parameters": {"a": 1}})

            has_resource = not no_resource
            res          = resources.get(res_name) if has_resource else None
            svc_spec     = res_svc.get(res_name,
                {"distribution": "constant", "parameters": {"a": 0}}) \
                if has_resource else \
                {"distribution": "constant", "parameters": {"a": 0}}
            batch_spec   = res_batch.get(res_name, {"enabled": False}) \
                if has_resource else {"enabled": False}
            op_cost      = res_opcost.get(res_name, 0.0) \
                if has_resource else 0.0

            batching_on = bool(batch_spec.get("enabled", False))
            batch_size  = int(batch_spec.get("batch_size", -1))
            max_wait    = float(batch_spec.get("max_wait_time", 0.0))

            # ── flush mode: no resource OR batch_size = -1 ─
            if no_resource or (batching_on and batch_size == -1):
                while True:
                    # flush FIRST, then wait
                    bom = bom_for(out_item)
                    if bom:
                        max_units = float("inf")
                        for mat, q in bom.items():
                            if q > 0 and mat in inv:
                                max_units = min(max_units, inv[mat].level / q)

                        make = int(max(0, math.floor(
                            max_units if max_units != float("inf") else 0)))
                        if make > 0:
                            consume_inputs(bom, make)

                            if out_item in work_q:
                                work_q[out_item] = max(
                                    0.0, work_q[out_item] - make)

                            cycle = max(1e-6, sample_distribution(cycle_spec, rng))

                            if env.now >= self.warm_up:
                                logger.add_operating_cost(op_cost * cycle)

                            if out_item in inter_names:
                                transfer = sample_distribution(
                                    self._edge_transfer(
                                        mfg_name, mfg_name, out_item), rng)
                                if transfer > 0:
                                    yield env.timeout(transfer)
                                add_to_inventory(out_item, make)

                            elif out_item in prod_names:
                                transfer = sample_distribution(
                                    self._edge_transfer(
                                        mfg_name, wh_name, out_item), rng)
                                if transfer <= 0:
                                    add_to_inventory(out_item, make)
                                    fulfill_backorders(out_item)
                                else:
                                    schedule_shipment(
                                        env.now + transfer, out_item, make)

                    # always wait the cycle before next flush
                    cycle = max(1e-6, sample_distribution(cycle_spec, rng))
                    yield env.timeout(cycle)
                return

            # ── per-unit mode (resource present) ──────────
            wait_start: Optional[float] = None

            while True:
                needed = work_q.get(out_item, 0.0)

                if needed <= 0:
                    wait_start = None
                    yield env.timeout(1.0)
                    continue

                if batching_on and batch_size > 0:
                    if wait_start is None:
                        wait_start = env.now
                    enough = needed >= batch_size
                    waited = (env.now - wait_start) >= max_wait \
                             if max_wait > 0 else True
                    if not (enough or waited):
                        yield env.timeout(0.25)
                        continue
                    make = min(int(batch_size), int(needed))
                else:
                    make = int(needed)

                make = int(max(0, math.floor(make)))
                if make <= 0:
                    yield env.timeout(1.0)
                    continue

                bom = bom_for(out_item)
                if not bom:
                    work_q[out_item] = 0
                    yield env.timeout(1.0)
                    continue

                if not can_consume(bom, make):
                    for mat, q in bom.items():
                        if mat in raw_names:
                            scheme = raw_schemes.get(mat, {})
                            if _lower(scheme.get("type", "")) == \
                                    "demand_driven":
                                shortage = max(
                                    0.0, q * make -
                                    inv.get(mat,
                                        Inventory("", 0, 0, 0, 1)).level)
                                if shortage > 0:
                                    place_supplier_order(mat, shortage)
                    yield env.timeout(1.0)
                    continue

                consume_inputs(bom, make)
                work_q[out_item] -= make
                wait_start = None

                req = None
                if has_resource and not _is_zero_constant(svc_spec):
                    req = yield from res.acquire()

                cycle     = max(0.0, sample_distribution(cycle_spec, rng))
                svc_total = 0.0
                if has_resource and not _is_zero_constant(svc_spec):
                    for _ in range(make):
                        svc_total += sample_distribution(svc_spec, rng)

                total_time = max(cycle, svc_total)
                if env.now >= self.warm_up:
                    logger.add_operating_cost(op_cost * total_time)

                yield env.timeout(total_time)

                if req is not None:
                    res.release(req)

                if out_item in inter_names:
                    transfer = sample_distribution(
                        self._edge_transfer(mfg_name, mfg_name, out_item), rng)
                    if transfer <= 0:
                        add_to_inventory(out_item, make)
                    else:
                        yield env.timeout(transfer)
                        add_to_inventory(out_item, make)

                elif out_item in prod_names:
                    transfer = sample_distribution(
                        self._edge_transfer(mfg_name, wh_name, out_item), rng)
                    schedule_shipment(
                        env.now + max(0.0, transfer), out_item, make)
                else:
                    add_to_inventory(out_item, make)

        for fac in mfg_facilities:
            env.process(operation_process(fac))

        # ── customer processes ─────────────────────────────
        def customer_process(c: Dict[str, Any]):
            cname      = c.get("name", "Customer")
            product    = c.get("product")
            unit_price = float(c.get("unit_selling_price", 0.0))
            policy     = _lower(c.get("shortage_policy", "backorder"))
            arr_spec   = c.get("arrival_time")
            dem_spec   = c.get("demand")
            pay_spec   = c.get("customer_payment_lead_time")

            first = True
            while True:
                if not first:
                    yield env.timeout(interarrival_time(arr_spec, rng))
                first = False
                dem = int(max(1, math.floor(sample_distribution(dem_spec, rng))))

                if env.now >= self.warm_up:
                    logger.record_order(dem)

                if product not in inv:
                    inv[product] = Inventory(product, 0, 0, 0, 1)
                    logger.inv_register(inv[product])

                available = int(max(0, inv[product].level))

                def recv_payment(units: int):
                    recv_t = env.now + (
                        sample_distribution(pay_spec, rng)
                        if pay_spec else 0.0)
                    bisect.insort(pending_receivables,
                                  (recv_t, unit_price * units))

                # ── full demand satisfied ──────────────────
                if available >= dem:
                    new_lvl = inv[product].level - dem
                    logger.inv_update(inv[product], new_lvl)
                    inv[product].level = new_lvl
                    if env.now >= self.warm_up:
                        logger.record_delivery(dem)
                    recv_payment(dem)
                    continue

                # ── shortage policies ──────────────────────
                if policy in ("sale_lost", "salelost"):
                    if env.now >= self.warm_up:
                        logger.record_lost(dem)
                    continue

                if policy in ("sale_lost_partial",
                               "salelostpartial",
                               "sale_lost_partial_fulfillment",
                               "Sale_lost_partial_fulfillment"):
                    if available > 0:
                        new_lvl = inv[product].level - available
                        logger.inv_update(inv[product], new_lvl)
                        inv[product].level = new_lvl
                        if env.now >= self.warm_up:
                            logger.record_delivery(available)
                        recv_payment(available)
                    lost = dem - available
                    if lost > 0 and env.now >= self.warm_up:
                        logger.record_lost(lost)
                    continue

                if policy in ("backorder_partial",
                               "backorderpartial",
                               "backorder_partial_fulfillment"):
                    if available > 0:
                        new_lvl = inv[product].level - available
                        logger.inv_update(inv[product], new_lvl)
                        inv[product].level = new_lvl
                        if env.now >= self.warm_up:
                            logger.record_delivery(available)
                        recv_payment(available)
                    rem = dem - available
                    if rem > 0:
                        backorders[product].append(Backorder(
                            customer=cname, product=product,
                            units=dem, unit_price=unit_price,
                            policy=policy, created_time=env.now,
                            remaining=rem,
                        ))
                        request_production(product, rem)
                    continue

                # default: full backorder
                backorders[product].append(Backorder(
                    customer=cname, product=product,
                    units=dem, unit_price=unit_price,
                    policy=policy, created_time=env.now,
                    remaining=dem,
                ))
                request_production(product, dem)

        for c in (self.cfg.get("customer", []) or []):
            env.process(customer_process(c))

        return env, logger, inv, res_caps, backorders, inv_daily_samples

    # ── run one replication ────────────────────────────────

    def _run_rep(self, seed: int) -> Dict[str, Any]:
        env, logger, inv, res_caps, backorders, inv_daily_samples = \
            self._build_once(seed)
        env.run(until=self.horizon)

        logger.inv_finalize()
        logger.res_finalize()
        logger.cash_finalize()

        bo_end = {p: sum(bo.remaining for bo in q)
                  for p, q in backorders.items()}
        logger.backorder_units_end = int(sum(bo_end.values()))

        inv_avg = {
            name: (sum(samples) / len(samples) if samples else 0.0)
            for name, samples in inv_daily_samples.items()
        }
        util    = logger.res_utilization(self.horizon, res_caps)
        q_waits = {r: (sum(ws) / len(ws) if ws else 0.0)
                   for r, ws in logger.res_queue_waits.items()}
        ow      = logger.order_wait_times
        pw      = logger.partial_wait_times

        units_dem = max(logger.units_demanded, 1)
        fill_rate = logger.units_delivered / units_dem

        return {
            "kpis": {
                "revenue":             logger.revenue,
                "procurement_cost":    logger.procurement_cost,
                "operating_cost":      logger.operating_cost,
                "holding_cost":        logger.holding_cost,
                "shortage_cost":       logger.shortage_cost,
                "profit":              logger.profit,
                "ending_cash":         logger.cash_balance,
                "avg_cash_balance":    logger.cash_time_avg(self.horizon),
                "orders":              logger.orders,
                "units_demanded":      logger.units_demanded,
                "units_delivered":     logger.units_delivered,
                "units_lost":          logger.units_lost,
                "backorder_units_end": logger.backorder_units_end,
                "fill_rate":           fill_rate,
            },
            "ending_inventory":  {n: inv[n].level for n in inv},
            "avg_inventory":     inv_avg,
            "resource_stats": {
                "utilization":    util,
                "avg_queue_wait": q_waits,
            },
            "order_wait": {
                "avg_full_order_wait":
                    sum(ow) / len(ow) if ow else 0.0,
                "avg_partial_delivery_wait":
                    sum(pw) / len(pw) if pw else 0.0,
                "n_full_wait_samples":    len(ow),
                "n_partial_wait_samples": len(pw),
            },
        }

    # ── run all replications ───────────────────────────────

    def run(self) -> Dict[str, Any]:
        print(ASSUMPTIONS)
        print(f"Running {self.replications} replication(s) "
              f"— horizon {self.horizon}, warm-up {self.warm_up}...\n")

        rep_results = []
        for r in range(self.replications):
            print(f"  Replication {r + 1}/{self.replications}...")
            rep_results.append(self._run_rep(self.base_seed + r))

        # ── aggregate ──────────────────────────────────────
        kpi_keys = rep_results[0]["kpis"].keys()
        kpis_agg = {
            k: mean_ci([rr["kpis"][k] for rr in rep_results])
            for k in kpi_keys
        }

        inv_keys = set()
        for rr in rep_results:
            inv_keys |= set(rr["ending_inventory"])
        ending_agg = {
            k: mean_ci([rr["ending_inventory"].get(k, 0.0)
                        for rr in rep_results])
            for k in sorted(inv_keys)
        }
        avg_agg = {
            k: mean_ci([rr["avg_inventory"].get(k, 0.0)
                        for rr in rep_results])
            for k in sorted(inv_keys)
        }

        res_keys = set()
        for rr in rep_results:
            res_keys |= set(rr["resource_stats"]["utilization"])
        util_agg = {
            k: mean_ci([rr["resource_stats"]["utilization"].get(k, 0.0)
                        for rr in rep_results])
            for k in sorted(res_keys)
        }
        wait_agg = {
            k: mean_ci([rr["resource_stats"]["avg_queue_wait"].get(k, 0.0)
                        for rr in rep_results])
            for k in sorted(res_keys)
        }

        ow_agg = {
            k: mean_ci([rr["order_wait"][k] for rr in rep_results
                        if isinstance(rr["order_wait"].get(k), (int, float))])
            for k in ("avg_full_order_wait", "avg_partial_delivery_wait")
        }

        return {
            "status": f"{self.replications} replication(s) completed",
            "simulation_params": {
                "horizon":      self.horizon,
                "warm_up":      self.warm_up,
                "replications": self.replications,
            },
            "aggregated": {
                "kpis":                    kpis_agg,
                "ending_inventory":        ending_agg,
                "avg_inventory":           avg_agg,
                "resource_utilization":    util_agg,
                "resource_avg_queue_wait": wait_agg,
                "order_wait":              ow_agg,
            },
            "replication_results": rep_results,
        }


# ============================================================
# Output helpers
# ============================================================

def print_summary(results: Dict[str, Any]) -> None:
    agg = results["aggregated"]

    def fmt(stat: Dict) -> str:
        m   = stat["mean"]
        c90 = stat["ci90"]
        c95 = stat["ci95"]
        c99 = stat["ci99"]
        return (f"{m:>14.2f}   "
                f"CI90[{c90[0]:.2f}, {c90[1]:.2f}]   "
                f"CI95[{c95[0]:.2f}, {c95[1]:.2f}]   "
                f"CI99[{c99[0]:.2f}, {c99[1]:.2f}]")

    print("\n" + "=" * 80)
    print("SIMULATION RESULTS SUMMARY")
    print("=" * 80)

    print("\n--- Financial KPIs ---")
    for k in ("revenue", "procurement_cost", "operating_cost",
              "holding_cost", "shortage_cost", "profit",
              "ending_cash", "avg_cash_balance"):
        print(f"  {k:<28} {fmt(agg['kpis'][k])}")

    print("\n--- Service KPIs ---")
    for k in ("orders", "units_demanded", "units_delivered",
              "units_lost", "backorder_units_end", "fill_rate"):
        print(f"  {k:<28} {fmt(agg['kpis'][k])}")

    print("\n--- Average Inventory Levels ---")
    for k, v in agg["avg_inventory"].items():
        print(f"  {k:<28} {fmt(v)}")

    print("\n--- Ending Inventory Levels ---")
    for k, v in agg["ending_inventory"].items():
        print(f"  {k:<28} {fmt(v)}")

    if agg["resource_utilization"]:
        print("\n--- Resource Utilization ---")
        for k, v in agg["resource_utilization"].items():
            print(f"  {k:<28} {fmt(v)}")

        print("\n--- Resource Avg Queue Wait ---")
        for k, v in agg["resource_avg_queue_wait"].items():
            print(f"  {k:<28} {fmt(v)}")

    print("\n--- Order Wait Times ---")
    for k, v in agg["order_wait"].items():
        print(f"  {k:<28} {fmt(v)}")

    print("=" * 80)


def _json_default(obj: Any) -> Any:
    """JSON serializer that handles float('inf') and float('nan')."""
    if isinstance(obj, float):
        if math.isinf(obj):
            return None   # inf → null in JSON
        if math.isnan(obj):
            return None
    return str(obj)


def save_results(results: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=_json_default)
    print(f"\nFull results saved → {path}")


# ============================================================
# Public API (importable)
# ============================================================

def run_simulation(config: Dict[str, Any],
                   output_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Run the full simulation pipeline.

    Parameters
    ----------
    config      : dict  — supply chain JSON config (raw or filtered)
                          'missing' placeholders are handled automatically
    output_path : str   — optional path to save results JSON

    Returns
    -------
    dict with keys: status, simulation_params, aggregated,
                    replication_results
    """
    engine  = SupplyChainEngine(config)
    results = engine.run()
    print_summary(results)
    if output_path:
        save_results(results, output_path)
    return results


# ============================================================
# CLI entry point
# ============================================================

if __name__ == "__main__":
    import argparse
    import os
    import sys

    THIS_DIR = os.path.dirname(os.path.abspath(__file__))
    if THIS_DIR not in sys.path:
        sys.path.insert(0, THIS_DIR)

    parser = argparse.ArgumentParser(
        description="Run NL2Sim supply chain DES simulation."
    )
    parser.add_argument("config_file",
                        help="Path to validated JSON config file")
    parser.add_argument("--output", "-o", default=None,
                        help="Path to save full results JSON "
                             "(default: auto-generated in outputs/)")
    args = parser.parse_args()

    with open(args.config_file, encoding="utf-8") as f:
        config = json.load(f)

    if args.output:
        out_path = args.output
    else:
        stem     = Path(args.config_file).stem
        out_path = str(
            Path(args.config_file).parent.parent /
            "outputs" / f"{stem}_results.json"
        )

    run_simulation(config, output_path=out_path)