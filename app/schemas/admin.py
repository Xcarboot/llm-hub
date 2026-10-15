from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.models import AccountKind, ReservationStatus, TransferKind


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class KeyCreated(BaseModel):
    id: UUID
    prefix: str
    api_key: str


class ModelCreate(BaseModel):
    public_name: str = Field(min_length=1, max_length=100)
    litellm_model: str = Field(min_length=1, max_length=200)
    route_order: int = Field(ge=0, le=1)


class PriceCreate(BaseModel):
    model_id: UUID
    input_micro_cny_per_million: int = Field(ge=0)
    output_micro_cny_per_million: int = Field(ge=0)
    effective_at: datetime


class RechargeCreate(BaseModel):
    tenant_id: UUID
    amount_micro_cny: int = Field(gt=0)
    external_ref: str = Field(min_length=1, max_length=180)


class ReservationAction(BaseModel):
    external_ref: str = Field(min_length=1, max_length=180)
    timeout_release: bool = False


class ReversalCreate(BaseModel):
    transfer_id: UUID
    external_ref: str = Field(min_length=1, max_length=180)


class Page[ItemT](BaseModel):
    items: list[ItemT]
    next_cursor: str | None
    has_more: bool


class AdminSession(BaseModel):
    authenticated: bool


class OverviewKpis(BaseModel):
    tenant_count: int
    active_key_count: int
    request_count: int
    success_count: int
    success_rate: float
    charged_micro_cny: int
    prompt_tokens: int
    completion_tokens: int


class OverviewTrendPoint(BaseModel):
    date: date
    requests: int
    successes: int
    charged_micro_cny: int
    prompt_tokens: int
    completion_tokens: int


class AdminOverview(BaseModel):
    kpis: OverviewKpis
    trend: list[OverviewTrendPoint]


class TenantSummary(BaseModel):
    id: UUID
    name: str


class TenantRead(TenantSummary):
    active: bool
    created_at: datetime
    key_count: int
    request_count: int
    last_request_at: datetime | None
    available_micro_cny: int
    frozen_micro_cny: int


class ApiKeyRead(BaseModel):
    id: UUID
    prefix: str
    revoked: bool
    created_at: datetime


class TenantBalance(BaseModel):
    tenant_id: UUID
    available_micro_cny: int
    frozen_micro_cny: int


class PriceVersionRead(BaseModel):
    id: UUID
    input_micro_cny_per_million: int
    output_micro_cny_per_million: int
    effective_at: datetime


class ModelRead(BaseModel):
    id: UUID
    public_name: str
    litellm_model: str
    route_order: int
    active: bool
    created_at: datetime
    prices: list[PriceVersionRead]


class LedgerTransferSummary(BaseModel):
    id: UUID
    kind: TransferKind
    external_ref: str
    posted: bool
    created_at: datetime


class LedgerReversalRead(BaseModel):
    reversal_of_id: UUID | None
    reversed_by_id: UUID | None


class LedgerEntryRead(BaseModel):
    account_kind: AccountKind
    account_scope: str
    amount_micro_cny: int


class LedgerTransferRead(BaseModel):
    transfer: LedgerTransferSummary
    reversal: LedgerReversalRead | None
    entries: list[LedgerEntryRead]


class ReservationSummary(BaseModel):
    id: UUID
    amount_micro_cny: int
    status: ReservationStatus
    selected_price_version_id: UUID | None
    expires_at: datetime


class GatewayRequestRead(BaseModel):
    id: UUID
    tenant: TenantSummary
    model: str
    stream: bool
    status: str
    usage_json: dict[str, int | bool] | None
    error_code: str | None
    created_at: datetime
    reservation: ReservationSummary | None


class ReservationRead(BaseModel):
    id: UUID
    tenant_id: UUID
    request_id: UUID
    amount_micro_cny: int
    status: ReservationStatus
    selected_price: PriceVersionRead | None
    expires_at: datetime
    created_at: datetime
