import base64
import json
import sqlite3
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.deps import AdminDep, SessionDep
from app.core.config import settings
from app.core.security import generate_api_key
from app.db.models import (
    AccountKind,
    ApiKey,
    AuditEvent,
    GatewayRequest,
    LedgerAccount,
    LedgerEntry,
    LedgerTransfer,
    ModelCatalog,
    PriceVersion,
    Reservation,
    ReservationStatus,
    Tenant,
    TransferKind,
)
from app.schemas.admin import (
    AdminOverview,
    AdminSession,
    ApiKeyRead,
    GatewayRequestRead,
    KeyCreated,
    LedgerEntryRead,
    LedgerReversalRead,
    LedgerTransferRead,
    LedgerTransferSummary,
    ModelCreate,
    ModelRead,
    OverviewKpis,
    OverviewTrendPoint,
    Page,
    PriceCreate,
    PriceVersionRead,
    RechargeCreate,
    ReservationAction,
    ReservationRead,
    ReservationSummary,
    ReversalCreate,
    TenantBalance,
    TenantCreate,
    TenantRead,
    TenantSummary,
)
from app.services.ledger import (
    create_tenant_accounts,
    ensure_platform_accounts,
    manual_recharge,
    release,
    reverse_transfer,
)

router = APIRouter(prefix="/admin", tags=["admin"])

Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(min_length=1, max_length=1024)]
FromAt = Annotated[datetime | None, Query()]
ToAt = Annotated[datetime | None, Query()]


def _http_422(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _is_tenant_name_conflict(exc: IntegrityError) -> bool:
    # asyncpg wraps the structured PostgreSQL exception in a DBAPI exception.
    error: BaseException | None = exc.orig
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, sqlite3.IntegrityError):
            return (
                getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_CONSTRAINT_UNIQUE
                and str(error) == "UNIQUE constraint failed: tenants.name"
            )
        diagnostic = getattr(error, "diag", None)
        constraint = getattr(error, "constraint_name", None) or getattr(
            diagnostic, "constraint_name", None
        )
        sqlstate = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
        if sqlstate == "23505" and constraint == "uq_tenants_name":
            return True
        error = error.__cause__
    return False


