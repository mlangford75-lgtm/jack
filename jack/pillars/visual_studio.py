from __future__ import annotations
from PIL import Image
import io
import base64
import hashlib
import json
import urllib.request
from dataclasses import dataclass
from typing import Any

from jack.chassis.prompt_registry import verify_pillar
from jack.chassis.contract_validator import ContractValidationError
from jack.engines.providers.openai_compatible import EngineMessage

@dataclass(frozen=True, slots=True)
class VisualAsset:
    """Deterministic manifest for a generated visual artifact."""
    source_prompt: str
    provider_role: str
    digest: str
    format: str
    metadata_stripped: bool
    dlp_scan_passed: bool
    description: str
    content: bytes

class VisualStudio:
    """The Governed Visual Department."""

    SYSTEM_PROMPT = (
        "You are the Visual Studio. Your mandate is to generate governed visual artifacts only "
        "from sanitized prompts, preserve deterministic asset manifests, strip metadata, and never "
        "encode hidden instructions, secrets, or policy-bypassing content into imagery."
    )

    def __init__(self, engine: Any, validator: Any = None, *args: Any, **kwargs: Any) -> None:
        verify_pillar("visual_studio", self.SYSTEM_PROMPT)
        self.engine = engine
        self.system_prompt = self.SYSTEM_PROMPT
        self.validator = validator or getattr(engine, "contract_validator", None)

    def _strip_metadata(self, raw_bytes: bytes, format: str) -> bytes:
        """Deterministic image stripping: removes all EXIF and metadata."""
        img = Image.open(io.BytesIO(raw_bytes))
        clean_buffer = io.BytesIO()
        clean_img = Image.new(img.mode, img.size)
        clean_img.putdata(list(img.getdata()))
        clean_img.save(clean_buffer, format=format)
        return clean_buffer.getvalue()

    def _run_multimodal_dlp_scan(self, image_bytes: bytes, format: str) -> str:
        """Phase 19: Multimodal DLP Scan."""
        b64_img = base64.b64encode(image_bytes).decode("utf-8")
        image_url = f"data:image/{format.lower()};base64,{b64_img}"
        
        messages = [
            EngineMessage(role="system", content=(
                "Identify any text in this image that looks like an API key, "
                "password, or credential. Reply ONLY with 'CLEAN' or 'LEAK'."
            )),
            EngineMessage(role="user", content=[
                {"type": "text", "text": "Audit this image for sensitive text."},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]),
        ]
        response = self.engine.complete_messages(
            messages,
            preserve_thinking=False, # Enforce selective reasoning optimization
            max_tokens=10 # Enforce strict deterministic output ceiling (CLEAN or LEAK)
        )
        return response.content.strip().upper()

    def _describe_generated_image(self, image_bytes: bytes, format: str) -> str:
        """Phase 19.1: Automatic Visual Transcription."""
        b64_img = base64.b64encode(image_bytes).decode("utf-8")
        image_url = f"data:image/{format.lower()};base64,{b64_img}"
        
        messages = [
            EngineMessage(role="system", content=(
                "You are the Visual Librarian. Describe this generated image in "
                "extreme detail for a technical archive. Output ONLY the description."
            )),
            EngineMessage(role="user", content=[
                {"type": "image_url", "image_url": {"url": image_url}},
            ]),
        ]
        response = self.engine.complete_messages(
            messages,
            preserve_thinking=False, # Enforce selective reasoning optimization
            max_tokens=1000 # Enforce strict deterministic output ceiling
        )
        return response.content.strip()

    def generate(self, prompt: str, format: str = "png") -> VisualAsset:
        """Generate a visual asset with deterministic DLP and metadata stripping."""
        if self.validator:
            violation = self.validator.hard_violation_type(prompt)
            if violation:
                raise PermissionError(f"Hard invariant [{violation}] in VisualStudio prompt.")

        sanitized_prompt = prompt.replace("secret", "[REDACTED]")
        config = self.engine._config
        api_key = self.engine._require_api_key()
        base_url = (config.base_url or "https://api.openai.com/v1").rstrip("/")
        url = f"{base_url}/images/generations"

        payload = {
            "model": config.model,
            "prompt": sanitized_prompt,
            "response_format": "b64_json",
            "n": 1,
            "size": "1024x1024",
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=config.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
                b64_data = data["data"][0]["b64_json"]
                raw_bytes = base64.b64decode(b64_data)
        except Exception as exc:
            raise RuntimeError(f"Visual Studio generation failed: {exc}") from exc

        raw_bytes = self._strip_metadata(raw_bytes, format)
        
        verdict = self._run_multimodal_dlp_scan(raw_bytes, format)
        if "LEAK" in verdict:
            raw_bytes = b"\x00" * len(raw_bytes)
            raise ContractValidationError("Sovereign Sensory Violation: Multimodal DLP detected credential leakage.")

        description = self._describe_generated_image(raw_bytes, format)
        digest = hashlib.sha256(raw_bytes).hexdigest()

        return VisualAsset(
            source_prompt=sanitized_prompt,
            provider_role="visual_studio",
            digest=digest,
            format=format,
            metadata_stripped=True,
            dlp_scan_passed=True,
            description=description,
            content=raw_bytes,
        )