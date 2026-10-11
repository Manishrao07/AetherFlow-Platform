"""Config must fail fast with clear errors and must never leak secrets."""

import pytest
from pydantic import ValidationError

from app.core.config import LLMProviderName
from tests.conftest import make_settings


def test_defaults_are_valid_and_use_mock_llm() -> None:
    settings = make_settings()
    assert settings.llm_provider is LLMProviderName.MOCK
    assert settings.effective_llm_model == "mock-model"
    assert not settings.is_production


def test_cors_origins_accepts_comma_separated_string() -> None:
    settings = make_settings(cors_origins="http://a.test, http://b.test ,")
    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_rejects_non_asyncpg_database_url() -> None:
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        make_settings(database_url="postgresql://u:p@h/db")


def test_rejects_short_jwt_secret() -> None:
    with pytest.raises(ValidationError, match="at least 32"):
        make_settings(jwt_secret="short")


def test_production_rejects_placeholder_secret() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        make_settings(environment="production", jwt_secret="change-me-" + "x" * 30)


def test_anthropic_requires_model_and_key() -> None:
    with pytest.raises(ValidationError, match="LLM_MODEL"):
        make_settings(llm_provider="anthropic", anthropic_api_key="k")
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        make_settings(llm_provider="anthropic", llm_model="some-model")


def test_anthropic_valid_when_model_and_key_given() -> None:
    settings = make_settings(llm_provider="anthropic", llm_model="m-1", anthropic_api_key="k")
    assert settings.effective_llm_model == "m-1"


def test_secrets_never_appear_in_repr_or_errors() -> None:
    settings = make_settings(jwt_secret="super-secret-value-super-secret-value-123")
    assert "super-secret-value" not in repr(settings)
    with pytest.raises(ValidationError) as exc:
        make_settings(jwt_secret="tiny-secret")
    assert "tiny-secret" not in str(exc.value)


def test_otlp_exporter_needs_an_endpoint() -> None:
    from tests.conftest import make_settings

    with pytest.raises(ValueError, match="OTLP_ENDPOINT"):
        make_settings(tracing_exporter="otlp")
    ok = make_settings(tracing_exporter="otlp", otlp_endpoint="http://collector:4318/v1/traces")
    assert ok.tracing_exporter.value == "otlp"


def test_empty_environment_values_are_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # docker-compose passes unset optional variables as "" - that must not break int fields.
    from app.core.config import Settings

    monkeypatch.setenv("WORKER_METRICS_PORT", "")
    monkeypatch.setenv("METRICS_TOKEN", "")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.worker_metrics_port is None and settings.metrics_token is None
