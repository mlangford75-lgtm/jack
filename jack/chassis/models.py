from dataclasses import dataclass, field
from typing import Any, Mapping

@dataclass(frozen=True, slots=True)
class StepExecutionResult:
    """Contains the aggregated records of all executed steps."""
    records: Mapping[str, Any] = field(default_factory=dict)