# simulationv2/config/what_if.py
from __future__ import annotations

from typing import Any, Dict, List
from copy import deepcopy


class WhatIfError(Exception):
    pass


def _normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        v = value.strip().lower()
        if v == "true":
            return True
        if v == "false":
            return False
        if v == "null":
            return None
    return value


def apply_what_if_config(
    base_config: Dict[str, Any],
    what_if_config: Dict[str, Any],
) -> Dict[str, Any]:
    cfg = deepcopy(base_config)
    changes = what_if_config.get("changes", [])
    if not isinstance(changes, list):
        raise WhatIfError("what_if_config.changes must be a list")

    for i, change in enumerate(changes):
        try:
            if "value" in change:
                change["value"] = _normalize_value(change["value"])
            _apply_single_change(cfg, change)
        except Exception as e:
            raise WhatIfError(f"Failed applying change[{i}]: {e}") from e

    return cfg


def _apply_single_change(cfg: Dict[str, Any], change: Dict[str, Any]) -> None:
    op = change.get("op")
    entity_type = change.get("entity_type")
    entity_id = change.get("entity_id")
    path = change.get("path")
    value = change.get("value")
    relation = change.get("relation")

    if op not in {"create", "update", "delete", "link", "unlink"}:
        raise WhatIfError(f"Unsupported op '{op}'")
    if entity_type is None:
        raise WhatIfError("entity_type is required")

    if op == "update":
        _apply_update(cfg, entity_type, entity_id, relation, path, value)
    elif op == "create":
        _apply_create(cfg, entity_type, value)
    elif op == "delete":
        # FIX: relation must be passed through here, mirroring _apply_update
        # -- edges (and only edges) have no 'name' field, so entity_id
        # alone can never identify one. Previously relation was dropped
        # entirely on this path, meaning natural-language edge deletion
        # could never work regardless of what the LLM produced.
        _apply_delete(cfg, entity_type, entity_id, relation)
    elif op == "link":
        _apply_link(cfg, relation)
    elif op == "unlink":
        _apply_unlink(cfg, relation)
    else:
        raise WhatIfError(f"Unhandled op '{op}'")


def _apply_update(
    cfg: Dict[str, Any],
    entity_type: str,
    entity_id: Dict[str, Any] | None,
    relation: Dict[str, Any] | None,
    path: str | None,
    value: Any,
) -> None:
    target = _resolve_entity(cfg, entity_type, entity_id, relation)
    if path is None:
        raise WhatIfError("update requires 'path'")
    _set_by_path(target, path, value)


def _apply_create(cfg: Dict[str, Any], entity_type: str, value: Any) -> None:
    section = _entity_section(entity_type)
    if section not in cfg:
        cfg[section] = []
    if not isinstance(cfg[section], list):
        raise WhatIfError(f"Config section '{section}' is not a list")
    cfg[section].append(value)


def _apply_delete(
    cfg: Dict[str, Any],
    entity_type: str,
    entity_id: Dict[str, Any] | None,
    relation: Dict[str, Any] | None = None,
) -> None:
    section = _entity_section(entity_type)
    items = cfg.get(section)

    if not isinstance(items, list):
        raise WhatIfError(f"Config section '{section}' is not deletable")

    # FIX: edges have no 'name' -- resolve deletion via the same
    # (source, destination, material_name) relation scheme _resolve_edge
    # already uses for updates, instead of falling through to the
    # name/index-only path below, which no edge can ever satisfy.
    if entity_type == "edge":
        _apply_delete_edge(cfg, relation)
        return

    name = entity_id.get("name") if entity_id else None
    index = entity_id.get("index") if entity_id else None

    if index is not None:
        items.pop(index)
        return

    if name is not None:
        cfg[section] = [x for x in items if x.get("name") != name]
        return

    raise WhatIfError("delete requires entity_id.name or entity_id.index")


