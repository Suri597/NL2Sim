
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
import re
import json

# ============================================================
# Utilities
# ============================================================

def _parse_path(path: str) -> List[Tuple[str, Optional[int]]]:
    out: List[Tuple[str, Optional[int]]] = []
    for part in path.split("."):
        m = re.match(r"^([a-zA-Z_]\w*)(?:\[(\d+)\])?$", part)
        if not m:
            out.append((part, None))
            continue
        key = m.group(1)
        idx = int(m.group(2)) if m.group(2) is not None else None
        out.append((key, idx))
    return out


def get_at_path(cfg: Dict[str, Any], path: str) -> Any:
    cur: Any = cfg
    for key, idx in _parse_path(path):
        cur = cur[key]
        if idx is not None:
            cur = cur[idx]
    return cur


def set_at_path(cfg: Dict[str, Any], path: str, value: Any) -> None:
    tokens = _parse_path(path)
    cur: Any = cfg
    for key, idx in tokens[:-1]:
        cur = cur[key]
        if idx is not None:
            cur = cur[idx]
    last_key, last_idx = tokens[-1]
    if last_idx is None:
        cur[last_key] = value
    else:
        cur[last_key][last_idx] = value


# ============================================================
# Human-readable context helpers
# ============================================================

def _name_for_entry(entry):
    if isinstance(entry, dict):
        n = entry.get("name")
        if n and str(n).strip().lower() != "missing":
            return str(n)
    return None


def _resolve_segment(segment, seq):
    mi = re.match(r"^\[(\d+)\]$", segment)
    mn = re.match(r"^\[name=(.+?)\]$", segment)

    if mi:
        idx   = int(mi.group(1))
        entry = seq[idx] if isinstance(seq, list) and idx < len(seq) else None
        name  = _name_for_entry(entry)
        return entry, (f"'{name}'" if name else f"[{idx}]")

    if mn:
        name  = mn.group(1)
        entry = next(
            (e for e in (seq or [])
             if isinstance(e, dict) and e.get("name") == name),
            None,
        )
        return entry, f"'{name}'"

    return None, segment


def describe_finding(cfg, path):
    """
    Convert a raw validator path into a full human-readable breadcrumb.

    Every level is shown. Array indices are replaced with the entry's
    name where one exists. Dots and brackets become ' → '.

    Examples
    --------
    inventory[2].procurement_scheme.parameters.a
        → Inventory → 'phosphorus' → Procurement scheme → Parameters → a

    supplier[1].supplier_lead_time.parameters.b
        → Supplier → 'AcmeChem' → Supplier lead time → Parameters → b

    products[0].bom.steel_sheet
        → Products → 'cpu_chip' → Bom → steel_sheet

    customer[0].shortage_policy
        → Customer → 'RetailCo' → Shortage policy

    edges[3].transfer_time.parameters.b
        → Edges → 'AcmeChem → Fab (steel_sheet)' → Transfer time → Parameters → b
    """
    try:
        parts = []
        node  = cfg
        tokens = re.findall(r"[a-zA-Z_]\w*(?:\[\d+\]|\[name=[^\]]+\])?", path)

        for token in tokens:
            km = re.match(r"^([a-zA-Z_]\w*)(\[.+\])?$", token)
            if not km:
                parts.append(token.replace("_", " ").capitalize())
                continue

            key     = km.group(1)
            bracket = km.group(2)

            child = node.get(key) if isinstance(node, dict) else (node if isinstance(node, list) else None)
            parts.append(key.replace("_", " ").capitalize())
            node = child

            if bracket:
                entry, entry_label = _resolve_segment(bracket, node)

                if key == "edges" and isinstance(entry, dict):
                    src = entry.get("source") or "?"
                    dst = entry.get("destination") or "?"
                    mat = entry.get("material_name")
                    edge_label = f"{src} → {dst}"
                    if mat and mat != "missing":
                        edge_label += f" ({mat})"
                    parts.append(f"'{edge_label}'")
                else:
                    parts.append(entry_label)

                node = entry

        return " → ".join(parts)

    except Exception:
        return path


# ============================================================
# Input helpers
# ============================================================

def _input_choice(prompt: str, choices: List[str], default: str) -> str:
    print(prompt)
    for i, c in enumerate(choices, 1):
        tag = " (default)" if c == default else ""
        print(f"  {i}) {c}{tag}")
    raw = input("Select option #: ").strip()
    if not raw:
        return default
    try:
        k = int(raw)
        if 1 <= k <= len(choices):
            return choices[k - 1]
    except Exception:
        pass
    return default


def _select_from_list(
    title: str,
    options: List[str],
    allow_multiple: bool = False,
) -> List[str]:
    if not options:
        print("No options available.")
        return []

    print(f"\n{title}")
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}")

    raw = input(
        "Select option number(s)"
        + (" (comma-separated): " if allow_multiple else ": ")
    ).strip()

    try:
        idxs = [int(x.strip()) - 1 for x in raw.split(",")]
        chosen = [options[i] for i in idxs if 0 <= i < len(options)]
        return chosen
    except Exception:
        print("Invalid selection.")
        return []


def _input_int(prompt: str) -> int:
    while True:
        raw = input(prompt).strip()
        try:
            return int(raw)
        except Exception:
            print("Please enter an integer.")


def _input_float(prompt: str) -> float:
    while True:
        raw = input(prompt).strip()
        try:
            return float(raw)
        except Exception:
            print("Please enter a number.")


# ============================================================
# Distribution parameter definitions
# ============================================================

DISTRIBUTION_PARAMS = {
    "constant":    [("a", "constant value")],
    "exponential": [("a", "rate (1/mean)")],
    "poisson":     [("a", "lambda (mean arrival rate)")],
    "normal":      [("a", "mean"), ("b", "standard deviation")],
    "uniform":     [("a", "minimum"), ("b", "maximum")],
    "triangular":  [("a", "minimum"), ("b", "mode"), ("c", "maximum")],
    "weibull":     [("a", "scale"), ("b", "shape")],
    "beta":        [("a", "alpha"), ("b", "beta")],
}

ALLOWED_DISTRIBUTIONS = list(DISTRIBUTION_PARAMS.keys())


def _prompt_distribution_parameters(dist: str) -> dict:
    """
    Prompt user only for the parameters relevant to the chosen distribution.
    All unused parameters default to 0.
    """
    params = {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}
    required = DISTRIBUTION_PARAMS.get(dist, [("a", "parameter a")])

    print(f"\nEnter parameters for {dist} distribution:")
    for key, label in required:
        while True:
            raw = input(f"  {key} ({label}): ").strip()
            if not raw:
                print(f"  {key} is required.")
                continue
            try:
                val = float(raw)
                if val < 0:
                    print(f"  Value must be non-negative. Try again.")
                    continue
                params[key] = val
                break
            except ValueError:
                print(f"  Invalid number. Try again.")

    return params


def _prompt_distribution_and_parameters() -> tuple:
    """
    Prompt user to select a distribution and enter its parameters.
    Returns (distribution_name, parameters_dict).
    """
    print("\nSelect distribution:")
    for i, d in enumerate(ALLOWED_DISTRIBUTIONS, 1):
        param_labels = ", ".join(
            f"{k}={label}" for k, label in DISTRIBUTION_PARAMS[d]
        )
        print(f"  {i}) {d}  [{param_labels}]")

    while True:
        raw = input("Select #: ").strip()
        try:
            idx = int(raw) - 1
            if not (0 <= idx < len(ALLOWED_DISTRIBUTIONS)):
                raise ValueError
            dist = ALLOWED_DISTRIBUTIONS[idx]
            break
        except ValueError:
            print("Invalid selection. Try again.")

    params = _prompt_distribution_parameters(dist)
    return dist, params


# ============================================================
# Finding interface
# ============================================================

@dataclass
class FindingLike:
    layer: str
    severity: str
    path: str
    message: str


ResolverFn = Callable[[Dict[str, Any], FindingLike], bool]


# ============================================================
# Resolver registry (decorator-based)
# ============================================================

class ResolverRegistry:
    def __init__(self) -> None:
        self._rules: List[
            Tuple[str, str, re.Pattern, re.Pattern, ResolverFn]
        ] = []

    def register(
        self,
        *,
        layer: str,
        severity: str,
        path_regex: str = r".*",
        message_regex: str = r".*",
    ):
        def deco(fn: ResolverFn) -> ResolverFn:
            self._rules.append(
                (
                    layer,
                    severity,
                    re.compile(path_regex),
                    re.compile(message_regex),
                    fn,
                )
            )
            return fn
        return deco

    def resolve(self, cfg: Dict[str, Any], finding: FindingLike) -> bool:
        for layer, severity, pr, mr, fn in self._rules:
            if finding.layer != layer:
                continue
            if finding.severity != severity:
                continue
            if not pr.search(finding.path):
                continue
            if not mr.search(finding.message):
                continue
            return fn(cfg, finding)
        return False


