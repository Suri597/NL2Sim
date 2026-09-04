"""
graph_only_to_json.py

Step for the Graph-only input mode: no NL description exists to reconcile
against, so this goes straight from a sketch/graph image to a complete
NL2Sim config JSON in one LLM pass, using a schema-aware prompt that
includes the full NL2Sim field set and what each field means.

The model is instructed to fill only what it can actually read or
confidently infer from the image (leaving "missing" placeholders
otherwise) and, critically, to NOT invent values it isn't sure about --
instead it raises a clarification question for anything uncertain
(illegible label, ambiguous facility type, unclear material flow, etc).

Those questions are then put to the user in a single refinement round
(free-text answer covering as many as they want, same interaction shape
as the terminal prompts elsewhere in the graph_input scripts), one LLM
call interprets the answer and fills in the corresponding fields, and the
loop repeats -- for anything still unresolved -- up to --max-rounds times.

This is intentionally NOT a two-source dispute like merge.py: there is no
NL text to match against, so there is no matched/graph_only/nl_only
bookkeeping -- just one JSON, gaps in it, and direct question -> answer ->
fill.

Usage:
    python graph_only_to_json.py images/sketch_20260817_161300.png
    python graph_only_to_json.py images/sketch.png --out output/graph_config.json
    python graph_only_to_json.py images/sketch.png --model gpt-4.1 --max-rounds 4

Requires:
    pip install openai --break-system-packages
    export OPENAI_API_KEY=sk-...
"""

import argparse
import base64
import json
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI

DEFAULT_MODEL = "gpt-5.6-terra"
NO_TEMPERATURE_ZERO_MODELS = {"gpt-5.6-terra"}

MISSING = "missing"
MISSING_DIST = {
    "distribution": MISSING,
    "parameters": {"a": MISSING, "b": MISSING, "c": MISSING, "d": MISSING, "e": MISSING},
}

# ---------------------------------------------------------------------------
# Schema-aware extraction prompt. Mirrors the stub shapes used in
# scripts/graph_input/merge.py (stub_supplier/stub_facility/stub_customer)
# so output from this script is structurally compatible with the rest of
# the NL2Sim pipeline without translation.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_EXTRACT = """You are a precise supply-chain scenario extractor for NL2Sim, a \
simulation tool. You will be shown an image of a hand-drawn or digital diagram representing a \
supply chain network. Unlike a normal topology-only pass, here there is NO separate text \
description to fill in the details later -- this image is the ONLY source of information, so you \
must attempt to extract every field the schema below defines, using only what the image actually \
shows or clearly implies.

