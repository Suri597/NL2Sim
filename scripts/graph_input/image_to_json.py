"""
image_to_json.py

Step 1 of the graph-input baseline: sends a hand-drawn (or any) graph sketch
image to an OpenAI vision-capable model and extracts ONLY network structure
(nodes + edges), output directly in NL2Sim's native config schema shape.
No attribute/field-level data (capacities, costs, distributions, etc.) is
extracted yet -- those are filled with "missing" placeholders per NL2Sim
convention, to be resolved later by the NL-derived JSON or left for manual
completion. This lets the output merge with an NL-derived config.json
without any intermediate translation step.

Usage:
    python image_to_json.py images/sketch_20260817_161300.png
    python image_to_json.py images/sketch_20260817_161300.png --out output/graph_topology.json
    python image_to_json.py images/sketch_20260817_161300.png --model gpt-4.1

Requires:
    pip install openai --break-system-packages
    export OPENAI_API_KEY=sk-...
    (or copy .env.example to .env and fill it in, then `pip install python-dotenv`)

Output schema (NL2Sim-native, topology only -- matches the "nodes"/"edges"
blocks of a real NL2Sim config.json):
{
  "nodes": [
    {
      "customer": ["<entity name>", ...],
      "facility": ["<entity name>", ...],
      "supplier": ["<entity name>", ...]
    }
  ],
  "edges": [
    {
      "source": "<entity name>",
      "destination": "<entity name>",
      "material_name": "missing",
      "material_type": "missing",
      "transfer_time": {
        "distribution": "missing",
        "parameters": {"a": "missing", "b": "missing", "c": "missing", "d": "missing", "e": "missing"}
      }
    }
  ]
}

Note: entity "names" are read directly from any text/labels in the image.
If a node has no visible label, a placeholder name is assigned
(Facility_1, Supplier_1, Customer_1, ...) -- these should be treated as
provisional until reconciled against the NL-derived entity names in the
merge step.
"""

import argparse
import base64
import json
import os
import sys

from openai import OpenAI
from dotenv import load_dotenv
load_dotenv()
# GPT-5.6 Terra does not support temperature=0 (per NL2Sim v2 conventions).
# Add any other model names here that share that constraint.
NO_TEMPERATURE_ZERO_MODELS = {"gpt-5.6-terra"}

DEFAULT_MODEL = "gpt-4.1"

SYSTEM_PROMPT = """You are a precise graph-structure extractor for a supply chain simulation \
tool called NL2Sim. You will be shown an image of a hand-drawn or digital diagram representing \
a supply chain network: suppliers, facilities (manufacturing plants, warehouses, distribution \
centers), and customers, connected by material-flow edges.

Extract ONLY the network structure: which entities exist, what category each belongs to, and \
how they are connected. Do NOT infer or invent capacities, costs, lead times, distributions, or \
any other attribute-level data -- those fields must be set to the literal string "missing".

Return STRICT JSON matching exactly this schema, with no markdown fences, no commentary, no \
preamble:

{
  "nodes": [
    {
      "customer": ["<entity name>", ...],
      "facility": ["<entity name>", ...],
      "supplier": ["<entity name>", ...]
    }
  ],
  "edges": [
    {
      "source": "<entity name>",
      "destination": "<entity name>",
      "material_name": "missing",
      "material_type": "missing",
      "transfer_time": {
        "distribution": "missing",
        "parameters": {"a": "missing", "b": "missing", "c": "missing", "d": "missing", "e": "missing"}
      }
    }
  ]
}

Rules:
- "nodes" is a single-element list containing one object with three keys: "customer", "facility", "supplier".
  Every entity you find in the image goes into exactly one of these three lists, by name.
- Use the entity's handwritten/typed label from the image as its name. If an entity has no visible \
label, assign a placeholder name in the form "Facility_1", "Supplier_1", "Customer_1" (numbered \
sequentially within its category in the order encountered).
- Classify each entity into "supplier" (raw material source, typically leftmost/upstream), \
"facility" (any manufacturing plant, warehouse, or distribution center -- anything that transforms \
or stores material in the middle of the chain), or "customer" (demand sink, typically \
rightmost/downstream). If you cannot tell, make your best guess from position and shape rather \
than omitting the entity.
- "edges" is a flat list. Each edge has "source" and "destination" set to entity names exactly as \
they appear in "nodes" above (must match exactly). Leave "material_name", "material_type", and all \
"transfer_time" fields as "missing" -- do not guess these.
- Only create an edge where the image shows a clear connecting line or arrow between two entities.
- If the image has no discernible nodes or edges, return {"nodes": [{"customer": [], "facility": [], "supplier": []}], "edges": []}.
- Do not invent entities or edges that are not visibly present in the image.
"""


def encode_image(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def extract_topology(image_path: str, model: str = DEFAULT_MODEL) -> dict:
    client = OpenAI()  # picks up OPENAI_API_KEY from env

    b64_image = encode_image(image_path)
    ext = os.path.splitext(image_path)[1].lstrip(".").lower()
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext or "png"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Extract the network structure (nodes and edges, in NL2Sim schema format) from this image."},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/{mime};base64,{b64_image}"},
                },
            ],
        },
    ]

    kwargs = {"model": model, "messages": messages}
    if model not in NO_TEMPERATURE_ZERO_MODELS:
        kwargs["temperature"] = 0

    response = client.chat.completions.create(**kwargs)
    raw_text = response.choices[0].message.content.strip()

    # Defensive cleanup in case the model wraps output in markdown fences anyway.
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        print("Error: model did not return valid JSON.", file=sys.stderr)
        print("Raw response:", raw_text, file=sys.stderr)
        raise e

    return parsed


def main():
    parser = argparse.ArgumentParser(description="Extract graph topology (nodes+edges) from an image.")
    parser.add_argument("image_path", help="Path to the sketch/graph image")
    parser.add_argument("--out", default=None, help="Output JSON path (default: output/<image_name>_topology.json)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"OpenAI model to use (default: {DEFAULT_MODEL})")
    args = parser.parse_args()

    if not os.path.exists(args.image_path):
        print(f"Error: image not found at {args.image_path}", file=sys.stderr)
        sys.exit(1)

    result = extract_topology(args.image_path, model=args.model)

    if args.out:
        out_path = args.out
    else:
        base = os.path.splitext(os.path.basename(args.image_path))[0]
        out_path = os.path.join("output", f"{base}_topology.json")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Extracted topology written to {out_path}")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()