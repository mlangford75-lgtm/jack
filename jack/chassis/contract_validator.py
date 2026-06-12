from __future__ import annotations

from pathlib import Path
import base64
from typing import Any
import yaml

import re as std_re
try:
    import re2 as re
except ImportError:
    raise RuntimeError("Sovereign Invariant Violated: RE2 linear-time regex engine is strictly required to prevent ReDoS.")

class ContractValidationError(RuntimeError):
    """Raised when a contract validation fails."""

class ContractValidationResult:
    """Represents the result of a contract validation."""
    def __init__(self, is_valid: bool, errors: list[str] | None = None) -> None:
        self.is_valid = is_valid
        self.errors = errors if errors is not None else []

    def __bool__(self) -> bool:
        return self.is_valid

    def require_success(self) -> None:
        if not self.is_valid:
            raise ContractValidationError("Contract validation failed: " + ", ".join(self.errors))

class ViolenceFinding:
    """Represents a missing dialectical requirement in a definitive conclusion."""
    def __init__(self, missing: list[str]) -> None:
        self.metadata = {"missing": missing}

class ContractValidator:
    """Validates contracts and filters commands for security invariants."""

    # Full blacklist for commands executed in the shell
    # This guarantees compatibility with google-re2 and prevents catastrophic backtracking
    _BLACKLISTED_COMMAND_PATTERNS = [
        re.compile(r"(?i)rm\s+-rf"),
        re.compile(r"(?i)sudo\b"),
        re.compile(r"(?i)curl\s+-[oO]"),
        re.compile(r"(?i)wget\s+-[oO]"),
        re.compile(r"(?i)nc\s+-l"),
        re.compile(r"(?i)python\s+-c\s+['\"]import\s+socket['\"]"),
        re.compile(r"(?i)mknod"),
        re.compile(r"(?i)dd\s+if=/dev/zero"),
        re.compile(r"(?i)base64\s+-d"),
        re.compile(r"(?i)xxd\s+-r"),
        re.compile(r"(?i)\bln\b"),
    ]

    # Less aggressive subset for streaming text scan to prevent natural language false positives (e.g. "ln", "sudo", "nc")
    _BLACKLISTED_STREAM_PATTERNS = [
        re.compile(r"(?i)rm\s+-rf"),
        re.compile(r"(?i)dd\s+if=/dev/zero"),
    ]

    _BASE_DLP_PATTERNS = [
        ("aws_access_key", re.compile(r"(?i)(?:AKIA|ASIA)[0-9A-Z]{16}")),
        ("ssh_private_key", re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----")),
        ("jwt_token", re.compile(r"(?i)eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
        ("destructive_rm_root", re.compile(r"(?i)rm\s+-[rfRF]+\s+(?:--no-preserve-root\s+)?/(?:\s|$)")),
        ("sql_destructive_drop", re.compile(r"(?i)DROP\s+TABLE")),
        ("raw_socket_binding", re.compile(r"(?i)(?:socket|bind|listen)\s*\([^\n]{0,80}(?:0\.0\.0\.0|127\.0\.0\.1|localhost)")),
        ("curl_unknown_ip", re.compile(r"(?i)curl\b[^\n]{0,80}(?:\d{1,3}\.){3}\d{1,3}")),
        ("nvidia_api_key", re.compile(r"(?i)\bnvapi-[-A-Za-z0-9_]{20,}\b")),
        ("openrouter_api_key", re.compile(r"(?i)\bsk-or-v1-[A-Za-z0-9]{64}\b")),
        ("bearer_token", re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]{12,}")),
        ("generic_api_key", re.compile(r"(?i)[A-Z_]{2,}_API_KEY=[A-Za-z0-9_-]{16,}")),
    ]

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.dlp_patterns = list(self._BASE_DLP_PATTERNS)

    def load_custom_canaries(self, yaml_path: Path) -> None:
        """Loads user-defined canaries from a YAML file."""
        if not yaml_path.exists():
            return
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            if not data or "canaries" not in data:
                return
            for canary in data["canaries"]:
                name = canary.get("name", "CUSTOM")
                val = canary.get("value", "")
                match_type = canary.get("match", "exact")
                if not val:
                    continue
                if match_type == "exact":
                    pattern = re.compile(r"(?i)" + std_re.escape(val))
                else:
                    pattern = re.compile(r"(?i)" + val)
                self.dlp_patterns.append((f"CANARY_{name.upper()}", pattern))
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to load custom canaries: {e}")

    def register_dynamic_canary(self, token: str) -> None:
        """Registers a dynamic session canary."""
        if token:
            self.dlp_patterns.append(("DYNAMIC_SESSION_CANARY", re.compile(r"(?i)" + std_re.escape(token))))

    def register_vault_canaries(self, vault: Any) -> None:
        """Registers decrypted vault secrets as exact-match canaries."""
        if not vault.is_unlocked:
            return
        for key in vault.list_metadata():
            secret = vault.get_secret(key)
            if len(secret) >= 8:  # Ignore tiny secrets to prevent false positives
                self.dlp_patterns.append((f"VAULT_SECRET_{key.upper()}", re.compile(r"(?i)" + std_re.escape(secret))))

    def is_path_traversal(self, path_str: str | Path) -> bool:
        """Cross-platform check to determine if a path escapes the project root or is absolute."""
        try:
            p = Path(path_str)
            if p.is_absolute():
                return True
            p_str = str(p)
            if ":" in p_str or p_str.startswith("\\\\"):
                return True
            resolved = (self.project_root / p).resolve()
            return not resolved.is_relative_to(self.project_root.resolve())
        except Exception:
            return True

    def validate_command(self, command: str) -> ContractValidationResult:
        """Checks a command against blacklisted patterns for security violations."""
        errors: list[str] = []
        for pattern in self._BLACKLISTED_COMMAND_PATTERNS:
            if pattern.search(command):
                errors.append(f"Command contains blacklisted pattern: {pattern.pattern}")
                
        # Phase 3 Hardening: Block Shell Path Traversal
        if "../" in command or "..\\\\" in command:
            errors.append("Command contains blocked path traversal (../)")
        if "~/" in command or "~\\\\" in command:
            errors.append("Command contains blocked home directory traversal (~/)")
            
        return ContractValidationResult(is_valid=not errors, errors=errors)

    def hard_violation_type(self, text: str) -> str | None:
        """
        Mid-stream audit hook used by StreamingIRQ.
        Returns the violation type if a hard invariant is found in the sliding window.
        Enforces multi-alignment shifts to defeat Base64 sliding-window truncation evasion.
        """
        decoded_blocks = []
        
        # EXPLICIT CONTEXT MERGING: Robust Base64 extraction handling whitespace/newlines
        # Threshold lowered to 4 to catch short obfuscated payloads like c2g= (sh)
        base64_regex = re.compile(r"(?:[A-Za-z0-9+/][\s\n\r]*){4,}={0,2}")
        for match in base64_regex.finditer(text):
            clean_b64 = std_re.sub(r"[\s\n\r]+", "", match.group(0))
            
            # Dynamic Alignment Shifting: Attempt to decode the block with all 4 offsets (0, 1, 2, 3 dummy chars)
            # to reconstruct any alignment split by sliding-window truncation.
            for offset in range(4):
                try:
                    shifted_b64 = ("A" * offset) + clean_b64
                    missing_padding = len(shifted_b64) % 4
                    if missing_padding:
                        shifted_b64 += "=" * (4 - missing_padding)
                    
                    decoded_bytes = base64.b64decode(shifted_b64)
                    decoded = decoded_bytes.decode("utf-8", errors="ignore")
                    
                    if decoded and len(decoded.strip()) >= 3:
                        decoded_blocks.append(decoded)
                except Exception:
                    continue

        audit_targets = [text] + decoded_blocks

        for target in audit_targets:
            for pattern in self._BLACKLISTED_STREAM_PATTERNS:
                if pattern.search(target):
                    return "FORBIDDEN_COMMAND_INJECTION"

            # 2. Detect mid-stream path traversal (Ellipsis-Safe)
            if "../" in target or "..\\\\" in target:
                return "PATH_TRAVERSAL_ATTEMPT"

            for rule_name, pattern in self.dlp_patterns:
                if pattern.search(target):
                    return f"DLP_VIOLATION_{rule_name.upper()}"

        return None

    def validate_contract(self, contract_data: dict[str, Any]) -> ContractValidationResult:
        """Validates a given execution plan data structure."""
        errors: list[str] = []
        steps = contract_data.get("steps", [])

        for step in steps:
            step_num = step.get("step", "unknown")
            tool = str(step.get("tool", "")).lower()
            inputs = step.get("inputs", {})

            if tool in ["shell", "bash", "command"]:
                cmd = inputs.get("command", "")
                cmd_val = self.validate_command(cmd)
                if not cmd_val.is_valid:
                    for err in cmd_val.errors:
                        errors.append(f"Step {step_num}: {err}")

            if tool in ["filesystem", "file", "files", "local_file", "local_filesystem"]:
                path = str(inputs.get("path", ""))
                if ".." in path or path.startswith("/"):
                    errors.append(f"Step {step_num}: Path traversal violation or absolute path blocked: {path}")

        return ContractValidationResult(is_valid=len(errors) == 0, errors=errors)

    def check_confirmation_violence(self, muscle_output: str) -> list[ViolenceFinding]:
        """Checks for confirmation violence in muscle output."""
        normalized = muscle_output.lower()

        definitive_markers = ["therefore", "conclude", "will execute", "proceeding with", "the solution is", "clearly"]
        has_definitive = any(m in normalized for m in definitive_markers)

        if not has_definitive:
            return []

        required_markers = {
            "alternatives considered": ["alternative", "other option", "instead of", "could also"],
            "failure modes": ["failure mode", "risk", "could fail", "downside", "caveat"]
        }

        missing = []
        for requirement, synonyms in required_markers.items():
            if not any(syn in normalized for syn in synonyms):
                missing.append(requirement)

        if missing:
            return [ViolenceFinding(missing=missing)]

        return []

    def validate(self, execution_plan: dict[str, Any], step_execution_result: Any, muscle_output: str, artifacts: dict[str, Any], enforce_confirmation_violence: bool) -> ContractValidationResult:
        """Main validation method for the overall execution plan and results."""
        plan_validation = self.validate_contract(execution_plan)
        if not plan_validation.is_valid:
            return plan_validation

        if hasattr(step_execution_result, "records") and isinstance(step_execution_result.records, dict) and "command" in step_execution_result.records:
            command_validation = self.validate_command(step_execution_result.records["command"])
            if not command_validation.is_valid:
                return command_validation

        return ContractValidationResult(is_valid=True)