def _utc_datetime(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _http_422(f"{field} must include a timezone offset")
    return value.astimezone(UTC)


def _validate_time_range(from_at: datetime | None, to_at: datetime | None) -> None:
    if from_at is not None:
        _utc_datetime(from_at, "from_at")
    if to_at is not None:
        _utc_datetime(to_at, "to_at")
    if from_at is not None and to_at is not None and from_at > to_at:
        raise _http_422("from_at must not be later than to_at")


def _encode_cursor(created_at: datetime, item_id: UUID) -> str:
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        created_at = created_at.replace(tzinfo=UTC)
    payload = json.dumps(
        {"created_at": created_at.astimezone(UTC).isoformat(), "id": str(item_id)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if cursor is None:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if not isinstance(payload, dict) or set(payload) != {"created_at", "id"}:
            raise ValueError
        created_raw = payload["created_at"]
        id_raw = payload["id"]
        if not isinstance(created_raw, str) or not isinstance(id_raw, str):
            raise ValueError
        created_at = _utc_datetime(datetime.fromisoformat(created_raw), "cursor.created_at")
        return created_at, UUID(id_raw)
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _http_422("invalid cursor") from exc


def _after_desc_cursor(created_at: Any, item_id: Any, cursor: tuple[datetime, UUID]) -> Any:
    cursor_at, cursor_id = cursor
    return or_(created_at < cursor_at, and_(created_at == cursor_at, item_id < cursor_id))


def _is_success(status: str) -> bool:
    return status in {"COMPLETED", "COMPLETED_WITH_OVERAGE"}


def _usage_tokens(value: object) -> tuple[int, int]:
    if not isinstance(value, dict):
        return 0, 0
    prompt = value.get("prompt_tokens")
    completion = value.get("completion_tokens")
    safe_prompt = (
        prompt
        if isinstance(prompt, int) and not isinstance(prompt, bool) and prompt >= 0
        else 0
    )
    safe_completion = (
        completion
        if isinstance(completion, int) and not isinstance(completion, bool) and completion >= 0
        else 0
    )
    return safe_prompt, safe_completion


def _safe_usage(value: object) -> dict[str, int | bool] | None:
    if not isinstance(value, dict):
        return None
    safe: dict[str, int | bool] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "calculated_cost_micro_cny",
        "reservation_ceiling_micro_cny",
    ):
        candidate = value.get(key)
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            safe[key] = candidate
    overage = value.get("overage")
    if isinstance(overage, bool):
        safe["overage"] = overage
    return safe or None


def _tenant_read(
    tenant: Tenant,
    key_count: int,
    request_count: int,
    last_request_at: datetime | None,
    available_micro_cny: int,
    frozen_micro_cny: int,
) -> TenantRead:
    return TenantRead(
        id=tenant.id,
        name=tenant.name,
        active=tenant.active,
        created_at=tenant.created_at,
        key_count=int(key_count),
        request_count=int(request_count),
        last_request_at=last_request_at,
        available_micro_cny=int(available_micro_cny),
        frozen_micro_cny=int(frozen_micro_cny),
    )


def _tenant_read_from_row(row: Any) -> TenantRead:
    return _tenant_read(
        cast(Tenant, row[0]),
        int(row[1]),
        int(row[2]),
        cast(datetime | None, row[3]),
        int(row[4]),
        int(row[5]),
    )


def _tenant_aggregate_statement() -> Any:
    key_counts = (
        select(ApiKey.tenant_id.label("tenant_id"), func.count(ApiKey.id).label("key_count"))
        .group_by(ApiKey.tenant_id)
        .subquery()
    )
    request_stats = (
        select(
            GatewayRequest.tenant_id.label("tenant_id"),
            func.count(GatewayRequest.id).label("request_count"),
            func.max(GatewayRequest.created_at).label("last_request_at"),
        )
        .group_by(GatewayRequest.tenant_id)
        .subquery()
    )
    balance_stats = (
        select(
            LedgerAccount.tenant_id.label("tenant_id"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            LedgerAccount.kind == AccountKind.AVAILABLE,
                            LedgerEntry.amount_micro_cny,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("available_micro_cny"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            LedgerAccount.kind == AccountKind.FROZEN,
                            LedgerEntry.amount_micro_cny,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("frozen_micro_cny"),
        )
        .outerjoin(LedgerEntry, LedgerEntry.account_id == LedgerAccount.id)
        .where(LedgerAccount.tenant_id.is_not(None))
        .group_by(LedgerAccount.tenant_id)
        .subquery()
    )
    return (
        select(
            Tenant,
            func.coalesce(key_counts.c.key_count, 0),
            func.coalesce(request_stats.c.request_count, 0),
            request_stats.c.last_request_at,
            func.coalesce(balance_stats.c.available_micro_cny, 0),
            func.coalesce(balance_stats.c.frozen_micro_cny, 0),
        )
        .outerjoin(key_counts, key_counts.c.tenant_id == Tenant.id)
        .outerjoin(request_stats, request_stats.c.tenant_id == Tenant.id)
        .outerjoin(balance_stats, balance_stats.c.tenant_id == Tenant.id)
    )


async def _require_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return tenant


@router.get("/session", response_model=AdminSession)
async def admin_session(response: Response, admin: AdminDep) -> AdminSession:
    del admin
    response.headers["Cache-Control"] = "no-store"
    return AdminSession(authenticated=True)


@router.get("/overview", response_model=AdminOverview)
async def overview(
    session: SessionDep,
    admin: AdminDep,
    days: Annotated[int, Query(ge=1, le=30)] = 7,
) -> AdminOverview:
    del admin
    today = datetime.now(UTC).date()
    first_day = today - timedelta(days=days - 1)
    start_at = datetime.combine(first_day, time.min, tzinfo=UTC)
    trend_by_date = {
        first_day + timedelta(days=offset): {
            "requests": 0,
            "successes": 0,
            "charged_micro_cny": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
        for offset in range(days)
    }

    tenant_count = int(await session.scalar(select(func.count(Tenant.id))) or 0)
    active_key_count = int(
        await session.scalar(
            select(func.count(ApiKey.id)).where(ApiKey.revoked.is_(False))
        )
        or 0
    )
    request_rows = (
        await session.execute(
            select(
                GatewayRequest.created_at,
                GatewayRequest.status,
                GatewayRequest.usage_json,
            ).where(GatewayRequest.created_at >= start_at)
        )
    ).all()
    for raw_created_at, raw_status, usage_json in request_rows:
        created_at = cast(datetime, raw_created_at)
        status = cast(str, raw_status)
        normalized_created_at = (
            created_at.replace(tzinfo=UTC)
            if created_at.tzinfo is None
            else created_at.astimezone(UTC)
        )
        request_date = normalized_created_at.date()
        point = trend_by_date.get(request_date)
        if point is None:
            continue
        point["requests"] += 1
        if _is_success(status):
            point["successes"] += 1
        prompt_tokens, completion_tokens = _usage_tokens(usage_json)
        point["prompt_tokens"] += prompt_tokens
        point["completion_tokens"] += completion_tokens

    # Period net consumption: actual settlement revenue minus settlement reversals,
    # each attributed to its own transfer date, not the reservation or request date.
    original_transfer = aliased(LedgerTransfer)
    revenue_rows = (
        await session.execute(
            select(LedgerTransfer.created_at, LedgerEntry.amount_micro_cny)
            .join(LedgerEntry, LedgerEntry.transfer_id == LedgerTransfer.id)
            .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
            .outerjoin(
                original_transfer, original_transfer.id == LedgerTransfer.reversal_of_id
            )
            .where(
                or_(
                    and_(
                        LedgerTransfer.kind == TransferKind.SETTLE,
                        LedgerEntry.amount_micro_cny > 0,
                    ),
                    and_(
                        LedgerTransfer.kind == TransferKind.REVERSAL,
                        original_transfer.kind == TransferKind.SETTLE,
                        original_transfer.posted.is_(True),
                        LedgerEntry.amount_micro_cny < 0,
                    ),
                ),
                LedgerTransfer.posted.is_(True),
                LedgerTransfer.created_at >= start_at,
                LedgerAccount.kind == AccountKind.REVENUE,
            )
        )
    ).all()
    for created_at, amount in revenue_rows:
        normalized_created_at = (
            created_at.replace(tzinfo=UTC)
            if created_at.tzinfo is None
            else created_at.astimezone(UTC)
        )
        charged_date = normalized_created_at.date()
        point = trend_by_date.get(charged_date)
        if point is not None:
            point["charged_micro_cny"] += int(amount)

    trend = [
        OverviewTrendPoint(date=day, **values)
        for day, values in sorted(trend_by_date.items())
    ]
    request_count = sum(point.requests for point in trend)
    success_count = sum(point.successes for point in trend)
    return AdminOverview(
        kpis=OverviewKpis(
            tenant_count=tenant_count,
            active_key_count=active_key_count,
            request_count=request_count,
            success_count=success_count,
            success_rate=success_count / request_count if request_count else 0.0,
            charged_micro_cny=sum(point.charged_micro_cny for point in trend),
            prompt_tokens=sum(point.prompt_tokens for point in trend),
            completion_tokens=sum(point.completion_tokens for point in trend),
        ),
        trend=trend,
    )


@router.get("/tenants", response_model=Page[TenantRead])
async def list_tenants(
    session: SessionDep,
    admin: AdminDep,
    search: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    active: Annotated[bool | None, Query()] = None,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> Page[TenantRead]:
    del admin
    statement = _tenant_aggregate_statement()
    if search is not None:
        statement = statement.where(
            func.lower(Tenant.name).contains(search.lower(), autoescape=True)
        )
    if active is not None:
        statement = statement.where(Tenant.active.is_(active))
    decoded = _decode_cursor(cursor)
    if decoded is not None:
        statement = statement.where(_after_desc_cursor(Tenant.created_at, Tenant.id, decoded))
    rows = (
        await session.execute(
            statement.order_by(Tenant.created_at.desc(), Tenant.id.desc()).limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    visible = rows[:limit]
    items = [_tenant_read_from_row(row) for row in visible]
    next_cursor = (
        _encode_cursor(visible[-1][0].created_at, visible[-1][0].id)
        if has_more and visible
        else None
    )
    return Page(items=items, next_cursor=next_cursor, has_more=has_more)


@router.get("/tenants/{tenant_id}", response_model=TenantRead)
async def get_tenant(tenant_id: UUID, session: SessionDep, admin: AdminDep) -> TenantRead:
    del admin
    row = (
        await session.execute(
            _tenant_aggregate_statement().where(Tenant.id == tenant_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return _tenant_read_from_row(row)


@router.get("/tenants/{tenant_id}/keys", response_model=Page[ApiKeyRead])
async def list_tenant_keys(
    tenant_id: UUID, session: SessionDep, admin: AdminDep
) -> Page[ApiKeyRead]:
    del admin
    await _require_tenant(session, tenant_id)
    keys = (
        await session.scalars(
            select(ApiKey)
            .where(ApiKey.tenant_id == tenant_id)
            .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
        )
    ).all()
    items = [
        ApiKeyRead(id=key.id, prefix=key.prefix, revoked=key.revoked, created_at=key.created_at)
        for key in keys
    ]
    return Page(items=items, next_cursor=None, has_more=False)


@router.get("/tenants/{tenant_id}/balance", response_model=TenantBalance)
async def get_tenant_balance(
    tenant_id: UUID, session: SessionDep, admin: AdminDep
) -> TenantBalance:
    del admin
    await _require_tenant(session, tenant_id)
    rows = (
        await session.execute(
            select(
                LedgerAccount.kind,
                func.coalesce(func.sum(LedgerEntry.amount_micro_cny), 0),
            )
            .outerjoin(LedgerEntry, LedgerEntry.account_id == LedgerAccount.id)
            .where(
                LedgerAccount.tenant_id == tenant_id,
                LedgerAccount.kind.in_([AccountKind.AVAILABLE, AccountKind.FROZEN]),
            )
            .group_by(LedgerAccount.kind)
        )
    ).all()
    balances = {kind: int(amount) for kind, amount in rows}
    return TenantBalance(
        tenant_id=tenant_id,
        available_micro_cny=balances.get(AccountKind.AVAILABLE, 0),
        frozen_micro_cny=balances.get(AccountKind.FROZEN, 0),
    )


@router.get("/models", response_model=Page[ModelRead])
async def list_models(session: SessionDep, admin: AdminDep) -> Page[ModelRead]:
    del admin
    models = (
        await session.scalars(
            select(ModelCatalog).order_by(ModelCatalog.public_name, ModelCatalog.route_order)
        )
    ).all()
    model_ids = [model.id for model in models]
    prices = (
        (
            await session.scalars(
                select(PriceVersion)
                .where(PriceVersion.model_id.in_(model_ids))
                .order_by(PriceVersion.effective_at, PriceVersion.id)
            )
        ).all()
        if model_ids
        else []
    )
    prices_by_model: dict[UUID, list[PriceVersionRead]] = {model_id: [] for model_id in model_ids}
    for price in prices:
        prices_by_model[price.model_id].append(
            PriceVersionRead(
                id=price.id,
                input_micro_cny_per_million=price.input_micro_cny_per_million,
                output_micro_cny_per_million=price.output_micro_cny_per_million,
                effective_at=price.effective_at,
            )
        )
    items = [
        ModelRead(
            id=model.id,
            public_name=model.public_name,
            litellm_model=model.litellm_model,
            route_order=model.route_order,
            active=model.active,
            created_at=model.created_at,
            prices=prices_by_model[model.id],
        )
        for model in models
    ]
    return Page(items=items, next_cursor=None, has_more=False)


@router.get("/ledger/transfers", response_model=Page[LedgerTransferRead])
async def list_ledger_transfers(
    session: SessionDep,
    admin: AdminDep,
    tenant_id: Annotated[UUID | None, Query()] = None,
    kind: Annotated[TransferKind | None, Query()] = None,
    from_at: FromAt = None,
    to_at: ToAt = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[LedgerTransferRead]:
    del admin
    _validate_time_range(from_at, to_at)
    statement = select(LedgerTransfer)
    if tenant_id is not None:
        tenant_entry = (
            select(LedgerEntry.id)
            .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
            .where(
                LedgerEntry.transfer_id == LedgerTransfer.id,
                LedgerAccount.tenant_id == tenant_id,
            )
        )
        statement = statement.where(exists(tenant_entry))
    if kind is not None:
        statement = statement.where(LedgerTransfer.kind == kind)
    if from_at is not None:
        statement = statement.where(LedgerTransfer.created_at >= from_at)
    if to_at is not None:
        statement = statement.where(LedgerTransfer.created_at <= to_at)
    decoded = _decode_cursor(cursor)
    if decoded is not None:
        statement = statement.where(
            _after_desc_cursor(LedgerTransfer.created_at, LedgerTransfer.id, decoded)
        )
    transfers = (
        await session.scalars(
            statement.order_by(LedgerTransfer.created_at.desc(), LedgerTransfer.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    has_more = len(transfers) > limit
    visible = transfers[:limit]
    transfer_ids = [transfer.id for transfer in visible]
    entry_rows = (
        (
            await session.execute(
                select(LedgerEntry, LedgerAccount)
                .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
                .where(LedgerEntry.transfer_id.in_(transfer_ids))
                .order_by(LedgerEntry.created_at, LedgerEntry.id)
            )
        ).all()
        if transfer_ids
        else []
    )
    entries_by_transfer: dict[UUID, list[LedgerEntryRead]] = {
        transfer_id: [] for transfer_id in transfer_ids
    }
    for entry, account in entry_rows:
        entries_by_transfer[entry.transfer_id].append(
            LedgerEntryRead(
                account_kind=account.kind,
                account_scope=account.scope,
                amount_micro_cny=entry.amount_micro_cny,
            )
        )
    reversed_rows = (
        (
            await session.execute(
                select(LedgerTransfer.reversal_of_id, LedgerTransfer.id).where(
                    LedgerTransfer.reversal_of_id.in_(transfer_ids)
                )
            )
        ).all()
        if transfer_ids
        else []
    )
    reversed_by = {original_id: reversal_id for original_id, reversal_id in reversed_rows}
    items = []
    for transfer in visible:
        reversed_by_id = reversed_by.get(transfer.id)
        reversal = (
            LedgerReversalRead(
                reversal_of_id=transfer.reversal_of_id,
                reversed_by_id=reversed_by_id,
            )
            if transfer.reversal_of_id is not None or reversed_by_id is not None
            else None
        )
        items.append(
            LedgerTransferRead(
                transfer=LedgerTransferSummary(
                    id=transfer.id,
                    kind=transfer.kind,
                    external_ref=transfer.external_ref,
                    posted=transfer.posted,
                    created_at=transfer.created_at,
                ),
                reversal=reversal,
                entries=entries_by_transfer[transfer.id],
            )
        )
    next_cursor = (
        _encode_cursor(visible[-1].created_at, visible[-1].id)
        if has_more and visible
        else None
    )
    return Page(items=items, next_cursor=next_cursor, has_more=has_more)


@router.get("/requests", response_model=Page[GatewayRequestRead])
async def list_requests(
    session: SessionDep,
    admin: AdminDep,
    tenant_id: Annotated[UUID | None, Query()] = None,
    status: Annotated[str | None, Query(min_length=1, max_length=40)] = None,
    model: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    from_at: FromAt = None,
    to_at: ToAt = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[GatewayRequestRead]:
    del admin
    _validate_time_range(from_at, to_at)
    statement = (
        select(GatewayRequest, Tenant, Reservation)
        .join(Tenant, Tenant.id == GatewayRequest.tenant_id)
        .outerjoin(Reservation, Reservation.gateway_request_id == GatewayRequest.id)
    )
    if tenant_id is not None:
        statement = statement.where(GatewayRequest.tenant_id == tenant_id)
    if status is not None:
        statement = statement.where(GatewayRequest.status == status)
    if model is not None:
        statement = statement.where(GatewayRequest.model == model)
    if from_at is not None:
        statement = statement.where(GatewayRequest.created_at >= from_at)
    if to_at is not None:
        statement = statement.where(GatewayRequest.created_at <= to_at)
    decoded = _decode_cursor(cursor)
    if decoded is not None:
        statement = statement.where(
            _after_desc_cursor(GatewayRequest.created_at, GatewayRequest.id, decoded)
        )
    rows = (
        await session.execute(
            statement.order_by(GatewayRequest.created_at.desc(), GatewayRequest.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    has_more = len(rows) > limit
    visible = rows[:limit]
    items = []
    for request, tenant, reservation in visible:
        reservation_summary = (
            ReservationSummary(
                id=reservation.id,
                amount_micro_cny=reservation.amount_micro_cny,
                status=reservation.status,
                selected_price_version_id=reservation.selected_price_version_id,
                expires_at=reservation.expires_at,
            )
            if reservation is not None
            else None
        )
        items.append(
            GatewayRequestRead(
                id=request.id,
                tenant=TenantSummary(id=tenant.id, name=tenant.name),
                model=request.model,
                stream=request.stream,
                status=request.status,
                usage_json=_safe_usage(request.usage_json),
                error_code=request.error_code,
                created_at=request.created_at,
                reservation=reservation_summary,
            )
        )
    next_cursor = (
        _encode_cursor(visible[-1][0].created_at, visible[-1][0].id)
        if has_more and visible
        else None
    )
    return Page(items=items, next_cursor=next_cursor, has_more=has_more)


@router.get("/reservations", response_model=Page[ReservationRead])
async def list_reservations(
    session: SessionDep,
    admin: AdminDep,
    tenant_id: Annotated[UUID | None, Query()] = None,
    status: Annotated[ReservationStatus | None, Query()] = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[ReservationRead]:
    del admin
    statement = select(Reservation, PriceVersion).outerjoin(
        PriceVersion, PriceVersion.id == Reservation.selected_price_version_id
    )
    if tenant_id is not None:
        statement = statement.where(Reservation.tenant_id == tenant_id)
    if status is not None:
        statement = statement.where(Reservation.status == status)
    decoded = _decode_cursor(cursor)
    if decoded is not None:
        statement = statement.where(
            _after_desc_cursor(Reservation.created_at, Reservation.id, decoded)
        )
    rows = (
        await session.execute(
            statement.order_by(Reservation.created_at.desc(), Reservation.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    has_more = len(rows) > limit
    visible = rows[:limit]
    items = []
    for reservation, price in visible:
        selected_price = (
            PriceVersionRead(
                id=price.id,
                input_micro_cny_per_million=price.input_micro_cny_per_million,
                output_micro_cny_per_million=price.output_micro_cny_per_million,
                effective_at=price.effective_at,
            )
            if price is not None
            else None
        )
        items.append(
            ReservationRead(
                id=reservation.id,
                tenant_id=reservation.tenant_id,
                request_id=reservation.gateway_request_id,
                amount_micro_cny=reservation.amount_micro_cny,
                status=reservation.status,
                selected_price=selected_price,
                expires_at=reservation.expires_at,
                created_at=reservation.created_at,
            )
        )
    next_cursor = (
        _encode_cursor(visible[-1][0].created_at, visible[-1][0].id)
        if has_more and visible
        else None
    )
    return Page(items=items, next_cursor=next_cursor, has_more=has_more)


@router.post("/tenants")
async def create_tenant(
    body: TenantCreate, session: SessionDep, admin: AdminDep
) -> dict[str, str]:
    del admin
    conflict = HTTPException(
        status_code=409,
        detail="租户名称已存在",
        headers={"Cache-Control": "no-store"},
    )
    if await session.scalar(select(Tenant.id).where(Tenant.name == body.name)) is not None:
        await session.rollback()
        raise conflict
    tenant = Tenant(name=body.name)
    session.add(tenant)
    try:
        await session.flush()
        await create_tenant_accounts(session, tenant.id)
        await ensure_platform_accounts(session)
        session.add(AuditEvent(actor="admin", action="tenant.create", target=str(tenant.id)))
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if _is_tenant_name_conflict(exc):
            raise conflict from exc
        raise
    return {"id": str(tenant.id), "name": tenant.name}


@router.post("/tenants/{tenant_id}/keys", response_model=KeyCreated)
async def create_key(tenant_id: UUID, session: SessionDep, admin: AdminDep) -> KeyCreated:
    del admin
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    raw, prefix, digest = generate_api_key(settings.api_key_hmac_secret)
    key = ApiKey(tenant_id=tenant.id, prefix=prefix, key_hash=digest)
    session.add(key)
    session.add(AuditEvent(actor="admin", action="api_key.create", target=prefix))
    await session.commit()
    return KeyCreated(id=key.id, prefix=prefix, api_key=raw)


@router.post("/models")
async def create_model(
    body: ModelCreate, session: SessionDep, admin: AdminDep
) -> dict[str, str]:
    del admin
    model = ModelCatalog(**body.model_dump())
    session.add(model)
    session.add(AuditEvent(actor="admin", action="model.create", target=body.public_name))
    await session.commit()
    return {"id": str(model.id)}


@router.post("/prices")
async def create_price(
    body: PriceCreate, session: SessionDep, admin: AdminDep
) -> dict[str, str]:
    del admin
    price = PriceVersion(**body.model_dump())
    session.add(price)
    session.add(AuditEvent(actor="admin", action="price.create", target=str(body.model_id)))
    await session.commit()
    return {"id": str(price.id)}


@router.post("/ledger/recharge")
async def recharge(
    body: RechargeCreate, session: SessionDep, admin: AdminDep
) -> dict[str, str]:
    del admin
    await ensure_platform_accounts(session)
    transfer = await manual_recharge(
        session, body.tenant_id, body.amount_micro_cny, body.external_ref
    )
    session.add(AuditEvent(actor="admin", action="ledger.recharge", target=str(transfer.id)))
    await session.commit()
    return {"transfer_id": str(transfer.id)}


@router.post("/reservations/{reservation_id}/release")
async def release_reservation(
    reservation_id: UUID,
    body: ReservationAction,
    session: SessionDep,
    admin: AdminDep,
) -> dict[str, str]:
    del admin
    reservation = await release(
        session,
        reservation_id,
        body.external_ref,
        timeout_release=body.timeout_release,
    )
    session.add(
        AuditEvent(actor="admin", action="reservation.release", target=str(reservation.id))
    )
    await session.commit()
    return {"status": reservation.status.value}


@router.post("/ledger/reverse")
async def reverse(
    body: ReversalCreate, session: SessionDep, admin: AdminDep
) -> dict[str, str]:
    del admin
    transfer = await reverse_transfer(session, body.transfer_id, body.external_ref)
    session.add(AuditEvent(actor="admin", action="ledger.reverse", target=str(transfer.id)))
    await session.commit()
    return {"transfer_id": str(transfer.id)}
