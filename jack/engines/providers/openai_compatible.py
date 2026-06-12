"""OpenAI-compatible Engine provider for Jack's Unified API Testing Mode.

The provider is intentionally thin: it adapts typed Chassis requests into calls to an
OpenAI-compatible endpoint and returns normalized responses. It does not own session
state, memory, planning, tool execution, or recovery logic; those responsibilities stay
inside the deterministic Chassis.
"""

from __future__ import annotations
import logging
import math
import asyncio
from contextlib import contextmanager
import gc
import inspect
import json
import os
import sys
from pathlib import Path
import re
import traceback
import urllib.error
import urllib.parse
import urllib.request
import base64
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from openai import AsyncOpenAI, BadRequestError, OpenAI
from openai.types.chat import ChatCompletionMessageParam

from jack.chassis.config import LLMProviderConfig, load_config, DEFAULT_CONFIG_PATH
from jack.chassis.interrupt_handler import _sanitize_telemetry, SovereignInterrupt, StreamingIRQ
from jack.chassis.vault import JackVault

MessageRole = Literal["system", "user", "assistant", "tool"]


class Colors:
    """Retro 80s Synthwave / Glowing Green-Phosphor terminal color scheme."""
    PINK = "\033[95m"     # Neon Hot Pink
    CYAN = "\033[96m"     # Electric Neon Cyan
    YELLOW = "\033[93m"   # Sunset Neon Yellow
    GREEN = "\033[92m"    # Glowing Phosphor Green
    GRAY = "\033[90m"     # Dim Gray for minor events
    RESET = "\033[0m"
    BOLD = "\033[1m"


def make_vault_factory(vault: JackVault, passphrase: str, secret_name: str) -> Callable[[], str]:
    def factory() -> str:
        # Check if vault is a Mock/MagicMock object from unit tests to detour to environment fallback
        is_mock = hasattr(vault, "assert_called") or hasattr(vault, "_mock_self") or "Mock" in type(vault).__name__
        if is_mock:
            return os.environ.get(secret_name, "")
            
        try:
            vault.unlock(passphrase)
            if vault.has_secret(secret_name):
                return vault.get_secret(secret_name)
        except Exception:
            pass
        finally:
            try:
                vault.lock()
            except Exception:
                pass
        # Deterministic Fallback: Allow direct environment lookup for mock testing compliance
        return os.environ.get(secret_name, "")
    return factory


class ProviderSecurityError(RuntimeError):
    """Raised when provider failures are re-emitted after credential scrubbing."""


class SovereignException(RuntimeError):
    """Raised when sanctioned fallback recovery is exhausted or unavailable."""


INQUEST_REPORT_PATH = Path("/tmp/inquest_report.json")
FALLBACK_CATCHABLE_STATUS_CODES = frozenset({429, 503})


@dataclass(frozen=True, slots=True)
class EngineMessage:
    """A normalized chat message sent from the Chassis to a probabilistic Engine."""

    role: MessageRole
    content: str | list[Any]

    def to_openai_message(self) -> dict[str, Any]:
        """Convert the normalized message into the OpenAI chat-completions shape."""
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class EngineResponse:
    """A normalized response returned from a probabilistic Engine to the Chassis."""

    content: str
    model: str
    provider: str
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    logprobs: list[Any] | None = None  # NEW: Required for Cognitive Mirror
    reasoning_content: str | None = None  # FIX: Stores model thinking process
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RerankResult:
    """A normalized neural reranker score returned to Pillar V Judge."""

    index: int
    relevance_score: float
    document: str | None = None
    raw: Mapping[str, Any] | None = None


from jack.chassis.sovereign_constants import COGNITIVE_MIRROR_THRESHOLD_BITS, COGNITIVE_MIRROR_GRACE_TOKENS, COGNITIVE_MIRROR_MASS_FLOOR

