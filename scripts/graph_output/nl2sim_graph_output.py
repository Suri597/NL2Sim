import argparse
import json
from pathlib import Path

import networkx as nx
from pyvis.network import Network

MISSING = "missing"

COLOR_MAP = {
    "supplier": "#4C72B0",
    "manufacturing": "#DD8452",
    "warehouse": "#55A868",
    "facility_other": "#937860",
    "customer": "#8172B2",
}


def is_set(value):
    """True if a field is present and not the pipeline's 'missing' placeholder."""
    return value is not None and value != MISSING


def fmt_dist(dist_obj):
    """Render a {'distribution': ..., 'parameters': {...}} block compactly, skipping missing fields."""
    if not dist_obj or not is_set(dist_obj.get("distribution")):
        return None
    dist = dist_obj["distribution"]
    params = dist_obj.get("parameters", {}) or {}
    parts = [f"{k}={v}" for k, v in params.items() if is_set(v)]
    if parts:
        return f"{dist}({', '.join(parts)})"
    return dist


def build_graph(scenario: dict) -> nx.MultiDiGraph:
    """
    Builds the graph strictly from what's already in the scenario JSON --
    no inference, no repair, no content editing. By the time a scenario
    reaches this script, verification/repair has already guaranteed every
    customer has a real, correct inbound edge (checks 10/11), every
    facility's inventory_managed is consistent with its edges (check 19),
    and so on -- there is nothing left here to guess or patch. This
    script's only job is to read and plot that already-correct data
    faithfully.
    """
    G = nx.MultiDiGraph()

    # --- Suppliers ---
    for s in scenario.get("supplier", []):
        name = s["name"]
        tooltip_lines = [f"{name} (supplier)"]
        if is_set(s.get("supply_material_name")):
            tooltip_lines.append(f"Supplies: {s['supply_material_name']}")
        if is_set(s.get("supplier_cost")):
            tooltip_lines.append(f"Cost: {s['supplier_cost']}")
        lt = fmt_dist(s.get("supplier_lead_time"))
        if lt:
            tooltip_lines.append(f"Lead time: {lt}")
        if is_set(s.get("supplier_capacity")):
            tooltip_lines.append(f"Capacity: {s['supplier_capacity']}")
        G.add_node(name, node_type="supplier", color=COLOR_MAP["supplier"],
                   title="\n".join(tooltip_lines), shape="dot", size=20)

    # --- Facilities ---
    for f in scenario.get("facility", []):
        name = f["name"]
        ftype = f.get("type", "facility_other")
        color = COLOR_MAP.get(ftype, COLOR_MAP["facility_other"])
        tooltip_lines = [f"{name} ({ftype})"]
        inv = [i for i in f.get("inventory_managed", []) if is_set(i)]
        if inv:
            tooltip_lines.append(f"Inventory managed: {', '.join(inv)}")
        op = f.get("operation", {})
        op_name = op.get("name")
        if is_set(op_name):
            tooltip_lines.append(f"Operation: {op_name}")
        op_in = [i for i in op.get("input", []) if is_set(i)]
        op_out = [o for o in op.get("output", []) if is_set(o)]
        if op_in:
            tooltip_lines.append(f"Input: {', '.join(op_in)}")
        if op_out:
            tooltip_lines.append(f"Output: {', '.join(op_out)}")
        cycle = fmt_dist(op.get("operation_cycle"))
        if cycle:
            tooltip_lines.append(f"Operation cycle: {cycle}")
        G.add_node(name, node_type=ftype, color=color,
                   title="\n".join(tooltip_lines), shape="box", size=25)

    # --- Customers ---
    for c in scenario.get("customer", []):
        name = c["name"]
        tooltip_lines = [f"{name} (customer)"]
        if is_set(c.get("product")):
            tooltip_lines.append(f"Orders: {c['product']}")
        demand = fmt_dist(c.get("demand"))
        if demand:
            tooltip_lines.append(f"Demand: {demand}")
        if is_set(c.get("unit_selling_price")):
            tooltip_lines.append(f"Unit price: {c['unit_selling_price']}")
        clt = fmt_dist(c.get("customer_lead_time"))
        if clt:
            tooltip_lines.append(f"Customer lead time: {clt}")
        if is_set(c.get("shortage_policy")):
            tooltip_lines.append(f"Shortage policy: {c['shortage_policy']}")
        G.add_node(name, node_type="customer", color=COLOR_MAP["customer"],
                   title="\n".join(tooltip_lines), shape="triangle", size=20)

    # --- Edges -- the ONLY source of edges. Draws every edge exactly as
    # given (supplier->facility, facility->facility, facility->customer,
    # etc.) -- no separate inference pass. If a customer's edge is
    # missing, that's a verification/repair gap upstream, not something
    # to paper over here.
    for e in scenario.get("edges", []):
        src, dst = e["source"], e["destination"]
        if src not in G or dst not in G:
            continue  # skip edges referencing nodes not defined above
        label_parts = []
        if is_set(e.get("material_name")):
            label_parts.append(e["material_name"])
        tt = fmt_dist(e.get("transfer_time"))
        tooltip = f"Material: {e.get('material_name', '?')}"
        if tt:
            tooltip += f"\nTransfer time: {tt}"
        G.add_edge(src, dst, label=" / ".join(label_parts), title=tooltip)

    return G


