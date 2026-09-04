"""
scripts/whatif_examples.py
---------------------------
Few-shot examples for the what-if LLM prompt.
Add new examples here to improve LLM accuracy for specific change types.
Each example has a plain English instruction and the expected what-if JSON output.
"""

WHATIF_EXAMPLES = """
Examples:

--- Example 1: update a single field ---
Instruction: "increase supplier capacity of Supplier A to 500"
Output:
{
  "changes": [
    {
      "op": "update",
      "entity_type": "supplier",
      "entity_id": {"name": "Supplier A"},
      "path": "supplier_capacity",
      "value": 500,
      "meta": {"reason": "increase supplier capacity to 500"}
    }
  ]
}

--- Example 2: update multiple fields in one instruction ---
Instruction: "change supplier lead time of Supplier A to uniform distribution with min 2 and max 5, and increase capacity to 200"
Output:
{
  "changes": [
    {
      "op": "update",
      "entity_type": "supplier",
      "entity_id": {"name": "Supplier A"},
      "path": "supplier_lead_time",
      "value": {
        "distribution": "uniform",
        "parameters": {"a": 2, "b": 5, "c": 0, "d": 0, "e": 0}
      },
      "meta": {"reason": "change supplier lead time to uniform(2,5)"}
    },
    {
      "op": "update",
      "entity_type": "supplier",
      "entity_id": {"name": "Supplier A"},
      "path": "supplier_capacity",
      "value": 200,
      "meta": {"reason": "increase supplier capacity to 200"}
    }
  ]
}

--- Example 3: delete an entity (non-edge) ---
Instruction: "remove Supplier B from the supply chain"
Output:
{
  "changes": [
    {
      "op": "delete",
      "entity_type": "supplier",
      "entity_id": {"name": "Supplier B"},
      "meta": {"reason": "remove Supplier B from supply chain"}
    }
  ]
}

--- Example 4: add a new entity -- fields the instruction did NOT specify use "missing" ---
Instruction: "add a new customer called TechCorp ordering Product X every 10 days"
Output:
{
  "changes": [
    {
      "op": "create",
      "entity_type": "customer",
      "value": {
        "name": "TechCorp",
        "product": "Product X",
        "arrival_time": {"distribution": "constant", "parameters": {"a": 10, "b": "missing", "c": "missing", "d": "missing", "e": "missing"}},
        "demand": {"distribution": "missing", "parameters": {"a": "missing", "b": "missing", "c": "missing", "d": "missing", "e": "missing"}},
        "customer_lead_time": {"distribution": "missing", "parameters": {"a": "missing", "b": "missing", "c": "missing", "d": "missing", "e": "missing"}},
        "shortage_policy": "missing",
        "unit_selling_price": "missing",
        "customer_payment_lead_time": {"distribution": "missing", "parameters": {"a": "missing", "b": "missing", "c": "missing", "d": "missing", "e": "missing"}}
      },
      "meta": {"reason": "add new customer TechCorp, ordering every 10 days as stated -- all other fields left as 'missing' since the instruction didn't specify them"}
    }
  ]
}
Note: ONLY "arrival_time" was actually specified ("every 10 days" -> a=10). Every
other field -- demand quantity, lead times, shortage policy, price -- was NEVER
mentioned in the instruction, so it is "missing", not a confidently invented
number. Inventing specific business values (a lead time, a price, a policy) that
were never stated is a hallucination, even when the invented value looks
plausible -- always use "missing" for anything genuinely unspecified, and let
the downstream repair process ask the person directly instead of guessing on
their behalf.

--- Example 5: structural change — move entity between categories ---
Instruction: "convert peacock from product to intermediate material and add polished_peacock as a new product"
Output:
{
  "changes": [
    {
      "op": "delete",
      "entity_type": "product",
      "entity_id": {"name": "peacock"},
      "meta": {"reason": "remove peacock from products"}
    },
    {
      "op": "create",
      "entity_type": "intermediate_material",
      "value": {
        "name": "peacock",
        "bom": {"black_feather": 6, "eye": 2}
      },
      "meta": {"reason": "add peacock as intermediate material"}
    },
    {
      "op": "create",
      "entity_type": "product",
      "value": {
        "name": "polished_peacock",
        "bom": {"peacock": 1}
      },
      "meta": {"reason": "add polished_peacock as final product"}
    }
  ]
}

--- Example 6: update an edge transfer time ---
Instruction: "change transfer time from Supplier A to Manufacturing Plant for Steel to exponential with rate 0.5"
Output:
{
  "changes": [
    {
      "op": "update",
      "entity_type": "edge",
      "relation": {
        "type": "edge",
        "from": "Supplier A",
        "to": "Manufacturing Plant",
        "attributes": {"material_name": "Steel"}
      },
      "path": "transfer_time",
      "value": {
        "distribution": "exponential",
        "parameters": {"a": 0.5, "b": 0, "c": 0, "d": 0, "e": 0}
      },
      "meta": {"reason": "change edge transfer time to exponential(0.5)"}
    }
  ]
}

--- Example 7: delete an edge -- edges use "relation", NEVER "entity_id" ---
Instruction: "stop shipping Steel from Supplier A to Manufacturing Plant"
Output:
{
  "changes": [
    {
      "op": "delete",
      "entity_type": "edge",
      "relation": {
        "type": "edge",
        "from": "Supplier A",
        "to": "Manufacturing Plant",
        "attributes": {"material_name": "Steel"}
      },
      "meta": {"reason": "stop shipping Steel from Supplier A to Manufacturing Plant"}
    }
  ]
}
Note: edges have no "name" field. entity_id is NEVER used for an edge,
for update OR delete -- always use "relation" for both.

--- Example 8: reroute — implies deleting BOTH old edges, not just adding the new one ---
Instruction: "Widget doesn't need to go through Central Warehouse anymore -- just have Plant A ship straight to Customer X."
Output:
{
  "changes": [
    {
      "op": "delete",
      "entity_type": "edge",
      "relation": {
        "type": "edge",
        "from": "Plant A",
        "to": "Central Warehouse",
        "attributes": {"material_name": "Widget"}
      },
      "meta": {"reason": "Widget no longer routes through Central Warehouse -- remove inbound leg"}
    },
    {
      "op": "delete",
      "entity_type": "edge",
      "relation": {
        "type": "edge",
        "from": "Central Warehouse",
        "to": "Customer X",
        "attributes": {"material_name": "Widget"}
      },
      "meta": {"reason": "Widget no longer routes through Central Warehouse -- remove outbound leg"}
    },
    {
      "op": "create",
      "entity_type": "edge",
      "value": {
        "source": "Plant A",
        "destination": "Customer X",
        "material_type": "product",
        "material_name": "Widget",
        "transfer_time": {"distribution": "constant", "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}}
      },
      "meta": {"reason": "direct shipment from Plant A to Customer X, bypassing Central Warehouse"}
    }
  ]
}
Note: "bypass Y" or "no longer go through Y" means removing BOTH the
edge INTO Y and the edge OUT of Y for that material, not just adding
the new direct edge. Leaving the old edges in place creates redundant,
stale routing even though it wouldn't fail validation.
"""