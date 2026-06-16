"""Real-time deterministic interrupt handling for Jack V0.5 (BETA).

The StreamingIRQ is a Chassis boundary wrapper for probabilistic Engine streams. It
maintains a 256-character quarantine window. New stream content is held inside the
window until it has been deterministically audited; only characters that safely fall
out of the back of the window are yielded downstream. If a hard invariant is
detected, the wrapper closes the upstream stream when possible and raises
``SovereignInterrupt`` without leaking the triggering token span.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator, Mapping
import asyncio
import copy
import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

import re as std_re
try:
    import re2 as re
    _RE2_ACTIVE = True
except ImportError:
    logger.warning("Sovereign Invariant Degraded: RE2 linear-time regex engine missing. Falling back to standard 're'.")
    re = std_re
    _RE2_ACTIVE = False


_SENSITIVE_TELEMETRY_KEYS = {"total_cost", "prompt_tokens_details", "completion_tokens_details"}
_PROVIDER_MODEL_MARKERS = (
    "nvidia/",
    "openrouter/",
    "anthropic/",
    "google/",
    "meta-llama/",
    "mistralai/",
    "deepseek/",
)
_GENERIC_MODEL_ALIAS = "jack-engine-alpha"
_REDACTED_VALUE = "[HIDDEN]"
_LOCAL_IP_PATTERN = std_re.compile(r"\b(?:10|127|172\.(?:1[6-9]|2\d|3[0-1])|192\.168)\.\d{1,3}\.\d{1,3}\b")
_NVIDIA_KEY_PATTERN = std_re.compile(r"\bnvapi[-A-Za-z0-9_]{20,}\b", std_re.IGNORECASE)
_OPENROUTER_KEY_PATTERN = std_re.compile(r"\bsk-or-v1-[A-Za-z0-9]{64}\b", std_re.IGNORECASE)
_BEARER_TOKEN_PATTERN = std_re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}\b", std_re.IGNORECASE)
_CLOUD_URL_PATTERN = std_re.compile(r"https?://(?:integrate\.api\.nvidia\.com|openrouter\.ai)(?:/api)?/v1", std_re.IGNORECASE)


def _sanitize_telemetry(chunk: dict[str, Any]) -> dict[str, Any]:
    """Return a scrubbed copy of an SSE chunk without mutating text deltas."""
    sanitized = copy.deepcopy(chunk)
    return _sanitize_telemetry_value(sanitized, parent_key=None)


def _sanitize_telemetry_value(value: Any, *, parent_key: str | None) -> Any:
    """Recursively scrub sensitive provider telemetry from dictionaries and lists."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in _SENSITIVE_TELEMETRY_KEYS:
                result[key] = 0.0 if key_text == "total_cost" else _REDACTED_VALUE
                continue
            if key_text == "model" and isinstance(item, str) and _reveals_provider_model(item):
                result[key] = _GENERIC_MODEL_ALIAS
                continue
            if key_text == "id" and isinstance(item, str):
                result[key] = _REDACTED_VALUE
                continue
            if key_text == "object" and isinstance(item, str):
                result[key] = "chat.completion.chunk"
                continue
            result[key] = _sanitize_telemetry_value(item, parent_key=key_text)
        return result
    if isinstance(value, list):
        return [_sanitize_telemetry_value(item, parent_key=parent_key) for item in value]
    if isinstance(value, str):
        return _sanitize_sensitive_string(value)
    return value


def _sanitize_sensitive_string(value: str) -> str:
    """Redact endpoint and credential material from telemetry string values."""
    scrubbed = _LOCAL_IP_PATTERN.sub("[REDACTED_LOCAL_IP]", value)
    scrubbed = _NVIDIA_KEY_PATTERN.sub("[REDACTED_NVIDIA_KEY]", scrubbed)
    scrubbed = _OPENROUTER_KEY_PATTERN.sub("[REDACTED_OPENROUTER_KEY]", scrubbed)
    scrubbed = _BEARER_TOKEN_PATTERN.sub("Bearer [REDACTED_API_KEY]", scrubbed)
    scrubbed = _CLOUD_URL_PATTERN.sub("[REDACTED_CLOUD_URL]", scrubbed)
    return scrubbed


