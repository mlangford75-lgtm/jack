"""Configuration loading and validation for the Jack Chassis.

This module belongs to the deterministic Chassis layer. It loads local configuration
from ``config.yaml`` and validates all runtime limits before any probabilistic Engine
is allowed to participate in a workflow.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator, model_validator

HOT_CONTEXT_MIN_TOKENS = 4_096
HOT_CONTEXT_MAX_TOKENS = 128_000
DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_RETRIEVAL_PROJECT_ID = "default"
DEFAULT_RETRIEVAL_COLLECTION_PREFIX = "jack_project"
DEFAULT_RETRIEVAL_RRF_K = 60
DEFAULT_PQ_DIMENSIONS = 1_024
DEFAULT_PQ_BITS = 8
DEFAULT_PQ_TARGET_RECALL = 0.95
DEFAULT_PQ_TARGET_RAM_REDUCTION = 0.70
DEFAULT_RERANKER_MODEL = "Qwen3-Reranker-8B"

ProviderKind = Literal["openai_compatible", "ollama", "lmstudio", "vllm", "local_stub"]
PillarRole = Literal[
    "manager",
    "muscle",
    "librarian",
    "eyes",
    "judge",
    "visual_studio",
    "audio_studio",
    "tas_thesis",
    "tas_antithesis",
    "tas_synthesis",
]


class ConfigError(RuntimeError):
    """Raised when Jack cannot load or validate its deterministic configuration."""


class HotContextConfig(BaseModel):
    """User-adjustable Hot Context settings enforced by the Chassis.

    The Chassis never silently expands or truncates this boundary. Values outside
    the supported range are rejected at startup so model calls cannot accidentally
    exceed the user's hardware-aware context policy.
    """

    max_tokens: int = Field(
        default=HOT_CONTEXT_MIN_TOKENS,
        description="Active high-fidelity context budget in tokens.",
    )

    @field_validator("max_tokens")
    @classmethod
    def enforce_slider_limits(cls, value: int) -> int:
        """Reject Hot Context limits outside the 4,096 to 128,000 token slider."""
        if value < HOT_CONTEXT_MIN_TOKENS or value > HOT_CONTEXT_MAX_TOKENS:
            raise ValueError(
                "hot_context.max_tokens must be between "
                f"{HOT_CONTEXT_MIN_TOKENS} and {HOT_CONTEXT_MAX_TOKENS} tokens."
            )
        return value


class RetrievalCompressionConfig(BaseModel):
    """Compression targets for project-scoped retrieval indexes.

    ChromaDB owns the concrete HNSW backend, while the Chassis owns the deterministic
    policy that describes how Jack should trade memory footprint against recall.
    Product Quantization is represented here as a validated backend policy so the
    Librarian can persist compression intent in collection metadata without allowing
    probabilistic components to alter index behavior at runtime.
    """

    enabled: bool = Field(default=True, description="Whether vector compression is expected for large ledgers.")
    method: Literal["pq", "none"] = Field(default="pq", description="Compression method requested for the vector index.")
    dimensions: int = Field(default=DEFAULT_PQ_DIMENSIONS, gt=0)
    bits_per_codebook: int = Field(default=DEFAULT_PQ_BITS, ge=1, le=16)
    target_recall: float = Field(default=DEFAULT_PQ_TARGET_RECALL, ge=0.0, le=1.0)
    target_ram_reduction: float = Field(default=DEFAULT_PQ_TARGET_RAM_REDUCTION, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_pq_policy(self) -> RetrievalCompressionConfig:
        """Reject compression policies that violate the Sovereign efficiency floor."""
        if not self.enabled:
            return self

        if self.method != "pq":
            raise ValueError("retrieval.compression.method must be 'pq' when compression is enabled.")

        if self.dimensions != DEFAULT_PQ_DIMENSIONS:
            raise ValueError(f"retrieval.compression.dimensions must remain {DEFAULT_PQ_DIMENSIONS} for Phase 4 PQ policy.")

        if self.bits_per_codebook != DEFAULT_PQ_BITS:
            raise ValueError(f"retrieval.compression.bits_per_codebook must remain {DEFAULT_PQ_BITS} for 8-bit PQ.")

        if self.target_ram_reduction < DEFAULT_PQ_TARGET_RAM_REDUCTION:
            raise ValueError(
                "retrieval.compression.target_ram_reduction must be at least "
                f"{DEFAULT_PQ_TARGET_RAM_REDUCTION:.2f}."
            )

        if self.target_recall < DEFAULT_PQ_TARGET_RECALL:
            raise ValueError(
                "retrieval.compression.target_recall must be at least "
                f"{DEFAULT_PQ_TARGET_RECALL:.2f}."
            )

        return self


class RetrievalConfig(BaseModel):
    """Project-scoped hybrid retrieval policy enforced by the Chassis."""

    project_id: str = Field(
        default=DEFAULT_RETRIEVAL_PROJECT_ID,
        description="Physical Shadow Ledger tenant boundary used as jack_project_{project_id}.",
    )
    collection_prefix: str = Field(default=DEFAULT_RETRIEVAL_COLLECTION_PREFIX)
    chunk_size: int = Field(default=1_000, gt=0)
    chunk_overlap: int = Field(default=100, ge=0)
    rrf_k: int = Field(default=DEFAULT_RETRIEVAL_RRF_K, gt=0)
    semantic_candidate_multiplier: int = Field(default=4, ge=1)
    lexical_candidate_multiplier: int = Field(default=8, ge=1)
    hnsw_space: Literal["cosine", "l2", "ip"] = Field(default="cosine")
    hnsw_construction_ef: int = Field(default=200, ge=1)
    hnsw_search_ef: int = Field(default=100, ge=1)
    hnsw_m: int = Field(default=32, ge=2)
    compression: RetrievalCompressionConfig = Field(default_factory=RetrievalCompressionConfig)

    @field_validator("project_id")
    @classmethod
    def normalize_project_id(cls, value: str) -> str:
        """Normalize the configured project identifier for safe collection names."""
        normalized = "".join(
            character.lower() if character.isalnum() else "_"
            for character in str(value or DEFAULT_RETRIEVAL_PROJECT_ID).strip()
        ).strip("_")
        while "__" in normalized:
            normalized = normalized.replace("__", "_")
        return normalized or DEFAULT_RETRIEVAL_PROJECT_ID

    @model_validator(mode="after")
    def validate_chunking(self) -> RetrievalConfig:
        """Reject retrieval chunk settings that would create degenerate overlap loops."""
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("retrieval.chunk_overlap must be smaller than retrieval.chunk_size.")
        return self

    @property
    def collection_name(self) -> str:
        """Return the physical project-scoped collection name."""
        return f"{self.collection_prefix}_{self.project_id}"

    def collection_metadata(self) -> dict[str, Any]:
        """Return deterministic metadata persisted with the Chroma collection."""
        metadata: dict[str, Any] = {
            "project_id": self.project_id,
            "collection_scope": "project",
            "retrieval_strategy": "hybrid_rrf_bm25_vector",
            "hnsw:space": self.hnsw_space,
            "hnsw:construction_ef": self.hnsw_construction_ef,
            "hnsw:search_ef": self.hnsw_search_ef,
            "hnsw:M": self.hnsw_m,
        }
        metadata.update(
            {
                "compression:enabled": self.compression.enabled,
                "compression:method": self.compression.method if self.compression.enabled else "none",
                "compression:dimensions": self.compression.dimensions,
                "compression:bits_per_codebook": self.compression.bits_per_codebook,
                "compression:target_recall": self.compression.target_recall,
                "compression:target_ram_reduction": self.compression.target_ram_reduction,
            }
        )
        return metadata


class LLMProviderConfig(BaseModel):
    """Connection settings for a swappable probabilistic Engine provider."""

    name: str = Field(description="Stable local name for this provider configuration.")
    kind: ProviderKind = Field(default="openai_compatible")
    model: str = Field(description="Provider-specific chat-completion model identifier.")
    fallback_model: str | None = Field(
        default=None,
        description=(
            "Optional hard-sanctioned backup chat-completion model identifier. "
            "The Chassis may pivot to this model exactly once only after an upstream "
            "429 rate-limit or 503 service-unavailable failure."
        ),
    )
    reranker_model: str = Field(
        default=DEFAULT_RERANKER_MODEL,
        description="Provider-specific reranker model identifier used by Pillar V Judge.",
    )
    base_url: str | None = Field(
        default=None,
        description="OpenAI-compatible API base URL, if required by the provider.",
    )
    api_key: SecretStr | None = Field(
        default=None,
        description="Direct API key value. Prefer api_key_env for committed config files.",
    )
    api_key_env: str | None = Field(
        default=None,
        description="Environment variable name containing the provider API key.",
    )
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=2, ge=0)

    @field_validator("fallback_model")
    @classmethod
    def normalize_fallback_model(cls, value: str | None) -> str | None:
        """Normalize blank fallback model declarations to an absent hard sanction."""
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def require_api_key_for_openai_compatible(self) -> LLMProviderConfig:
        """Ensure OpenAI-compatible providers have a resolvable API key."""
        if self.kind != "openai_compatible":
            return self

        if self.api_key is not None:
            return self

        # SEMANTIC UPGRADE: Check if key variable is configured rather than actively populated.
        # This prevents startup crashes during packaging or unconfigured dry-runs.
        if self.api_key_env:
            return self

        raise ValueError(
            "OpenAI-compatible provider "
            f"'{self.name}' requires api_key or api_key_env pointing to a populated environment variable."
        )

    def resolved_api_key(self) -> str | None:
        """Return the actual API key from config or environment without mutating state."""
        if self.api_key is not None:
            return self.api_key.get_secret_value()

        if self.api_key_env:
            return os.getenv(self.api_key_env)

        return None


class RoleEngineConfig(BaseModel):
    """Maps one of Jack's seven pillars to a configured Engine provider."""

    role: PillarRole
    provider: str = Field(description="Name of an entry in llm.providers.")
    system_prompt: str | None = Field(
        default=None,
        description="Optional role wrapper controlled by the Chassis for role overloading.",
    )


