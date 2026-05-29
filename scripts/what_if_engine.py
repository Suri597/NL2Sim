# simulationv2/config/what_if.py
from __future__ import annotations

from typing import Any, Dict, List
from copy import deepcopy


class WhatIfError(Exception):
    pass


# ============================================================
# Public API
# ============================================================
def _normalize_value(value: Any) -> Any:
    """
    Normalize JSON/LLM values into Python-native types.
    """
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
    """
    Applies a bounded what-if change set to a base config.

    - Does NOT validate
    - Does NOT prompt
    - Deterministic
    """

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


# ============================================================
# Change dispatcher
# ============================================================

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
        _apply_delete(cfg, entity_type, entity_id)
    elif op == "link":
        _apply_link(cfg, relation)
    elif op == "unlink":
        _apply_unlink(cfg, relation)
    else:
        raise WhatIfError(f"Unhandled op '{op}'")


# ============================================================
# UPDATE
# ============================================================

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


# ============================================================
# CREATE
# ============================================================

def _apply_create(
    cfg: Dict[str, Any],
    entity_type: str,
    value: Any,
) -> None:
    section = _entity_section(entity_type)

    if section not in cfg:
        cfg[section] = []

    if not isinstance(cfg[section], list):
        raise WhatIfError(f"Config section '{section}' is not a list")

    cfg[section].append(value)


# ============================================================
# DELETE
# ============================================================

def _apply_delete(
    cfg: Dict[str, Any],
    entity_type: str,
    entity_id: Dict[str, Any] | None,
) -> None:
    section = _entity_section(entity_type)
    items = cfg.get(section)

    if not isinstance(items, list):
        raise WhatIfError(f"Config section '{section}' is not deletable")

    name = entity_id.get("name") if entity_id else None
    index = entity_id.get("index") if entity_id else None

    if index is not None:
        items.pop(index)
        return

    if name is not None:
        cfg[section] = [x for x in items if x.get("name") != name]
        return

    raise WhatIfError("delete requires entity_id.name or entity_id.index")


# ============================================================
# LINK / UNLINK (reserved for future)
# ============================================================

def _apply_link(cfg: Dict[str, Any], relation: Dict[str, Any]) -> None:
    raise WhatIfError("link operation not implemented yet")


def _apply_unlink(cfg: Dict[str, Any], relation: Dict[str, Any]) -> None:
    raise WhatIfError("unlink operation not implemented yet")


# ============================================================
# Entity resolution
# ============================================================

def _resolve_entity(
    cfg: Dict[str, Any],
    entity_type: str,
    entity_id: Dict[str, Any] | None,
    relation: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """
    Resolves the target object to mutate.
    """

    # --- Edge resolution (special case)
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
        if (
            edge.get("source") == src
            and edge.get("destination") == dst
            and edge.get("material_name") == mat
        ):
            return edge

    raise WhatIfError(
        f"Edge not found: {src} → {dst} ({mat})"
    )


# ============================================================
# Helpers
# ============================================================

def _entity_section(entity_type: str) -> str:
    return {
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
    }[entity_type]


def _set_by_path(obj: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj

    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]

    cur[parts[-1]] = value