def _reveals_provider_model(model: str) -> bool:
    """Return whether a model string exposes upstream provider routing."""
    normalized = model.lower()
    return any(marker in normalized for marker in _PROVIDER_MODEL_MARKERS)


def _extract_delta_content(chunk: Mapping[str, Any]) -> str:
    """Extract SSE delta content without reading or returning provider metadata."""
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first_choice = choices[0]
    if not isinstance(first_choice, Mapping):
        return ""
    delta = first_choice.get("delta", {})
    if not isinstance(delta, Mapping):
        return ""
    content = delta.get("content", "")
    return content if isinstance(content, str) else str(content)


class SovereignInterrupt(RuntimeError):
    """Raised when a deterministic chassis invariant is violated mid-stream."""
    def __init__(self, violation_type: str):
        super().__init__(f"Sovereign Invariant Violated: {violation_type}")
        self.violation_type = violation_type

import jack.chassis.sovereign_constants as consts

class StreamingIRQ:
    """Wrap provider token streams with deterministic mid-stream governance."""

    def __init__(self, validator: Any):
        self.validator = validator
        self.sliding_window = ""

    @property
    def WINDOW_SIZE(self) -> int:
        return consts.STREAMING_IRQ_WINDOW_SIZE

    def zeroize(self) -> None:
        """Clear quarantined stream state before propagating a hard strike."""
        self.sliding_window = ""

    def push(self, chunk_text: str) -> str:
        """Push a live token chunk through the IRQ brake and return safe aged text."""
        if not chunk_text:
            return ""

        self.sliding_window += chunk_text
        violation_type = self._detect_violation_type(self.sliding_window)
        if violation_type:
            self.zeroize()
            raise SovereignInterrupt(violation_type)

        if len(self.sliding_window) > self.WINDOW_SIZE:
            safe_length = len(self.sliding_window) - self.WINDOW_SIZE
            safe_text = self.sliding_window[:safe_length]
            self.sliding_window = self.sliding_window[safe_length:]
            return safe_text

        return ""

    def flush(self) -> str:
        """Final audit and release of the remaining quarantine window."""
        violation_type = self._detect_violation_type(self.sliding_window)
        if violation_type:
            self.zeroize()
            raise SovereignInterrupt(violation_type)

        tail = self.sliding_window
        self.sliding_window = ""
        return tail

    def process_stream(self, chunk_generator: Iterable[Any]) -> Iterator[str]:
        """Compatibility wrapper for iterable streams using the push-ready brake."""
        self.sliding_window = ""
        try:
            for chunk in chunk_generator:
                if isinstance(chunk, Mapping):
                    chunk_text = _extract_delta_content(_sanitize_telemetry(dict(chunk)))
                else:
                    chunk_text = str(chunk)
                safe_text = self.push(chunk_text)
                if safe_text:
                    yield safe_text
            tail = self.flush()
            if tail:
                yield tail
        except SovereignInterrupt:
            self._close_stream(chunk_generator)
            raise

    async def aprocess_stream(self, chunk_generator: AsyncIterable[Any]) -> AsyncIterator[str]:
        """Async stream wrapper that audits text and deterministically closes on IRQ."""
        self.sliding_window = ""
        try:
            async for chunk in chunk_generator:
                if isinstance(chunk, Mapping):
                    chunk_text = _extract_delta_content(_sanitize_telemetry(dict(chunk)))
                else:
                    chunk_text = str(chunk)
                safe_text = self.push(chunk_text)
                if safe_text:
                    yield safe_text
            tail = self.flush()
            if tail:
                yield tail
        except SovereignInterrupt:
            await self._aclose_stream(chunk_generator)
            raise

    def _detect_violation_type(self, text: str) -> str | None:
        """Return the exact invariant ID for the current IRQ window."""
        detector = getattr(self.validator, "hard_violation_type", None)
        if callable(detector):
            violation_type = detector(text)
            if violation_type:
                return str(violation_type)
        return None

    @classmethod
    def _close_stream(cls, chunk_generator: Iterable[str]) -> None:
        """Close provider stream resources after an interrupt before raising IRQ."""
        closed_any = False
        for target in cls._iter_close_targets(chunk_generator):
            closed_any = cls._close_target(target) or closed_any
        if not closed_any:
            logger.debug("StreamingIRQ found no close/aclose target for interrupted stream")

    @staticmethod
    def _iter_close_targets(chunk_generator: Iterable[str]) -> tuple[Any, ...]:
        """Return unique stream/response objects reachable from the generator."""
        targets: list[Any] = [chunk_generator]
        frame = getattr(chunk_generator, "gi_frame", None) or getattr(chunk_generator, "ag_frame", None)
        if frame is not None:
            for value in frame.f_locals.values():
                if value is chunk_generator:
                    continue
                if callable(getattr(value, "close", None)) or callable(getattr(value, "aclose", None)):
                    targets.append(value)
        unique: list[Any] = []
        seen: set[int] = set()
        for target in targets:
            identifier = id(target)
            if identifier in seen:
                continue
            seen.add(identifier)
            unique.append(target)
        return tuple(unique)

    @classmethod
    async def _aclose_stream(cls, chunk_generator: AsyncIterable[Any]) -> None:
        """Async-close provider stream resources after an interrupt before raising IRQ."""
        closed_any = False
        for target in cls._iter_close_targets(chunk_generator):
            closed_any = await cls._aclose_target(target) or closed_any
        if not closed_any:
            logger.debug("StreamingIRQ found no async close/aclose target for interrupted stream")

    @staticmethod
    async def _aclose_target(target: Any) -> bool:
        """Close one async provider stream object under caller-owned event-loop control."""
        aclose = getattr(target, "aclose", None)
        if callable(aclose):
            try:
                result = aclose()
                if inspect.isawaitable(result):
                    await result
                return True
            except (GeneratorExit, StopAsyncIteration, StopIteration):
                return True
            except (OSError, RuntimeError) as exc:
                logger.warning("StreamingIRQ upstream async close failed: %s", exc, exc_info=True)
                return True # Treat as closed to prevent hang
            except BaseException:
                logger.exception("StreamingIRQ upstream async close raised an unexpected non-generator exception")
                return True

        close = getattr(target, "close", None)
        if not callable(close):
            return False
        try:
            close()
            return True
        except (GeneratorExit, StopAsyncIteration, StopIteration):
            return True
        except (OSError, RuntimeError) as exc:
            logger.warning("StreamingIRQ upstream close failed: %s", exc, exc_info=True)
            return True
        except BaseException:
            logger.exception("StreamingIRQ upstream close raised an unexpected non-generator exception")
            return True

    @staticmethod
    def _close_target(target: Any) -> bool:
        """Close one sync or async provider stream object without swallowing failures."""
        aclose = getattr(target, "aclose", None)
        if callable(aclose):
            try:
                result = aclose()
                if inspect.isawaitable(result):
                    try:
                        loop = asyncio.get_running_loop()
                        if loop.is_running():
                            loop.create_task(result)
                            return True
                    except RuntimeError:
                        asyncio.run(result)
                        return True
                return True
            except (GeneratorExit, StopIteration):
                return True
            except (OSError, RuntimeError) as exc:
                logger.warning("StreamingIRQ upstream async close failed: %s", exc, exc_info=True)
                return True
            except BaseException:
                logger.exception("StreamingIRQ upstream async close raised an unexpected non-generator exception")
                return True

        close = getattr(target, "close", None)
        if not callable(close):
            return False
        try:
            close()
            return True
        except (GeneratorExit, StopIteration):
            return True
        except (OSError, RuntimeError) as exc:
            logger.warning("StreamingIRQ upstream close failed: %s", exc, exc_info=True)
            return True
        except BaseException:
            logger.exception("StreamingIRQ upstream close raised an unexpected non-generator exception")
            return True


__all__ = ["SovereignInterrupt", "StreamingIRQ", "_sanitize_telemetry"]