from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping


_LOCK_PATH = Path(__file__).with_name("prompts.lock.json")


def _load_hashes() -> Mapping[str, str]:
    """Load the committed build-time prompt lockfile."""
    try:
        raw_hashes = json.loads(_LOCK_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError("Prompt lockfile missing") from exc

    if not isinstance(raw_hashes, dict):
        raise RuntimeError("Prompt lockfile must contain an object")
    return {str(name): str(digest) for name, digest in raw_hashes.items()}


_HASHES: Mapping[str, str] | None = None


def _hashes() -> Mapping[str, str]:
    """Return cached prompt hashes, loading them only when verification is requested."""
    global _HASHES
    if _HASHES is None:
        _HASHES = _load_hashes()
    return _HASHES


def prompt_digest(prompt: str) -> str:
    """Return Jack's canonical SHA-256 digest for a pillar mandate."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def verify_pillar(name: str, prompt: str) -> None:
    """Raise if a pillar's runtime mandate differs from the committed lockfile."""
    if not prompt:
        raise RuntimeError(f"Empty prompt provided for {name} verification")
    
    digest = prompt_digest(prompt)
    expected = _hashes().get(name)
    
    if expected is None:
        raise RuntimeError(f"No locked hash found for pillar: {name}")
        
    if digest != expected:
        raise RuntimeError(f"Prompt tamper detected for {name}")