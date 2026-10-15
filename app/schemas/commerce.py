"""Safe input DTOs. Routes explicitly construct outputs, with decimal-string money.

CustomerProfile.csrf_token is populated only on session/login responses. Never
serialize ORM objects or ProviderWrite as a provider response.
"""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

MicroCNY = Annotated[str, Field(strict=True, pattern=r"^[0-9]{1,19}$")]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=200)]


class CommerceDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomerProfile(CommerceDTO):
    id: UUID
    tenant_id: UUID
    email: str
    tenant_name: str
    csrf_token: str | None = None


class AccountActivationCreate(CommerceDTO):
    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        local, separator, domain = value.partition("@")
        if (
            not separator
            or not local
            or not domain
            or "@" in domain
            or any(character.isspace() for character in value)
        ):
            raise ValueError("invalid email address")
        return value


class ActivateBody(CommerceDTO):
    token: str = Field(min_length=1, max_length=512)
    password: str = Field(min_length=12, max_length=1024, repr=False)


class LoginBody(AccountActivationCreate):
    password: str = Field(min_length=1, max_length=1024, repr=False)


class PaymentCreate(CommerceDTO):
    amount_micro_cny: MicroCNY
    idempotency_key: IdempotencyKey

    @field_validator("amount_micro_cny")
    @classmethod
    def whole_cent_amount(cls, value: str) -> str:
        amount = int(value)
        if not 0 < amount <= 2**63 - 1 or amount % 10_000:
            raise ValueError("amount must be positive whole cents within BigInteger range")
        return str(amount)


class RefundCreate(PaymentCreate):
    payment_order_id: UUID
    reason: str = Field(min_length=1, max_length=2000)


class RefundDecision(CommerceDTO):
    reason: str = Field(min_length=1, max_length=2000)


class ProviderWrite(CommerceDTO):
    alias: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    model: str = Field(min_length=1, max_length=200)
    api_base: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr | None = Field(default=None, exclude=True)
    enabled: bool = True