def assign_layout_positions(G: nx.MultiDiGraph) -> None:
    """
    Assigns explicit (x, y) positions to every node, arranged in columns
    by node_type (supplier -> manufacturing -> warehouse -> facility_other
    -> customer), spread out vertically within each column.

    This is necessary because physics is disabled in render_html (so
    nodes stay put when dragged, per the interaction settings below) --
    without physics AND without explicit positions, vis.js places every
    node at the same default coordinate, causing all nodes (and
    therefore every edge) to collapse onto a single point. Edges don't
    go missing in that case -- they become zero-length lines converging
    on one spot, visually indistinguishable from nothing being there at
    all. This function is purely a layout concern -- it doesn't touch
    graph topology (nodes/edges), only where each node is drawn.
    """
    column_order = ["supplier", "manufacturing", "warehouse", "facility_other", "customer"]
    column_x = {node_type: i * 300 for i, node_type in enumerate(column_order)}

    by_column: dict = {node_type: [] for node_type in column_order}
    for node, data in G.nodes(data=True):
        node_type = data.get("node_type", "facility_other")
        by_column.setdefault(node_type, []).append(node)

    for node_type, nodes_in_column in by_column.items():
        x = column_x.get(node_type, len(column_order) * 300)
        count = len(nodes_in_column)
        for i, node in enumerate(nodes_in_column):
            # Center the column vertically around y=0, evenly spaced.
            y = (i - (count - 1) / 2) * 150
            G.nodes[node]["x"] = x
            G.nodes[node]["y"] = y


def separate_parallel_edges(G: nx.MultiDiGraph) -> None:
    """
    Assigns a DIFFERENT curve (smooth type + roundness) to each edge
    within a group of parallel edges (same source and destination) --
    necessary because vis.js's global "smooth" option applies the exact
    same curve to every edge uniformly; it does NOT automatically detect
    and fan out multiple edges sharing the same two endpoints. Without
    this, parallel edges curve identically and still overlap perfectly,
    just as curves instead of straight lines.

    For a group of N edges between the same pair, alternates
    curvedCW/curvedCCW and spreads roundness across a range so they
    visually fan out into distinct, separated lines.
    """
    from collections import defaultdict

    groups: dict = defaultdict(list)
    for u, v, key in G.edges(keys=True):
        groups[(u, v)].append(key)

    for (u, v), keys in groups.items():
        n = len(keys)
        if n == 1:
            continue
        for i, key in enumerate(keys):
            offset = (i - (n - 1) / 2)
            curve_type = "curvedCW" if offset >= 0 else "curvedCCW"
            roundness = min(0.15 + abs(offset) * 0.2, 0.9)
            G.edges[u, v, key]["smooth"] = {
                "enabled": True,
                "type": curve_type,
                "roundness": roundness,
            }


def render_html(G: nx.MultiDiGraph, output_path: str):
    net = Network(height="800px", width="100%", directed=True,
                  bgcolor="#ffffff", font_color="black", notebook=False,
                  cdn_resources="in_line")
    net.from_nx(G)

    net.set_options("""
    {
      "physics": { "enabled": false },
      "edges": {
        "smooth": {
          "enabled": true,
          "type": "curvedCW",
          "roundness": 0.2
        },
        "arrows": { "to": { "enabled": true } }
      },
      "interaction": {
        "dragNodes": true,
        "dragView": true,
        "zoomView": true,
        "hover": true,
        "tooltipDelay": 100
      }
    }
    """)

    net.write_html(output_path, open_browser=False, notebook=False)


def main():
    parser = argparse.ArgumentParser(description="Render an NL2Sim scenario JSON as an interactive graph.")
    parser.add_argument("--input", required=True, help="Path to validated NL2Sim scenario JSON.")
    parser.add_argument("--output", default=None, help="Path to output HTML file.")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_name(input_path.stem + "_graph.html")

    with open(input_path, "r") as f:
        scenario = json.load(f)

    G = build_graph(scenario)
    assign_layout_positions(G)
    separate_parallel_edges(G)
    render_html(G, str(output_path))
    print(f"Nodes: {G.number_of_nodes()}, Edges: {G.number_of_edges()}")
    print(f"Saved interactive graph to: {output_path}")


if __name__ == "__main__":
    main()