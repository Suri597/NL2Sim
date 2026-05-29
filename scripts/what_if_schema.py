"""
scripts/whatif_schema.py
-------------------------
What-if JSON schema definition.
Edit this file to add new operations, entity types, or relation types.
"""

WHATIF_SCHEMA = """
What-if JSON schema:

{
  "changes": [
    {
      "op": "create | update | delete",

      "entity_type": "raw_material | intermediate_material | product | inventory | supplier | resource | facility | customer | node | edge",

      "entity_id": {
        "name": "string (required for update and delete)",
        "index": "integer (optional alternative to name)"
      },

      "path": "dot-separated path to field (required for update only)",

      "value": "new value — scalar or full object (required for create and update)",

      "relation": {
        "type": "edge",
        "from": "source node name",
        "to": "destination node name",
        "attributes": {
          "material_name": "string"
        }
      },

      "meta": {
        "reason": "brief explanation of this change"
      }
    }
  ]
}

Rules:
- op must be one of: create, update, delete
- entity_type must be one of the allowed values above
- update requires entity_id + path + value
- create requires value with the full object
- delete requires entity_id with name
- edge updates use relation instead of entity_id
- path uses dot notation e.g. supplier_lead_time.distribution
- Multiple changes can be included in one what-if JSON
- Output valid JSON only — no markdown, no explanation
"""