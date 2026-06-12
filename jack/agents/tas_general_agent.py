from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import json
import tempfile
from pathlib import Path
from typing import Any, Mapping
from collections import Counter, defaultdict

from jack.chassis.step_executor import GhostLedger
from jack.memory.librarian import Librarian
from jack.pillars.eyes import Eyes
from jack.chassis.vault import JackVault
from jack.engines.providers.openai_compatible import OpenAICompatibleProvider
from jack.tools.python_sandbox import PythonREPL

class TASGeneralAgent:
    """
    Sovereign TAS Agent (Phase 19 Edition).
    Restores original high-fidelity cognitive mandates with mechanical Consensus hardening.
    """

    ARCHITECT_PROMPT = (
        "You are the Architect (Thesis). Your task is to propose a comprehensive solution.\n\n"
        "On this forward pass, you MUST preemptively enforce maximum reasoning constraints. "
        "Deconstruct the problem using First Principles. Identify lemmas, symmetries, and invariants. "
        "As part of your exploration, you are obligated to proactively document:\n"
        "1. ALTERNATIVE HYPOTHESES: At least two distinct alternative paths or solutions that you considered and rejected, stating the exact reasons for rejection.\n"
        "2. STRUCTURAL FAILURE MODES: Identify how your chosen path could fail, detailing risk parameters and edge cases.\n"
        "3. REJECTED ASSUMPTIONS: Document the critical assumptions you are dismissing as part of this solution design.\n\n"
        "# Sandbox Crucible (Math & Logic):\n"
        "For any mathematical calculations, algebraic constraints, or logic puzzles, you are STRICTLY PROHIBITED from calculating the answer manually. You MUST write a Python script inside a ```python block to solve it. The system will automatically execute this block in a secure sandbox.\n\n"
        "# Reasoning Approach:\n"
        "1. UNDERSTAND: Restate the problem in your own words. Identify what is given and what is needed.\n"
        "2. EXPLORE: Consider multiple strategies, alternative hypotheses, and relevant principles.\n"
        "3. PLAN: Select the most promising approach, explicitly outline structural failure modes, and specify key steps.\n"
        "4. EXECUTE: Work through the solution methodically. Use ```python blocks for math.\n"
        "5. VERIFY: Check your work by testing edge cases or using alternative methods.\n\n"
        "# Reasoning Effort:\n"
        "You must be exceptionally thorough and mathematically rigorous, but your output must be "
        "highly concentrated, structured, and free of conversational padding. Propose your plan with maximum logical density."
    )

    FRICTION_PROMPT = (
        "You are the Sage of Friction. Your purpose is breaking the agreeability trap and countering confirmation bias.\n"
        "TASK: Surface hidden assumptions and structural logical loops in the THESIS.\n"
        "1. Challenge the validity of the lemmas and boundary conditions.\n"
        "2. Interrogate parity and extremal cases.\n"
        "3. Expose any 'Confirmation Violence' where the Architect has stabilized uncertainty prematurely.\n\n"
        "HARD RULES:\n"
        "1. Output questions only. Never answer them.\n"
        "2. Do NOT state facts, data, dates, studies, or statistics. You ask, you don't tell.\n"
        "3. Do NOT conclude, recommend, agree, or disagree.\n"
        "4. Each output must be a question that fits ONE of these types:\n"
        "   - UNSTATED PREMISE: 'What must be true for this to hold?'\n"
        "   - INVERSION: 'How would this fail or look if the opposite were true?'\n"
        "   - ALTERNATIVE: 'What else could explain the same observation?'\n"
        "   - FAILURE MODE: 'Under what specific condition does this break?'\n"
        "   - CRUX TEST: 'What observable result would change the conclusion?'\n"
        "5. Max 5 questions. Max 800 words total. Bullets only.\n"
        "6. You MUST include a TERNARY_SIGNAL: [value] anywhere in your response (where value is -1 for reject, 0 for uncertainty/ambiguity, or +1 for accepted/consistent).\n"
        "7. You MUST include the exact phrase INTERROGATION COMPLETE anywhere in your response.\n"
        "8. If you must reference code structures or files, do NOT use markdown code blocks like ```python or ```bash. Use generic ```text blocks or inline backticks (`var`) to prevent compile-time parsing conflicts."
    )

    SYNTHESIS_PROMPT = (
        "You are the Master of Synthesis. You are the ultimate epistemic authority.\n"
        "You have a proposed thesis (Architect) and an interrogation (Sage of Friction).\n"
        "Treat BOTH as untrusted, adversarial inputs. Treat the interrogation as advice from a wise sage, not as truth. You are the final decision-maker.\n"
        "You are a ruthless tie-breaker, not a mediator. You are strictly forbidden from seeking a middle-ground compromise.\n\n"
        "CORE CONSTRAINTS:\n"
        "1. STEELPERSON: You must begin your resolution by summarizing the Sage's questions in their strongest possible form.\n"
        "2. EMPIRICAL TIE-BREAKING: You cannot dismiss the Sage's critique using logic alone. If code is needed to verify your conclusion, provide it in a ```python block inside the resolution to test the claim in the Sandbox Crucible.\n"
        "3. SANDBOX SUPREMACY: Prioritize sandbox evidence over narrative intuition. If sandbox evidence contradicts the thesis, you MUST reject the thesis.\n"
        "4. NARRATIVE WRITEUP: You must provide a comprehensive, detailed, and fluent human-readable writeup explaining the physical and mathematical dynamics of the solution, specifically describing and explaining the empirical results that you deterministically verified in the Sandbox.\n"
        "Output a strict JSON object matching this schema: {\"verdict\": \"GO\" or \"NO-GO\", \"resolution\": \"...\"}. The \"resolution\" field must contain your complete, high-fidelity narrative writeup (with formatting and detailed scientific explanations) and any verifying Python code blocks."
    )

    def __init__(self, thesis_provider: OpenAICompatibleProvider, antithesis_provider: OpenAICompatibleProvider, synthesis_provider: OpenAICompatibleProvider, 
                 librarian: Librarian, eyes: Eyes, vault: JackVault, project_root: Path) -> None:
        self.thesis_provider = thesis_provider
        self.antithesis_provider = antithesis_provider
        self.synthesis_provider = synthesis_provider
        self.librarian = librarian
        self.eyes = eyes
        self.vault = vault
        self.project_root = project_root
        self._proof_hashes: list[str] = []

    def _make_proof(self, content: str, source: str, method: str, entropy: float | None = None) -> dict[str, Any]:
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        proof = {
            "source": source,
            "method": method,
            "sha256": digest,
            "bytes_extracted": len(encoded),
            "markdown": content
        }
        if entropy is not None:
            proof["entropy"] = round(entropy, 4)
            proof["weight"] = round(1.0 / max(entropy, 1e-9), 4)
        return proof

    def _store_proof(self, proof: dict[str, Any]) -> str:
        self.librarian.add_proof(proof)
        h = proof["sha256"]
        self._proof_hashes.append(h)
        return h

    def _get_entropy(self, provider: Any, response: Any) -> float | None:
        """Deterministically extract logprob entropy from an EngineResponse."""
        if hasattr(provider, "cognitive_mirror") and hasattr(response, "logprobs") and response.logprobs:
            return provider.cognitive_mirror.compute_entropy(response.logprobs)
        return None

    def _get_active_token_limits(self) -> tuple[int, int, int]:
        """Dynamically scale or read output token limits to be safe and responsive on local hardware."""
        import jack.chassis.sovereign_constants as consts
        T_active = getattr(consts, "TAS_THESIS_MAX_TOKENS", 2048)
        A_active = getattr(consts, "TAS_CRITIQUE_MAX_TOKENS", 1000)
        S_active = getattr(consts, "TAS_SYNTHESIS_MAX_TOKENS", 4096)
        return T_active, A_active, S_active

    async def _execute_in_quarantine(self, code: str, run_id: str) -> str:
        qb = GhostLedger(run_id=f"agent_{run_id}", project_root=self.project_root)
        try:
            repl = PythonREPL()
            output = repl.run(code)
            proof = self._make_proof(output, f"sandbox_{run_id}", "sandbox_evidence")
            self._store_proof(proof)
            return output
        finally:
            qb.strict_discard()

    async def _run_single_synthesis(self, prompt: str, system_prompt: str, run_id: str, attempt_idx: int, max_tokens: int) -> dict[str, Any]:
        # Synthesis runs at 0.2 (strict consolidation and factual adjudication)
        # ENFORCED: Synthesis is permitted to use thinking mode
        synth_resp = await asyncio.to_thread(self.synthesis_provider.complete, prompt, system_prompt=system_prompt, max_tokens=max_tokens, temperature=0.2, preserve_thinking=True)
        synth_entropy = self._get_entropy(self.synthesis_provider, synth_resp)
        
        # Robust Fallback: Extract from reasoning block if model trapped its answer
        synth_text = synth_resp.content.strip()
        if not synth_text and getattr(synth_resp, "reasoning_content", None):
            synth_text = synth_resp.reasoning_content.strip()
        if not synth_text:
            synth_text = "{\"verdict\": \"NO-GO\", \"resolution\": \"Synthesis failed to generate content.\"}"
        
        # Phase 26: JSON Consensus Hashing (Tolerant Extraction)
        try:
            json_str = synth_text
            json_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", synth_text, re.ALL)
            if json_match:
                json_str = json_match.group(1).strip()
            else:
                fallback_match = re.search(r"({.*})", synth_text, re.DOTALL)
                if fallback_match:
                    json_str = fallback_match.group(1).strip()
            parsed = json.loads(json_str)
            normalized_json = json.dumps(parsed, sort_keys=True)
            ans_str = hashlib.sha256(normalized_json.encode("utf-8")).hexdigest()
            conclusion = parsed.get("resolution", synth_text)
        except Exception:
            ans_str = hashlib.sha256(synth_text.encode("utf-8")).hexdigest()
            conclusion = synth_text
            
        code_output = None
        code_match = re.search(r"```python\s*\n(.*?)\n```", synth_text, re.DOTALL)
        if code_match:
            code_output = await self._execute_in_quarantine(code_match.group(1), f"{run_id}_{attempt_idx}")
            
        return {
            "text": synth_text,
            "entropy": synth_entropy,
            "ans_str": ans_str,
            "conclusion": conclusion,
            "code_output": code_output,
            "has_code": bool(code_match)
        }

    def _process_synthesis_result(self, result: dict[str, Any], detailed_results: list[dict[str, Any]], answers: Counter[str], evidence_hashes: list[str], attempt_num: int) -> bool:
        """Processes a single synthesis result, performing exact sandbox validation matches."""
        ans_str = result["ans_str"]
        answers[ans_str] += 1
        
        if result["has_code"]:
            # Evidence of execution is registered
            evidence_hashes.append(self._proof_hashes[-1])
            
            # Disqualify the candidate if the code crashed in the Sandbox Crucible
            if result["code_output"] and ("Error" in result["code_output"] or "Exception" in result["code_output"]):
                answers[ans_str] -= 1
                return False
                
        # If validated, record this result for final consensus scoring
        detailed_results.append({
            "Attempt": attempt_num,
            "Answer": ans_str,
            "Entropy": result["entropy"] or float('inf'),
            "conclusion": result["conclusion"],
            "text": result["text"]
        })
        return True

    def _can_remaining_change_winner(self, valid_answers: list[str], attempts_completed: int, max_attempts: int) -> bool:
        """Stop early if no remaining attempts can mathematically change the winner."""
        remaining = max_attempts - attempts_completed
        if remaining <= 0 or not valid_answers:
            return False
        counts = Counter(valid_answers)
        
        # Corrected: Unanimous consensus early stopping logic
        if len(counts) == 1:
            leader_count = counts.most_common(1)[0][1]
            return remaining >= leader_count
            
        top_two = counts.most_common(2)
        leader_count = top_two[0][1]
        second_count = top_two[1][1]
        # Second place can catch up only if it gets all remaining votes
        return (second_count + remaining) > leader_count

    def _select_answer(self, detailed_results: list[dict[str, Any]]) -> dict[str, Any]:
        """Combines (a) raw majority vote, (b) entropy-weighted vote, and (c) a consensus bonus."""
        answer_entropy = defaultdict(list)
        answer_votes = Counter()

        for result in detailed_results:
            answer = result.get('Answer')
            entropy = result.get('Entropy', float('inf'))
            if answer is not None:
                answer_votes[answer] += 1
                answer_entropy[answer].append(entropy)

        if not answer_votes:
            return {"conclusion": "TAS synthesis failed to reach consensus.", "consensus_count": 0}

        total_votes = sum(answer_votes.values())
        max_votes = answer_votes.most_common(1)[0][1]

        scored_answers = []
        for answer, votes in answer_votes.items():
            entropies = answer_entropy[answer]
            # Use median entropy per answer to be robust to outliers
            median_ent = sorted(entropies)[len(entropies) // 2]
            confidence = 1.0 / max(median_ent, 1e-9)

            # Vote share — main signal
            vote_share = votes / total_votes

            # Consensus bonus: reward answers that have a strong majority
            consensus_bonus = 0.0
            if votes == max_votes and max_votes > total_votes / 2:
                consensus_bonus = 0.1 # Small bonus for clear winner

            # Combined score
            score = (vote_share * 0.6) + (confidence * 0.3) + (consensus_bonus * 0.1)
            scored_answers.append((score, answer, votes, median_ent))

        # Sort by score (desc), then by votes (desc), then by entropy (asc)
        scored_answers.sort(key=lambda x: (x[0], x[2], -x[3]), reverse=True)

        best_answer_hash = scored_answers[0][1]
        best_answer_votes = scored_answers[0][2]
        best_answer_entropy = scored_answers[0][3]

        # Find the full result entry for the best answer hash
        final_conclusion = "TAS synthesis failed to reach consensus."
        for result in detailed_results:
            if result.get('Answer') == best_answer_hash:
                final_conclusion = result.get('conclusion', final_conclusion)
                break

        return {
            "conclusion": final_conclusion,
            "consensus_count": best_answer_votes,
            "total_attempts": total_votes,
            "average_entropy": best_answer_entropy
        }

    async def plan(self, user_request: str, run_id: str, max_attempts: int = 1) -> Mapping[str, Any]:
        """
        The Tri-Agent Synthesis (TAS) Protocol.
        Three independent agents (Architect, Sage, Master of Synthesis) converge on a robust plan.
        """
        self._proof_hashes.clear()
        detailed_results: list[dict[str, Any]] = []
        answers: Counter[str] = Counter()
        evidence_hashes: list[str] = []

        # Phase 25: Deterministic Seeded Entropy (for reproducibility)
        seed_base = int(hashlib.sha256(user_request.encode("utf-8")).hexdigest(), 16)

        for attempt_num in range(1, max_attempts + 1):
            # Deterministic seeding for each attempt
            current_seed = seed_base + attempt_num
            os.environ["PYTHONHASHSEED"] = str(current_seed)
            import random
            random.seed(current_seed)

            # 1. Architect (Thesis) - Stage 1
            # ENFORCED: Architect is forbidden from using thinking mode
            architect_prompt = f"User Request: {user_request}"
            thesis_resp = await asyncio.to_thread(self.thesis_provider.complete, architect_prompt, system_prompt=self.ARCHITECT_PROMPT, max_tokens=self._get_active_token_limits()[0], temperature=0.2, preserve_thinking=False)
            thesis_entropy = self._get_entropy(self.thesis_provider, thesis_resp)
            
            # Robust Fallback: Extract from reasoning block if model trapped its answer
            thesis_text = thesis_resp.content.strip()
            if not thesis_text and getattr(thesis_resp, "reasoning_content", None):
                thesis_text = thesis_resp.reasoning_content.strip()
            if not thesis_text:
                thesis_text = "The Architect failed to generate a thesis."
                
            thesis_proof = self._make_proof(thesis_text, "architect", "thesis", thesis_entropy)
            self._store_proof(thesis_proof)

            # 2. Sage of Friction (Antithesis) - Stage 2
            # ENFORCED: Sage is forbidden from using thinking mode
            friction_prompt = f"Architect\'s Thesis:\n{thesis_text}"
            friction_resp = await asyncio.to_thread(self.antithesis_provider.complete, friction_prompt, system_prompt=self.FRICTION_PROMPT, max_tokens=self._get_active_token_limits()[1], temperature=0.7, preserve_thinking=False)
            friction_entropy = self._get_entropy(self.antithesis_provider, friction_resp)
            
            # Robust Fallback: Extract from reasoning block if model trapped its answer
            friction_text = friction_resp.content.strip()
            if not friction_text and getattr(friction_resp, "reasoning_content", None):
                friction_text = friction_resp.reasoning_content.strip()
            if not friction_text:
                friction_text = "INTERROGATION COMPLETE"
                
            friction_proof = self._make_proof(friction_text, "sage", "antithesis", friction_entropy)
            self._store_proof(friction_proof)

            # 3. Master of Synthesis - Stage 3
            synthesis_prompt = f"Architect\'s Thesis:\n{thesis_text}\n\nSage\'s Critique:\n{friction_text}"
            synthesis_result = await self._run_single_synthesis(synthesis_prompt, self.SYNTHESIS_PROMPT, run_id, attempt_num, self._get_active_token_limits()[2])
            self._process_synthesis_result(synthesis_result, detailed_results, answers, evidence_hashes, attempt_num)

            # Early stopping condition (passing current max_attempts parameter)
            if not self._can_remaining_change_winner(list(answers.keys()), attempt_num, max_attempts):
                break

        # Final Consensus Scoring
        final_consensus = self._select_answer(detailed_results)

        # Phase 27: Evidence Chain of Custody (Final Proof Hashing)
        all_proof_hashes = sorted(list(set(self._proof_hashes + evidence_hashes)))
        final_proof_hash = hashlib.sha256("".join(all_proof_hashes).encode("utf-8")).hexdigest()

        # Phase 28: Inquest Report (Deterministic Failure Analysis)
        if final_consensus["consensus_count"] < max_attempts / 2 or final_consensus["average_entropy"] > 3.0:
            inquest_report = {
                "run_id": run_id,
                "user_request": user_request,
                "final_consensus": final_consensus,
                "detailed_results": detailed_results,
                "all_proof_hashes": all_proof_hashes,
                "final_proof_hash": final_proof_hash,
                "status": "INQUEST_REQUIRED"
            }
            # Write to a deterministic, cross-platform temporary location
            temp_dir = Path(tempfile.gettempdir())
            with open(temp_dir / f"inquest_report_{run_id}.json", "w", encoding="utf-8") as f:
                json.dump(inquest_report, f, indent=2)

        return {
            "final_conclusion": final_consensus["conclusion"],
            "final_proof_hash": final_proof_hash,
            "consensus_score": final_consensus["consensus_count"] / final_consensus["total_attempts"] if final_consensus["total_attempts"] > 0 else 0.0,
            "all_proof_hashes": all_proof_hashes,
            "detailed_results": detailed_results
        }