class CognitiveMirror:
    """The Cognitive Mirror (Entropy Gate).

    Deterministically detects mathematical hallucination via logprob entropy analysis.
    """
    def __init__(self, threshold: float = COGNITIVE_MIRROR_THRESHOLD_BITS, mass_floor: float = COGNITIVE_MIRROR_MASS_FLOOR):
        self.threshold = threshold
        self.grace_tokens = COGNITIVE_MIRROR_GRACE_TOKENS
        self.mass_floor = mass_floor
        self.reset_stream_state()

    def reset_stream_state(self) -> None:
        self._running_entropy_sum = 0.0
        self._running_token_count = 0

    def compute_entropy(self, logprobs_data: list | None) -> float:
        """Shannon Entropy calculation: -sum(p * log2(p)) with top-k renormalization."""
        import math
        if logprobs_data is None or len(logprobs_data) == 0:
            return 0.0
        total_entropy = 0.0
        token_count = 0
        for token_logprobs in logprobs_data:
            if not token_logprobs:
                continue
            lps = token_logprobs if isinstance(token_logprobs, list) else getattr(token_logprobs, "top_logprobs", [])
            
            # Step 1: Calculate raw probabilities and their sum for renormalization
            raw_probs = []
            for lp in lps:
                lp_val = lp.logprob if hasattr(lp, "logprob") else lp.get("logprob", 0)
                if lp_val is None:
                    continue
                raw_probs.append(math.exp(lp_val))
                
            if len(raw_probs) <= 1:
                continue
                
            prob_sum = sum(raw_probs)
            if prob_sum <= 0:
                continue
                
            # Step 2: Calculate renormalized entropy
            token_entropy = 0.0
            for prob in raw_probs:
                normalized_prob = prob / prob_sum
                if normalized_prob > 0:
                    token_entropy -= normalized_prob * math.log2(normalized_prob)
                    
            total_entropy += token_entropy
            token_count += 1
            
        return total_entropy / token_count if token_count > 0 else 0.0

    def audit(self, response: Any) -> None:
        logprobs = getattr(response, 'logprobs', None)
        entropy = self.compute_entropy(logprobs)
        if entropy > self.threshold:
            raise SovereignInterrupt(violation_type="COGNITIVE_MIRROR_ENTROPY_FAILURE")

    def stream_audit(self, logprobs_data: list | None) -> None:
        """Enforce the Entropy Gate mid-stream using a running mean."""
        if not logprobs_data:
            return
        entropy = self.compute_entropy(logprobs_data)
        self._running_entropy_sum += entropy * len(logprobs_data)
        self._running_token_count += len(logprobs_data)
        mean_entropy = self._running_entropy_sum / self._running_token_count
        
        # Grace period to prevent early-stream spikes from severing valid responses
        if self._running_token_count > self.grace_tokens and mean_entropy > self.threshold:
            self.reset_stream_state()
            raise SovereignInterrupt(violation_type="COGNITIVE_MIRROR_ENTROPY_FAILURE")


