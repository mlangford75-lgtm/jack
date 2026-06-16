from __future__ import annotations

import os
import json
import re
import hmac
import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from jack.chassis.contract_validator import ContractValidator
from jack.chassis.models import StepExecutionResult

class StrikeReason(Enum):
    COMMAND_FAILED = "command_failed"
    SECURITY_VIOLATION = "security_violation"
    EPISTEMIC_INSTABILITY = "epistemic_instability"
    UNKNOWN_FAILURE = "unknown_failure"

@dataclass(frozen=True, slots=True)
class TASJudgeResult:
    is_strike: bool
    reason: StrikeReason | None = None
    message: str = ""

class TASJudge:
    """Audits execution results and enforces the Friction Protocol (Tri-Agent Synthesis)."""

    # Tools that trigger the expensive, adversarial TAS loop
    HIGH_RISK_TOOLS = {
        "shell", "bash", "command", 
        "python_repl", "sandbox", 
        "filesystem", "file", "files", "local_file", "local_filesystem"
    }

    def __init__(self, project_root: Path, contract_validator: ContractValidator) -> None:
        self.project_root = project_root
        self.contract_validator = contract_validator
        self.verb_allowlist_path = self.project_root / ".jack" / "verb_allowlist.json"
        self.read_verbs = self._load_verb_allowlist()

    def _load_verb_allowlist(self) -> set:
        """Load deterministic allowlist of safe read verbs from Librarian proof with HMAC verification."""
        base_verbs = {'read','list','view','open','cat','show','display','get','read_file'}
        if self.verb_allowlist_path.exists():
            try:
                data = json.loads(self.verb_allowlist_path.read_text(encoding="utf-8"))
                if 'hmac_signature' in data and 'verbs' in data:
                    # Secure key derivation anchored to the master vault passphrase
                    passphrase = os.environ.get("JACK_VAULT_PASSPHRASE", "default_chassis_anchor_key")
                    key = hashlib.sha256(passphrase.encode("utf-8")).digest()
                    
                    content = json.dumps(data['verbs'], sort_keys=True).encode("utf-8")
                    computed_mac = hmac.new(key, content, hashlib.sha256).hexdigest()
                    
                    # Prevent timing-attack verification shortcuts via hmac.compare_digest
                    if hmac.compare_digest(computed_mac, data['hmac_signature']):
                        base_verbs.update(set(data['verbs']))
            except Exception:
                pass
        return base_verbs

    def _save_verb_allowlist(self, verb: str, proof_hash: str):
        """Append verb to allowlist with HMAC signature. Deterministic only."""
        verbs = sorted(list(self.read_verbs | {verb}))
        content = json.dumps(verbs, sort_keys=True).encode("utf-8")
        
        # Sign the allowlist securely using the master vault passphrase
        passphrase = os.environ.get("JACK_VAULT_PASSPHRASE", "default_chassis_anchor_key")
        key = hashlib.sha256(passphrase.encode("utf-8")).digest()
        mac_sig = hmac.new(key, content, hashlib.sha256).hexdigest()
        
        data = {
            'verbs': verbs,
            'hmac_signature': mac_sig,
            'last_proof': proof_hash,
            'updated_by': 'deterministic_router'
        }
        self.verb_allowlist_path.parent.mkdir(parents=True, exist_ok=True)
        self.verb_allowlist_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.read_verbs = set(verbs)

    async def judge(self, command: str, step_execution_result: StepExecutionResult) -> TASJudgeResult:
        """Judges the outcome of a command execution deterministically."""
        return self.evaluate_payload(command, step_execution_result)

    def evaluate_payload(self, command: str, step_execution_result: StepExecutionResult) -> TASJudgeResult:
        """Deterministic Gate: Audits the results of a command execution for security rules."""
        # 1. Deterministic Security Blacklist
        command_validation = self.contract_validator.validate_command(command)
        if not command_validation.is_valid:
            return TASJudgeResult(
                is_strike=True,
                reason=StrikeReason.SECURITY_VIOLATION,
                message="Security violation detected: " + ", ".join(command_validation.errors)
            )

        # 2. Execution Failure Check
        if step_execution_result.records.get("returncode") != 0:
            stderr = step_execution_result.records.get("stderr", "").strip()
            return TASJudgeResult(
                is_strike=True,
                reason=StrikeReason.COMMAND_FAILED,
                message=f"Command failed with exit code {step_execution_result.records.get('returncode')}. Stderr: {stderr}"
            )

        return TASJudgeResult(is_strike=False, message="Command executed successfully and passed security audit.")

    def _requires_friction_protocol(self, execution_plan: dict[str, Any]) -> bool:
        """Determines if a plan requires expensive Tri-Agent Synthesis."""
        steps = execution_plan.get("steps", [])

        # Phase II: Cooperative Autonomy & Mandated Red Lines
        # We trust the local Omni 30B to execute isolated tools (python_repl, filesystem)
        # guarded silently by the static ContractValidator.
        # We ONLY escalate to TAS for true structural hazards (shell, browser).
        
        for step in steps:
            tool = str(step.get("tool", "")).lower()
            
            # Red Line 1: Host-Level Mutation (shell)
            if tool in ["shell", "bash", "command"]:
                # Fast-path safe read-only shell commands
                inputs = step.get("inputs", {})
                command = str(inputs.get("command", "")).strip()
                if command:
                    verb = command.split()[0].lower()
                    if verb in self.read_verbs and not any(op in command for op in [";", "&&", "||", "|", "`", "$(", ">", "<"]):
                        continue # Safe read, no friction needed
                return True # Unallowlisted or complex shell command -> TAS
                
            # Red Line 2: Cross-Boundary Network Interaction (browser)
            if tool in ["browser", "web_navigator", "web"]:
                return True # Network access -> TAS
                
        # If the plan only uses python_repl, filesystem, image_gen, audio_gen, etc.,
        # we bypass TAS and rely on the ContractValidator's static checks.
        return False

    def _get_active_token_limits(self) -> tuple[int, int, int]:
        """Dynamically scale output token limits based on user-defined hot context."""
        from jack.chassis.config import load_config
        try:
            config = load_config(self.project_root / "config.yaml")
            C = config.hot_context.max_tokens
        except Exception:
            C = 128000  # Fallback to high-capacity baseline
            
        # Proportional Scaling Formulas (T-25%, A-10%, S-50%)
        T_active = min(12000, max(512, int(C * 0.25)))
        A_active = min(1000, max(256, int(C * 0.10)))
        S_active = min(64000, max(1024, int(C * 0.50)))
        return T_active, A_active, S_active

    def evaluate_plan(self, execution_plan: dict[str, Any], engine: Any, sage_engine: Any = None, force: bool = False) -> TASJudgeResult:
        """
        Probabilistic Gate: Executes the 3-Stage Friction Protocol over a provided plan,
        but ONLY if the plan trips the deterministic Escalation Gate or if friction is forced.
        """
        # 1. Deterministic Pre-flight (Chassis Law always precedes Probabilistic Friction)
        contract_val = self.contract_validator.validate_contract(execution_plan)
        if not contract_val.is_valid:
            return TASJudgeResult(
                is_strike=True, 
                reason=StrikeReason.SECURITY_VIOLATION, 
                message="Deterministic pre-flight failed: " + ", ".join(contract_val.errors)
            )

        # 2. Escalation Gate (Save tokens if low risk)
        if not force and not self._requires_friction_protocol(execution_plan):
            return TASJudgeResult(is_strike=False, message="Fast-path approved: No high-risk tools detected.")
            
        # Calculate active, context-scaled token ceilings
        T_active, A_active, S_active = self._get_active_token_limits()

        # 3. Stage 2: Antithesis (Sage of Friction)
        plan_str = json.dumps(execution_plan, indent=2)
        sage_system_prompt = (
            "You are a wise sage. Your purpose is breaking the agreeability trap and countering confirmation bias.\n\n"
            "Propose questions that expose gaps, assumptions, or fragility. This is NOT physical friction and NOT a list of facts.\n\n"
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
            "6. You MUST include the exact phrase INTERROGATION COMPLETE at the end of your response. If you have no questions, output ONLY 'INTERROGATION COMPLETE'."
        )
        sage_prompt = f"Critique this Architect's Thesis (Execution Plan):\n{plan_str}"
        
        active_sage_engine = sage_engine if sage_engine is not None else engine
        
        friction_critique = ""
        violations = 0
        max_violations = 3
        
        # FIX: Simple retry loop ONLY for empty responses (stochastic blanking).
        while violations < max_violations:
            sage_response = active_sage_engine.complete(
                sage_prompt, 
                system_prompt=sage_system_prompt,
                max_tokens=A_active
            )
            critique = sage_response.content
            
            if not critique or not critique.strip():
                print(f"[TASJudge Warning] Attempt {violations + 1}: Sage critique was empty. Retrying...")
                violations += 1
                continue
                
            friction_critique = critique
            break
            
        if violations >= max_violations:
            return TASJudgeResult(
                is_strike=True,
                reason=StrikeReason.EPISTEMIC_INSTABILITY,
                message="Sage critique was empty after 3 attempts."
            )
            
        # 4. Stage 3: Synthesis (Master of Synthesis)
        master_system_prompt = (
            "You are the Master of Synthesis. Review the Architect's Thesis and the Antithesis.\n"
            "You are a ruthless tie-breaker, not a mediator. You are strictly forbidden from compromising.\n"
            "You MUST deliberate inside <deliberation>...</deliberation> tags. Inside this block, you must:\n"
            "1. Summarize the Sage's questions in their strongest possible form (Steelperson).\n"
            "2. If you choose to proceed with the plan, you MUST explicitly dismantle the Sage's WCS with empirical logic.\n"
            "3. Write a comprehensive, detailed, and fluent human-readable writeup explaining the physical, structural, or mathematical dynamics of the plan, specifically describing and explaining the empirical parameters that you verified.\n"
            "Outside the deliberation block, you MUST provide the complete, high-fidelity narrative writeup followed by exactly VERDICT: GO or VERDICT: NO-GO."
        )
        master_prompt = f"Thesis (Plan):\n{plan_str}\n\nAntithesis (Friction):\nCritique:\n{friction_critique}\n\nProvide Synthesis:"
        
        # Pass the context-scaled Synthesis ceiling (S_active)
        synthesis_response = engine.complete(
            master_prompt, 
            system_prompt=master_system_prompt,
            max_tokens=S_active
        )
        synthesis_verdict = synthesis_response.content
        
        # Tolerant Extraction & Closing the Fail-Open Vulnerability
        all_verdicts = re.findall(r"(?i)\bVERDICT:\s*(GO|NO-GO)\b", synthesis_verdict)
        
        if not all_verdicts:
            return TASJudgeResult(
                is_strike=True, 
                reason=StrikeReason.EPISTEMIC_INSTABILITY, 
                message=f"Synthesis failed to provide a strict VERDICT: GO or VERDICT: NO-GO. Output: {synthesis_verdict}"
            )
            
        normalized_verdicts = [v.upper() for v in all_verdicts]
        
        # Fail-closed: If NO-GO exists ANYWHERE in the output, it is a strike.
        if "NO-GO" in normalized_verdicts:
            return TASJudgeResult(
                is_strike=True, 
                reason=StrikeReason.EPISTEMIC_INSTABILITY, 
                message=f"Master of Synthesis rejected plan due to epistemic instability: {synthesis_verdict}"
            )
        elif "GO" in normalized_verdicts:
            return TASJudgeResult(is_strike=False, message=f"Friction Protocol survived. Synthesis: {synthesis_verdict}")
        else:
            return TASJudgeResult(
                is_strike=True,
                reason=StrikeReason.EPISTEMIC_INSTABILITY,
                message="Ambiguous verdict parsed."
            )