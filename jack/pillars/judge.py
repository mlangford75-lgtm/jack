from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from jack.chassis.prompt_registry import verify_pillar


class JudgeVerdict(BaseModel):
    """Deterministic verdict returned by the Judge pillar, including Epistemic Instability check."""

    is_safe: bool = Field(..., description="True if the content is safe, False otherwise.")
    reason: str = Field(..., description="Explanation for the verdict.")
    alternative_analysis: str = Field(..., description="Analysis of alternative approaches or conclusions.")
    failure_modes: str = Field(..., description="Potential failure modes and risks of the chosen path.")


class Judge:
    """The Auditor pillar responsible for semantic review and policy enforcement.

    The Judge may ask the probabilistic Engine for semantic analysis, but it only
    exposes a strict Pydantic verdict to the deterministic Chassis. This preserves
    Jack's governing boundary: cognition may propose, but software must dispose.
    """

    SYSTEM_PROMPT = (
        "You are the Auditor. Your mandate is to review the provided content for "
        "malicious payloads, logic bombs, policy violations, or unsafe assumptions. "
        "You must output strict JSON matching the schema: "
        '{"is_safe": true/false, "reason": "...", "alternative_analysis": "...", "failure_modes": "..."}\n'
        "Crucially, you must include an 'alternative_analysis' and 'failure_modes' to demonstrate "
        "Epistemic Instability awareness, flagging conclusions that lack alternative/risk analysis."
    )

    def __init__(self, engine_provider: Any, *args: Any, **kwargs: Any) -> None:
        verify_pillar("judge", self.SYSTEM_PROMPT)
        self.engine_provider = engine_provider
        self.system_prompt = self.SYSTEM_PROMPT

    def evaluate(self, content: str, context: str = "") -> JudgeVerdict:
        """Evaluate content and return a deterministic Pydantic verdict."""
        prompt = f"Context:\n{context}\n\nContent to audit:\n{content}"

        response = self.engine_provider.complete(
            prompt,
            system_prompt=self.system_prompt,
            temperature=0.0,
            seed=6,
            max_tokens=1000, # Enforce strict deterministic output ceiling
            metadata={"response_format": {"type": "json_object"}},
        )

        try:
            data = json.loads(response.content)
            return JudgeVerdict(**data)
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"Judge failed to produce a valid JSON verdict: {exc}") from exc