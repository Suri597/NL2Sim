"""
scripts2/question_scheduler.py
--------------------------------
Groups Layer1 issues into ROUNDS of independent questions.

Before: the repair loop asked ONE question, re-ran all three verification
layers, asked the next question, and so on -- one verify/ask cycle per
issue. After: every Layer1 issue that does not depend on another open
issue is resolved in the same round, verification runs once, and the
next round picks up whatever those answers unlocked.

INTEGRATION (for the developer wiring this in)
    - Place this file in the same folder as repair_orchestrator.py,
      repair.py, issue_types.py and verification_layer1.py (imports are
      flat, like the rest of the scripts).
    - Only repair_orchestrator.py imports this file.
    - BATCH_QUESTIONS = False restores the old one-at-a-time behavior
      exactly. Nothing else needs to change to roll back.
    - USE_LLM_PHRASING = False makes every prompt raised from repair.py
      show its deterministic fallback text instead of calling the LLM.
      This is done by swapping repair.generate_question at runtime
      (apply_phrasing_mode), so repair.py itself is NOT edited. Prompts
      raised from other modules that import llm_humanize directly are
      not affected.
    - verification_layer1/2/3.py, repair.py, issue_types.py, pipeline.py:
      unchanged. This file reads a few of repair.py's existing helpers
      (describe_location, normalize_location, the lookup tables) but
      never modifies them.

EACH ITERATION OF THE REPAIR LOOP (when BATCH_QUESTIONS is True)
    1. Automatic fixes first. Layer1 issues whose repair needs no input
       (config version, edge transfer_time default, plural/singular enum
       auto-correct) are applied silently, then verification re-runs.
       Without this, an auto-correctable value (e.g. inventory.type
       "raw_materials") holds back its dependents for a whole extra
       round even though nobody had to answer anything.
    2. Otherwise, build the round (select_round), print ALL its questions
       together as a preview (format_round_preview), then ask them one by
       one. In the terminal only one prompt can be answered at a time;
       the preview shows which questions belong to the same round. In
       the UI, describe_round() gives the same list as structured data so
       all of them can be shown and answered together.
    3. Otherwise, fall through to the original one-at-a-time path
       (Layer2/Layer3 issues and anything left over).

WHY LAYER1 NEEDS SO LITTLE LOGIC
    Every conditional field in verification_layer1.SECTION_SPECS is gated
    by a `required` lambda that reads a sibling's CURRENT value, and the
    procurement_scheme check returns early when its type is missing or
    invalid. So a field whose gate is unresolved is never flagged in the
    first place: facility.operation is not flagged until facility.type is
    known, batching.batch_size not until batching.enabled is known,
    inventory.procurement_scheme not until inventory.type is known, and so
    on. Re-running Layer1 after each round therefore reveals the next
    level by itself. This file only handles the cases where Layer1 DOES
    flag a child while its gate is still open.

THE TWO DEFERRAL RULES
    1. Ancestor rule: an issue is deferred if another open Layer1 issue
       sits at an ancestor location (a placeholder container, a malformed
       entry, etc.). Purely path-based, no schema knowledge needed.
    2. Parameter rule: X.parameters / X.parameters.k is deferred while X,
       X.distribution, or X.type has an open issue. Layer1 falls back to
       requiring parameters.a when the distribution type is unknown, and
       repair.py asks the parameters together with the distribution type
       anyway, so asking 'a' first would ask it twice, and before the user
       knows what 'a' means for that distribution.

WITHIN-ROUND STALENESS
    Some repair actions fix more than their own issue (e.g.
    repair_procurement_scheme_field fills every missing parameter of the
    scheme at once). is_still_open() re-runs Layer1 only (no prompts, no
    LLM) right before each dispatch, so an issue already fixed by an
    earlier answer in the same round is skipped instead of asked again.

SCOPE
    Layer1 BLOCKING issues only. WARNING issues are never batched (the
    loop never repairs them). Layer2/Layer3 issues keep the original
    one-at-a-time path, which runs once Layer1 has nothing left to batch.

NOTE ON COUNTS
    One dispatched repair can show more than one prompt (a whole missing
    container, or a distribution type followed by its parameters). The
    stats below count dispatched repairs, not individual prompts.
"""

from __future__ import annotations

import re

from issue_types import DefectType, Severity
from verification_layer1 import check_field_requirements, LAYER as LAYER1_NAME


# ----------------------------------------------------------------------
# Switches
# ----------------------------------------------------------------------

BATCH_QUESTIONS = True      # False = original one-question-per-cycle loop
USE_LLM_PHRASING = False    # False = deterministic fallback text in repair.py prompts


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------

def issue_signature(issue) -> tuple:
    """Same key repair_orchestrator uses for its skip sets."""
    return (issue.location, issue.defect_type)


def _is_layer1_blocking(issue) -> bool:
    return issue.layer == LAYER1_NAME and issue.severity == Severity.BLOCKING


# ----------------------------------------------------------------------
# Automatic fixes (no user input needed)
# ----------------------------------------------------------------------
# These mirror the branches in repair.dispatch_repair that never prompt.
# If a new silent repair is added to dispatch_repair, add its pattern here
# too; if it isn't added, the only cost is that it runs inside a normal
# round instead of before it.

