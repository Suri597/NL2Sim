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

--- Example 3: delete an entity ---
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

--- Example 4: add a new entity ---
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
        "arrival_time": {"distribution": "constant", "parameters": {"a": 10, "b": 0, "c": 0, "d": 0, "e": 0}},
        "demand": {"distribution": "constant", "parameters": {"a": 1, "b": 0, "c": 0, "d": 0, "e": 0}},
        "customer_lead_time": {"distribution": "constant", "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}},
        "shortage_policy": "backorder",
        "unit_selling_price": 0,
        "customer_payment_lead_time": {"distribution": "constant", "parameters": {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}}
      },
      "meta": {"reason": "add new customer TechCorp"}
    }
  ]
}

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
"""