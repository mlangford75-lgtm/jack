from __future__ import annotations

from typing import Any
from dataclasses import dataclass

from jack.chassis.prompt_registry import verify_pillar

@dataclass(frozen=True, slots=True)
class MuscleResult:
    """Contains the synthesized output produced by the Muscle pillar."""
    output: str = ""

class Muscle:
    """The Synthesizer pillar responsible for drafting code, text, and commands."""

    SYSTEM_PROMPT = (
        "You are the Muscle. You synthesize concrete code, text, or commands based EXACTLY "
        "on the provided execution plan. Do not deviate. Do not converse. "
        "Output only the requested work product. "
        "For any mathematical calculations, rigorous proofs, or logical constraints, you MUST formulate the logic into an offline Z3 Satisfaction Modulo Theories (SMT) script. "
        "Pre-import `z3`, `numpy`, and `sympy`. "
        "Structure your script to initialize `s = z3.Solver()`, add constraints using `s.add()`, and print the model if `s.check() == z3.sat` else print 'unsat'. "
        "Do not compute numerically yourself unless the plan explicitly requests a float."
    )

    def __init__(self, engine: Any, *args: Any, **kwargs: Any) -> None:
        verify_pillar("muscle", self.SYSTEM_PROMPT)
        self.engine = engine
        self.system_prompt = self.SYSTEM_PROMPT

    async def execute(self, task_description: str, mode: str = "flash", timeout_seconds: float | None = None, *args: Any, **kwargs: Any) -> MuscleResult:
        """Synthesize the required work product using the probabilistic Engine."""
        response = self.engine.complete(
            task_description,
            system_prompt=self.system_prompt,
            preserve_thinking=False, # Enforce selective reasoning optimization
            max_tokens=2048, # Enforce strict output ceiling
            timeout_seconds=timeout_seconds
        )
        return MuscleResult(output=response.content)