_TRANSFER_TIME_RE = re.compile(r"^edges\[\d+\]\.transfer_time")


def _plural_autocorrect_applies(config: dict, location: str) -> bool:
    """
    True if repair_invalid_enum_value would fix this value on its own,
    i.e. the current value is a plural whose singular form is a valid
    enum value (e.g. "raw_materials" -> "raw_material").
    """
    import repair
    try:
        enum_values = repair._lookup_enum_values(repair.normalize_location(location))
        if not enum_values:
            return False
        parent, key = repair._navigate_to_parent(config, repair._parse_location_steps(location))
        value = parent.get(key) if isinstance(parent, dict) else None
    except Exception:
        return False
    return isinstance(value, str) and value.endswith("s") and value[:-1] in enum_values


def is_auto_fixable(config: dict, issue) -> bool:
    if not _is_layer1_blocking(issue):
        return False
    loc = issue.location

    if issue.defect_type == DefectType.MISSING_REQUIRED_VALUE and loc == "config_info[0].version":
        return True

    if issue.defect_type in (DefectType.MISSING_REQUIRED_VALUE, DefectType.INVALID_VALUE) \
            and _TRANSFER_TIME_RE.match(loc):
        return True

    if issue.defect_type == DefectType.INVALID_VALUE and "procurement_scheme" not in loc:
        return _plural_autocorrect_applies(config, loc)

    return False


def select_auto_fixes(config: dict, blocking_issues: list, skipped_signatures: set = frozenset()) -> list:
    """Layer1 issues that can be repaired right now without asking anything."""
    return [
        i for i in blocking_issues
        if issue_signature(i) not in skipped_signatures and is_auto_fixable(config, i)
    ]


# ----------------------------------------------------------------------
# Deferral rules and round selection
# ----------------------------------------------------------------------

_PARAMETERS_RE = re.compile(r"^(?P<owner>.+)\.parameters(?:\.[^.]+)?$")
_GATE_KEYS = ("distribution", "type")


def _is_ancestor(candidate: str, location: str) -> bool:
    """
    True if `candidate` is a strict ancestor path of `location`.
    'inventory[0].procurement_scheme' is an ancestor of
    'inventory[0].procurement_scheme.parameters.a'; 'edges' is an
    ancestor of 'edges[3]'.
    """
    return location.startswith(candidate + ".") or location.startswith(candidate + "[")


def _gate_locations(location: str) -> set:
    """
    For X.parameters or X.parameters.k, returns the locations whose open
    issues must be resolved first: X, X.distribution, X.type. Empty set
    for any other location.
    """
    match = _PARAMETERS_RE.match(location)
    if not match:
        return set()
    owner = match.group("owner")
    return {owner} | {f"{owner}.{key}" for key in _GATE_KEYS}


def is_deferred(issue, open_locations: set) -> bool:
    """True if this issue must wait for another open issue to be resolved."""
    loc = issue.location
    if any(other != loc and _is_ancestor(other, loc) for other in open_locations):
        return True
    return any(gate != loc and gate in open_locations for gate in _gate_locations(loc))


def select_round(blocking_issues: list, skipped_signatures: set = frozenset()) -> tuple:
    """
    Returns (batch, deferred) for the current round.

    batch:    Layer1 BLOCKING issues that can be resolved now, together.
    deferred: Layer1 BLOCKING issues waiting on another open issue.

    Skipped issues (repair_orchestrator's permanent/temporary skip sets)
    are never put in the batch, but they still count as OPEN when
    deciding deferral. A child of a gate that cannot be repaired right now
    stays deferred here, and is left to the original one-at-a-time path.
    """
    layer1 = [i for i in blocking_issues if _is_layer1_blocking(i)]
    open_locations = {i.location for i in layer1}

    batch, deferred = [], []
    for issue in layer1:
        if issue_signature(issue) in skipped_signatures:
            continue
        if is_deferred(issue, open_locations):
            deferred.append(issue)
        else:
            batch.append(issue)
    return batch, deferred


def is_still_open(config: dict, issue) -> bool:
    """
    Re-runs Layer1 only (cheap, silent) to check whether an issue picked
    for this round has already been fixed by an earlier answer in the
    same round.
    """
    try:
        current = check_field_requirements(config)
    except Exception:
        # Let dispatch_repair run and surface the error the way it always
        # has, instead of hiding it here.
        return True
    target = issue_signature(issue)
    return any(issue_signature(i) == target for i in current)


# ----------------------------------------------------------------------
# Round preview (terminal now, UI later)
# ----------------------------------------------------------------------

_BOM_VALUE_RE = re.compile(r"^(intermediate_materials|products)\[\d+\]\.bom\.[^.]+$")

_KIND_TO_NEED = {
    "num": "enter a number",
    "name": "enter a name",
    "str": "enter a value",
    "bool": "answer yes or no",
}


