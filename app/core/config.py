"""Application configuration, loaded from environment variables and validated at startup.

What: one typed ``Settings`` object (pydantic-settings) that is the ONLY place that reads
the environment.
Why: "fail fast" - a bad or missing setting should stop the process at boot with a clear
message, not surface as a confusing error at 3 a.m. on the first request.
Connects to: every other module receives settings via ``get_settings()`` / dependency
injection; nothing else touches ``os.environ``.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# WHY: a well-known placeholder lets us detect "someone forgot to set a real secret".
_PLACEHOLDER_SECRET_PREFIX = "change-me"  # noqa: S105


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class TracingExporter(StrEnum):
    NONE = "none"
    CONSOLE = "console"
    OTLP = "otlp"


class EmbeddingProviderName(StrEnum):
    NONE = "none"  # keyword search only
    HASH = "hash"  # built-in offline embedder (no key, no network)
    OPENAI = "openai"  # any OpenAI-compatible /embeddings endpoint


class LLMProviderName(StrEnum):
    MOCK = "mock"
    ANTHROPIC = "anthropic"
    GROQ = "groq"
    GEMINI = "gemini"  # Google Gemini through its OpenAI-compatible endpoint


class Settings(BaseSettings):
    """All runtime configuration. Field ``foo_bar`` is read from env var ``FOO_BAR``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # WHY: docker-compose passes unset optional variables as empty strings ("PORT=").
        env_ignore_empty=True,
        # WHY: validation errors would otherwise echo the offending VALUE (e.g. a too-short
        # JWT secret) into logs and tracebacks.
        hide_input_in_errors=True,
    )

    # --- App ---------------------------------------------------------------
    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    log_json: bool = True

    # --- Database ----------------------------------------------------------
    database_url: str
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_connect_retries: int = Field(default=20, ge=1)
    db_connect_retry_delay_seconds: float = Field(default=1.0, gt=0)

    # --- Redis (optional) --------------------------------------------------
    redis_url: str | None = None

    # --- Auth --------------------------------------------------------------
    # WHY SecretStr: pydantic prints invalid input values in error messages; SecretStr
    # guarantees a secret can never leak into a startup traceback or a log line.
    jwt_secret: SecretStr
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"  # symmetric only, allow-listed
    access_token_minutes: int = Field(default=15, ge=1)
    refresh_token_days: int = Field(default=7, ge=1)

    # --- HTTP --------------------------------------------------------------
    # WHY NoDecode: lets us accept a plain "a,b,c" string instead of requiring JSON.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    max_request_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    # WHY off by default: X-Forwarded-For is client-controlled unless a trusted proxy sets it.
    trust_proxy_headers: bool = False

    # --- Rate limiting -----------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_default_per_minute: int = Field(default=300, ge=1)
    rate_limit_auth_per_minute: int = Field(default=10, ge=1)

    # --- LLM ---------------------------------------------------------------
    llm_provider: LLMProviderName = LLMProviderName.MOCK
    llm_model: str | None = None  # WHY: never hardcoded; chosen per deployment.
    anthropic_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    # WHY configurable: any OpenAI-compatible endpoint works (and tests point it at a stub).
    groq_base_url: str = "https://api.groq.com/openai/v1"
    gemini_api_key: SecretStr | None = None
    # Extra output tokens for Gemini's hidden reasoning (it is counted in the same limit).
    gemini_thinking_headroom_tokens: int = Field(default=4096, ge=0, le=32768)
    # How hard Gemini "thinks" before answering: none | minimal | low | medium | high.
    # "low" is fast enough for planning/review calls; blank = the model's own default.
    gemini_reasoning_effort: str | None = "low"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    # Backup model used when the primary is rate-limited or down (same provider and key).
    llm_fallback_model: str | None = None
    # Simulated model latency for the mock provider (load tests and demos feel realistic).
    mock_llm_latency_seconds: float = Field(default=0.0, ge=0, le=30)
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_max_attempts: int = Field(default=3, ge=1, le=10)
    llm_retry_base_delay_seconds: float = Field(default=0.5, ge=0)
    # Max requests per minute sent to the provider (0 = unlimited). Unset: 8 for Gemini and 20 for
    # Groq (their free tiers are small), unlimited otherwise.
    llm_requests_per_minute: int | None = Field(default=None, ge=0, le=100000)
    llm_breaker_failure_threshold: int = Field(default=5, ge=1)
    llm_breaker_recovery_seconds: float = Field(default=30.0, gt=0)
    # WHY configurable prices: they change over time and differ per model; cost is an
    # ESTIMATE for dashboards. 0 (default) simply reports zero cost.
    llm_input_cost_per_mtok: Decimal = Field(default=Decimal(0), ge=0)
    llm_output_cost_per_mtok: Decimal = Field(default=Decimal(0), ge=0)

    # --- Tools -------------------------------------------------------------
    # WHY default-deny with one harmless host: http_fetch can only reach hosts listed here.
    http_fetch_allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["example.com"]
    )
    http_fetch_timeout_seconds: float = Field(default=10.0, gt=0)
    http_fetch_max_bytes: int = Field(default=1_048_576, ge=1024)
    tool_output_max_chars: int = Field(default=20_000, ge=100)
    # Research tools. WHY base URLs are settings: tests point them at stubs and a deployment
    # can use a proxy; the model only ever controls the query text, never the host.
    tavily_api_key: SecretStr | None = None
    tavily_base_url: str = "https://api.tavily.com"
    wikipedia_base_url: str = "https://en.wikipedia.org"
    arxiv_base_url: str = "https://export.arxiv.org"
    research_timeout_seconds: float = Field(default=10.0, gt=0)
    research_max_results: int = Field(default=5, ge=1, le=10)
    research_snippet_chars: int = Field(default=1500, ge=100, le=10_000)

    # --- Embeddings (semantic search) ---------------------------------------
    # "hash" works offline with no key; "openai" calls EMBEDDING_BASE_URL/embeddings.
    embedding_provider: EmbeddingProviderName = EmbeddingProviderName.HASH
    embedding_model: str | None = None
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: SecretStr | None = None
    embedding_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    # Vector-only hits below this cosine similarity are dropped as noise.
    semantic_min_similarity: float = Field(default=0.2, ge=0, le=1)
    # How many chunk vectors one search may compare in-process (see docs/tradeoffs.md).
    semantic_max_candidates: int = Field(default=5000, ge=100, le=100_000)

    # --- Agents & memory ---------------------------------------------------
    max_plan_steps: int = Field(default=10, ge=1, le=30)
    retrieval_top_k: int = Field(default=5, ge=1, le=10)
    # Deep research: how many sub-questions the Planner may create (each = one parallel step).
    deep_research_max_questions: int = Field(default=4, ge=2, le=6)
    # WHY a score floor in config: the LLM's own "APPROVED" is not trusted blindly; the code
    # downgrades verdicts whose score is below this value.
    review_min_score: float = Field(default=0.7, ge=0, le=1)
    short_term_token_budget: int = Field(default=2000, ge=200)
    short_term_keep_recent: int = Field(default=6, ge=1)
    summary_max_tokens: int = Field(default=300, ge=50)
    document_max_chars: int = Field(default=200_000, ge=1000)

    # --- Orchestrator --------------------------------------------------------
    worker_enabled: bool = True  # False = API only (run workers as separate processes)
    max_concurrent_runs: int = Field(default=4, ge=1, le=64)
    max_parallel_steps: int = Field(default=4, ge=1, le=32)
    step_max_attempts: int = Field(default=3, ge=1, le=10)
    step_retry_base_delay_seconds: float = Field(default=0.5, ge=0)
    step_retry_max_delay_seconds: float = Field(default=8.0, ge=0)
    default_max_revisions: int = Field(default=2, ge=0, le=5)
    approval_ttl_seconds: int = Field(default=86_400, ge=1)
    run_lease_seconds: float = Field(default=30.0, gt=0)
    sweep_interval_seconds: float = Field(default=15.0, gt=0)
    sse_poll_interval_seconds: float = Field(default=0.5, gt=0)

    # --- Observability -------------------------------------------------------
    # If set, GET /metrics requires "Authorization: Bearer <token>" (Prometheus supports this).
    metrics_token: SecretStr | None = None
    # Port for the stand-alone worker's own /metrics endpoint (the API serves /metrics itself).
    worker_metrics_port: int | None = Field(default=None, ge=1, le=65535)
    tracing_exporter: TracingExporter = TracingExporter.CONSOLE
    tracing_service_name: str = "workflow-platform"
    otlp_endpoint: str | None = None  # e.g. http://otel-collector:4318/v1/traces
    otlp_timeout_seconds: float = Field(default=5.0, gt=0)

    # --- Caching -------------------------------------------------------------
    cache_enabled: bool = True
    cache_max_entries: int = Field(default=1000, ge=10)
    retrieval_cache_ttl_seconds: int = Field(default=60, ge=1)
    # WHY off by default: a real model is not deterministic, so serving yesterday's answer is a
    # product decision. Turn it on for cost control or demos (docs/performance.md).
    llm_cache_enabled: bool = False
    llm_cache_ttl_seconds: int = Field(default=3600, ge=1)

    @field_validator("cors_origins", "http_fetch_allowed_hosts", mode="before")
    @classmethod
    def _split_cors(cls, value: object) -> object:
        """Accept 'a,b,c' (env-friendly) as well as a real list."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("llm_requests_per_minute", mode="before")
    @classmethod
    def _blank_rpm_is_unset(cls, value: object) -> object:
        """Docker Compose passes an unset variable as an empty string; treat it as 'not set'."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg(cls, value: str) -> str:
        # WHY: the whole app is async; a sync driver URL would fail confusingly later.
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must start with 'postgresql+asyncpg://'")
        return value

    @field_validator("jwt_secret")
    @classmethod
    def _min_secret_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return value

    @model_validator(mode="after")
    def _cross_field_rules(self) -> Self:
        """Rules that involve more than one field."""
        if (
            self.environment is Environment.PRODUCTION
            and self.jwt_secret.get_secret_value().startswith(_PLACEHOLDER_SECRET_PREFIX)
        ):
            raise ValueError("JWT_SECRET still has the placeholder value; set a real secret")
        if self.llm_provider is LLMProviderName.ANTHROPIC:
            if not self.llm_model:
                raise ValueError("LLM_MODEL is required when LLM_PROVIDER=anthropic")
            if self.anthropic_api_key is None or not self.anthropic_api_key.get_secret_value():
                raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        if self.llm_provider is LLMProviderName.GROQ:
            if not self.llm_model:
                raise ValueError("LLM_MODEL is required when LLM_PROVIDER=groq")
            if self.groq_api_key is None or not self.groq_api_key.get_secret_value():
                raise ValueError("GROQ_API_KEY is required when LLM_PROVIDER=groq")
        if self.llm_provider is LLMProviderName.GEMINI:
            if not self.llm_model:
                raise ValueError("LLM_MODEL is required when LLM_PROVIDER=gemini")
            if self.gemini_api_key is None or not self.gemini_api_key.get_secret_value():
                raise ValueError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")
        if self.embedding_provider is EmbeddingProviderName.OPENAI:
            if not self.embedding_model:
                raise ValueError("EMBEDDING_MODEL is required when EMBEDDING_PROVIDER=openai")
            if self.embedding_api_key is None or not self.embedding_api_key.get_secret_value():
                raise ValueError("EMBEDDING_API_KEY is required when EMBEDDING_PROVIDER=openai")
        if self.tracing_exporter is TracingExporter.OTLP and not self.otlp_endpoint:
            raise ValueError("OTLP_ENDPOINT is required when TRACING_EXPORTER=otlp")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def effective_llm_timeout_seconds(self) -> float:
        """Gemini can take longer to answer; widen the default unless the operator chose one."""
        if (
            self.llm_provider is LLMProviderName.GEMINI
            and "llm_timeout_seconds" not in self.model_fields_set
        ):
            return max(self.llm_timeout_seconds, 90.0)
        return self.llm_timeout_seconds

    @property
    def effective_llm_requests_per_minute(self) -> int:
        """Explicit setting wins; otherwise pace Gemini (small free quota) and nobody else."""
        if self.llm_requests_per_minute is not None:
            return self.llm_requests_per_minute
        if self.llm_provider is LLMProviderName.GEMINI:
            return 8
        return 20 if self.llm_provider is LLMProviderName.GROQ else 0

    @property
    def effective_llm_model(self) -> str:
        """Model name used for logging/cost; the mock provider reports a fixed label."""
        return self.llm_model or "mock-model"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings (validated once, then cached)."""
    return Settings()  # required fields are read from the environment