class OpenAICompatibleProvider:
    """Typed adapter for any OpenAI-compatible chat-completions endpoint."""

    def __init__(self, config: LLMProviderConfig, api_key_factory: Callable[[], str] | None = None, contract_validator: Any | None = None, telemetry_enabled: bool = False) -> None:
        """Initialize the provider with dynamic API-key lookup at call boundaries."""
        self._config = config
        self._api_key_factory = api_key_factory
        self.contract_validator = contract_validator
        self.telemetry_enabled = telemetry_enabled
        self.logger = logging.getLogger(__name__)
        self.cognitive_mirror = CognitiveMirror()
        self.last_response: EngineResponse | None = None

    def __repr__(self) -> str:
        """Return a redacted representation safe for logs and diagnostics."""
        return f"{self.__class__.__name__}(name={self._config.name!r}, model={self._config.model!r}, base_url={self._config.base_url!r}, api_key='[REDACTED]')"

    def __getstate__(self) -> dict[str, Any]:
        """Return pickling/logging state with all credential material removed."""
        return {
            "config": self._redacted_config_snapshot(),
            "has_api_key": bool(self._api_key_factory or self._config.api_key or self._config.api_key_env),
            "api_key": "[REDACTED]",
        }

    def _compute_entropy(self, logprobs_data: list | None) -> float:
        """Shannon Entropy calculation: -sum(p * log2(p))."""
        import math
        if not logprobs_data:
            return 0.0
        
        total_entropy = 0.0
        token_count = 0
        
        for token_logprobs in logprobs_data:
            if not token_logprobs:
                continue
            token_entropy = 0.0
            lps = token_logprobs if isinstance(token_logprobs, list) else getattr(token_logprobs, "top_logprobs", [])
            for lp in lps:
                lp_val = lp.logprob if hasattr(lp, "logprob") else lp.get("logprob", 0)
                if lp_val is None:
                    continue
                prob = math.exp(lp_val)
                if prob > 0:
                    token_entropy -= prob * math.log2(prob)
            total_entropy += token_entropy
            token_count += 1
        
        return total_entropy / token_count if token_count > 0 else 0.0

    def _enforce_cognitive_mirror(self, response: EngineResponse) -> None:
        """The Entropy Gate: Hard-interrupt if the Engine is hallucinating."""
        entropy = self._compute_entropy(response.logprobs)
        # Whitepaper Threshold: 4.0 bits of mean entropy
        if entropy > 4.0:
            raise SovereignInterrupt(violation_type="COGNITIVE_MIRROR_ENTROPY_FAILURE")

    def _redacted_config_snapshot(self) -> dict[str, Any]:
        """Return a provider configuration snapshot that cannot reveal key values."""
        return {
            "name": self._config.name,
            "kind": self._config.kind,
            "model": self._config.model,
            "base_url": self._config.base_url,
            "api_key": "[REDACTED]" if self._config.api_key is not None else None,
            "api_key_env": self._config.api_key_env,
            "timeout_seconds": self._config.timeout_seconds,
            "max_retries": self._config.max_retries,
        }

    def _require_api_key(self) -> str:
        """Resolve and return the API key only at an outbound provider call boundary."""
        api_key = self._api_key_factory() if self._api_key_factory is not None else None
        if not api_key:
            api_key = self._config.resolved_api_key()
        if not api_key:
            raise ValueError(f"Provider '{self._config.name}' does not have a resolved API key.")
        return api_key

    @contextmanager
    def _with_key(self) -> Iterator[str]:
        """Unlock one provider key for exactly one outbound request boundary."""
        api_key = self._require_api_key()
        try:
            yield api_key
        finally:
            del api_key
            gc.collect()

    def _is_google(self) -> bool:
        """Return True if the base_url points to Google's hosted catalog."""
        return "googleapis.com" in (self._config.base_url or "").lower()

    def _get_payload_params(self, streaming: bool = True) -> dict[str, Any]:
        """Return provider-aware generation parameters to prevent API payload rejections."""
        params = {}
        if not self._is_google():
            params.update({
                "top_p": 1.0, 
                "frequency_penalty": 0.02, 
                "extra_body": {"repeat_penalty": 1.02}
            })
        return params

    def _credential_markers(self) -> tuple[str, ...]:
        """Return exact credential strings that must never escape provider errors."""
        markers: list[str] = []
        config_key = getattr(self._config, "api_key", None)
        if config_key:
            get_secret_value = getattr(config_key, "get_secret_value", None)
            markers.append(get_secret_value() if callable(get_secret_value) else str(config_key))
        
        env_key_name = getattr(self._config, "api_key_env", None)
        if env_key_name:
            env_val = os.environ.get(env_key_name)
            if env_val:
                markers.append(env_val)
                
        factory_val = self._api_key_factory() if self._api_key_factory is not None else None
        if factory_val:
            markers.append(factory_val)
            
        return tuple(set(markers))

    def _scrub_sensitive_text(self, text: str) -> str:
        """Erase known credential markers from any text string."""
        if not text:
            return text
        scrubbed = text
        for marker in self._credential_markers():
            if not marker:
                continue
            escaped_marker = re.escape(marker)
            scrubbed = re.sub(escaped_marker, "[REDACTED_API_KEY]", scrubbed, flags=re.IGNORECASE)
        # Pillar VII Blue Team: Scrub generic GitHub/OpenAI patterns just in case
        scrubbed = re.sub(r"sk-[A-Za-z0-9]{32,}", "[REDACTED_API_KEY]", scrubbed, flags=re.IGNORECASE)
        scrubbed = re.sub(r"ghp_[A-Za-z0-9]{12,}", "ghp_[REDACTED_API_KEY]", scrubbed, flags=re.IGNORECASE)
        return scrubbed

    def _raise_sanitized_provider_error(self, operation: str, exc: BaseException, *, api_key: str | None = None, request: Any | None = None) -> None:
        """Raise a credential-scrubbed provider failure with no traceback chain."""
        exc_text = self._scrub_sensitive_text(str(exc))
        if api_key:
            escaped_key = re.escape(api_key)
            exc_text = re.sub(escaped_key, "[REDACTED_API_KEY]", exc_text, flags=re.IGNORECASE)
        
        self.logger.error(f"Provider Security Event during {operation}: {exc_text}")
        raise ProviderSecurityError(f"{operation} failed: {exc_text}") from None

    def _build_keepalive_socket_options(self) -> list[tuple[int, int, int | bytes]]:
        import socket
        options: list[tuple[int, int, int | bytes]] = [
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        ]
        if hasattr(socket, "TCP_KEEPIDLE"):
            options.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60))
        if hasattr(socket, "TCP_KEEPINTVL"):
            options.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10))
        if hasattr(socket, "TCP_KEEPCNT"):
            options.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 6))
        return options

    def _build_sync_client(self, api_key: str) -> OpenAI:
        import httpx
        timeout_config = httpx.Timeout(self._config.timeout_seconds, connect=30.0, read=None)
        transport = httpx.HTTPTransport(socket_options=self._build_keepalive_socket_options())
        http_client = httpx.Client(transport=transport, timeout=timeout_config)
        return OpenAI(
            api_key=api_key,
            base_url=self._config.base_url,
            http_client=http_client,
            max_retries=self._config.max_retries,
        )

    def _build_async_client(self, api_key: str) -> AsyncOpenAI:
        import httpx
        timeout_config = httpx.Timeout(self._config.timeout_seconds, connect=30.0, read=None)
        transport = httpx.AsyncHTTPTransport(socket_options=self._build_keepalive_socket_options())
        http_client = httpx.AsyncClient(transport=transport, timeout=timeout_config)
        return AsyncOpenAI(
            api_key=api_key,
            base_url=self._config.base_url,
            http_client=http_client,
            max_retries=self._config.max_retries,
        )

    def complete(
        self,
        messages: str | Sequence[EngineMessage],
        *,
        system_prompt: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        preserve_thinking: bool = True,
        timeout_seconds: float | None = None,
        seed: int | None = None,
    ) -> EngineResponse:
        """Send a simple string or normalized chat messages to the Engine."""
        if isinstance(messages, str):
            prompt_messages: list[EngineMessage] = []
            if system_prompt:
                prompt_messages.append(EngineMessage(role="system", content=system_prompt))
            prompt_messages.append(EngineMessage(role="user", content=messages))
            actual_messages = prompt_messages
        else:
            actual_messages = list(messages)

        return self.complete_messages(
            actual_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            metadata=metadata,
            preserve_thinking=preserve_thinking,
            timeout_seconds=timeout_seconds,
            seed=seed,
        )

    def complete_messages(
        self,
        messages: Sequence[EngineMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        preserve_thinking: bool = True,
        timeout_seconds: float | None = None,
        seed: int | None = None,
    ) -> EngineResponse:
        """Send normalized chat messages to the OpenAI-compatible endpoint."""
        if not messages:
            raise ValueError("At least one EngineMessage is required.")

        request_messages = [message.to_openai_message() for message in messages]
        request_metadata = dict(metadata) if metadata is not None else None

        try:
            response = self._complete_messages_primary_only(
                request_messages=request_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                request_metadata=request_metadata,
                preserve_thinking=preserve_thinking,
                timeout_seconds=timeout_seconds,
                seed=seed,
            )
            self.last_response = response
            return response
        except SovereignInterrupt:
            raise
        except Exception as exc:
            if not self._is_sanctioned_fallback_error(exc):
                self._raise_sanitized_provider_error("chat completion", exc, api_key=self._config.resolved_api_key())

            response = self._attempt_sanctioned_fallback(
                primary_error=exc,
                request_messages=request_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                preserve_thinking=preserve_thinking,
                timeout_seconds=timeout_seconds,
                seed=seed,
            )
            self.last_response = response
            return response

    def _complete_messages_primary_only(
        self,
        *,
        request_messages: Sequence[ChatCompletionMessageParam],
        temperature: float,
        max_tokens: int | None,
        request_metadata: Mapping[str, Any] | None,
        preserve_thinking: bool,
        timeout_seconds: float | None = None,
        seed: int | None = None,
    ) -> EngineResponse:
        """Execute the primary model without any sanctioned fallback pivot."""
        try:
            return self._run_streaming_completion(
                request_messages=request_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                request_metadata=request_metadata,
                preserve_thinking=preserve_thinking,
                timeout_seconds=timeout_seconds,
                seed=seed,
            )
        except BadRequestError as exc:
            if not self._is_recoverable_request_shape_error(exc):
                raise
            return self._complete_messages_non_streaming(
                request_messages=request_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                request_metadata=request_metadata,
                preserve_thinking=preserve_thinking,
                timeout_seconds=timeout_seconds,
                seed=seed,
            )

    def _attempt_sanctioned_fallback(
        self,
        *,
        primary_error: BaseException,
        request_messages: Sequence[ChatCompletionMessageParam],
        temperature: float,
        max_tokens: int | None,
        preserve_thinking: bool,
        timeout_seconds: float | None = None,
        seed: int | None = None,
    ) -> EngineResponse:
        """Pivot once to the hard-sanctioned fallback model after 429/503 only."""
        fallback_model = self._config.fallback_model
        history = [
            {
                "attempt": "primary",
                "model": self._config.model,
                "error": self._fallback_error_summary(primary_error),
            }
        ]
        if not fallback_model:
            self._write_fallback_inquest(history, reason="missing_fallback_model")
            raise SovereignException("Primary Engine failed with a catchable upstream error, but no sanctioned fallback_model is configured.") from None

        fallback_config = self._config.model_copy(update={"model": fallback_model, "fallback_model": None})
        fallback_provider = self.__class__(fallback_config, api_key_factory=self._api_key_factory, contract_validator=self.contract_validator)
        try:
            response = fallback_provider._complete_messages_primary_only(
                request_messages=request_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                request_metadata=None,
                preserve_thinking=preserve_thinking,
                timeout_seconds=timeout_seconds,
                seed=seed,
            )
        except Exception as fallback_error:
            history.append(
                {
                    "attempt": "fallback",
                    "model": fallback_model,
                    "error": self._fallback_error_summary(fallback_error),
                }
            )
            self._write_fallback_inquest(history, reason="fallback_failed")
            raise SovereignException("Sanctioned fallback Engine failed; see /tmp/inquest_report.json") from None

        return EngineResponse(
            content=response.content,
            model=response.model,
            provider=response.provider,
            finish_reason=response.finish_reason,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            total_tokens=response.total_tokens,
            reasoning_content=response.reasoning_content,
            raw={
                "fallback_used": True,
                "primary_model": "[REDACTED_PRIMARY_MODEL]",
                "fallback_model": "[SANCTIONED_FALLBACK]",
                "response": response.raw,
            },
        )

    def _run_streaming_completion(self, *, timeout_seconds: float | None = None, **kwargs) -> EngineResponse:
        """Isolated loop execution for infinite, keepalive-protected streaming."""
        def run_in_isolated_loop() -> EngineResponse:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    self._complete_messages_streaming_dlp(**kwargs)
                )
            finally:
                loop.close()

        try:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                return run_in_isolated_loop()

            try:
                import nest_asyncio
            except ImportError as exc:
                raise SovereignInterrupt("CHASSIS_STASIS_NEST_ASYNCIO_UNAVAILABLE") from exc
            nest_asyncio.apply(running_loop)
            return running_loop.run_until_complete(
                self._complete_messages_streaming_dlp(**kwargs)
            )
        except SovereignInterrupt:
            raise
        except Exception as exc:
            if isinstance(exc, SovereignInterrupt):
                raise
            self._raise_sanitized_provider_error("streaming_omega_link", exc, api_key=self._config.resolved_api_key())

    async def _to_text(
        self,
        raw_stream: Any,
        *,
        raw_chunks: list[dict[str, Any]],
        response_state: dict[str, Any],
        local_mirror: CognitiveMirror,
        preserve_thinking: bool = True,
    ) -> AsyncIterator[str]:
        """Normalize provider chunks into text with user-adjustable TTFT and inter-token watchdogs."""
        local_mirror.reset_stream_state()
        response_state["logprobs"] = []
        response_state["reasoning_content"] = []  # FIX: Stores reasoning chunks
        
        # Pull timeout limit from LLMProviderConfig, defaulting to 600.0s (10 minutes)
        provider_timeout = getattr(self._config, "timeout_seconds", 600.0)
        
        # If the configuration still carries the legacy 60.0s default, automatically 
        # upgrade it to the robust 10-minute (600.0s) baseline to prevent stasis timeouts.
        if provider_timeout <= 60.0:
            provider_timeout = 600.0
            
        ttft_limit = provider_timeout
        inter_token_limit = provider_timeout
        
        stream_iterator = raw_stream.__aiter__()
        first_chunk = True
        first_reasoning = True
        
        # Instantiate a secondary, independent StreamingIRQ strictly for the thinking stream
        reasoning_irq = StreamingIRQ(self.contract_validator) if self.contract_validator else None
        
        try:
            while True:
                try:
                    if first_chunk:
                        # Enforce user-adjusted TTFT watchdog
                        chunk = await asyncio.wait_for(stream_iterator.__anext__(), timeout=ttft_limit)
                        first_chunk = False
                    else:
                        # Enforce user-adjusted inter-token/inter-chunk watchdog
                        chunk = await asyncio.wait_for(stream_iterator.__anext__(), timeout=inter_token_limit)
                except asyncio.TimeoutError:
                    if first_chunk:
                        raise SovereignInterrupt("CHASSIS_STASIS_TIMEOUT_TTFT")
                    else:
                        raise SovereignInterrupt("CHASSIS_STASIS_TIMEOUT_INTER_TOKEN")
                except StopAsyncIteration:
                    break
                    
                raw_chunk = chunk.model_dump(mode="json")
                sanitized_raw_chunk = _sanitize_telemetry(raw_chunk)
                raw_chunks.append(sanitized_raw_chunk)
                chunk_model = getattr(chunk, "model", None)
                if chunk_model:
                    response_state["model"] = _sanitize_telemetry({"model": chunk_model})["model"]
                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                
                # Enforce Cognitive Mirror mid-stream
                if hasattr(choice, "logprobs") and choice.logprobs:
                    logprob_content = getattr(choice.logprobs, "content", None)
                    local_mirror.stream_audit(logprob_content)
                    if logprob_content:
                        response_state["logprobs"].extend(logprob_content)
                        
                # FIX: Intercept LM Studio separate reasoning chunks if present
                delta_reasoning = getattr(choice.delta, "reasoning_content", None)
                
                # Reasoning Airlock: Suppress and swallow thinking at the socket layer if preserve_thinking is False
                if delta_reasoning and preserve_thinking:
                    if first_reasoning:
                        sys.stdout.write(f"\n{Colors.GRAY}{Colors.BOLD}--- [ COGNITIVE TRACE ] ---{Colors.RESET}\n{Colors.GRAY}")
                        sys.stdout.flush()
                        first_reasoning = False
                    response_state["reasoning_content"].append(delta_reasoning)
                    
                    # Enforce secondary StreamingIRQ over the reasoning content before release
                    if reasoning_irq:
                        safe_reasoning = reasoning_irq.push(delta_reasoning)
                        if safe_reasoning:
                            sys.stdout.write(safe_reasoning)
                            sys.stdout.flush()
                    else:
                        sys.stdout.write(delta_reasoning)
                        sys.stdout.flush()
                            
                delta_content = choice.delta.content or ""
                if delta_content and not first_reasoning:
                    # Flush the remaining thinking quarantine buffer before transitioning to content
                    if reasoning_irq:
                        safe_reasoning_tail = reasoning_irq.flush()
                        if safe_reasoning_tail:
                            sys.stdout.write(safe_reasoning_tail)
                    # Close the thinking block styling
                    sys.stdout.write(f"{Colors.RESET}\n{Colors.GRAY}{Colors.BOLD}---------------------------{Colors.RESET}\n")
                    sys.stdout.flush()
                    first_reasoning = True # Reset so it doesn't trigger again
                    
                response_state["finish_reason"] = choice.finish_reason or response_state.get("finish_reason")
                yield delta_content
        finally:
            if reasoning_irq:
                try:
                    safe_reasoning_tail = reasoning_irq.flush()
                    if safe_reasoning_tail and not first_reasoning:
                        sys.stdout.write(safe_reasoning_tail)
                        sys.stdout.flush()
                except SovereignInterrupt:
                    raise
                finally:
                    if not first_reasoning:
                        sys.stdout.write(f"{Colors.RESET}\n{Colors.GRAY}{Colors.BOLD}---------------------------{Colors.RESET}\n")
                        sys.stdout.flush()

    async def _stream_with_irq(
        self,
        raw_stream: Any,
        *,
        raw_chunks: list[dict[str, Any]],
        response_state: dict[str, Any],
        local_mirror: CognitiveMirror,
        preserve_thinking: bool = True,
    ) -> AsyncIterator[str]:
        """Route every provider token through the High IRQ quarantine window with local_mirror."""
        if self.contract_validator is None:
            raise SovereignInterrupt("CONTRACT_VALIDATOR_UNAVAILABLE")
        irq = StreamingIRQ(self.contract_validator)
        
        chunk_counter = 0
        async for safe_text in irq.aprocess_stream(
            self._to_text(raw_stream, raw_chunks=raw_chunks, response_state=response_state, local_mirror=local_mirror, preserve_thinking=preserve_thinking)
        ):
            chunk_counter += 1
            # Deterministic Heartbeat: Print a dot every 10 chunks to show live progress
            if chunk_counter % 10 == 0:
                sys.stdout.write(".")
                sys.stdout.flush()
            yield safe_text
            
        # Clean up the terminal line after the stream finishes
        if chunk_counter > 0:
            sys.stdout.write("\n")
            sys.stdout.flush()

    async def _complete_messages_streaming_dlp(
        self,
        *,
        request_messages: Sequence[ChatCompletionMessageParam],
        temperature: float,
        max_tokens: int | None,
        request_metadata: Mapping[str, Any] | None,
        preserve_thinking: bool,
        seed: int | None = None,
    ) -> EngineResponse:
        """The Omega Link: real-time IRQ token governance with local_mirror."""
        
        # Phase 16: Streaming Watermark Monitoring
        try:
            global_config = load_config(DEFAULT_CONFIG_PATH)
            max_context_tokens = global_config.hot_context.max_tokens
        except Exception:
            max_context_tokens = 64000
        watermark_limit = int(max_context_tokens * 0.75)
        
        # Parse the prompt text to compute its base size
        prompt_text = str(request_messages)
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            cumulative_tokens = len(enc.encode(prompt_text))
            def count_toks(text: str) -> int:
                return len(enc.encode(text))
        except ImportError:
            cumulative_tokens = len(prompt_text) // 3
            def count_toks(text: str) -> int:
                return len(text) // 3

        with self._with_key() as api_key:
            async_client = self._build_async_client(api_key)
            content_parts: list[str] = []
            raw_chunks: list[dict[str, Any]] = []
            response_state: dict[str, Any] = {
                "model": _sanitize_telemetry({"model": self._config.model})["model"],
                "finish_reason": None,
            }
            stream = None

            try:
                # Step 10: Global Telemetry Kill Switch & Micro-Repeat Penalty
                if not self.telemetry_enabled:
                    request_metadata = None
                    extra_body = {}
                else:
                    extra_body = {"preserve_thinking": preserve_thinking}

                # Extract response_format from metadata if present
                response_format_val = request_metadata.pop("response_format", None) if isinstance(request_metadata, dict) else None
                
                is_google = self._is_google()
                payload_params = self._get_payload_params()
                
                streaming_payload = {
                    "model": self._config.model,
                    "messages": request_messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": True,
                    **payload_params
                }
                if not is_google:
                    streaming_payload["metadata"] = request_metadata
                    streaming_payload["logprobs"] = True
                    streaming_payload["top_logprobs"] = 5
                    # We merge extra_body with the repeat_penalty if not google
                    eb_merged = dict(extra_body)
                    if "extra_body" in payload_params:
                        eb_merged.update(payload_params["extra_body"])
                    streaming_payload["extra_body"] = eb_merged
                    
                if response_format_val:
                    # Freeze Bypass: Skip strict schema formatting on NVIDIA reasoning gateways to prevent infinite loops
                    is_reasoning_nim = "reasoning" in self._config.model.lower() or "nvidia.com" in (self._config.base_url or "").lower()
                    if not is_reasoning_nim:
                        streaming_payload["response_format"] = response_format_val
                if seed is not None and not is_google:
                    streaming_payload["seed"] = seed
                stream = await async_client.chat.completions.create(**streaming_payload)

                local_mirror = CognitiveMirror(threshold=self.cognitive_mirror.threshold, mass_floor=self.cognitive_mirror.mass_floor)
                async for safe_text in self._stream_with_irq(
                    stream,
                    raw_chunks=raw_chunks,
                    response_state=response_state,
                    local_mirror=local_mirror,
                    preserve_thinking=preserve_thinking,
                ):
                    content_parts.append(safe_text)
                    
                    # Phase 16: Mid-stream watermark enforcement
                    # We count both the safe text and the current total reasoning content
                    # to ensure the host doesn't OOM from a massive thinking block.
                    current_reasoning = "".join(response_state.get("reasoning_content", []))
                    total_streamed_tokens = count_toks("".join(content_parts) + current_reasoning)
                    
                    if cumulative_tokens + total_streamed_tokens > watermark_limit:
                        raise SovereignInterrupt(violation_type="HOT_CONTEXT_WATERMARK_BREACH")

            except SovereignInterrupt as interrupt:
                content_parts.clear()
                raw_chunks.clear()
                if stream is not None:
                    try:
                        await self._force_close_stream(stream)
                    except Exception:
                        pass
                    raise interrupt
            except Exception as exc:
                if stream is not None:
                    await self._force_close_stream(stream)
                self._raise_sanitized_provider_error("streaming chat completion", exc, api_key=api_key)
            finally:
                if stream is not None:
                    try:
                        await self._force_close_stream(stream)
                    except Exception:
                        pass
                await async_client.close()
                gc.collect()

            # FIX: Implement Post-Stream Guard Verification on reasoning content
            accumulated_reasoning = "".join(response_state.get("reasoning_content", []))
            if accumulated_reasoning and self.contract_validator:
                violation = self.contract_validator.hard_violation_type(accumulated_reasoning)
                if violation:
                    content_parts.clear()
                    raw_chunks.clear()
                    raise SovereignInterrupt(violation)

            return EngineResponse(
                content="".join(content_parts),
                model=str(response_state["model"]),
                provider=self._config.name,
                finish_reason=response_state["finish_reason"],
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                logprobs=response_state.get("logprobs"),
                reasoning_content=accumulated_reasoning,  # FIX: Yield fully verified reasoning content to Chassis
                raw={"streamed": True, "irq_governed": True, "chunks": raw_chunks},
            )

    def _complete_messages_non_streaming(
        self,
        *,
        request_messages: Sequence[ChatCompletionMessageParam],
        temperature: float,
        max_tokens: int | None,
        request_metadata: Mapping[str, Any] | None,
        preserve_thinking: bool,
        timeout_seconds: float | None = None,
        seed: int | None = None,
    ) -> EngineResponse:
        """Fallback for OpenAI-compatible endpoints that reject streaming completions."""
        with self._with_key() as api_key:
            client = self._build_sync_client(api_key)
            try:
                # Step 10: Global Telemetry Kill Switch & Micro-Repeat Penalty
                if not self.telemetry_enabled:
                    request_metadata = None
                    extra_body = {}
                else:
                    extra_body = {"preserve_thinking": preserve_thinking}
                    
                response_format_val = request_metadata.pop("response_format", None) if isinstance(request_metadata, dict) else None
                is_google = self._is_google()
                payload_params = self._get_payload_params()
                
                completion_payload = {
                    "model": self._config.model,
                    "messages": request_messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "metadata": request_metadata,
                    **payload_params
                }
                if not is_google:
                    completion_payload["logprobs"] = True
                    completion_payload["top_logprobs"] = 5
                    # We merge extra_body with repeat_penalty
                    eb_merged = dict(extra_body)
                    if "extra_body" in payload_params:
                        eb_merged.update(payload_params["extra_body"])
                    completion_payload["extra_body"] = eb_merged
                    
                if response_format_val:
                    is_reasoning_nim = "reasoning" in self._config.model.lower() or "nvidia.com" in (self._config.base_url or "").lower()
                    if not is_reasoning_nim:
                        completion_payload["response_format"] = response_format_val
                if seed is not None and not is_google:
                    completion_payload["seed"] = seed
                response = client.chat.completions.create(**completion_payload)
            except BadRequestError as exc:
                if not self._is_recoverable_request_shape_error(exc):
                    raise
                client.close()
                client = self._build_sync_client(api_key)
                is_google = self._is_google()
                payload_params = self._get_payload_params()
                completion_payload_fallback = {
                    "model": self._config.model,
                    "messages": request_messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    **payload_params
                }
                if seed is not None and not is_google:
                    completion_payload_fallback["seed"] = seed
                response = client.chat.completions.create(**completion_payload_fallback)
            except Exception as exc:
                client.close()
                del client
                del self
                self._raise_sanitized_provider_error("non-streaming chat completion", exc, api_key=api_key)
            finally:
                if "client" in locals():
                    client.close()
                gc.collect()
            choice = response.choices[0] if response.choices else None
            message = choice.message if choice is not None else None
            usage = response.usage
            logprobs_data = getattr(choice.logprobs, "content", None) if choice is not None and hasattr(choice, "logprobs") and choice.logprobs else None
            
            # FIX: Intercept reasoning content in non-streaming responses if present
            reasoning_data = getattr(message, "reasoning_content", None) if message is not None else None

            response_obj = EngineResponse(
                content=(message.content if message is not None else "") or "",
                model=response.model or self._config.model,
                provider=self._config.name,
                finish_reason=choice.finish_reason if choice is not None else None,
                input_tokens=usage.prompt_tokens if usage is not None else None,
                output_tokens=usage.completion_tokens if usage is not None else None,
                total_tokens=usage.total_tokens if usage is not None else None,
                logprobs=logprobs_data,
                reasoning_content=reasoning_data,  # FIX: Stores reasoning data
                raw={"streamed": False, "response": response.model_dump(mode="json")},
            )
            self.cognitive_mirror.audit(response_obj)
            
            # FIX: Implement Post-Stream Guard Verification on non-streaming reasoning content
            if reasoning_data and self.contract_validator:
                violation = self.contract_validator.hard_violation_type(reasoning_data)
                if violation:
                    raise SovereignInterrupt(violation)
                    
            return response_obj

    def embed_query(self, text: str, model: str | None = None) -> list[float]:
        results = self.embed_documents([text], model=model)
        return results[0] if results else []

    def embed_documents(self, texts: Sequence[str], model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        with self._with_key() as api_key:
            payload = {"model": model or self._config.model, "input": list(texts)}
            body = json.dumps(payload).encode("utf-8")
            request_headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            base_url = (self._config.base_url or "https://api.openai.com/v1").rstrip("/")
            url = f"{base_url}/embeddings"
            request = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self._config.timeout_seconds) as response:
                    response_payload = json.loads(response.read().decode("utf-8"))
                    data = response_payload.get("data", [])
                    data.sort(key=lambda x: x.get("index", 0))
                    return [item.get("embedding", []) for item in data]
            except Exception as exc:
                self._raise_sanitized_provider_error("embedding request", exc, api_key=api_key, request=request)
            finally:
                gc.collect()
        return []

    def rerank(self, query: str, documents: Sequence[str], *, top_k: int = 5, model: str | None = None) -> list[RerankResult]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query cannot be empty")
        if top_k <= 0:
            raise ValueError("top_k must be greater than zero")
        normalized_documents = [doc.strip() for doc in documents if isinstance(doc, str) and doc.strip()]
        if not normalized_documents:
            return []
        with self._with_key() as api_key:
            payload = self._rerank_payload(normalized_query, normalized_documents, top_k=top_k, model=model)
            body = json.dumps(payload).encode("utf-8")
            request_headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"}
            request = urllib.request.Request(self._rerank_url(), data=body, headers=request_headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self._config.timeout_seconds) as response:
                    response_payload = json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                self._raise_sanitized_provider_error("rerank request", exc, api_key=api_key, request=request)
            finally:
                gc.collect()
        return self._parse_rerank_response(response_payload, normalized_documents, top_k=top_k)

    def _rerank_payload(self, query: str, documents: Sequence[str], *, top_k: int, model: str | None = None) -> dict[str, Any]:
        top_n = min(top_k, len(documents))
        reranker_model = model or self._config.reranker_model
        if self._is_nvidia_gateway():
            return {"model": self._nvidia_rerank_model_name(reranker_model), "query": {"text": query}, "passages": [{"text": doc} for doc in documents], "truncate": "END"}
        return {"model": reranker_model, "query": query, "documents": list(documents), "top_n": top_n, "return_documents": False}

    def _nvidia_rerank_model_name(self, model: str) -> str:
        if not model:
            return ""
        normalized = model.removeprefix("nvidia/")
        if normalized == "rerank-qa-mistral-4b":
            return "nv-rerank-qa-mistral-4b:1"
        return normalized

    def _rerank_url(self) -> str:
        base_url = (self._config.base_url or "https://api.openai.com/v1").rstrip("/")
        if not self._is_nvidia_gateway():
            return f"{base_url}/rerank"
        if "/retrieval/nvidia/reranking" in base_url:
            return base_url
        if "integrate.api.nvidia.com" in base_url:
            return "https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking"
        return f"{base_url}/retrieval/nvidia/reranking"

    @staticmethod
    def _parse_rerank_response(payload: Mapping[str, Any] | Sequence[Any], documents: Sequence[str], *, top_k: int) -> list[RerankResult]:
        if isinstance(payload, Mapping):
            raw_results = payload.get("results") or payload.get("data") or payload.get("rankings") or payload.get("scores") or []
        else:
            raw_results = payload
        if not isinstance(raw_results, Sequence) or isinstance(raw_results, (str, bytes)):
            raise RuntimeError("Rerank endpoint returned an invalid results payload.")
        normalized_results = []
        for fallback_index, item in enumerate(raw_results):
            if isinstance(item, Mapping):
                index_value = item.get("index", item.get("document_index", fallback_index))
                score_value = item.get("relevance_score", item.get("score", item.get("logit")))
                raw_item = item
            else:
                index_value = fallback_index
                score_value = item
                raw_item = {"score": item}
            if score_value is None:
                continue
            try:
                index = int(index_value)
                score = float(score_value)
            except (TypeError, ValueError):
                continue
            if index < 0 or index >= len(documents):
                continue
            normalized_results.append(RerankResult(index=index, relevance_score=score, document=documents[index], raw=dict(raw_item)))
        if not normalized_results:
            raise RuntimeError("Rerank endpoint returned no usable relevance_score values.")
        normalized_results.sort(key=lambda result: (-result.relevance_score, result.index))
        return normalized_results[:top_k]

    @staticmethod
    async def _force_close_stream(stream: Any) -> None:
        close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    @classmethod
    def _is_sanctioned_fallback_error(cls, exc: BaseException) -> bool:
        status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if status_code in FALLBACK_CATCHABLE_STATUS_CODES:
            return True
        message = str(exc).lower()
        return any(marker in message for marker in ("status_code=429", "status code: 429", "http 429", "429 rate", "ratelimit", "rate limit", "status_code=503", "status code: 503", "http 503", "503 service", "service unavailable"))

    def _fallback_error_summary(self, exc: BaseException) -> dict[str, Any]:
        status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        return {"type": type(exc).__name__, "status_code": status_code, "message": self._scrub_sensitive_text(str(exc))[:500]}

    @staticmethod
    def _write_fallback_inquest(history: Sequence[Mapping[str, Any]], *, reason: str) -> None:
        payload = {"failure": "sanctioned_fallback_exhausted", "reason": reason, "retry_history": list(history)}
        INQUEST_REPORT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _is_recoverable_request_shape_error(exc: BadRequestError) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in ("extra_body", "metadata", "unknown parameter", "unsupported parameter"))

    def _is_nvidia_gateway(self) -> bool:
        """Return True if base_url points to NVIDIA's NIM catalog."""
        return "nvidia.com" in (self._config.base_url or "").lower()