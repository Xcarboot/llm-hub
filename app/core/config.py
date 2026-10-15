from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "LLM Governance Gateway"
    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://gateway:REPLACE_ME@localhost:5432/gateway"
    redis_url: str = "redis://localhost:6379/0"
    admin_token: str = "REPLACE_ME"
    api_key_hmac_secret: str = "REPLACE_ME"
    litellm_config_path: str = "litellm_config.yaml"
    trusted_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1"]
    )
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    max_request_bytes: int = 1_048_576
    max_message_chars: int = 100_000
    max_output_tokens: int = 8_192
    token_estimate_safety_factor: float = 1.25
    rpm_limit: int = 60
    tpm_limit: int = 200_000
    concurrency_limit: int = 10
    concurrency_lease_seconds: int = Field(default=300, gt=0)
    request_timeout_seconds: float = Field(default=120, gt=0)
    reservation_ttl_seconds: int = 600

    payment_mode: Literal["disabled", "mock", "alipay_sandbox"] = "disabled"
    customer_cookie_secure: bool = True
    customer_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    customer_session_ttl_seconds: int = Field(default=3600, gt=0)
    activation_ttl_seconds: int = Field(default=86400, gt=0)
    provider_encryption_key: SecretStr = SecretStr("")
    provider_allowed_host_suffixes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["cn-beijing.maas.aliyuncs.com", "dashscope.aliyuncs.com"]
    )
    alipay_app_id: SecretStr = SecretStr("")
    alipay_seller_id: SecretStr = SecretStr("")
    # Keys may contain PEM text or a filesystem path, interpreted by the provider.
    alipay_public_key: SecretStr = SecretStr("")
    alipay_private_key: SecretStr = SecretStr("")
    alipay_notify_url: str = ""
    alipay_return_url: str = ""

    @property
    def mock_payment_enabled(self) -> bool:
        return self.app_env.lower() in {"development", "test"} and self.payment_mode == "mock"

    @field_validator(
        "trusted_hosts",
        "cors_origins",
        "customer_allowed_origins",
        "provider_allowed_host_suffixes",
        mode="before",
    )
    @classmethod
    def split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def request_timeout_precedes_lease_expiry(self) -> Self:
        if self.request_timeout_seconds >= self.concurrency_lease_seconds:
            raise ValueError("REQUEST_TIMEOUT_SECONDS must be less than CONCURRENCY_LEASE_SECONDS")
        return self

    def assert_safe(self) -> None:
        if self.payment_mode == "mock" and not self.mock_payment_enabled:
            raise RuntimeError("mock payments are only allowed in development/test")
        if self.app_env.lower() not in {"development", "test"} and not self.customer_cookie_secure:
            raise RuntimeError("secure customer cookies are required outside development/test")
        for name, value in (
            ("ADMIN_TOKEN", self.admin_token),
            ("API_KEY_HMAC_SECRET", self.api_key_hmac_secret),
        ):
            normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
            if len(value) < 32 or any(marker in normalized for marker in _PLACEHOLDER_MARKERS):
                raise RuntimeError(
                    f"{name} must be at least 32 random characters without placeholder text"
                )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
