# layer_b_checker.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Set, Tuple, Optional
from collections import defaultdict, deque
import json

# ============================================================
# Finding model (same style as Layer A/0)
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
# Layer B validator (Supply-chain structural validity)
# ============================================================

class LayerBValidator:
    
    LAYER = "LayerB"

    # You can adjust these policies:
    REQUIRE_SUPPLIER_FOR_EACH_RAW = True    # error if raw has no supplier; else warning
    REQUIRE_PRODUCER_FOR_NONRAW = True      # error if intermediate/product has no producer AND no inventory; else warning

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.findings: List[ValidationFinding] = []

        # caches
        # self.raw_set: Set[str] = set()
        # self.inter_set: Set[str] = set()
        # self.prod_set: Set[str] = set()
        self.all_materials: Set[str] = set()

        self.inventory_by_name: Dict[str, Dict[str, Any]] = {}
        self.supplier_by_name: Dict[str, Dict[str, Any]] = {}
        self.resource_by_name: Dict[str, Dict[str, Any]] = {}
        self.facility_list: List[Dict[str, Any]] = []
        self.products_list: List[Dict[str, Any]] = []
        self.intermediate_list: List[Dict[str, Any]] = []

        # nodes/edges
        self.node_names: Set[str] = set()
        self.edge_list: List[Dict[str, Any]] = []

    # -------------------------
    # Public API
    # -------------------------
    def _index_entities(self) -> None: 
        self.raw_set = {
            r.get("name")
            for r in self.config.get("raw_materials", [])
            if isinstance(r, dict) and "name" in r
        }

        self.inter_set = self.config.get("intermediate_materials", [])
        self.prod_set = self.config.get("products", [])

    def validate(self) -> Dict[str, List[ValidationFinding]]:
        self.findings.clear()
        self._index_entities()
        self._index_catalogs()
        self._check_unique_names()
        self._check_boms()
        self._check_inventory_links()
        self._check_suppliers()
        self._check_facilities_and_operations()
        self._check_nodes_edges()
        self._check_producibility()
        self._check_transformation_cycles()
        self._check_unused_raw_materials()

        errors = [f for f in self.findings if f.severity == "error"]
        warnings = [f for f in self.findings if f.severity == "warning"]
        return {"errors": errors, "warnings": warnings}

    # ============================================================
    # Indexing helpers
    # ============================================================

    def _index_catalogs(self) -> None:
        # raw_materials can be modeled either as:
        #   [{"name": "x"}, {"name": "y"}]
        # or (your example changed) maybe:
        #   [{"name": ["x","y","z"]}]
        self.raw_set = self._extract_names_from_section("raw_materials", "raw_materials")
        self.inter_set = self._extract_names_from_section("intermediate_materials", "intermediate_materials")
        self.prod_set = self._extract_names_from_section("products", "products")

        self.all_materials = set(self.raw_set) | set(self.inter_set) | set(self.prod_set)

        # inventory
        self.inventory_by_name = {}
        inv = self.config.get("inventory", [])
        if isinstance(inv, list):
            for i, item in enumerate(inv):
                if isinstance(item, dict):
                    n = item.get("name")
                    if isinstance(n, str):
                        self.inventory_by_name[n] = item

        # suppliers/resources/facilities
        self.supplier_by_name = {}
        sup = self.config.get("supplier", [])
        if isinstance(sup, list):
            for i, item in enumerate(sup):
                if isinstance(item, dict):
                    n = item.get("name")
                    if isinstance(n, str):
                        self.supplier_by_name[n] = item

        self.resource_by_name = {}
        res = self.config.get("resource", [])
        if isinstance(res, list):
            for i, item in enumerate(res):
                if isinstance(item, dict):
                    n = item.get("name")
                    if isinstance(n, str):
                        self.resource_by_name[n] = item

        fac = self.config.get("facility", [])
        self.facility_list = fac if isinstance(fac, list) else []

        prod = self.config.get("products", [])
        self.products_list = prod if isinstance(prod, list) else []

        inter = self.config.get("intermediate_materials", [])
        self.intermediate_list = inter if isinstance(inter, list) else []

        # nodes/edges
        self.node_names = self._collect_node_names()
        edges = self.config.get("edges", [])
        self.edge_list = edges if isinstance(edges, list) else []

    def _extract_names_from_section(self, key: str, path_prefix: str) -> Set[str]:
        out: Set[str] = set()
        sec = self.config.get(key, [])
        if not isinstance(sec, list):
            return out

        for i, item in enumerate(sec):
            if not isinstance(item, dict):
                continue
            name = item.get("name")

            # case A: "name": "green_feather"
            if isinstance(name, str):
                if name.strip().lower() != "missing":
                    out.add(name)

            # case B: "name": ["green_feather","black_feather"]
            elif isinstance(name, list):
                for j, v in enumerate(name):
                    if isinstance(v, str):
                        out.add(v)
                    else:
                        self._warn(f"{path_prefix}[{i}].name[{j}]", f"Non-string material name: {v}")

            else:
                # Layer A should catch types, but we keep safe
                self._warn(f"{path_prefix}[{i}].name", f"Unexpected name format: {type(name).__name__}")

        return out

    def _collect_node_names(self) -> Set[str]:
        """
        nodes format in your config:
        nodes = [{"supplier": ["A","B"], "facility": ["X"], "warehouse": ["W1"]}]
        """
        out: Set[str] = set()
        nodes = self.config.get("nodes", [])
        if not isinstance(nodes, list):
            return out

        for i, entry in enumerate(nodes):
            if not isinstance(entry, dict):
                continue
            for group_key, maybe_list in entry.items():
                if isinstance(maybe_list, list):
                    for j, n in enumerate(maybe_list):
                        if isinstance(n, str):
                            out.add(n)
                        else:
                            self._warn(f"nodes[{i}].{group_key}[{j}]", f"Non-string node name: {n}")
                else:
                    # Layer A should catch types
                    self._warn(f"nodes[{i}].{group_key}", f"Expected list of node names, got {type(maybe_list).__name__}")
        return out

    # ============================================================
    # 1) Unique naming / duplicates
    # ============================================================

    def _check_unique_names(self) -> None:
        self._check_duplicates_in_section("supplier", "supplier.name")
        self._check_duplicates_in_section("resource", "resource.name")
        # facility names might intentionally repeat (multiple operations in same facility).
        # We'll warn if name duplicates AND operation name duplicates in exact same facility.
        self._check_facility_duplicates()

        # materials sets are already sets; duplicates not possible after extraction
        # but we can check collisions between categories:
        overlaps = (self.raw_set & self.inter_set) | (self.raw_set & self.prod_set) | (self.inter_set & self.prod_set)
        for name in sorted(overlaps):
            self._err("materials", f"Material name '{name}' appears in multiple categories (raw/intermediate/products)")

    def _check_duplicates_in_section(self, section_key: str, path_for_msg: str) -> None:
        seen: Set[str] = set()
        sec = self.config.get(section_key, [])
        if not isinstance(sec, list):
            return
        for i, item in enumerate(sec):
            if not isinstance(item, dict):
                continue
            n = item.get("name")
            if not isinstance(n, str):
                continue
            if n in seen:
                self._err(f"{section_key}[{i}].name", f"Duplicate name '{n}' in section '{section_key}'")
            else:
                seen.add(n)

    def _check_facility_duplicates(self) -> None:
        """
        Warn only if the same facility defines the same operation
        with identical input and output materials.
        """

        seen: Set[Tuple[str, str, Tuple[str, ...], Tuple[str, ...]]] = set()

        for i, fac in enumerate(self.facility_list):
            if not isinstance(fac, dict):
                continue

            fname = fac.get("name")
            op = fac.get("operation") if isinstance(fac.get("operation"), dict) else {}
            opname = op.get("name")

            if not isinstance(fname, str) or not isinstance(opname, str):
                continue

            # normalize inputs and outputs
            inputs = self._to_str_list(op.get("input"), f"facility[{i}].operation.input")
            outputs = self._to_str_list(op.get("output"), f"facility[{i}].operation.output")

            norm_inputs = tuple(sorted(inputs))
            norm_outputs = tuple(sorted(outputs))

            key = (fname, opname, norm_inputs, norm_outputs)

            if key in seen:
                self._warn(
                    f"facility[{i}]",
                    f"Duplicate operation detected: facility='{fname}', "
                    f"operation='{opname}', inputs={list(norm_inputs)}, outputs={list(norm_outputs)}"
                )
            else:
                seen.add(key)


    # ============================================================
    # 2) BOM checks
    # ============================================================

    def _check_boms(self) -> None:
        # intermediate_materials bom
        for i, item in enumerate(self.intermediate_list):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            bom = item.get("bom")
            if not isinstance(name, str) or not isinstance(bom, dict):
                continue
            self._check_bom_dict(bom, f"intermediate_materials[{i}].bom", parent=name)

        # products bom
        for i, item in enumerate(self.products_list):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            bom = item.get("bom")
            if not isinstance(name, str) or not isinstance(bom, dict):
                continue
            self._check_bom_dict(bom, f"products[{i}].bom", parent=name)

    def _check_bom_dict(self, bom: Dict[str, Any], path: str, parent: str) -> None:
        if not bom:
            self._warn(path, f"BOM for '{parent}' is empty (is that intended?)")
            return

        for mat, qty in bom.items():
            if not isinstance(mat, str):
                self._err(path, f"BOM key must be material name string, got {mat}")
                continue

            if mat not in self.all_materials:
                self._err(f"{path}.{mat}", f"BOM references unknown material '{mat}' (parent '{parent}')")

            if not isinstance(qty, int) or qty <= 0:
                self._err(f"{path}.{mat}", f"BOM quantity must be positive int, got {qty}")

        # parent sanity: raw materials should not have BOM (not enforced here)
        if parent in self.raw_set:
            self._err(path, f"Raw material '{parent}' should not have BOM")

    def _check_unused_raw_materials(self) -> None:
        """
        Every raw material must appear in at least one BOM.
        Operation inputs alone are NOT sufficient.
        """

        raws_used_in_boms: Set[str] = set()

        # Collect raw materials used in BOMs only
        for item in self.intermediate_list + self.products_list:
            if not isinstance(item, dict):
                continue

            bom = item.get("bom")
            if not isinstance(bom, dict):
                continue

            for mat in bom.keys():
                if isinstance(mat, str) and mat in self.raw_set:
                    raws_used_in_boms.add(mat)

        # Any raw not in BOMs is an error
        unused = sorted(self.raw_set - raws_used_in_boms)

        for raw in unused:
            self._err(
                "raw_materials",
                f"Raw material '{raw}' does not appear in any BOM"
            )



    # ============================================================
    # 3) Inventory linkage checks
    # ============================================================

    def _check_inventory_links(self) -> None:
        inv = self.config.get("inventory", [])
        if not isinstance(inv, list):
            return

        for i, item in enumerate(inv):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            itype = item.get("type")

            if not isinstance(name, str):
                continue
            if name not in self.all_materials:
                self._err(f"inventory[{i}].name", f"Inventory refers to unknown material '{name}'")
                continue

            # type must match catalog category
            if isinstance(itype, str):
                if name in self.raw_set and itype != "raw_materials":
                    self._err(f"inventory[{i}].type", f"'{name}' is raw_materials but inventory.type is '{itype}'")
                if name in self.inter_set and itype != "intermediate_materials":
                    self._err(f"inventory[{i}].type", f"'{name}' is intermediate_materials but inventory.type is '{itype}'")
                if name in self.prod_set and itype != "products":
                    self._err(f"inventory[{i}].type", f"'{name}' is products but inventory.type is '{itype}'")

    # ============================================================
    # 4) Supplier checks
    # ============================================================

    def _check_suppliers(self) -> None:
        suppliers = self.config.get("supplier", [])
        if not isinstance(suppliers, list):
            return

        supplied_by_raw: Dict[str, List[str]] = defaultdict(list)

        for i, s in enumerate(suppliers):
            if not isinstance(s, dict):
                continue
            sname = s.get("name")
            mat = s.get("supply_material_name")

            if isinstance(sname, str) and isinstance(mat, str):
                supplied_by_raw[mat].append(sname)

            if isinstance(mat, str):
                if mat not in self.raw_set:
                    # In your modeling suppliers provide raw materials only
                    if mat in self.all_materials:
                        self._err(f"supplier[{i}].supply_material_name", f"Supplier supplies '{mat}' but it's not a raw_material")
                    else:
                        self._err(f"supplier[{i}].supply_material_name", f"Supplier supplies unknown material '{mat}'")

        # enforce at least one supplier per raw (policy)
        for raw in sorted(self.raw_set):
            if raw not in supplied_by_raw or not supplied_by_raw[raw]:
                msg = f"No supplier found for raw material '{raw}'"
                if self.REQUIRE_SUPPLIER_FOR_EACH_RAW:
                    self._err("supplier", msg)
                else:
                    self._warn("supplier", msg)

    # ============================================================
    # 5) Facility + operation checks
    # ============================================================

    def _check_facilities_and_operations(self) -> None:
        facilities = self.facility_list
        if not isinstance(facilities, list):
            return

        for i, fac in enumerate(facilities):
            if not isinstance(fac, dict):
                continue

            base = f"facility[{i}]"
            ftype = fac.get("type", "")

            # ── Skip warehouses entirely — no operations expected ──
            if ftype == "warehouse":
                continue

            fname = fac.get("name")
            inv_managed = fac.get("inventory_managed")
            op = fac.get("operation") if isinstance(fac.get("operation"), dict) else {}

            # inventory_managed should reference valid materials
            if isinstance(inv_managed, list):
                for j, m in enumerate(inv_managed):
                    if isinstance(m, str):
                        if m not in self.all_materials:
                            self._err(f"{base}.inventory_managed[{j}]", f"Unknown material '{m}' in inventory_managed")
                    else:
                        self._err(f"{base}.inventory_managed[{j}]", f"Non-string material in inventory_managed: {m}")

            inputs  = op.get("input")
            outputs = op.get("output")
            res_req = op.get("resource_required")

            in_list  = self._to_str_list(inputs,  f"{base}.operation.input")
            out_list = self._to_str_list(outputs, f"{base}.operation.output")

            # must reference known materials
            for j, m in enumerate(in_list):
                if m not in self.all_materials:
                    self._err(f"{base}.operation.input[{j}]", f"Unknown input material '{m}'")

            for j, m in enumerate(out_list):
                if m not in self.all_materials:
                    self._err(f"{base}.operation.output[{j}]", f"Unknown output material '{m}'")

            # resource_required validation
            if isinstance(res_req, str) and res_req.strip().lower() == "none":
                fac["operation"]["resource_required"] = None
                res_req = None

            if res_req is None:
                pass
            elif isinstance(res_req, str):
                if res_req.strip() == "" or res_req.strip().lower() == "none":
                    pass
                elif res_req.strip().lower() == "missing":
                    # only flag as error if resources are defined
                    # if no resources exist, "missing" is expected placeholder
                    if len(self.resource_by_name) > 0:
                        self._err(
                            f"{base}.operation.resource_required",
                            f"Resource is defined in config but resource_required is 'missing' — please specify which resource this operation uses"
                        )
                elif res_req not in self.resource_by_name:
                    self._err(
                        f"{base}.operation.resource_required",
                        f"Unknown resource '{res_req}'"
                    )
            else:
                self._err(
                    f"{base}.operation.resource_required",
                    f"resource_required must be a string or None, got {type(res_req).__name__}"
                )

        # ── BOM alignment check removed ──
        # Operation inputs do not need to match BOM keys exactly.
        # BOM defines what is consumed per unit of output.
        # Operations define what the machine physically processes.
        # These are related but not required to be identical.

    def _to_str_list(self, v: Any, path: str) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            out = []
            for i, x in enumerate(v):
                if isinstance(x, str):
                    out.append(x)
                else:
                    self._err(f"{path}[{i}]", f"Expected string, got {x}")
            return out
        self._err(path, f"Expected list[str] or str, got {type(v).__name__}")
        return []

    def _bom_for_material(self, mat: str) -> Optional[Dict[str, Any]]:
        # if mat is intermediate
        for item in self.intermediate_list:
            if isinstance(item, dict) and item.get("name") == mat and isinstance(item.get("bom"), dict):
                return item["bom"]
        # if mat is product
        for item in self.products_list:
            if isinstance(item, dict) and item.get("name") == mat and isinstance(item.get("bom"), dict):
                return item["bom"]
        return None

    # ============================================================
    # 6) Nodes/Edges checks
    # ============================================================

    def _check_nodes_edges(self) -> None:
        # Node existence:
        # suppliers and facilities must exist by name;
        # warehouses are free-form (no dedicated section), so we only require consistency with edges.
        nodes = self.config.get("nodes", [])
        if not isinstance(nodes, list):
            self._err("nodes", "nodes must be a list")
            return

        facility_names = {f.get("name") for f in self.facility_list if isinstance(f, dict)}
        warehouse_names = self._collect_warehouse_names()
        all_facility_nodes = facility_names | warehouse_names
        supplier_names = set(self.supplier_by_name.keys())

        ALLOWED_NODE_KEYS = {"supplier", "facility"}

        for i, entry in enumerate(nodes):
            path = f"nodes[{i}]"

            if not isinstance(entry, dict):
                self._err(path, "Each nodes entry must be a dictionary")
                continue

            # ------------------------------------
            # 1️⃣ Only allowed keys
            # ------------------------------------
            for key in entry.keys():
                if key not in ALLOWED_NODE_KEYS:
                    self._err(
                        f"{path}.{key}",
                        f"Invalid node category '{key}'. Only 'supplier' and 'facility' are allowed. "
                        f"Warehouses must be included under 'facility'."
                    )

            # ------------------------------------
            # 2️⃣ Supplier list validation
            # ------------------------------------
            suppliers = entry.get("supplier", [])
            if not isinstance(suppliers, list):
                self._err(f"{path}.supplier", "supplier must be a list")
            else:
                for s in suppliers:
                    if s not in supplier_names:
                        self._err(
                            f"{path}.supplier",
                            f"Unknown supplier '{s}' in nodes"
                        )

            # ------------------------------------
            # 3️⃣ Facility list validation
            # ------------------------------------
            facilities = entry.get("facility", [])
            if not isinstance(facilities, list):
                self._err(f"{path}.facility", "facility must be a list")
            else:
                for f in facilities:
                    if f not in all_facility_nodes:
                        self._err(
                            f"{path}.facility",
                            f"Unknown facility or warehouse '{f}' in nodes"
                        )

                # completeness check (warning, not error)
                missing = all_facility_nodes - set(facilities)
                if missing:
                    self._warn(
                        f"{path}.facility",
                        f"nodes.facility does not include all known facilities/warehouses: {sorted(missing)}"
                    )

        node_set = self.node_names

        # check that supplier nodes exist in supplier section
        for sname in node_set:
            # don't require everything to exist in supplier/resource, because warehouses exist too
            pass

        # edges: source/destination in nodes, material exists, type matches
        for i, e in enumerate(self.edge_list):
            if not isinstance(e, dict):
                continue
            base = f"edges[{i}]"
            src = e.get("source")
            dst = e.get("destination")
            mtype = e.get("material_type")
            mname = e.get("material_name")

            # --------------------------------------------------
            # Supplier → material consistency check (NEW)
            # --------------------------------------------------
            if isinstance(src, str) and src in self.supplier_by_name:
                supplier = self.supplier_by_name[src]
                supplied_material = supplier.get("supply_material_name")

                if isinstance(mname, str) and isinstance(supplied_material, str):
                    if mname != supplied_material:
                        self._err(
                            f"{base}.material_name",
                            f"Supplier '{src}' supplies '{supplied_material}' "
                            f"but edge transports '{mname}'"
                        )

            if isinstance(src, str) and src not in node_set:
                self._err(f"{base}.source", f"Edge source '{src}' not found in nodes")
            if isinstance(dst, str) and dst not in node_set:
                self._err(f"{base}.destination", f"Edge destination '{dst}' not found in nodes")

            # supplier/facility existence checks (only if they look like those entities)
            if isinstance(src, str) and src in self.supplier_by_name:
                # ok
                pass
            if isinstance(dst, str) and dst in self.supplier_by_name:
                self._warn(f"{base}.destination", f"Destination '{dst}' is a supplier (unusual)")

            # material check
            if isinstance(mname, str):
                if mname not in self.all_materials:
                    self._err(f"{base}.material_name", f"Unknown material '{mname}' in edge")
                else:
                    # material_type match check
                    if isinstance(mtype, str):
                        expected = self._material_category(mname)
                        # normalise singular/plural variants
                        mtype_norm = mtype.strip().lower().rstrip("s") + "s" \
                            if not mtype.strip().lower().endswith("s") \
                            else mtype.strip().lower()
                        if expected and mtype_norm != expected:
                            self._err(f"{base}.material_type", f"material_type '{mtype}' mismatches '{mname}' category '{expected}'")
        
        # --------------------------------------------------
        # Supplier → edge coverage check (CRITICAL)
        # --------------------------------------------------

        # Map: supplier -> set(materials transported by edges)
        supplier_edges = defaultdict(set)

        for e in self.edge_list:
            if not isinstance(e, dict):
                continue
            src = e.get("source")
            mat = e.get("material_name")
            if isinstance(src, str) and isinstance(mat, str):
                supplier_edges[src].add(mat)

        # Check each supplier
        for sname, supplier in self.supplier_by_name.items():
            supplied_material = supplier.get("supply_material_name")

            # 1) Supplier must appear in nodes
            if sname not in self.node_names:
                self._err(
                    "nodes",
                    f"Supplier '{sname}' is defined but not present in nodes"
                )
                continue

            # 2) Supplier must have an edge transporting its supplied material
            if supplied_material not in supplier_edges.get(sname, set()):
                self._err(
                    "edges",
                    f"Supplier '{sname}' supplies '{supplied_material}' but has no edge transporting it"
                )
        
        # --------------------------------------------------
        # Node usage check: every node must appear in ≥1 edge
        # --------------------------------------------------

        used_nodes = set()

        for e in self.edge_list:
            if not isinstance(e, dict):
                continue

            src = e.get("source")
            dst = e.get("destination")

            if isinstance(src, str):
                used_nodes.add(src)
            if isinstance(dst, str):
                used_nodes.add(dst)

        # Any node declared but never used in edges → ERROR
        for node in sorted(self.node_names):
            if node not in used_nodes:
                self._err(
                    "nodes",
                    f"Node '{node}' is declared but does not appear in any edge"
                )

    def _collect_warehouse_names(self) -> Set[str]:
        """
        Warehouses are nodes that:
        - appear in edges as destinations
        - are NOT suppliers
        - are NOT facilities
        """
        warehouses = set()

        facility_names = {f.get("name") for f in self.facility_list if isinstance(f, dict)}
        supplier_names = set(self.supplier_by_name.keys())

        for e in self.edge_list:
            if not isinstance(e, dict):
                continue
            dst = e.get("destination")
            if not isinstance(dst, str):
                continue

            if dst not in supplier_names and dst not in facility_names:
                warehouses.add(dst)

        return warehouses


    def _material_category(self, name: str) -> Optional[str]:
        if name in self.raw_set:
            return "raw_materials"
        if name in self.inter_set:
            return "intermediate_materials"
        if name in self.prod_set:
            return "products"
        return None

    # ============================================================
    # 7) Producibility checks
    # ============================================================

    def _check_producibility(self) -> None:
        produced_by_ops: Set[str] = set()
        for i, fac in enumerate(self.facility_list):
            if not isinstance(fac, dict):
                continue
            op = fac.get("operation") if isinstance(fac.get("operation"), dict) else {}
            outs = self._to_str_list(op.get("output"), f"facility[{i}].operation.output")
            for m in outs:
                produced_by_ops.add(m)

        # intermediates/products should be produced somewhere OR have inventory
        for mat in sorted(self.inter_set | self.prod_set):
            if mat in produced_by_ops:
                continue
            if mat in self.inventory_by_name:
                # if you allow “exogenous inventory” with no production, treat as ok but warn
                self._warn("producibility", f"'{mat}' has inventory but no producing facility operation")
                continue

            msg = f"'{mat}' is intermediate/product but has no producing facility operation and no inventory"
            if self.REQUIRE_PRODUCER_FOR_NONRAW:
                self._err("producibility", msg)
            else:
                self._warn("producibility", msg)

    # ============================================================
    # 8) Transformation cycle detection (inputs -> outputs)
    # ============================================================

    def _check_transformation_cycles(self) -> None:
        # Build directed graph over materials: input -> output for each facility operation.
        g: Dict[str, Set[str]] = defaultdict(set)
        indeg: Dict[str, int] = defaultdict(int)

        mats = set(self.all_materials)
        for m in mats:
            indeg[m] = 0

        for i, fac in enumerate(self.facility_list):
            if not isinstance(fac, dict):
                continue
            op = fac.get("operation") if isinstance(fac.get("operation"), dict) else {}
            ins = self._to_str_list(op.get("input"), f"facility[{i}].operation.input")
            outs = self._to_str_list(op.get("output"), f"facility[{i}].operation.output")
            for a in ins:
                for b in outs:
                    if a in mats and b in mats and b not in g[a]:
                        g[a].add(b)
                        indeg[b] += 1

        # Kahn's algorithm for cycle detection
        q = deque([m for m in mats if indeg[m] == 0])
        visited = 0
        while q:
            u = q.popleft()
            visited += 1
            for v in g.get(u, set()):
                indeg[v] -= 1
                if indeg[v] == 0:
                    q.append(v)

        if visited != len(mats):
            # cycle exists; find nodes still with indeg > 0
            cyc = sorted([m for m in mats if indeg[m] > 0])
            self._err("transformation_graph", f"Material transformation graph has a cycle involving: {cyc}")

    # ============================================================
    # Helpers
    # ============================================================

    def _err(self, path: str, msg: str) -> None:
        self.findings.append(ValidationFinding(self.LAYER, "error", path, msg))

    def _warn(self, path: str, msg: str) -> None:
        self.findings.append(ValidationFinding(self.LAYER, "warning", path, msg))


# ============================================================
# Example usage
# ============================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python validation_layer_b.py <config.json>")
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        config = json.load(f)

    v = LayerBValidator(config)
    report = v.validate()

    print("\n--- LAYER B ERRORS ---")
    for f in report["errors"]:
        print(f)

    print("\n--- LAYER B WARNINGS ---")
    for f in report["warnings"]:
        print(f)
