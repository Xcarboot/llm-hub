import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import litellm
import yaml

from app.core.errors import InfrastructureUnavailableError

_ENV_REFERENCE = re.compile(r"os\.environ/([A-Za-z_][A-Za-z0-9_]*)")
_PLACEHOLDER_MARKERS = (
    "REPLACE",
    "CHANGE_ME",
    "CHANGEME",
    "EXAMPLE",
    "PLACEHOLDER",
    "YOUR_",
    "INSERT_",
    "SET_ME",
    "DUMMY",
    "SAMPLE",
)


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
    return not normalized or any(marker in normalized for marker in _PLACEHOLDER_MARKERS)


def _validate_config_value(value: object) -> None:
    if isinstance(value, str):
        if value.startswith("os.environ/"):
            if _ENV_REFERENCE.fullmatch(value) is None:
                raise InfrastructureUnavailableError("LiteLLM configuration is invalid")
            return
        if _is_placeholder(value):
            raise InfrastructureUnavailableError("LiteLLM configuration contains a placeholder")
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise InfrastructureUnavailableError("LiteLLM configuration is invalid")
            _validate_config_value(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _validate_config_value(nested)
        return
    if value is not None and not isinstance(value, (bool, int, float)):
        raise InfrastructureUnavailableError("LiteLLM configuration is invalid")


def _resolve_config_value(value: object) -> object:
    if isinstance(value, str):
        match = _ENV_REFERENCE.fullmatch(value)
        if match is None:
            return value
        resolved = os.environ.get(match.group(1))
        if resolved is None or _is_placeholder(resolved):
            raise InfrastructureUnavailableError(
                "LiteLLM configuration environment is missing or unsafe"
            )
        return resolved
    if isinstance(value, Mapping):
        return {str(key): _resolve_config_value(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_resolve_config_value(nested) for nested in value]
    return value


def _load_model_aliases(config_path: str | Path) -> dict[str, dict[str, Any]]:
    try:
        document: object = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise InfrastructureUnavailableError("LiteLLM configuration cannot be loaded") from None

    if not isinstance(document, Mapping):
        raise InfrastructureUnavailableError("LiteLLM configuration is invalid")
    raw_model_list = document.get("model_list")
    if not isinstance(raw_model_list, list) or not raw_model_list:
        raise InfrastructureUnavailableError("LiteLLM model_list is invalid")

    aliases: dict[str, dict[str, Any]] = {}
    for entry in raw_model_list:
        if not isinstance(entry, Mapping):
            raise InfrastructureUnavailableError("LiteLLM model entry is invalid")
        alias = entry.get("model_name")
        raw_params = entry.get("litellm_params")
        if not isinstance(alias, str) or not alias.strip() or not isinstance(raw_params, Mapping):
            raise InfrastructureUnavailableError("LiteLLM model entry is invalid")
        if alias in aliases:
            raise InfrastructureUnavailableError("LiteLLM model alias is duplicated")
        if any(not isinstance(key, str) for key in raw_params):
            raise InfrastructureUnavailableError("LiteLLM model parameters are invalid")

        params = dict(raw_params)
        for required in ("model", "api_key", "api_base"):
            if not isinstance(params.get(required), str):
                raise InfrastructureUnavailableError("LiteLLM model parameters are incomplete")
        _validate_config_value(params)
        resolved = _resolve_config_value(params)
        aliases[alias] = cast(dict[str, Any], resolved)

    return aliases


class LiteLLMClient:
    def __init__(self, config_path: str | Path = "litellm_config.yaml") -> None:
        self._models = _load_model_aliases(config_path)

    async def completion(self, **kwargs: Any) -> Any:
        alias = kwargs.get("model")
        if not isinstance(alias, str) or alias not in self._models:
            raise InfrastructureUnavailableError("LiteLLM model alias is not configured")

        request = dict(kwargs)
        request.update(self._models[alias])
        return await litellm.acompletion(**request)