def _what_is_needed(issue) -> str:
    """
    Short, deterministic description of what the user will be asked for.
    Mirrors how repair.dispatch_repair routes each issue. Falls back to
    the issue's own detail text for any shape it doesn't recognize.
    """
    import repair

    if issue.defect_type == DefectType.MALFORMED_ENTRY:
        return f"malformed entry: {issue.detail}"

    loc = issue.location
    normalized = repair.normalize_location(loc)

    try:
        if "procurement_scheme" in loc:
            need = "choose the procurement scheme type and its settings"
        elif _BOM_VALUE_RE.match(loc):
            need = "enter the quantity"
        elif normalized.endswith(".distribution"):
            need = "choose a distribution type and its parameters"
        elif normalized in repair.SCALAR_SELECT_CANDIDATE_FNS:
            need = "select from existing entries"
        elif normalized in repair.LIST_SELECT_CANDIDATE_FNS:
            need = "select one or more existing entries"
        elif repair._lookup_enum_values(normalized):
            need = f"choose one of: {', '.join(repair._lookup_enum_values(normalized))}"
        elif repair._lookup_field_type(normalized):
            need = _KIND_TO_NEED.get(repair._lookup_field_type(normalized), "enter a value")
        else:
            fspec = repair.find_spec_node(normalized) or {}
            if fspec.get("is_distribution"):
                need = "choose a distribution type and its parameters"
            elif fspec.get("is_dict_values"):
                need = "select ingredients and enter their quantities"
            elif "fields" in fspec:
                need = "fill in all of its required fields"
            else:
                need = issue.detail
    except Exception:
        need = issue.detail

    if issue.defect_type == DefectType.INVALID_VALUE:
        need = f"current value is invalid; {need}"
    return need


def describe_round(config: dict, batch: list) -> list:
    """
    Structured description of every question in the round, in the order
    they will be asked. The terminal preview is built from this; a UI can
    use the same list to show all questions of the round at once, each in
    its own input box or dropdown.
    """
    import repair

    items = []
    for number, issue in enumerate(batch, start=1):
        need = _what_is_needed(issue)
        try:
            # [LABELS] A parameter question is named after its distribution
            # and meaning (e.g. "Normal distribution parameter: standard deviation")
            # instead of "Parameters -> B" -- same labels repair.py prompts use.
            param_label = repair.param_label_from_config(config, issue.location)
            if param_label:
                owner = repair.param_owner_location(issue.location)
                label = f"{repair.describe_location(config, owner)} -> {param_label}"
                need = "enter a number"
            else:
                label = repair.describe_location(config, issue.location)
        except Exception:
            label = issue.location
        items.append({
            "number": number,
            "location": issue.location,
            "label": label,
            "need": need,
            "defect_type": issue.defect_type.value,
        })
    return items


def format_round_preview(config: dict, batch: list) -> str:
    """All questions of the round, grouped, for printing before answering."""
    lines = ["  These questions are independent of each other and belong to this round:"]
    for item in describe_round(config, batch):
        lines.append(f"    Q{item['number']}. {item['label']} -- {item['need']}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# LLM phrasing switch
# ----------------------------------------------------------------------

_ORIGINAL_GENERATE_QUESTION = None


def _fallback_only(config, location, description="", fallback_text="", **kwargs) -> str:
    """
    Stand-in for llm_humanize.generate_question: returns the deterministic
    fallback text that repair.py already passes on every call.
    """
    return fallback_text


def apply_phrasing_mode() -> None:
    """
    Points repair.generate_question at either the real LLM function or
    _fallback_only, depending on USE_LLM_PHRASING. repair.py looks the name
    up at call time, so this takes effect for every prompt in repair.py
    without editing it. Safe to call repeatedly.
    """
    global _ORIGINAL_GENERATE_QUESTION
    import repair

    if _ORIGINAL_GENERATE_QUESTION is None:
        _ORIGINAL_GENERATE_QUESTION = repair.generate_question

    repair.generate_question = _ORIGINAL_GENERATE_QUESTION if USE_LLM_PHRASING else _fallback_only


# ----------------------------------------------------------------------
# Run statistics (for evaluating batching against the old loop)
# ----------------------------------------------------------------------

LAST_RUN_STATS: dict = {}


def reset_stats() -> None:
    LAST_RUN_STATS.clear()
    LAST_RUN_STATS.update({
        "auto_fixed": 0,              # repairs applied with no input, before rounds
        "rounds": 0,                  # Layer1 question rounds run
        "repairs_per_round": [],      # repairs dispatched in each round
        "deferred_per_round": [],     # issues held back in each round
        "already_fixed_skipped": 0,   # batched issues fixed by an earlier answer in the same round
        "one_at_a_time_repairs": 0,   # repairs done on the original path (Layer2/3, leftovers)
    })


def format_stats() -> str:
    s = LAST_RUN_STATS
    return (
        f"Automatic fixes: {s['auto_fixed']} | "
        f"Layer1 rounds: {s['rounds']} | "
        f"repairs per round: {s['repairs_per_round']} | "
        f"deferred per round: {s['deferred_per_round']} | "
        f"already fixed within a round: {s['already_fixed_skipped']} | "
        f"one-at-a-time repairs: {s['one_at_a_time_repairs']}"
    )


reset_stats()