"""
scripts2/issue_types.py
------------------------
Shared contract for Phase 1 validation layers.

Every validation layer (Layer0, LayerB, LayerC — LayerB/LayerC added later)
emits a list of ValidationIssue objects. This is the single source of truth
for "what kind of problem is this" and "does it block downstream processing."

Design rule (this is the fix for the v1 bug where a skipped structural issue
was silently allowed to reach canonicalize_config and crash):
    - severity is decided HERE, by the validator, at detection time.
    - severity is NOT decided later by a human choosing "skip" in a repair UI.
    - Any BLOCKING issue must prevent canonicalize_config / save / simulate
      from running until it is resolved (fixed, not skipped).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DefectType(Enum):
    MISSING_REQUIRED_VALUE = "missing_required_value"      # required field absent or == "missing"
    MISSING_OPTIONAL_VALUE = "missing_optional_value"       # optional field absent or == "missing"
    MALFORMED_ENTRY = "malformed_entry"                     # e.g. None where a dict was expected
    INVALID_VALUE = "invalid_value"                         # wrong type / out of range / bad enum
    DANGLING_REFERENCE = "dangling_reference"                # e.g. edge -> nonexistent node (LayerB)
    DUPLICATE_ENTITY = "duplicate_entity"                    # e.g. duplicate edge (LayerB)
    INCONSISTENT_CROSS_FIELD = "inconsistent_cross_field"    # e.g. supplier not registered in nodes (LayerB)


class Severity(Enum):
    BLOCKING = "blocking"   # must be resolved before proceeding to the next layer / canonicalize / save
    WARNING = "warning"     # can proceed, but should be surfaced to the user


@dataclass
class ValidationIssue:
    layer: str                                   # "Layer0" | "LayerB" | "LayerC"
    location: str                                # dotted/bracket path, e.g. "edges[4]" or
                                                  # "supplier[2].supplier_lead_time.parameters.a"
    defect_type: DefectType
    severity: Severity
    detail: str                                  # human-readable explanation
    context: dict[str, Any] = field(default_factory=dict)  # raw values / entity names for resolvers

    def __repr__(self) -> str:
        return (
            f"[{self.layer}::{self.severity.value}::{self.defect_type.value}] "
            f"{self.location}: {self.detail}"
        )


def has_blocking(issues: list[ValidationIssue]) -> bool:
    """Convenience check used to gate canonicalize_config / save / simulate."""
    return any(i.severity == Severity.BLOCKING for i in issues)


def filter_by_severity(issues: list[ValidationIssue], severity: Severity) -> list[ValidationIssue]:
    return [i for i in issues if i.severity == severity]