============================================================
NL2Sim CONFIG SCHEMA (target output shape)
============================================================
{
  "config_info": [{"name": "", "version": ""}],
  "raw_materials": [{"name": ""}],
  "intermediate_materials": [{"name": "", "bom": {"<material_name>": 0}}],
  "products": [{"name": "", "bom": {"<material_name>": 0}}],
  "inventory": [
    {
      "name": "", "type": "",
      "procurement_scheme": {"type": "", "distribution": "",
                              "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}},
      "procurement_arrival": {"distribution": "", "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}},
      "initial_inventory": 0,
      "inventory_costs": {"holding_cost": 0, "shortage_cost": 0, "review_time": 0}
    }
  ],
  "supplier": [
    {
      "name": "", "supply_material_name": "",
      "supplier_lead_time": {"distribution": "", "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}},
      "supplier_capacity": 0, "supplier_cost": 0,
      "supplier_payment_lead_time": {"distribution": "", "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}}
    }
  ],
  "resource": [
    {
      "name": "", "capacity": 0,
      "service_time": {"distribution": "", "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}},
      "batching": {"enabled": false, "batch_size": 0, "max_wait_time": 0},
      "failure": {"enabled": false,
                  "uptime": {"distribution": "", "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}},
                  "downtime": {"distribution": "", "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}}},
      "operating_cost_per_time": 0
    }
  ],
  "facility": [
    {
      "name": "", "type": "", "inventory_managed": [""],
      "operation": {
        "name": "", "input": [""], "output": [""], "resource_required": "",
        "operation_cycle": {"distribution": "", "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}}
      }
    }
  ],
  "customer": [
    {
      "name": "", "product": "",
      "arrival_time": {"distribution": "", "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}},
      "demand": {"distribution": "", "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}},
      "customer_lead_time": {"distribution": "", "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}},
      "shortage_policy": "", "unit_selling_price": 0,
      "customer_payment_lead_time": {"distribution": "", "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}}
    }
  ],
  "nodes": [{"supplier": [""], "facility": [""], "customer": [""]}],
  "edges": [
    {
      "source": "", "destination": "", "material_type": "", "material_name": "",
      "transfer_time": {"distribution": "", "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}}
    }
  ],
  "simulation": {"time_unit": "", "horizon": 0, "warm_up": 0, "replications": 0, "random_seed": 0}
}

============================================================
FIELD MEANINGS
============================================================
- config_info: metadata about this config (name, schema version) -- essentially never shown in a
  sketch; leave "missing" without asking about it.
- raw_materials / intermediate_materials / products: the three material tiers. raw_materials are
  what suppliers provide; intermediate_materials are produced and consumed internally (bom = bill
  of materials, i.e. what quantities of which other materials go into making one unit of it);
  products are what customers order (also have a bom). Only extract a material into one of these
  three lists if the image gives a real signal about its role (e.g. it's what a supplier supplies,
  what a facility outputs, or what a customer orders) -- bom quantities are almost never legible
  from a sketch, leave those "missing" rather than asking, unless a quantity is actually written.
- inventory: a stocking point for a material, separate from facility/warehouse entities -- only
  extract this if the image clearly depicts inventory as its own node (not just a facility that
  happens to store things, which is covered by facility.inventory_managed instead).
  - procurement_scheme / procurement_arrival: how and when this inventory gets replenished.
  - initial_inventory: starting stock level.
  - inventory_costs: holding_cost (cost per unit held per period), shortage_cost (cost per unit
    short), review_time (how often inventory is checked/reordered).
- supplier_capacity: maximum quantity the supplier can provide per period.
- supplier_cost: unit cost the supplier charges.
- supplier_lead_time / customer_lead_time: time between order and fulfillment.
- supplier_payment_lead_time / customer_payment_lead_time: time between fulfillment and payment.
- supply_material_name: the raw material this supplier provides.
- resource: equipment/labor capacity a facility's operation draws on.
  - capacity: how many operations it can run concurrently.
  - service_time: time to service one operation.
  - batching: whether operations are grouped before processing (batch_size, max_wait_time).
  - failure: whether the resource can break down (uptime/downtime distributions).
  - operating_cost_per_time: cost to run this resource per time unit.
- facility.type: "manufacturing" transforms input material(s) into a DIFFERENT output material;
  "warehouse" stores/passes material through unchanged.
- inventory_managed: materials a warehouse-type facility stores (empty for pure manufacturing).
- operation.input / operation.output: materials consumed / produced by a manufacturing facility.
- operation.operation_cycle: time to complete one production cycle.
- resource_required: which "resource" entry (by name) this operation draws on.
- customer.product: the finished product this customer orders.
- demand: the customer's order quantity distribution.
- unit_selling_price: price per unit sold to this customer.
- arrival_time: distribution of when customer orders arrive.
- shortage_policy: how unmet demand is handled (e.g. backorder, lost sale).
- edges.material_name / material_type: what flows across that edge, and its category
  (raw_material / intermediate_material / product).
- transfer_time: time for material to move across an edge.
- simulation: run parameters (time_unit, horizon, warm_up period, number of replications,
  random_seed) -- these are simulation configuration, essentially never depicted in a sketch;
  leave "missing" without asking about it.
- Every "distribution" block follows {"distribution": "<name>", "parameters": {"a".."e": value}} --
  only fill in the parameters a distribution of that name actually uses; leave the rest "missing".

============================================================
CRITICAL RULE -- DO NOT INVENT
============================================================
If a field is not legible, not shown, or not confidently inferable from the image, set it to the \
literal string "missing" -- do NOT guess a plausible-sounding value. Whenever a field is left \
"missing" because of genuine ambiguity (as opposed to simply not being the kind of thing a sketch \
would show), add ONE entry to "clarification_questions" asking the user about it directly. Do not \
ask about fields that are routinely absent from a sketch -- config_info, simulation, bom \
quantities, cost/capacity numbers, and most inventory/resource detail fall in this category by \
default; only ask about them if the image seems to show a specific value you can't quite read, not \
just because the value is unknown in general. Prioritize asking about: illegible or ambiguous \
entity labels, unclear facility type (manufacturing vs warehouse) when the image gives some signal \
but not a clear one, and material flow that's genuinely ambiguous (e.g. a shared edge between more \
than two plausible materials).

============================================================
OUTPUT FORMAT
============================================================
Return STRICT JSON, no markdown fences, no commentary:
{
  "config": { ...the NL2Sim config schema above, fully populated with real values or "missing"... },
  "clarification_questions": [
    {
      "id": "q1",
      "entity_name": "<entity this concerns, or null if it's about an edge/general>",
      "category": "config_info"|"raw_materials"|"intermediate_materials"|"products"|"inventory"|"supplier"|"resource"|"facility"|"customer"|"edge"|"simulation"|"general",
      "field": "<schema field this concerns, e.g. 'supplier_capacity', or null>",
      "question": "<plain-language question to ask the user>"
    }
  ]
}

Rules:
- Extract node names exactly as labeled in the image; if unlabeled, use placeholder names
  (Facility_1, Supplier_1, Customer_1, ...) numbered sequentially within their category.
- Only create an edge where the image shows a clear connecting line or arrow.
- "clarification_questions" may be an empty list if nothing is genuinely ambiguous.
- Never leave a field silently wrong -- either extract it correctly, mark it "missing", or ask.
- "simulation" is a single object, not a list -- always include it (with "missing"/0 placeholders
  as appropriate) even though it will almost never be extractable from the image itself.
"""

REFINE_INTERPRET_PROMPT = """You are applying the user's free-text answer to a set of \
clarification questions about a supply-chain config extracted from a sketch. You're given the \
current config, the list of open questions, and the user's single answer (which may address some, \
all, or none of the questions).

For each question, decide whether the answer resolves it. If resolved, output the field update(s) \
needed. If not addressed, mark it unresolved so it can be asked again.

Return STRICT JSON, no markdown fences, no commentary:
{
  "updates": [
    {
      "entity_name": "<entity this concerns, or null for edge/general>",
      "category": "config_info"|"raw_materials"|"intermediate_materials"|"products"|"inventory"|"supplier"|"resource"|"facility"|"customer"|"edge"|"simulation"|"general",
      "field": "<schema field being set>",
      "value": <the value to set -- respect the schema's type for that field>,
      "resolved_question_id": "<id of the question this answers>"
    }
  ],
  "still_unresolved_ids": ["<question id>", ...]
}

Rules:
- Only set fields the user's answer actually specifies or clearly confirms -- do not invent values
  the answer doesn't support, even to fill a gap.
- If the user's answer implies a rename (e.g. corrects a garbled label), use field "name" with
  category matching the entity's category, and value set to the corrected name.
- Every question id from the input must appear exactly once, either in an "updates" entry's
  resolved_question_id or in "still_unresolved_ids".
"""


def encode_image(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _llm_call(system_prompt: str, user_content, model: str) -> dict:
    client = OpenAI()
    if isinstance(user_content, str):
        messages = [{"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}]
    else:
        messages = [{"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}]
    kwargs = {"model": model, "messages": messages}
    if model not in NO_TEMPERATURE_ZERO_MODELS:
        kwargs["temperature"] = 0
    response = client.chat.completions.create(**kwargs)
    raw_text = response.choices[0].message.content.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()
    return json.loads(raw_text)


def extract_full_config(image_path: str, model: str = DEFAULT_MODEL) -> dict:
    """Returns {"config": {...}, "clarification_questions": [...]}."""
    b64_image = encode_image(image_path)
    ext = os.path.splitext(image_path)[1].lstrip(".").lower()
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext or "png"

    user_content = [
        {"type": "text", "text": "Extract the full NL2Sim config from this image, per the schema and rules given."},
        {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64_image}"}},
    ]
    return _llm_call(SYSTEM_PROMPT_EXTRACT, user_content, model)


def print_questions(questions: list) -> None:
    print(f"\n  {len(questions)} thing(s) need clarification:")
    for q in questions:
        loc = f' ("{q["entity_name"]}")' if q.get("entity_name") else ""
        print(f'    [{q["id"]}]{loc} {q["question"]}')


def collect_refinement_answer(questions: list) -> str:
    print_questions(questions)
    return input("\n  Your answer (address as many as you'd like): ").strip()


def interpret_refinement_answer(config: dict, questions: list, answer: str, model: str = DEFAULT_MODEL) -> dict:
    payload = {
        "config": config,
        "open_questions": questions,
        "answer": answer,
    }
    return _llm_call(REFINE_INTERPRET_PROMPT, json.dumps(payload, indent=2), model)


def apply_updates(config: dict, updates: list) -> list:
    """Mutates config in place. Returns human-readable log lines."""
    log = []
    LIST_SINGLETON_CATEGORIES = {"config_info"}  # list with exactly one object, no "name" key
    OBJECT_CATEGORIES = {"simulation"}            # plain object, not a list at all
    NAMED_LIST_CATEGORIES = {
        "raw_materials", "intermediate_materials", "products", "inventory",
        "supplier", "resource", "facility", "customer",
    }

    for u in updates:
        entity_name, category, field, value = u.get("entity_name"), u["category"], u["field"], u.get("value")

        if category == "general":
            log.append(f"General note recorded: {field} = {value}")
            continue

        if category in OBJECT_CATEGORIES:
            config.setdefault(category, {})[field] = value
            log.append(f'"{category}".{field} = {value!r}')
            continue

        if category in LIST_SINGLETON_CATEGORIES:
            bucket = config.setdefault(category, [{}])
            if not bucket:
                bucket.append({})
            bucket[0][field] = value
            log.append(f'"{category}".{field} = {value!r}')
            continue

        if category == "edge":
            matched = False
            for e in config.get("edges", []):
                if entity_name is None or e.get("source") == entity_name or e.get("destination") == entity_name:
                    e[field] = value
                    matched = True
            log.append(f'Edge field "{field}" set to {value!r}' + (f' for "{entity_name}"' if entity_name else ""))
            continue

        if category not in NAMED_LIST_CATEGORIES:
            log.append(f'Unknown category "{category}" for field "{field}" -- skipped')
            continue

        bucket = config.get(category, [])
        target = next((e for e in bucket if e.get("name") == entity_name), None)
        if target is None:
            log.append(f'Could not find {category} "{entity_name}" to update field "{field}" -- skipped')
            continue

        if field == "name":
            old_name = target["name"]
            target["name"] = value
            # name propagation into nodes/edges only applies to node-bearing
            # categories (supplier/facility/customer) -- materials don't
            # appear in "nodes" or as edge endpoints by their own name.
            if category in ("supplier", "facility", "customer"):
                if config.get("nodes"):
                    names = config["nodes"][0].get(category, [])
                    config["nodes"][0][category] = [value if n == old_name else n for n in names]
                for e in config.get("edges", []):
                    if e.get("source") == old_name:
                        e["source"] = value
                    if e.get("destination") == old_name:
                        e["destination"] = value
            log.append(f'Renamed "{old_name}" -> "{value}"')
        else:
            target[field] = value
            log.append(f'"{entity_name}".{field} = {value!r}')

    return log


def main():
    parser = argparse.ArgumentParser(description="Graph-only: image to full NL2Sim config, with clarification questions for anything uncertain.")
    parser.add_argument("image_path", help="Path to the sketch/graph image")
    parser.add_argument("--out", default=None, help="Output JSON path (default: output/<image_name>_config.json)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"OpenAI model to use (default: {DEFAULT_MODEL})")
    parser.add_argument("--max-rounds", type=int, default=4, help="Safety cap on refinement rounds (default: 4)")
    args = parser.parse_args()

    if not os.path.exists(args.image_path):
        print(f"Error: image not found at {args.image_path}", file=sys.stderr)
        sys.exit(1)

    if args.out:
        out_path = args.out
    else:
        base = os.path.splitext(os.path.basename(args.image_path))[0]
        out_path = os.path.join("output", f"{base}_config.json")

    print(f"\nExtracting config from {args.image_path}...")
    result = extract_full_config(args.image_path, model=args.model)
    config = result.get("config", {})
    questions = result.get("clarification_questions", [])

    print(f"\nInitial extraction complete.")
    print(json.dumps(config, indent=2))

    for round_num in range(1, args.max_rounds + 1):
        if not questions:
            break
        print(f"\n--- Clarification round {round_num} ---")
        answer = collect_refinement_answer(questions)
        if not answer:
            print("  No answer given -- leaving remaining questions unresolved.")
            break

        result = interpret_refinement_answer(config, questions, answer, model=args.model)
        updates = result.get("updates", [])
        still_unresolved_ids = set(result.get("still_unresolved_ids", []))

        log = apply_updates(config, updates)
        print("\n  Applied:")
        for line in log:
            print(f"    {line}")

        questions = [q for q in questions if q["id"] in still_unresolved_ids]
    else:
        if questions:
            print(f"\n  Reached max rounds ({args.max_rounds}) with {len(questions)} question(s) still unresolved.")

    if questions:
        print(f"\n  {len(questions)} field(s) remain 'missing' with unresolved questions:")
        for q in questions:
            print(f'    [{q["id"]}] {q["question"]}')
    else:
        print("\n  All clarification questions resolved.")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"\nFinal config written to {out_path}")


if __name__ == "__main__":
    main()