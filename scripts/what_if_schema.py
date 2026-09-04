"""
scripts/what_if_schema.py
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
        "name": "string (required for update and delete, EXCEPT edge)",
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
- For "create", any field NOT specified or implied by the instruction
  must be set to the literal string "missing" -- including inside
  nested distribution objects (both "distribution" and every
  "parameters" entry not given). Never invent a plausible-looking
  specific value (a lead time, a price, a policy, a quantity) for
  something the instruction never actually stated. The one exception:
  an edge's "transfer_time" defaults to {"distribution": "constant",
  "parameters": {"a": 0, ...}} when unspecified, since instantaneous
  transfer is this domain's own established default -- every other
  field follows the "missing" rule above.
- op must be one of: create, update, delete
- entity_type must be one of the allowed values above
- update requires entity_id + path + value
- create requires value with the full object
- delete requires entity_id with name -- EXCEPT for entity_type "edge"
- edges have NO name field at all. For BOTH update AND delete of an
  edge, use "relation" instead of "entity_id" -- entity_id is never
  valid for edges under any operation. A "delete" on an edge with
  entity_type "edge" must supply "relation" (from/to/material_name),
  not entity_id.name -- there is no such thing as an edge's name.
- relation.from and relation.to may both be omitted if the instruction
  doesn't specify them (e.g. "delete the edge from X" with no named
  destination) -- match only on what's actually known; do not invent
  a "to" value that duplicates "from" when the destination wasn't
  stated.
- path uses dot notation e.g. supplier_lead_time.distribution
- Multiple changes can be included in one what-if JSON
- When an instruction implies MULTIPLE structural consequences (e.g.
  "route X directly instead of through Y" implies deleting BOTH the
  old incoming AND outgoing edges through Y, not just adding the new
  direct edge), include ALL of them as separate changes -- do not
  leave the old, now-redundant edges in place.
- Output valid JSON only — no markdown, no explanation
"""