REGISTRY = ResolverRegistry()


# ============================================================
# Layer A resolvers
# ============================================================

@REGISTRY.register(
    layer="Layer0",
    severity="error",
    path_regex=r"^inventory\[\d+\]\.procurement_scheme$",
    message_regex=r"Missing required field",
)
@REGISTRY.register(
    layer="Layer0",
    severity="error",
    path_regex=r"^inventory\[\d+\]\.procurement_scheme$",
    message_regex=r"Expected <class 'dict'>",
)
def resolve_layer0_incomplete_procurement_scheme(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    import re

    m = re.match(r"inventory\[(\d+)\]", finding.path)
    if not m:
        return False

    idx = int(m.group(1))
    inventory = cfg.get("inventory", [])

    if idx >= len(inventory):
        return False

    item = inventory[idx]
    inv_type = item.get("type", "")

    # ── Skip non-raw-materials silently ───────────────────
    if inv_type != "raw_materials":
        print(f"\n  Skipping procurement scheme for '{item.get('name')}' — type '{inv_type}' does not require it.")
        item["procurement_scheme"] = {
            "type": "periodic_supply",
            "distribution": "constant",
            "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}
        }
        return True

    print("\n--- Missing procurement scheme ---")
    print(f"  {describe_finding(cfg, finding.path)}")

    PROCUREMENT_TYPES = [
        (
            "periodic_supply",
            "Supply arrives at regular intervals — define arrival distribution and quantity distribution"
        ),
        (
            "inventory_threshold",
            "Order placed when stock falls below minimum (s) — replenish up to maximum (S)"
        ),
        (
            "demand_driven",
            "Order placed based on customer demand — define quantity distribution"
        ),
    ]

    print("\nSelect procurement scheme type:")
    for i, (ptype, description) in enumerate(PROCUREMENT_TYPES, 1):
        print(f"  {i}) {ptype}")
        print(f"     {description}")

    while True:
        raw = input("Select #: ").strip()
        try:
            idx_p = int(raw) - 1
            if not (0 <= idx_p < len(PROCUREMENT_TYPES)):
                raise ValueError
            proc_type = PROCUREMENT_TYPES[idx_p][0]
            break
        except ValueError:
            print("Invalid selection. Try again.")

    ps = {"type": proc_type}

    if proc_type == "inventory_threshold":
        print(f"\nInventory threshold (s, S) policy:")
        while True:
            try:
                s = float(input("  a — minimum threshold (small s): ").strip())
                S = float(input("  b — maximum threshold (big S)  : ").strip())
                if S <= s:
                    print(f"  big S ({S}) must be greater than small s ({s}). Try again.")
                    continue
                break
            except ValueError:
                print("  Invalid number. Try again.")

        ps["distribution"] = "uniform"
        ps["parameters"]   = {"a": s, "b": S, "c": 0, "d": 0, "e": 0}

    elif proc_type in {"periodic_supply", "demand_driven"}:
        print(f"\nQuantity distribution for {proc_type}:")
        dist, params = _prompt_distribution_and_parameters()
        ps["distribution"] = dist
        ps["parameters"]   = params

    if proc_type == "periodic_supply":
        print("\nProcurement arrival distribution (how often orders arrive):")
        arrival_dist, arrival_params = _prompt_distribution_and_parameters()
        item["procurement_arrival"] = {
            "distribution": arrival_dist,
            "parameters":   arrival_params,
        }

    item["procurement_scheme"] = ps

    print(f"\nUpdated procurement_scheme:")
    print(json.dumps(ps, indent=2))
    return True


@REGISTRY.register(
    layer="Layer0",
    severity="error",
    path_regex=r"^customer\[\d+\]\.shortage_policy$",
    message_regex=r"Invalid shortage_policy",
)
def resolve_layer0_invalid_shortage_policy(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    import re

    m = re.match(r"customer\[(\d+)\]", finding.path)
    if not m:
        return False

    idx = int(m.group(1))
    customers = cfg.get("customer", [])

    if idx >= len(customers):
        return False

    customer = customers[idx]

    print("\n--- Invalid shortage policy ---")
    print(f"  {describe_finding(cfg, finding.path)}")
    print(f"  Current value : {customer.get('shortage_policy')}")

    SHORTAGE_POLICIES = [
        (
            "backorder",
            "Customer waits until product inventory is enough to fulfill the full order"
        ),
        (
            "sale_lost",
            "Customer leaves without any sale if demand cannot be fulfilled"
        ),
        (
            "lost_sales",
            "Customer leaves without any sale if demand cannot be fulfilled"
        ),
        (
            "backorder_partial",
            "Order partially fulfilled immediately, remainder fulfilled when stock is available"
        ),
        (
            "sale_lost_partial",
            "Order partially fulfilled from available stock, remainder is lost"
        ),
        (
            "sale_lost_partial_fulfillment",
            "Order partially fulfilled from available stock, remainder is lost"
        ),
        (
            "Sale_lost_partial_fulfillment",
            "Order partially fulfilled from available stock, remainder is lost"
        ),
    ]

    print("\nSelect shortage policy:")
    for i, (policy, description) in enumerate(SHORTAGE_POLICIES, 1):
        print(f"  {i}) {policy}")
        print(f"     {description}")

    while True:
        raw = input("Select #: ").strip()
        try:
            idx_p = int(raw) - 1
            if not (0 <= idx_p < len(SHORTAGE_POLICIES)):
                raise ValueError
            chosen = SHORTAGE_POLICIES[idx_p][0]
            break
        except ValueError:
            print("Invalid selection. Try again.")

    customer["shortage_policy"] = chosen
    print(f"\nUpdated shortage_policy → '{chosen}'")
    return True


@REGISTRY.register(
    layer="Layer0",
    severity="error",
    path_regex=r"^facility\[\d+\]\.type$",
    message_regex=r"Unknown facility type",
)
def resolve_layer0_invalid_facility_type(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    import re

    m = re.match(r"facility\[(\d+)\]", finding.path)
    if not m:
        return False

    idx = int(m.group(1))
    facilities = cfg.get("facility", [])

    if idx >= len(facilities):
        return False

    fac = facilities[idx]

    print("\n--- Invalid facility type ---")
    print(f"  {describe_finding(cfg, finding.path)}")
    print(f"  Current type : {fac.get('type')}")

    print("\nSelect facility type:")
    print("  1) manufacturing")
    print("     A production facility that transforms raw/intermediate materials into products")
    print("  2) warehouse")
    print("     A storage facility that holds finished products for customer fulfillment")

    while True:
        raw = input("Select #: ").strip()
        if raw == "1":
            fac["type"] = "manufacturing"
            break
        elif raw == "2":
            fac["type"] = "warehouse"
            break
        else:
            print("Invalid selection. Please enter 1 or 2.")

    print(f"Updated facility type → '{fac['type']}'")
    return True


# ============================================================
# Layer A / Layer 0 resolvers (ORDER MATTERS)
# ============================================================

# ------------------------------------------------------------
# 1️⃣ SPECIFIC: customer_lead_time.distribution (dropdown)
# ------------------------------------------------------------
@REGISTRY.register(
    layer="Layer0",
    severity="missing_required",
    path_regex=r"^customer\[\d+\]\.customer_lead_time\.distribution$",
)
def resolve_layer0_customer_lead_time_distribution(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    import re

    m = re.match(
        r"customer\[(\d+)\]\.customer_lead_time\.distribution",
        finding.path,
    )
    if not m:
        return False

    idx = int(m.group(1))
    customers = cfg.get("customer", [])

    if idx >= len(customers):
        return False

    customer = customers[idx]

    print("\n--- Missing customer lead time distribution ---")
    print(f"  {describe_finding(cfg, finding.path)}")

    ALLOWED_DISTS = [
        "poisson",
        "exponential",
        "normal",
        "uniform",
        "weibull",
        "beta",
        "constant",
    ]

    print("\nSelect customer_lead_time distribution:")
    for i, d in enumerate(ALLOWED_DISTS, 1):
        print(f"  {i}) {d}")

    try:
        sel = int(input("Select #: ").strip()) - 1
        dist = ALLOWED_DISTS[sel]
    except Exception:
        print("Invalid selection.")
        return False

    clt = customer.setdefault("customer_lead_time", {})
    clt["distribution"] = dist

    print("\nEnter distribution parameters (leave blank to skip):")
    params = clt.setdefault("parameters", {})

    for p in ["a", "b", "c", "d", "e"]:
        raw = input(f"  {p}: ").strip()
        if raw:
            try:
                params[p] = float(raw)
            except Exception:
                print(f"Invalid value for parameter '{p}'.")
                return False

    print("\nUpdated customer_lead_time:")
    print(clt)

    return True


# ------------------------------------------------------------
# 2️⃣ GENERIC FALLBACK (LAST — DO NOT MOVE UP)
# ------------------------------------------------------------
@REGISTRY.register(
    layer="Layer0",
    severity="missing_required",
)
def resolve_layer0_missing_required(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    """
    Generic fallback for missing required fields.
    MUST be last so that specific resolvers run first.
    """
    print("\n--- Missing required field ---")
    print(f"  {describe_finding(cfg, finding.path)}")

    raw = input("  Enter value: ").strip()

    if raw.lower() in {"true", "false"}:
        val: Any = raw.lower() == "true"
    else:
        try:
            val = int(raw)
        except Exception:
            try:
                val = float(raw)
            except Exception:
                val = raw

    set_at_path(cfg, finding.path, val)
    return True


@REGISTRY.register(
    layer="Layer0",
    severity="error",
    message_regex=r"Missing required field",
)
@REGISTRY.register(
    layer="Layer0",
    severity="error",
    path_regex=r"^inventory\[\d+\]\.procurement_scheme\.type$",
    message_regex=r"Invalid procurement type",
)
def resolve_layer0_invalid_procurement_type(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    import re

    m = re.match(r"inventory\[(\d+)\]", finding.path)
    if not m:
        return False

    idx = int(m.group(1))
    inventory = cfg.get("inventory", [])

    if idx >= len(inventory):
        return False

    item = inventory[idx]
    inv_type = item.get("type", "")

    # ── skip non-raw-materials silently ───────────────────
    if inv_type != "raw_materials":
        item.setdefault("procurement_scheme", {})["type"] = "periodic_supply"
        return True

    print("\n--- Invalid procurement type ---")
    print(f"  {describe_finding(cfg, finding.path)}")
    print(f"  Current type : {item.get('procurement_scheme', {}).get('type')}")

    PROCUREMENT_TYPES = [
        (
            "periodic_supply",
            "Supply arrives at regular intervals — define quantity and arrival distributions"
        ),
        (
            "inventory_threshold",
            "Order placed when stock falls below minimum (s) — replenish up to maximum (S)"
        ),
        (
            "demand_driven",
            "Order placed based on customer demand"
        ),
    ]

    print("\nSelect procurement scheme type:")
    for i, (ptype, description) in enumerate(PROCUREMENT_TYPES, 1):
        print(f"  {i}) {ptype}")
        print(f"     {description}")

    while True:
        raw = input("Select #: ").strip()
        try:
            idx_p = int(raw) - 1
            if not (0 <= idx_p < len(PROCUREMENT_TYPES)):
                raise ValueError
            proc_type = PROCUREMENT_TYPES[idx_p][0]
            break
        except ValueError:
            print("Invalid selection. Try again.")

    item["procurement_scheme"]["type"] = proc_type
    print(f"Updated procurement_scheme.type → '{proc_type}'")
    return True


def resolve_layer0_missing_required_field_error(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    """
    Generic resolver for 'Missing required field' errors.
    Prompts user to enter a value and patches the config.
    """
    print("\n--- Missing required field ---")
    print(f"  {describe_finding(cfg, finding.path)}")

    raw = input("  Enter value: ").strip()

    if not raw:
        print("No value entered — skipping.")
        return False

    if raw.lower() in {"true", "false"}:
        val: Any = raw.lower() == "true"
    else:
        try:
            val = int(raw)
        except Exception:
            try:
                val = float(raw)
            except Exception:
                val = raw

    set_at_path(cfg, finding.path, val)
    print(f"  ✓ Set → {val}")
    return True


@REGISTRY.register(
    layer="Layer0",
    severity="error",
    path_regex=r"\.distribution$",
    message_regex=r"Invalid distribution",
)
def resolve_layer0_invalid_distribution_numeric(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    """
    Universal resolver for ALL invalid *.distribution fields.
    Forces numeric selection from allowed distributions.
    """
    ALLOWED_DISTS = [
        "poisson",
        "exponential",
        "normal",
        "uniform",
        "weibull",
        "beta",
        "constant",
    ]

    print("\n--- Invalid distribution detected ---")
    print(f"  {describe_finding(cfg, finding.path)}")
    print(f"  Issue: {finding.message}")

    print("\nSelect a valid distribution:")
    for i, d in enumerate(ALLOWED_DISTS, 1):
        print(f"  {i}) {d}")

    raw = input("Select option #: ").strip()

    try:
        idx = int(raw) - 1
        if idx < 0 or idx >= len(ALLOWED_DISTS):
            raise ValueError
        chosen = ALLOWED_DISTS[idx]
    except Exception:
        print("Invalid selection.")
        return False

    set_at_path(cfg, finding.path, chosen)
    print(f"\nUpdated distribution → '{chosen}'")
    return True


@REGISTRY.register(
    layer="Layer0",
    severity="error",
    path_regex=r"^supplier\[\d+\]\.supplier_payment_lead_time$",
)
def resolve_layer0_supplier_payment_lead_time(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    import re

    m = re.match(r"supplier\[(\d+)\]\.supplier_payment_lead_time", finding.path)
    if not m:
        return False

    idx = int(m.group(1))
    suppliers = cfg.get("supplier", [])

    if idx >= len(suppliers):
        return False

    supplier = suppliers[idx]

    print("\n--- Missing supplier payment lead time ---")
    print(f"  {describe_finding(cfg, finding.path)}")

    ALLOWED_DISTS = [
        "poisson",
        "exponential",
        "normal",
        "uniform",
        "weibull",
        "beta",
        "triangular",
        "constant",
    ]

    print("\nSelect payment lead time distribution:")
    for i, d in enumerate(ALLOWED_DISTS, 1):
        print(f"  {i}) {d}")

    try:
        dsel = int(input("Select #: ").strip()) - 1
        dist = ALLOWED_DISTS[dsel]
    except Exception:
        print("Invalid distribution selection.")
        return False

    print("\nEnter distribution parameters (press Enter to skip optional ones):")
    params = {}

    for p in ["a", "b", "c", "d", "e"]:
        raw = input(f" {p}: ").strip()
        if raw:
            try:
                params[p] = float(raw)
            except Exception:
                print(f"Invalid value for parameter '{p}'.")
                return False

    supplier["supplier_payment_lead_time"] = {
        "distribution": dist,
        "parameters": params,
    }

    print("\nAdded supplier_payment_lead_time:")
    print(supplier["supplier_payment_lead_time"])

    return True


@REGISTRY.register(
    layer="Layer0",
    severity="error",
    path_regex=r"^customer\[\d+\]\.customer_payment_lead_time$",
)
def resolve_layer0_customer_payment_lead_time(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    print("\n--- Missing customer payment lead time ---")
    print(f"  {describe_finding(cfg, finding.path)}")

    dist = input(
        "Enter customer_payment_lead_time distribution "
        "(poisson, exponential, normal, uniform, weibull, beta, constant): "
    ).strip()

    if not dist:
        print("Distribution is required.")
        return False

    params = {}
    print("Enter distribution parameters (leave blank to skip):")
    for p in ["a", "b", "c", "d", "e"]:
        val = input(f"  {p}: ").strip()
        if val:
            try:
                params[p] = float(val)
            except Exception:
                print(f"Invalid value for parameter '{p}'.")
                return False

    payment_lead_time = {
        "distribution": dist,
        "parameters": params,
    }

    set_at_path(cfg, finding.path, payment_lead_time)

    print("\nAdded customer_payment_lead_time:")
    print(payment_lead_time)

    return True


# ============================================================
# Layer B resolvers
# ============================================================

@REGISTRY.register(
    layer="LayerB",
    severity="error",
    path_regex=r"^nodes\[\d+\]\.warehouse$",
    message_regex=r"Invalid node category 'warehouse'",
)
def resolve_layerb_invalid_warehouse_node(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    import re

    m = re.match(r"nodes\[(\d+)\]\.warehouse", finding.path)
    if not m:
        return False

    idx = int(m.group(1))
    nodes = cfg.get("nodes", [])

    if idx >= len(nodes):
        return False

    entry = nodes[idx]
    warehouses = entry.get("warehouse", [])

    if not isinstance(warehouses, list):
        print("Invalid warehouse node format.")
        return False

    print("\n--- Invalid warehouse node detected ---")
    print(f"  Node index : {idx}")
    print(f"  Warehouses : {warehouses}")

    print("\nChoose how to resolve:")
    print("  1) Delete warehouse node(s)")
    print("  2) Move warehouse node(s) to facility")
    print("  3) Move warehouse node(s) to supplier")
    print("  4) Abort")

    choice = input("Select option #: ").strip()

    if choice == "1":
        del entry["warehouse"]
        print("Deleted 'warehouse' node entry.")
        return True

    if choice == "2":
        entry.setdefault("facility", [])
        for w in warehouses:
            if w not in entry["facility"]:
                entry["facility"].append(w)
        del entry["warehouse"]
        print(f"Moved {warehouses} → nodes[{idx}].facility")
        return True

    if choice == "3":
        entry.setdefault("supplier", [])
        for w in warehouses:
            if w not in entry["supplier"]:
                entry["supplier"].append(w)
        del entry["warehouse"]
        print(f"Moved {warehouses} → nodes[{idx}].supplier")
        return True

    if choice == "4":
        raise SystemExit("Aborted by user.")

    return False


@REGISTRY.register(
    layer="LayerB",
    severity="error",
    path_regex=r"^edges\[\d+\]\.source$",
    message_regex=r"Edge source '(.+)' not found in nodes",
)
def resolve_layerb_missing_node_for_edge_source(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    import re

    m = re.search(r"Edge source '(.+)' not found in nodes", finding.message)
    if not m:
        return False

    node_name = m.group(1)

    print("\n--- Edge source missing from nodes ---")
    print(f"  {describe_finding(cfg, finding.path)}")
    print(f"  Missing node: {node_name}")

    is_supplier = any(
        s.get("name") == node_name for s in cfg.get("supplier", [])
        if isinstance(s, dict)
    )

    is_facility = any(
        f.get("name") == node_name for f in cfg.get("facility", [])
        if isinstance(f, dict)
    )

    print("\nChoose how to resolve:")
    opts = []

    if is_supplier:
        opts.append(("supplier", "Add to nodes as supplier (recommended)"))
    if is_facility:
        opts.append(("facility", "Add to nodes as facility"))

    opts.append(("change", "Change edge source"))
    opts.append(("abort", "Abort"))

    for i, (_, desc) in enumerate(opts, 1):
        print(f"  {i}) {desc}")

    try:
        choice = int(input("Select option #: ").strip()) - 1
        action, _ = opts[choice]
    except Exception:
        print("Invalid selection.")
        return False

    if action in {"supplier", "facility"}:
        nodes = cfg.setdefault("nodes", [])
        if not nodes:
            nodes.append({})
        entry = nodes[0]
        entry.setdefault(action, [])
        if node_name not in entry[action]:
            entry[action].append(node_name)
        print(f"Added '{node_name}' to nodes[0].{action}")
        return True

    if action == "change":
        valid_nodes = set()
        for entry in cfg.get("nodes", []):
            if isinstance(entry, dict):
                for lst in entry.values():
                    if isinstance(lst, list):
                        valid_nodes.update(lst)

        if not valid_nodes:
            print("No valid nodes available to replace source.")
            return False

        valid_nodes = sorted(valid_nodes)

        print("\nSelect new edge source:")
        for i, n in enumerate(valid_nodes, 1):
            print(f"  {i}) {n}")

        try:
            sel = int(input("Select #: ").strip()) - 1
            new_src = valid_nodes[sel]
        except Exception:
            print("Invalid selection.")
            return False

        edge_idx = int(re.search(r"edges\[(\d+)\]", finding.path).group(1))
        cfg["edges"][edge_idx]["source"] = new_src
        print(f"Edge source updated → '{new_src}'")
        return True

    if action == "abort":
        raise SystemExit("Aborted by user.")

    return False


@REGISTRY.register(
    layer="LayerC",
    severity="error",
    path_regex=r"facility\[\d+\]\(.*\)\.operation\(.*\)\.resource_required$",
    message_regex=r"resource_required '.*' is not defined in resource\[\]",
)
def resolve_layerc_unknown_resource_required(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    import re

    m = re.match(r"facility\[(\d+)\]", finding.path)
    if not m:
        return False

    idx        = int(m.group(1))
    facilities = cfg.get("facility", [])
    resources  = cfg.get("resource", [])

    if idx >= len(facilities):
        return False

    fac = facilities[idx]
    op  = fac.get("operation", {}) or {}

    if not resources:
        op["resource_required"] = ""
        print(f"  No resources defined — clearing resource_required for {fac.get('name')}")
        return True

    print(f"\n--- Unknown resource_required ---")
    print(f"  {describe_finding(cfg, finding.path)}")
    print(f"  Current : {op.get('resource_required')}")

    print("\nOptions:")
    print("  1) No resource required — clear this field")

    resource_names = [r.get("name") for r in resources if isinstance(r, dict)]
    for i, rname in enumerate(resource_names, 2):
        print(f"  {i}) Use resource: '{rname}'")

    while True:
        raw = input("Select #: ").strip()
        try:
            choice = int(raw)
            if choice == 1:
                op["resource_required"] = ""
                print("  resource_required cleared — no resource needed.")
                return True
            elif 2 <= choice <= len(resource_names) + 1:
                selected = resource_names[choice - 2]
                op["resource_required"] = selected
                print(f"  resource_required set to '{selected}'")
                return True
            else:
                print("Invalid selection. Try again.")
        except ValueError:
            print("Invalid selection. Try again.")


@REGISTRY.register(
    layer="LayerC",
    severity="error",
    path_regex=r"\.procurement_scheme$|\.procurement_arrival$|\.supplier_lead_time$|\.supplier_payment_lead_time$|\.arrival_time$|\.demand$|\.customer_lead_time$|\.customer_payment_lead_time$|\.service_time$|\.operation_cycle$",
    message_regex=r"distribution requires",
)
def resolve_layerc_invalid_distribution_parameters(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    import re

    print(f"\n--- Invalid distribution parameters ---")
    print(f"  {describe_finding(cfg, finding.path)}")
    print(f"  Issue : {finding.message}")

    block = None

    m = re.match(r"^(\w+)\[name=(.+?)\]\.(.+)$", finding.path)
    if m:
        section = m.group(1)
        name    = m.group(2)
        field   = m.group(3)

        entries = cfg.get(section, [])
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name") == name:
                block = entry.get(field)
                break

    if not isinstance(block, dict):
        print("  Could not locate distribution block.")
        return False

    dist = block.get("distribution")
    if not dist or dist == "missing":
        print("  Distribution type not set.")
        return False

    print(f"\n  Distribution : {dist}")

    while True:
        print(f"  Current params: {block.get('parameters', {})}")
        print(f"  Issue         : {finding.message}")
        print(f"\n  Please re-enter parameters for '{dist}' distribution:")

        new_params = _prompt_distribution_parameters(dist)

        a = new_params.get("a")
        b = new_params.get("b")
        c = new_params.get("c")
        valid = True

        if dist == "uniform" and isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if b <= a:
                print(f"\n  ✗ Invalid: b ({b}) must be greater than a ({a}). Try again.")
                valid = False

        elif dist == "normal" and isinstance(b, (int, float)):
            if b <= 0:
                print(f"\n  ✗ Invalid: std (b) must be > 0, got {b}. Try again.")
                valid = False

        elif dist == "triangular" and all(isinstance(x, (int, float)) for x in [a, b, c]):
            if not (a <= b <= c):
                print(f"\n  ✗ Invalid: must satisfy a <= b <= c, got a={a}, b={b}, c={c}. Try again.")
                valid = False

        elif dist in ("weibull", "beta"):
            if isinstance(a, (int, float)) and a <= 0:
                print(f"\n  ✗ Invalid: a must be > 0, got {a}. Try again.")
                valid = False
            elif isinstance(b, (int, float)) and b <= 0:
                print(f"\n  ✗ Invalid: b must be > 0, got {b}. Try again.")
                valid = False

        if valid:
            block["parameters"] = new_params
            print(f"\n  ✓ Updated parameters → {new_params}")
            return True


@REGISTRY.register(
    layer="LayerB",
    severity="error",
    path_regex=r"^edges\[\d+\]\.destination$",
    message_regex=r"Edge destination '(.+)' not found in nodes",
)
def resolve_layerb_missing_node_for_edge_destination(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    import re

    m = re.search(r"Edge destination '(.+)' not found in nodes", finding.message)
    if not m:
        return False

    node_name = m.group(1)

    print("\n--- Edge destination missing from nodes ---")
    print(f"  {describe_finding(cfg, finding.path)}")
    print(f"  Missing node : {node_name}")

    print("\nChoose how to resolve:")
    print("  1) Add to nodes as facility (recommended)")
    print("  2) Change edge destination")
    print("  3) Abort")

    choice = input("Select option #: ").strip()

    if choice == "1":
        nodes = cfg.setdefault("nodes", [])
        if not nodes:
            nodes.append({})
        entry = nodes[0]
        entry.setdefault("facility", [])
        if node_name not in entry["facility"]:
            entry["facility"].append(node_name)
        print(f"Added '{node_name}' to nodes[0].facility")
        return True

    if choice == "2":
        valid_nodes = set()
        for entry in cfg.get("nodes", []):
            if isinstance(entry, dict):
                for lst in entry.values():
                    if isinstance(lst, list):
                        valid_nodes.update(lst)

        if not valid_nodes:
            print("No valid nodes available.")
            return False

        valid_nodes = sorted(valid_nodes)

        print("\nSelect new edge destination:")
        for i, n in enumerate(valid_nodes, 1):
            print(f"  {i}) {n}")

        try:
            sel = int(input("Select #: ").strip()) - 1
            new_dst = valid_nodes[sel]
        except Exception:
            print("Invalid selection.")
            return False

        edge_idx = int(re.search(r"edges\[(\d+)\]", finding.path).group(1))
        cfg["edges"][edge_idx]["destination"] = new_dst
        print(f"Edge destination updated → '{new_dst}'")
        return True

    if choice == "3":
        raise SystemExit("Aborted by user.")

    return False


@REGISTRY.register(
    layer="Layer0",
    severity="error",
    message_regex=r"Distribution parameter must be int or float",
)
def resolve_layer0_invalid_parameter_type(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    print("\n--- Invalid distribution parameter ---")
    print(f"  {describe_finding(cfg, finding.path)}")
    print(f"  Current value: {get_at_path(cfg, finding.path)}")

    while True:
        raw = input("  Enter a valid non-negative number: ").strip()

        if not raw:
            print("No value entered — skipping.")
            return False

        try:
            try:
                val = int(raw)
            except ValueError:
                val = float(raw)
        except ValueError:
            print("Invalid — please enter a number.")
            continue

        if val < 0:
            print(f"Invalid — value must be non-negative (got {val}). Please try again.")
            continue

        break

    set_at_path(cfg, finding.path, val)
    print(f"  ✓ Set → {val}")
    return True


@REGISTRY.register(
    layer="LayerB",
    severity="error",
    path_regex=r"^nodes\[\d+\]\.supplier$",
    message_regex=r"Unknown supplier '(.+)' in nodes",
)
def resolve_layerb_unknown_supplier_in_nodes(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    import re

    m = re.search(r"Unknown supplier '(.+)' in nodes", finding.message)
    if not m:
        return False

    supplier_name = m.group(1)

    print(f"\n--- Unknown supplier in nodes ---")
    print(f"  Supplier '{supplier_name}' is in nodes but not in supplier section.")

    valid_suppliers = [
        s.get("name") for s in cfg.get("supplier", [])
        if isinstance(s, dict) and isinstance(s.get("name"), str)
    ]

    print("\nChoose how to resolve:")
    print(f"  1) Remove '{supplier_name}' from nodes (recommended)")
    print(f"  2) Keep it and add to supplier section manually")
    print(f"  3) Replace with an existing supplier")
    print(f"  4) Abort")

    choice = input("Select option #: ").strip()

    if choice == "1":
        for entry in cfg.get("nodes", []):
            if isinstance(entry, dict) and "supplier" in entry:
                entry["supplier"] = [
                    s for s in entry["supplier"] if s != supplier_name
                ]
        print(f"Removed '{supplier_name}' from nodes.")
        return True

    if choice == "2":
        print("Skipping — add supplier manually to the supplier section.")
        return False

    if choice == "3":
        if not valid_suppliers:
            print("No valid suppliers available to replace with.")
            return False

        print("\nSelect replacement supplier:")
        for i, s in enumerate(valid_suppliers, 1):
            print(f"  {i}) {s}")

        try:
            sel = int(input("Select #: ").strip()) - 1
            new_supplier = valid_suppliers[sel]
        except Exception:
            print("Invalid selection.")
            return False

        for entry in cfg.get("nodes", []):
            if isinstance(entry, dict) and "supplier" in entry:
                entry["supplier"] = [
                    new_supplier if s == supplier_name else s
                    for s in entry["supplier"]
                ]
        print(f"Replaced '{supplier_name}' → '{new_supplier}' in nodes.")
        return True

    if choice == "4":
        raise SystemExit("Aborted by user.")

    return False


@REGISTRY.register(
    layer="LayerB",
    severity="error",
    path_regex=r"^supplier$",
    message_regex=r"No supplier found for raw material '(.+)'",
)
def resolve_layerb_missing_supplier(cfg: Dict[str, Any], finding: FindingLike) -> bool:
    import re

    m = re.search(r"raw material '(.+)'", finding.message)
    if not m:
        return False

    raw = m.group(1)

    print(f"\nMissing supplier detected for raw material: '{raw}'")
    print("Choose how to resolve:")
    print("  1) Add a supplier for this raw material")
    print("  2) Delete raw material entirely")
    print("  3) Abort")

    choice = input("Select option #: ").strip()

    if choice == "1":
        sname = input("Enter new supplier name: ").strip()
        if not sname:
            print("Supplier name cannot be empty.")
            return False

        dist = input("Enter supplier lead time distribution (e.g., poisson): ").strip()
        if not dist:
            print("Distribution is required.")
            return False

        params = {}
        print("Enter supplier lead time parameters:")
        for p in ["a", "b", "c", "d", "e"]:
            val = input(f"  {p}: ").strip()
            if val:
                try:
                    params[p] = float(val)
                except Exception:
                    print(f"Invalid value for parameter '{p}'.")
                    return False

        try:
            capacity = int(input("Enter supplier capacity: ").strip())
            cost = float(input("Enter supplier cost: ").strip())
        except Exception:
            print("Invalid capacity or cost.")
            return False

        supplier = {
            "name": sname,
            "supply_material_name": raw,
            "supplier_lead_time": {
                "distribution": dist,
                "parameters": params,
            },
            "supplier_capacity": capacity,
            "supplier_cost": cost,
        }

        cfg.setdefault("supplier", []).append(supplier)

        nodes = cfg.setdefault("nodes", [])
        if nodes:
            entry = nodes[0]
            entry.setdefault("supplier", [])
            if sname not in entry["supplier"]:
                entry["supplier"].append(sname)

        print("\nAdded supplier:")
        print(supplier)
        return True

    if choice == "2":
        cfg["raw_materials"] = [
            r for r in cfg.get("raw_materials", [])
            if r.get("name") != raw
        ]
        cfg["inventory"] = [
            i for i in cfg.get("inventory", [])
            if i.get("name") != raw
        ]
        cfg["supplier"] = [
            s for s in cfg.get("supplier", [])
            if s.get("supply_material_name") != raw
        ]
        print(f"Deleted raw material '{raw}' and all dependent entries.")
        return True

    if choice == "3":
        raise SystemExit("Aborted by user.")

    return False


@REGISTRY.register(
    layer="LayerB",
    severity="error",
    path_regex=r"^edges\[\d+\]\.material_name$",
    message_regex=r"Supplier '(.+)' supplies '(.+)' but edge transports '(.+)'",
)
def _delete_edge_by_path(cfg: Dict[str, Any], path: str) -> bool:
    m = re.match(r"^edges\[(\d+)\]\.", path)
    if not m:
        print("Could not determine edge index from path:", path)
        return False

    idx = int(m.group(1))
    edges = cfg.get("edges", [])
    if not isinstance(edges, list) or idx >= len(edges):
        print("Edge index out of range.")
        return False

    removed = edges.pop(idx)
    print("Deleted edge:")
    print(removed)
    return True


def resolve_layerb_supplier_edge_material_mismatch(
    cfg: Dict[str, Any], finding: FindingLike
) -> bool:
    import re

    m = re.search(
        r"Supplier '(.+)' supplies '(.+)' but edge transports '(.+)'",
        finding.message,
    )
    if not m:
        return False

    supplier, supplied_material, edge_material = m.groups()

    print("\n--- Supplier / Edge material mismatch ---")
    print(f"  {describe_finding(cfg, finding.path)}")
    print(f"  Supplier          : {supplier}")
    print(f"  Supplied material : {supplied_material}")
    print(f"  Edge material     : {edge_material}")

    print("\nChoose how to resolve:")
    print("  1) Delete this edge")
    print("  2) Change edge material_name to supplier's material")
    print("  3) Change supplier (edge source)")
    print("  4) Abort")

    choice = input("Select option #: ").strip()

    if choice == "1":
        return _delete_edge_by_path(cfg, finding.path)

    if choice == "2":
        set_at_path(cfg, finding.path, supplied_material)
        print(f"Edge material updated: '{edge_material}' → '{supplied_material}'")
        return True

    if choice == "3":
        valid_suppliers = [
            s["name"]
            for s in cfg.get("supplier", [])
            if s.get("supply_material_name") == edge_material
        ]

        if not valid_suppliers:
            print(f"No supplier supplies '{edge_material}'. Cannot reassign supplier.")
            return False

        print("\nSelect new supplier:")
        for i, s in enumerate(valid_suppliers, 1):
            print(f"  {i}) {s}")

        try:
            idx = int(input("Select #: ").strip()) - 1
            new_supplier = valid_suppliers[idx]
        except Exception:
            print("Invalid selection.")
            return False

        edge_idx = int(re.search(r"edges\[(\d+)\]", finding.path).group(1))
        cfg["edges"][edge_idx]["source"] = new_supplier
        print(f"Edge source updated: '{supplier}' → '{new_supplier}'")
        return True

    if choice == "4":
        raise SystemExit("Aborted by user.")

    return False


@REGISTRY.register(
    layer="LayerB",
    severity="error",
    path_regex=r"^inventory\[\d+\]\.type$",
    message_regex=r"is intermediate_materials but inventory.type is 'products'|"
                  r"is raw_materials but inventory.type is 'products'|"
                  r"is products but inventory.type is '.+'",
)
def resolve_layerb_inventory_type_mismatch(cfg: Dict[str, Any], finding: FindingLike) -> bool:
    import re

    m = re.match(r"inventory\[(\d+)\]\.type", finding.path)
    if not m:
        return False

    idx = int(m.group(1))
    inventory = cfg.get("inventory", [])

    if idx >= len(inventory):
        return False

    inv_item = inventory[idx]
    mat_name = inv_item.get("name")

    print("\n--- Inventory type mismatch ---")
    print(f"  {describe_finding(cfg, finding.path)}")
    print(f"  Issue: {finding.message}")

    correct_type = None
    for sec, tname in [
        ("raw_materials", "raw_materials"),
        ("intermediate_materials", "intermediate_materials"),
        ("products", "products"),
    ]:
        for item in cfg.get(sec, []):
            if isinstance(item, dict) and item.get("name") == mat_name:
                correct_type = tname
                break

    print("\nChoose how to resolve:")
    print(f"  1) Change inventory.type → '{correct_type}' (recommended)")
    print("  2) Delete inventory entry")
    print("  3) Abort")

    choice = input("Select option #: ").strip()

    if choice == "1":
        inv_item["type"] = correct_type
        print(f"Updated inventory.type to '{correct_type}'")
        return True

    if choice == "2":
        removed = inventory.pop(idx)
        print("Deleted inventory entry:")
        print(removed)
        return True

    if choice == "3":
        raise SystemExit("Aborted by user.")

    return False


@REGISTRY.register(
    layer="LayerB",
    severity="error",
    path_regex=r"^edges\[\d+\]\.material_type$",
    message_regex=r"material_type '.+' mismatches '.+' category '.+'",
)
def resolve_layerb_edge_material_type_mismatch(cfg: Dict[str, Any], finding: FindingLike) -> bool:
    import re

    m = re.match(r"edges\[(\d+)\]\.material_type", finding.path)
    if not m:
        return False

    idx = int(m.group(1))
    edges = cfg.get("edges", [])

    if idx >= len(edges):
        return False

    edge = edges[idx]
    material = edge.get("material_name")

    print("\n--- Edge material_type mismatch ---")
    print(f"  {describe_finding(cfg, finding.path)}")
    print(f"  Issue        : {finding.message}")

    correct_type = None
    for sec, tname in [
        ("raw_materials", "raw_materials"),
        ("intermediate_materials", "intermediate_materials"),
        ("products", "products"),
    ]:
        for item in cfg.get(sec, []):
            if isinstance(item, dict) and item.get("name") == material:
                correct_type = tname
                break

    print("\nChoose how to resolve:")
    print(f"  1) Change material_type → '{correct_type}' (recommended)")
    print("  2) Change material_name")
    print("  3) Delete this edge")
    print("  4) Abort")

    choice = input("Select option #: ").strip()

    if choice == "1":
        edge["material_type"] = correct_type
        print(f"Updated material_type to '{correct_type}'")
        return True

    if choice == "2":
        allowed = set()
        for sec in ("raw_materials", "intermediate_materials", "products"):
            for item in cfg.get(sec, []):
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    allowed.add(item["name"])

        allowed = sorted(allowed)

        print("\nAllowed material names:")
        for i, mname in enumerate(allowed, 1):
            print(f"  {i}) {mname}")

        try:
            sel = int(input("Select #: ").strip()) - 1
            new_mat = allowed[sel]
        except Exception:
            print("Invalid selection.")
            return False

        edge["material_name"] = new_mat

        for sec, tname in [
            ("raw_materials", "raw_materials"),
            ("intermediate_materials", "intermediate_materials"),
            ("products", "products"),
        ]:
            if any(
                isinstance(x, dict) and x.get("name") == new_mat
                for x in cfg.get(sec, [])
            ):
                edge["material_type"] = tname
                break

        print(f"Updated edge to material '{new_mat}'")
        return True

    if choice == "3":
        removed = edges.pop(idx)
        print("Deleted edge:")
        print(removed)
        return True

    if choice == "4":
        raise SystemExit("Aborted by user.")

    return False


@REGISTRY.register(
    layer="LayerC",
    severity="error",
    path_regex=r"^customer\[\d+\]\.product",
    message_regex=r"Customer demands '.+' but it is not declared in products",
)
def resolve_layerc_customer_demands_nonproduct(cfg: Dict[str, Any], finding: FindingLike) -> bool:
    import re

    m1 = re.search(r"customer\[(\d+)\]", finding.path)
    m2 = re.search(r"demands '(.+)'", finding.message)

    if not m1 or not m2:
        return False

    idx = int(m1.group(1))
    bad_material = m2.group(1)

    customers = cfg.get("customer", [])
    if idx >= len(customers):
        return False

    cust = customers[idx]

    print("\n--- Customer demands non-product ---")
    print(f"  {describe_finding(cfg, finding.path)}")
    print(f"  Demands  : {bad_material}")

    products = [
        p.get("name")
        for p in cfg.get("products", [])
        if isinstance(p, dict) and isinstance(p.get("name"), str)
    ]

    print("\nChoose how to resolve:")
    print("  1) Change customer to demand a valid product")
    print("  2) Convert demanded material back to product")
    print("  3) Delete customer")
    print("  4) Abort")

    choice = input("Select option #: ").strip()

    if choice == "1":
        if not products:
            print("No products available.")
            return False

        print("\nAvailable products:")
        for i, p in enumerate(products, 1):
            print(f"  {i}) {p}")

        try:
            new_prod = products[int(input("Select #: ").strip()) - 1]
        except Exception:
            print("Invalid selection.")
            return False

        cust["product"] = new_prod
        print(f"Customer demand updated → '{new_prod}'")
        return True

    if choice == "2":
        cfg["intermediate_materials"] = [
            x for x in cfg.get("intermediate_materials", [])
            if x.get("name") != bad_material
        ]

        if bad_material not in products:
            cfg.setdefault("products", []).append({
                "name": bad_material,
                "bom": {},
            })

        print(f"Material '{bad_material}' converted back to product.")
        return True

    if choice == "3":
        removed = customers.pop(idx)
        print("Deleted customer:")
        print(removed)
        return True

    if choice == "4":
        raise SystemExit("Aborted by user.")

    return False


@REGISTRY.register(
    layer="LayerB",
    severity="error",
    path_regex=r"^producibility$",
    message_regex=r"is intermediate/product but has no producing facility operation",
)
def resolve_layerb_missing_producer(cfg: Dict[str, Any], finding: FindingLike) -> bool:
    import re

    m = re.search(r"'(.+)' is intermediate/product", finding.message)
    if not m:
        return False

    material = m.group(1)

    print("\n--- Missing producer detected ---")
    print(f"  Material: {material}")
    print(f"  Issue   : {finding.message}")

    print("\nChoose how to resolve:")
    print("  1) Add producing facility operation (recommended)")
    print("  2) Add inventory (exogenous supply)")
    print("  3) Delete material entirely")
    print("  4) Abort")

    choice = input("Select option #: ").strip()

    if choice == "1":
        facilities = cfg.setdefault("facility", [])

        print("\nSelect facility:")
        for i, f in enumerate(facilities, 1):
            print(f"  {i}) {f.get('name')}")
        print(f"  {len(facilities) + 1}) Add NEW facility")

        try:
            sel = int(input("Select #: ").strip()) - 1
        except Exception:
            print("Invalid selection.")
            return False

        if sel == len(facilities):
            fname = input("Enter new facility name: ").strip()
            if not fname:
                print("Facility name required.")
                return False

            fac = {
                "name": fname,
                "type": "manufacturing",
                "inventory_managed": [],
            }
            facilities.append(fac)
            print(f"Created new facility '{fname}'.")
        elif 0 <= sel < len(facilities):
            fac = facilities[sel]
        else:
            print("Invalid selection.")
            return False

        all_materials = sorted(
            {x.get("name") for sec in ("raw_materials", "intermediate_materials", "products")
            for x in cfg.get(sec, []) if isinstance(x, dict)}
        )

        inputs = _select_from_list(
            "Select input materials:",
            all_materials,
            allow_multiple=True,
        )

        if not inputs:
            print("At least one input material is required.")
            return False

        resources = cfg.setdefault("resource", [])
        resource_names = [r.get("name") for r in resources if isinstance(r, dict)]

        print("\nSelect resource_required:")
        for i, r in enumerate(resource_names, 1):
            print(f"  {i}) {r}")
        print(f"  {len(resource_names) + 1}) Add NEW resource")

        try:
            sel = int(input("Select #: ").strip()) - 1
        except Exception:
            print("Invalid selection.")
            return False

        if sel == len(resource_names):
            rname = input("Enter new resource name: ").strip()
            if not rname:
                print("Resource name required.")
                return False

            capacity = int(input("Enter resource capacity: ").strip())
            dist = input("Enter service_time distribution: ").strip()

            params = {}
            for p in ["a", "b", "c", "d", "e"]:
                val = input(f"  {p}: ").strip()
                if val:
                    params[p] = float(val)

            new_resource = {
                "name": rname,
                "capacity": capacity,
                "service_time": {
                    "distribution": dist,
                    "parameters": params,
                },
                "batching": {"enabled": False},
                "operating_cost_per_time": 1,
            }

            resources.append(new_resource)
            resource = rname
            print(f"Created new resource '{rname}'.")

        elif 0 <= sel < len(resource_names):
            resource = resource_names[sel]
        else:
            print("Invalid selection.")
            return False

        op = {
            "name": f"produce_{material}",
            "input": inputs,
            "output": [material],
            "resource_required": resource,
        }

        fac["operation"] = op

        inv = fac.setdefault("inventory_managed", [])
        if material not in inv:
            inv.append(material)

        print("\nAdded producing operation:")
        print(op)
        return True

    if choice == "2":
        qty = int(input("Enter initial inventory quantity: ").strip())

        inv = {
            "name": material,
            "type": (
                "products"
                if any(p.get("name") == material for p in cfg.get("products", []))
                else "intermediate_materials"
            ),
            "procurement_scheme": {"type": "exogenous", "parameters": {}},
            "initial_inventory": qty,
            "inventory_costs": {
                "holding_cost": 1,
                "shortage_cost": 1,
                "review_time": 1,
            },
        }

        cfg.setdefault("inventory", []).append(inv)
        print("\nAdded exogenous inventory:")
        print(inv)
        return True

    if choice == "3":
        for sec in ("products", "intermediate_materials"):
            cfg[sec] = [x for x in cfg.get(sec, []) if x.get("name") != material]

        cfg["inventory"] = [
            i for i in cfg.get("inventory", []) if i.get("name") != material
        ]

        print(f"Deleted material '{material}'.")
        return True

    if choice == "4":
        raise SystemExit("Aborted by user.")

    return False


@REGISTRY.register(
    layer="LayerB",
    severity="error",
    path_regex=r"^(intermediate_materials\[\d+\]\.bom\..+|products\[\d+\]\.bom\..+)$",
    message_regex=r"BOM references unknown material",
)
def resolve_layerb_unknown_bom_material(cfg: Dict[str, Any], finding: FindingLike) -> bool:
    print("\n--- Unknown BOM material ---")
    print(f"  {describe_finding(cfg, finding.path)}")

    unknown = finding.path.split(".")[-1]
    bom_path = ".".join(finding.path.split(".")[:-1])
    bom = get_at_path(cfg, bom_path)

    action = _input_choice(
        "How do you want to fix this?",
        ["Rename to a known material", "Delete this BOM entry", "Skip"],
        default="Rename to a known material",
    )

    if action == "Skip":
        return False

    if action == "Delete this BOM entry":
        bom.pop(unknown, None)
        return True

    new_name = input("Enter correct material name: ").strip()
    qty = bom.pop(unknown)
    bom[new_name] = qty
    return True


@REGISTRY.register(
    layer="LayerB",
    severity="error",
    path_regex=r"^nodes$",
    message_regex=r"Node '(.+)' is declared but does not appear in any edge",
)
def resolve_layerb_unused_node(cfg: Dict[str, Any], finding: FindingLike) -> bool:
    import re

    m = re.search(r"Node '(.+)'", finding.message)
    if not m:
        return False

    node = m.group(1)

    print("\nUnused node detected:", node)
    print("Choose how to resolve:")
    print("  1) Add an edge involving this node")
    print("  2) Remove node from nodes")
    print("  3) Abort")

    choice = input("Select option #: ").strip()

    if choice == "1":
        return _add_edge_for_node(cfg, node)

    if choice == "2":
        return _remove_node(cfg, node)

    if choice == "3":
        raise SystemExit("Aborted by user.")

    return False


def _remove_node(cfg: Dict[str, Any], node: str) -> bool:
    for entry in cfg.get("nodes", []):
        if not isinstance(entry, dict):
            continue
        for k, v in entry.items():
            if isinstance(v, list):
                entry[k] = [x for x in v if x != node]

    print(f"Removed node '{node}' from nodes.")
    return True


def _add_edge_for_node(cfg: Dict[str, Any], node: str) -> bool:
    edges = cfg.setdefault("edges", [])

    print("\nAdding new edge involving node:", node)

    direction = _input_choice(
        "Is this node the source or destination?",
        ["source", "destination"],
        default="source",
    )

    other = input("Enter the other node name: ").strip()
    material_name = input("Enter material_name: ").strip()

    material_type = _input_choice(
        "Select material_type:",
        ["raw_materials", "intermediate_materials", "products"],
        default="raw_materials",
    )

    distribution = input("Enter transfer_time distribution (e.g., poisson): ").strip()

    params = {}
    for p in ["a", "b", "c", "d", "e"]:
        try:
            params[p] = float(input(f"Enter parameter {p}: ").strip())
        except Exception:
            params[p] = 1.0

    edge = {
        "source": node if direction == "source" else other,
        "destination": other if direction == "source" else node,
        "material_type": material_type,
        "material_name": material_name,
        "transfer_time": {
            "distribution": distribution,
            "parameters": params,
        },
    }

    edges.append(edge)
    print("Added edge:")
    print(edge)

    return True


@REGISTRY.register(
    layer="LayerB",
    severity="error",
    path_regex=r"^edges$",
    message_regex=r"Supplier '(.+)' supplies '(.+)' but has no edge transporting it",
)
def resolve_layerb_missing_supplier_edge(cfg: Dict[str, Any], finding: FindingLike) -> bool:
    try:
        supplier = finding.message.split("'")[1]
        material = finding.message.split("'")[3]
    except Exception:
        print("Could not parse supplier/material from error message.")
        return False

    print(f"\nMissing transport edge detected:")
    print(f"  Supplier : {supplier}")
    print(f"  Material : {material}")

    print("\nChoose how to resolve:")
    print("  1) Add missing edge (recommended)")
    print("  2) Delete supplier")
    print("  3) Abort")

    choice = input("Select option #: ").strip()

    if choice == "1":
        nodes = set()
        for entry in cfg.get("nodes", []):
            if isinstance(entry, dict):
                for lst in entry.values():
                    if isinstance(lst, list):
                        nodes.update(lst)

        destinations = sorted(n for n in nodes if n != supplier)

        if not destinations:
            print("No valid destinations available.")
            return False

        print("\nSelect destination node:")
        for i, d in enumerate(destinations, 1):
            print(f"  {i}) {d}")

        try:
            dst = destinations[int(input("Select #: ").strip()) - 1]
        except Exception:
            print("Invalid destination selection.")
            return False

        dist = input(
            "Enter transfer_time distribution (e.g., poisson, normal): "
        ).strip()

        if not dist:
            print("Distribution is required.")
            return False

        print("Enter transfer_time parameters:")
        params = {}
        for p in ["a", "b", "c", "d", "e"]:
            val = input(f"  {p}: ").strip()
            if val:
                try:
                    params[p] = float(val)
                except Exception:
                    print(f"Invalid value for parameter '{p}'.")
                    return False

        edge = {
            "source": supplier,
            "destination": dst,
            "material_type": "raw_materials",
            "material_name": material,
            "transfer_time": {
                "distribution": dist,
                "parameters": params,
            },
        }

        cfg.setdefault("edges", []).append(edge)
        print("\nAdded edge:")
        print(edge)
        return True

    if choice == "2":
        cfg["supplier"] = [
            s for s in cfg.get("supplier", [])
            if s.get("name") != supplier
        ]

        for n in cfg.get("nodes", []):
            if isinstance(n, dict) and "supplier" in n:
                n["supplier"] = [x for x in n["supplier"] if x != supplier]

        print(f"Deleted supplier '{supplier}'.")
        return True

    if choice == "3":
        raise SystemExit("Aborted by user.")

    return False


@REGISTRY.register(
    layer="LayerB",
    severity="error",
    path_regex=r"^(intermediate_materials\[\d+\]\.bom\..+|products\[\d+\]\.bom\..+)$",
    message_regex=r"BOM quantity must be positive int",
)
def resolve_layerb_invalid_bom_qty(cfg: Dict[str, Any], finding: FindingLike) -> bool:
    print("\n--- Invalid BOM quantity ---")
    print(f"  {describe_finding(cfg, finding.path)}")

    bom_path = ".".join(finding.path.split(".")[:-1])
    key = finding.path.split(".")[-1]
    bom = get_at_path(cfg, bom_path)

    action = _input_choice(
        "Fix BOM quantity:",
        ["Enter positive integer", "Delete this BOM entry", "Skip"],
        default="Enter positive integer",
    )

    if action == "Skip":
        return False

    if action == "Delete this BOM entry":
        bom.pop(key, None)
        return True

    qty = _input_int(f"Enter quantity for '{key}': ")
    if qty <= 0:
        print("Quantity must be > 0.")
        return False

    bom[key] = qty
    return True


@REGISTRY.register(
    layer="LayerB",
    severity="error",
    path_regex=r"^raw_materials$",
    message_regex=r"Raw material '(.+)' does not appear in any BOM",
)
def resolve_layerb_unused_raw_material(cfg: Dict[str, Any], finding: FindingLike) -> bool:
    raw = finding.message.split("'")[1]

    print(f"\nUnused raw material detected: {raw}")
    print("Choose how to resolve:")
    print("  1) Add raw material to a BOM")
    print("  2) Delete raw material entirely")
    print("  3) Abort")

    choice = input("Select option #: ").strip()

    if choice == "1":
        targets = [
            x for sec in ("intermediate_materials", "products")
            for x in cfg.get(sec, [])
            if isinstance(x, dict) and "name" in x
        ]

        for i, t in enumerate(targets, 1):
            print(f"  {i}) {t['name']}")

        idx = int(input("Select #: ")) - 1
        qty = _input_int(f"Quantity of '{raw}': ")

        targets[idx].setdefault("bom", {})[raw] = qty
        return True

    if choice == "2":
        cfg["raw_materials"] = [r for r in cfg["raw_materials"] if r["name"] != raw]
        cfg["inventory"] = [i for i in cfg["inventory"] if i["name"] != raw]
        cfg["supplier"] = [s for s in cfg["supplier"] if s["supply_material_name"] != raw]
        return True

    raise SystemExit("Aborted by user.")


@REGISTRY.register(
    layer="LayerB",
    severity="error",
    path_regex=r"^edges\[\d+\]\.material_name$",
    message_regex=r"Unknown material '(.+)' in edge",
)
def resolve_layerb_unknown_edge_material(cfg: Dict[str, Any], finding: FindingLike) -> bool:
    import re

    m = re.search(r"Unknown material '(.+)'", finding.message)
    if not m:
        return False

    bad_material = m.group(1)

    print("\n--- Unknown material in edge ---")
    print(f"  {describe_finding(cfg, finding.path)}")
    print(f"  Unknown material: '{bad_material}'")

    allowed = set()

    for sec in ("raw_materials", "intermediate_materials", "products"):
        for item in cfg.get(sec, []):
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str):
                    allowed.add(name)

    if not allowed:
        print("No known materials available to rename to.")
        return False

    choices = sorted(allowed)

    print("\nAllowed material names:")
    for i, m in enumerate(choices, 1):
        print(f"  {i}) {m}")

    print("  0) Delete this edge")

    raw = input("Select option #: ").strip()

    if raw == "0":
        return _delete_edge(cfg, finding.path)

    try:
        idx = int(raw) - 1
        if not (0 <= idx < len(choices)):
            raise ValueError
    except Exception:
        print("Invalid selection.")
        return False

    new_material = choices[idx]
    set_at_path(cfg, finding.path, new_material)
    print(f"Replaced material '{bad_material}' → '{new_material}'")

    return True


def _delete_edge(cfg: Dict[str, Any], path: str) -> bool:
    import re

    m = re.match(r"edges\[(\d+)\]\.", path)
    if not m:
        print("Could not determine edge index.")
        return False

    idx = int(m.group(1))
    edges = cfg.get("edges", [])
    if not isinstance(edges, list) or idx >= len(edges):
        print("Edge index out of range.")
        return False

    removed = edges.pop(idx)
    print("Deleted edge:")
    print(removed)

    return True


# ============================================================
# Layer C resolvers
# ============================================================

@REGISTRY.register(
    layer="LayerC",
    severity="error",
    message_regex=r"Invalid \(s, S\) policy",
)
def resolve_layerc_inventory_threshold(cfg: Dict[str, Any], finding: FindingLike) -> bool:
    import re

    print("\n--- Invalid (s, S) policy ---")
    print(f"  {describe_finding(cfg, finding.path)}")

    m = re.search(r"inventory\[name=(.+?)\]", finding.path)
    if m:
        inv_name = m.group(1)
    else:
        m2 = re.search(r"inventory\[(\d+)\]", finding.path)
        if m2:
            idx = int(m2.group(1))
            inventory = cfg.get("inventory", [])
            if idx < len(inventory):
                inv_name = inventory[idx].get("name", "")
            else:
                print("Could not determine inventory item.")
                return False
        else:
            print("Could not determine inventory item from path.")
            return False

    inv = next(
        (i for i in cfg.get("inventory", []) if i.get("name") == inv_name),
        None
    )

    if not inv:
        print(f"Inventory item '{inv_name}' not found.")
        return False

    params = inv["procurement_scheme"].setdefault("parameters", {})

    print(f"  Current values : a (small s) = {params.get('a')}  |  b (big S) = {params.get('b')}")
    print("\nEnter new (s, S) values — big S must be greater than small s:")

    while True:
        try:
            a = float(input("  a — minimum threshold (small s): ").strip())
            b = float(input("  b — maximum threshold (big S)  : ").strip())
            if b <= a:
                print(f"  Invalid — big S ({b}) must be greater than small s ({a}). Try again.")
                continue
            break
        except ValueError:
            print("  Invalid number. Try again.")

    params["a"] = a
    params["b"] = b
    print(f"\nUpdated (s, S) → a={a}, b={b}")
    return True