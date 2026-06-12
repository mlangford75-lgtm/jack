from __future__ import annotations
import re
import json
import hashlib
from pathlib import Path
from typing import Literal

from jack.chassis.sovereign_constants import ARITHMETIC_PATTERN

class DeterministicRouter:
    # Sovereign Deterministic Router (Phase 32).
    # Routes intents via State-Change Invariance and proof-backed verb allowlists.
    
    def __init__(self, project_root: Path | None = None) -> None:
        self.deep_prefix_pattern = re.compile(r'^\s*deep(?:[\s:,\-]+|$)', re.IGNORECASE)
        self.project_root = project_root or Path.cwd()
        self.allowlist_path = self.project_root / ".jack" / "verb_allowlist.json"
        self.base_verbs = {'read', 'list', 'view', 'open', 'cat', 'show', 'display', 'get'}
        
        # Phase 32: State-Change Invariance Filter
        self.tool_intent_pattern = re.compile(
            r'\b(read|list|view|open|cat|write|save|create|delete|remove|update|run|execute|bash|shell|script|download|fetch|curl|wget|mkdir|touch|install|pip|npm|docker|git|search|browse|find|grep|examine|inspect|check|test)\b', 
            re.IGNORECASE
        )
        self.safe_prefixes = {
            'rewrite', 'summarize', 'correct', 'paraphrase', 'format', 'shorten', 
            'explain', 'define', 'translate', 'what', 'who', 'where', 'when', 
            'why', 'how', 'is', 'are', 'can', 'does', 'do', 'hi', 'hello', 'hey', 'thanks', 'ok'
        }
        self.deep_keywords = {
            'plan', 'design', 'architect', 'research', 'debug', 'investigate', 
            'step by step', 'deep dive'
        }

    def _count_tokens(self, text: str) -> int:
        # Kept for backwards compatibility with legacy tests
        return len(text.split())

    def _get_learned_verbs(self) -> set[str]:
        if not self.allowlist_path.exists():
            return set()
        try:
            data = json.loads(self.allowlist_path.read_text(encoding="utf-8"))
            if 'verbs' in data and 'sha256' in data:
                content = json.dumps(data['verbs'], sort_keys=True).encode()
                if hashlib.sha256(content).hexdigest() == data['sha256']:
                    return set(data['verbs'])
        except Exception:
            pass
        return set()

    def classify(self, prompt: str) -> tuple[Literal["FAST", "PLAN", "DEEP"], str, bool]:
        # 1. Manual DEEP Override Check
        match = self.deep_prefix_pattern.match(prompt)
        if match:
            stripped = prompt[match.end():].lstrip()
            if stripped:
                return "DEEP", stripped, True
            return "PLAN", prompt, False

        prompt_strip = prompt.strip()
        prompt_lower = prompt_strip.lower()
        tokens = prompt_lower.split()

        # 2. DEEP Path: Explicit Keywords
        if any(kw in prompt_lower for kw in self.deep_keywords):
            return "DEEP", prompt, False

        # 3. FAST Path: Explicit Safe Prefixes & Learned Verbs (Bypasses tool check)
        if tokens:
            verb = tokens[0]
            if verb in self.safe_prefixes or verb in self._get_learned_verbs():
                return "FAST", prompt, False

        # 4. Arithmetic Proof Gate Trigger
        if ARITHMETIC_PATTERN.search(prompt_lower):
            return "PLAN", prompt, False

        # 5. State-Change & Tool-Intent Filter
        if self.tool_intent_pattern.search(prompt_lower):
            return "PLAN", prompt, False

        # 5. Default FAST Path: State-Change Invariant
        return "FAST", prompt, False