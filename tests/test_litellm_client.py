from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.errors import InfrastructureUnavailableError
from app.infra.litellm_client import LiteLLMClient


def write_config(path: Path, *, duplicate: bool = False) -> Path:
    duplicate_entry = """
  - model_name: primary
    litellm_params:
      model: provider/duplicate
      api_key: os.environ/PROVIDER_API_KEY
      api_base: os.environ/PROVIDER_API_BASE
""" if duplicate else ""
    path.write_text(
        """model_list:
  - model_name: primary
    litellm_params:
      model: provider/actual-model
      api_key: os.environ/PROVIDER_API_KEY
      api_base: os.environ/PROVIDER_API_BASE
"""
        + duplicate_entry,
        encoding="utf-8",
    )
    return path


async def test_alias_resolves_model_endpoint_and_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_config(tmp_path / "litellm.yaml")
    monkeypatch.setenv("PROVIDER_API_KEY", "sk-live-random-value-without-markers")
    monkeypatch.setenv("PROVIDER_API_BASE", "https://provider.internal/v1")
    completion = AsyncMock(return_value={"id": "response"})
    monkeypatch.setattr("app.infra.litellm_client.litellm.acompletion", completion)
    client = LiteLLMClient(config_path)

    result = await client.completion(
        model="primary",
        messages=[{"role": "user", "content": "hello"}],
        api_key="request-must-not-win",
        api_base="https://request.invalid",
    )

    assert result == {"id": "response"}
    completion.assert_awaited_once_with(
        model="provider/actual-model",
        messages=[{"role": "user", "content": "hello"}],
        api_key="sk-live-random-value-without-markers",
        api_base="https://provider.internal/v1",
    )


@pytest.mark.parametrize("unsafe_value", [None, "REPLACE_ME"])
async def test_missing_or_placeholder_environment_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_value: str | None,
) -> None:
    config_path = write_config(tmp_path / "litellm.yaml")
    monkeypatch.setenv("PROVIDER_API_BASE", "https://provider.internal/v1")
    if unsafe_value is None:
        monkeypatch.delenv("PROVIDER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("PROVIDER_API_KEY", unsafe_value)
    completion = AsyncMock()
    monkeypatch.setattr("app.infra.litellm_client.litellm.acompletion", completion)

    with pytest.raises(InfrastructureUnavailableError):
        LiteLLMClient(config_path)

    completion.assert_not_called()


async def test_unknown_alias_fails_closed_without_upstream_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_config(tmp_path / "litellm.yaml")
    monkeypatch.setenv("PROVIDER_API_KEY", "sk-live-random-value-without-markers")
    monkeypatch.setenv("PROVIDER_API_BASE", "https://provider.internal/v1")
    completion = AsyncMock()
    monkeypatch.setattr("app.infra.litellm_client.litellm.acompletion", completion)
    client = LiteLLMClient(config_path)

    with pytest.raises(InfrastructureUnavailableError):
        await client.completion(model="unknown", messages=[])

    completion.assert_not_called()


def test_duplicate_alias_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_config(tmp_path / "litellm.yaml", duplicate=True)
    monkeypatch.setenv("PROVIDER_API_KEY", "sk-live-random-value-without-markers")
    monkeypatch.setenv("PROVIDER_API_BASE", "https://provider.internal/v1")

    with pytest.raises(InfrastructureUnavailableError):
        LiteLLMClient(config_path)
