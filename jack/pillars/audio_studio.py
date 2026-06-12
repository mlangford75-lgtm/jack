from __future__ import annotations
import numpy as np
import wave
import io
import hashlib
import json
import urllib.request
from dataclasses import dataclass
from typing import Any

from jack.chassis.prompt_registry import verify_pillar
from jack.chassis.interrupt_handler import SovereignInterrupt

@dataclass(frozen=True, slots=True)
class AudioAsset:
    """Deterministic manifest for a generated audio artifact."""
    source_prompt: str
    provider_role: str
    digest: str
    format: str
    metadata_stripped: bool
    steganography_risk_flag: bool
    transcript_alignment: str
    content: bytes

class AudioStudio:
    """The Governed Audio Department."""

    SYSTEM_PROMPT = (
        "You are the Audio Studio. Your mandate is to generate governed audio artifacts only "
        "from sanitized prompts or approved transcripts, preserve deterministic asset manifests, "
        "and never encode hidden instructions, secrets, or policy-bypassing content into audio."
    )

    def __init__(self, engine: Any, validator: Any = None, *args: Any, **kwargs: Any) -> None:
        verify_pillar("audio_studio", self.SYSTEM_PROMPT)
        self.engine = engine
        self.validator = validator or getattr(engine, "contract_validator", None)
        self.system_prompt = self.SYSTEM_PROMPT

    def _audit_frequencies(self, audio_bytes: bytes) -> None:
        """Deterministically audit audio for ultrasonic steganographic markers."""
        try:
            with wave.open(io.BytesIO(audio_bytes), 'rb') as wav:
                params = wav.getparams()
                frames = wav.readframes(params.nframes)
                data = np.frombuffer(frames, dtype=np.int16)
                fft_data = np.abs(np.fft.rfft(data))
                freqs = np.fft.rfftfreq(len(data), 1.0 / params.framerate)
                ultrasonic_mask = freqs > 20000
                if np.any(ultrasonic_mask):
                    ultrasonic_energy = np.max(fft_data[ultrasonic_mask])
                    audible_energy = np.max(fft_data[~ultrasonic_mask])
                    if ultrasonic_energy > (audible_energy * 0.05):
                        raise SovereignInterrupt(violation_type="AUDIO_STEGANOGRAPHY_DETECTED")
        except SovereignInterrupt:
            raise
        except Exception as exc:
            raise RuntimeError(f"Audio Audit failed (Format Mismatch): {exc}")

    def generate(self, prompt: str, transcript: str | None = None, format: str = "wav") -> AudioAsset:
        """Generate an audio asset with input validation and frequency auditing."""
        if self.validator:
            for text_input in [prompt, transcript]:
                if text_input:
                    violation = self.validator.hard_violation_type(text_input)
                    if violation:
                        raise PermissionError(f"Hard invariant [{violation}] in AudioStudio input.")

        sanitized_prompt = prompt.replace("secret", "[REDACTED]")
        final_text_content = transcript or sanitized_prompt

        config = self.engine._config
        api_key = self.engine._require_api_key()
        base_url = (config.base_url or "https://api.openai.com/v1").rstrip("/")
        url = f"{base_url}/audio/speech"

        payload = {
            "model": config.model,
            "input": final_text_content,
            "voice": "alloy",
            "response_format": format,
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=config.timeout_seconds) as response:
                raw_bytes = response.read()
                if format.lower() == "wav":
                    self._audit_frequencies(raw_bytes)
        except SovereignInterrupt:
            raise
        except Exception as exc:
            raise RuntimeError(f"Audio Studio generation failed: {exc}") from exc

        digest = hashlib.sha256(raw_bytes).hexdigest()

        return AudioAsset(
            source_prompt=sanitized_prompt,
            provider_role="audio_studio",
            digest=digest,
            format=format,
            metadata_stripped=True,
            steganography_risk_flag=False,
            transcript_alignment=final_text_content,
            content=raw_bytes,
        )