def _apply_delete_edge(cfg: Dict[str, Any], relation: Dict[str, Any] | None) -> None:
    """
    Deletes edge(s) matching a PARTIAL relation -- only fields actually
    provided need to match (e.g. an instruction naming only a source,
    like "delete the edge from TechRetail Corp", matches on source alone;
    fields not specified act as wildcards). If more than one edge
    matches an under-specified relation, ALL of them are deleted, but
    the count is reported clearly so an unintentionally-broad deletion
    is visible rather than silently ambiguous.

    SELF-LOOP FALLBACK: a real, observed failure mode is the upstream
    LLM producing relation.from == relation.to when the instruction only
    specified a source (e.g. "delete the edge from TechRetail Corp") --
    with no destination given, it appears to default "to" to the same
    value as "from" rather than omitting it, describing a self-loop that
    almost never exists in this domain instead of the real, differently-
    destined edge the person meant. If an exact from==to match fails,
    retry once treating the destination as unspecified (source+material
    only) before giving up -- a genuine self-loop deletion still works
    correctly, since the exact match is always tried FIRST.
    """
    if not relation:
        raise WhatIfError("edge delete requires relation")

    src = relation.get("from")
    dst = relation.get("to")
    mat = relation.get("attributes", {}).get("material_name") if relation.get("attributes") else None

    if src is None and dst is None and mat is None:
        raise WhatIfError(
            "edge delete requires at least one of relation.from, "
            "relation.to, or relation.attributes.material_name"
        )

    edges = cfg.get("edges", [])
    if not isinstance(edges, list):
        raise WhatIfError("Config section 'edges' is not deletable")

    def find_matches(src, dst, mat):
        def matches(edge):
            if src is not None and edge.get("source") != src:
                return False
            if dst is not None and edge.get("destination") != dst:
                return False
            if mat is not None and edge.get("material_name") != mat:
                return False
            return True
        return [e for e in edges if matches(e)], matches

    matching, matches = find_matches(src, dst, mat)

    if not matching and src is not None and dst is not None and src == dst:
        # Exact self-loop match failed -- likely a hallucinated "to" that
        # just copied "from" because the destination was never actually
        # specified. Retry treating destination as unspecified.
        fallback_matching, fallback_matches = find_matches(src, None, mat)
        if fallback_matching:
            print(f"  Note: relation specified a self-loop ({src!r} -> {dst!r}), "
                  f"which doesn't match any real edge -- treating destination as "
                  f"unspecified and matching on source (and material, if given) instead.")
            matching, matches = fallback_matching, fallback_matches
            dst = None

    if not matching:
        raise WhatIfError(
            f"No edge found matching source={src!r}, destination={dst!r}, "
            f"material_name={mat!r}"
        )

    cfg["edges"] = [e for e in edges if not matches(e)]
    print(f"  Deleted {len(matching)} edge(s) matching "
          f"source={src!r}, destination={dst!r}, material_name={mat!r}.")


def _apply_link(cfg: Dict[str, Any], relation: Dict[str, Any]) -> None:
    raise WhatIfError("link operation not implemented yet")


def _apply_unlink(cfg: Dict[str, Any], relation: Dict[str, Any]) -> None:
    raise WhatIfError("unlink operation not implemented yet")


def _resolve_entity(
    cfg: Dict[str, Any],
    entity_type: str,
    entity_id: Dict[str, Any] | None,
    relation: Dict[str, Any] | None,
) -> Dict[str, Any]:
    if entity_type == "edge":
        return _resolve_edge(cfg, relation)

    section = _entity_section(entity_type)
    items = cfg.get(section)
    if not isinstance(items, list):
        raise WhatIfError(f"Section '{section}' not found")
    if entity_id is None:
        raise WhatIfError("entity_id required")
    if "index" in entity_id and entity_id["index"] is not None:
        return items[entity_id["index"]]
    name = entity_id.get("name")
    if name is not None:
        for item in items:
            if item.get("name") == name:
                return item
    raise WhatIfError(f"Entity not found in '{section}': {entity_id}")


def _resolve_edge(cfg: Dict[str, Any], relation: Dict[str, Any]) -> Dict[str, Any]:
    if not relation:
        raise WhatIfError("edge update requires relation")
    if relation.get("type") != "edge":
        raise WhatIfError("relation.type must be 'edge'")
    src = relation.get("from")
    dst = relation.get("to")
    mat = relation.get("attributes", {}).get("material_name")
    for edge in cfg.get("edges", []):
        if (edge.get("source") == src and edge.get("destination") == dst
                and edge.get("material_name") == mat):
            return edge
    raise WhatIfError(f"Edge not found: {src} → {dst} ({mat})")


_ENTITY_TYPE_TO_SECTION = {
    "raw_material": "raw_materials",
    "intermediate_material": "intermediate_materials",
    "product": "products",
    "inventory": "inventory",
    "supplier": "supplier",
    "resource": "resource",
    "facility": "facility",
    "customer": "customer",
    "edge": "edges",
    "node": "nodes",
}


def _entity_section(entity_type: str) -> str:
    """
    Maps entity_type -> the config section it lives in.

    Auto-corrects the common singular/plural confusion (e.g. "products"
    instead of "product") -- this is the SAME failure pattern already
    seen and specifically handled elsewhere in this system for JSON
    field values (verification_layer1.py's repair_invalid_enum_value),
    now showing up here too: an LLM that correctly understands the
    STRUCTURE of a change but uses the section name instead of the
    singular entity_type value. Since section names ARE the plural
    form of every entity_type here except "inventory"/"supplier"/
    "resource"/"facility"/"customer" (already identical either way) and
    "edge"/"node" (irregular -- "edges"/"nodes"), stripping a trailing
    "s" and re-checking covers this unambiguously without needing a
    hardcoded list of every possible typo.

    Raises a clear, actionable WhatIfError (not a raw KeyError) when
    entity_type genuinely isn't recognized -- a bare KeyError's string
    representation is just the missing key itself (e.g. "'products'"),
    which gives no indication anything is wrong with entity_type
    specifically, let alone what the valid values are.
    """
    if entity_type in _ENTITY_TYPE_TO_SECTION:
        return _ENTITY_TYPE_TO_SECTION[entity_type]

    if isinstance(entity_type, str) and entity_type.endswith("s"):
        singular = entity_type[:-1]
        if singular in _ENTITY_TYPE_TO_SECTION:
            return _ENTITY_TYPE_TO_SECTION[singular]

    raise WhatIfError(
        f"Unknown entity_type: {entity_type!r} -- must be one of: "
        f"{', '.join(sorted(_ENTITY_TYPE_TO_SECTION.keys()))}"
    )


def _set_by_path(obj: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value