class LLMConfig(BaseModel):
    """All probabilistic Engine configuration used by the deterministic Chassis."""

    default_provider: str
    providers: list[LLMProviderConfig] = Field(default_factory=list)
    roles: list[RoleEngineConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_provider_references(self) -> LLMConfig:
        """Ensure defaults and role bindings reference declared providers."""
        provider_names = {provider.name for provider in self.providers}

        if not provider_names:
            raise ValueError("llm.providers must declare at least one provider.")

        if self.default_provider not in provider_names:
            raise ValueError(
                "llm.default_provider must match one configured provider name. "
                f"Known providers: {sorted(provider_names)}."
            )

        unknown_roles = [role for role in self.roles if role.provider not in provider_names]
        if unknown_roles:
            formatted = ", ".join(f"{role.role}->{role.provider}" for role in unknown_roles)
            raise ValueError(f"llm.roles contains unknown provider references: {formatted}.")

        return self

    def provider_by_name(self, name: str | None = None) -> LLMProviderConfig:
        """Return a provider by name, falling back to the configured default."""
        target_name = name or self.default_provider
        for provider in self.providers:
            if provider.name == target_name:
                return provider

        raise ConfigError(f"Provider '{target_name}' is not configured.")

    def provider_for_role(self, role: PillarRole) -> LLMProviderConfig:
        """Return the provider bound to a pillar role, or the default provider."""
        for role_config in self.roles:
            if role_config.role == role:
                return self.provider_by_name(role_config.provider)

        return self.provider_by_name(self.default_provider)


class JackConfig(BaseModel):
    """Top-level Jack runtime configuration controlled by the Chassis."""

    hot_context: HotContextConfig = Field(default_factory=HotContextConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    llm: LLMConfig
    telemetry_enabled: bool = Field(default=False, description="Allow external logging or crash reporting.")
    streaming_irq_window_size: int = Field(default=256, ge=256, description="Size of the sliding window for DLP streaming audit.")
    allow_local_subprocess_fallback: bool = Field(default=False, description="Allow running code on host if Docker is missing.")
    tas_thesis_max_tokens: int = Field(default=2048)
    tas_critique_max_tokens: int = Field(default=1000)
    tas_synthesis_max_tokens: int = Field(default=4096)
    show_thinking: bool = Field(default=False, description="Enable printing the model's thinking/reasoning process.")

    @model_validator(mode="after")
    def apply_globals(self) -> JackConfig:
        import jack.chassis.sovereign_constants as consts
        consts.STREAMING_IRQ_WINDOW_SIZE = self.streaming_irq_window_size
        consts.ALLOW_LOCAL_SUBPROCESS_FALLBACK = self.allow_local_subprocess_fallback
        consts.TAS_THESIS_MAX_TOKENS = self.tas_thesis_max_tokens
        consts.TAS_CRITIQUE_MAX_TOKENS = self.tas_critique_max_tokens
        consts.TAS_SYNTHESIS_MAX_TOKENS = self.tas_synthesis_max_tokens
        return self


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> JackConfig:
    """Load and validate Jack configuration from a YAML file."""
    config_path = Path(path).expanduser()

    if not config_path.exists():
        raise ConfigError(f"Configuration file not found: {config_path}")

    try:
        raw_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Configuration file is not valid YAML: {config_path}") from exc
    except OSError as exc:
        raise ConfigError(f"Unable to read configuration file: {config_path}") from exc

    if raw_config is None:
        raise ConfigError(f"Configuration file is empty: {config_path}")

    if not isinstance(raw_config, dict):
        raise ConfigError("Configuration root must be a YAML mapping/object.")

    try:
        return JackConfig.model_validate(raw_config)
    except ValidationError as exc:
        raise ConfigError(f"Invalid Jack configuration in {config_path}:\n{exc}") from exc


def load_config_from_mapping(mapping: dict[str, Any]) -> JackConfig:
    """Validate an in-memory configuration mapping."""
    try:
        return JackConfig.model_validate(mapping)
    except ValidationError as exc:
        raise ConfigError(f"Invalid Jack configuration mapping:\n{exc}") from exc