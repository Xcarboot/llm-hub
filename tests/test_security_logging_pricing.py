import json
import logging
import secrets

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import JsonFormatter
from app.core.security import generate_api_key, verify_api_key
from app.services.pricing import calculate_cost_micro_cny, estimate_input_tokens


def test_key_hash_is_verifiable_and_plaintext_is_not_stored() -> None:
    raw, prefix, digest = generate_api_key("test-secret")
    assert raw.startswith("gw_")
    assert prefix == raw[:12]
    assert raw not in digest
    assert verify_api_key(raw, digest, "test-secret")
    assert not verify_api_key(raw + "x", digest, "test-secret")


def test_structured_log_redacts_sensitive_fields() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "request", (), None)
    record.event_data = {
        "Authorization": "Bearer gw_secret",
        "messages": [{"content": "commercial secret"}],
        "tenant_id": "t1",
    }
    output = json.loads(JsonFormatter().format(record))
    assert output["data"]["Authorization"] == "[REDACTED]"
    assert output["data"]["messages"] == "[REDACTED]"
    assert "commercial secret" not in json.dumps(output)

    message_record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "Bearer gw_super_secret", (), None
    )
    assert "gw_super_secret" not in JsonFormatter().format(message_record)


def test_byte_level_input_estimate_is_conservative_for_chinese() -> None:
    messages = [{"role": "user", "content": "你好"}]
    serialized_bytes = len(
        json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    estimate = estimate_input_tokens(messages, 1.0)
    assert estimate > serialized_bytes
    assert estimate >= len("你好".encode())


def test_integer_micro_cny_price_rounds_up() -> None:
    assert calculate_cost_micro_cny(1, 1, 1, 1) == 2
    assert calculate_cost_micro_cny(1_000_000, 2_000_000, 3, 5) == 13


def test_secrets_reject_placeholders_in_every_environment() -> None:
    settings = Settings(
        app_env="development",
        admin_token="REPLACE_WITH_A_LONG_RANDOM_ADMIN_TOKEN",
        api_key_hmac_secret="b" * 32,
    )
    with pytest.raises(RuntimeError, match="ADMIN_TOKEN"):
        settings.assert_safe()


def test_example_env_parses_csv_and_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)

    parsed = Settings(_env_file=".env.example")

    assert parsed.trusted_hosts == ["localhost", "127.0.0.1"]
    assert parsed.cors_origins == []


def test_random_secrets_are_accepted() -> None:
    admin_token = secrets.token_urlsafe(48)
    hmac_secret = secrets.token_urlsafe(48)

    Settings(admin_token=admin_token, api_key_hmac_secret=hmac_secret).assert_safe()


def test_request_timeout_must_precede_concurrency_lease() -> None:
    with pytest.raises(ValidationError, match="REQUEST_TIMEOUT_SECONDS"):
        Settings(
            admin_token="a" * 32,
            api_key_hmac_secret="b" * 32,
            request_timeout_seconds=300,
            concurrency_lease_seconds=300,
        )
