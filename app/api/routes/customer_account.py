from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Response
from sqlalchemy import String, and_, case, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.customer_deps import NoStoreRoute, PortalDep, PortalWriteDep
from app.api.deps import AdminDep, SessionDep
from app.api.routes.admin import (
    Cursor,
    Limit,
    _after_desc_cursor,
    _decode_cursor,
    _encode_cursor,
    _safe_usage,
)
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
)
from app.schemas.admin import (
    ApiKeyRead,
    GatewayRequestRead,
    KeyCreated,
    LedgerEntryRead,
    LedgerReversalRead,
    LedgerTransferRead,
    LedgerTransferSummary,
    ModelRead,
    Page,
    PriceVersionRead,
    ReservationSummary,
    TenantSummary,
)
from app.services.customer_auth import failure

router = APIRouter(tags=["customer-account"], route_class=NoStoreRoute)


def _cursor_filter(
    session: AsyncSession, created_at: Any, item_id: Any, cursor: tuple[datetime, UUID]
) -> Any:
    if session.get_bind().dialect.name != "sqlite":
        return _after_desc_cursor(created_at, item_id, cursor)
    # SQLite CURRENT_TIMESTAMP omits .000000, unlike bound DateTime values.
    # Normalize text without truncating subsecond precision or losing ID tie-breaking.
    text = cast(created_at, String)
    normalized = case((func.instr(text, ".") == 0, text + ".000000"), else_=text)
    at, cursor_id = cursor
    bound = at.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")
    return or_(normalized < bound, and_(normalized == bound, item_id < cursor_id))


@router.get("/portal/balance")
async def portal_balance(
    response: Response, session: SessionDep, identity: PortalDep
) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    rows = (
        await session.execute(
            select(LedgerAccount.kind, func.sum(LedgerEntry.amount_micro_cny))
            .join(LedgerEntry, LedgerEntry.account_id == LedgerAccount.id)
            .join(LedgerTransfer, LedgerTransfer.id == LedgerEntry.transfer_id)
            .where(LedgerAccount.tenant_id == identity.tenant.id, LedgerTransfer.posted.is_(True))
            .group_by(LedgerAccount.kind)
        )
    ).all()
    balances = {kind: int(amount) for kind, amount in rows}
    available = balances.get(AccountKind.AVAILABLE, 0)
    frozen = balances.get(AccountKind.FROZEN, 0)
    refund = balances.get(AccountKind.REFUND_FROZEN, 0)
    return {
        "available_micro_cny": str(available),
        "frozen_micro_cny": str(frozen),
        "refund_frozen_micro_cny": str(refund),
        "total_micro_cny": str(available + frozen + refund),
    }


@router.get("/portal/keys", response_model=Page[ApiKeyRead])
async def portal_keys(
    response: Response,
    session: SessionDep,
    identity: PortalDep,
    limit: Limit = 25,
    cursor: Cursor = None,
) -> Page[ApiKeyRead]:
    response.headers["Cache-Control"] = "no-store"
    statement = select(ApiKey).where(ApiKey.tenant_id == identity.tenant.id)
    decoded = _decode_cursor(cursor)
    if decoded:
        statement = statement.where(_cursor_filter(session, ApiKey.created_at, ApiKey.id, decoded))
    keys = (
        await session.scalars(
            statement.order_by(ApiKey.created_at.desc(), ApiKey.id.desc()).limit(limit + 1)
        )
    ).all()
    visible = keys[:limit]
    has_more = len(keys) > limit
    return Page(
        items=[
            ApiKeyRead(id=k.id, prefix=k.prefix, revoked=k.revoked, created_at=k.created_at)
            for k in visible
        ],
        next_cursor=_encode_cursor(visible[-1].created_at, visible[-1].id) if has_more else None,
        has_more=has_more,
    )


@router.get("/portal/keys/{key_id}", response_model=ApiKeyRead)
async def portal_get_key(
    key_id: UUID, response: Response, session: SessionDep, identity: PortalDep
) -> ApiKeyRead:
    response.headers["Cache-Control"] = "no-store"
    key = await session.scalar(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.tenant_id == identity.tenant.id,
        )
    )
    if key is None:
        raise failure(404, "API key not found")
    return ApiKeyRead(id=key.id, prefix=key.prefix, revoked=key.revoked, created_at=key.created_at)


@router.post("/portal/keys", response_model=KeyCreated)
async def portal_create_key(
    response: Response, session: SessionDep, identity: PortalWriteDep
) -> KeyCreated:
    raw, prefix, key_hash = generate_api_key(settings.api_key_hmac_secret)
    key = ApiKey(tenant_id=identity.tenant.id, prefix=prefix, key_hash=key_hash)
    session.add(key)
    session.add(
        AuditEvent(
            actor="customer:" + str(identity.user.id), action="api_key.create", target=prefix
        )
    )
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return KeyCreated(id=key.id, prefix=prefix, api_key=raw)


async def _revoke_key(
    session: AsyncSession, tenant_id: UUID, key_id: UUID, actor: str
) -> ApiKeyRead:
    key = await session.scalar(
        select(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if key is None:
        raise failure(404, "API key not found")
    if not key.revoked:
        key.revoked = True
        session.add(AuditEvent(actor=actor, action="api_key.revoke", target=key.prefix))
    await session.commit()
    return ApiKeyRead(id=key.id, prefix=key.prefix, revoked=key.revoked, created_at=key.created_at)


@router.post("/portal/keys/{key_id}/revoke", response_model=ApiKeyRead)
async def portal_revoke_key(
    key_id: UUID, response: Response, session: SessionDep, identity: PortalWriteDep
) -> ApiKeyRead:
    response.headers["Cache-Control"] = "no-store"
    return await _revoke_key(
        session, identity.tenant.id, key_id, "customer:" + str(identity.user.id)
    )


@router.post("/admin/tenants/{tenant_id}/keys/{key_id}/revoke", response_model=ApiKeyRead)
async def admin_revoke_key(
    tenant_id: UUID, key_id: UUID, response: Response, session: SessionDep, admin: AdminDep
) -> ApiKeyRead:
    del admin
    response.headers["Cache-Control"] = "no-store"
    return await _revoke_key(session, tenant_id, key_id, "admin")


@router.get("/portal/requests", response_model=Page[GatewayRequestRead])
async def portal_requests(
    response: Response,
    session: SessionDep,
    identity: PortalDep,
    status: Annotated[str | None, Query(min_length=1, max_length=40)] = None,
    model: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    limit: Limit = 25,
    cursor: Cursor = None,
) -> Page[GatewayRequestRead]:
    response.headers["Cache-Control"] = "no-store"
    statement = (
        select(GatewayRequest, Reservation)
        .outerjoin(
            Reservation,
            and_(
                Reservation.gateway_request_id == GatewayRequest.id,
                Reservation.tenant_id == identity.tenant.id,
            ),
        )
        .where(GatewayRequest.tenant_id == identity.tenant.id)
    )
    if status is not None:
        statement = statement.where(GatewayRequest.status == status)
    if model is not None:
        statement = statement.where(GatewayRequest.model == model)
    decoded = _decode_cursor(cursor)
    if decoded:
        statement = statement.where(
            _cursor_filter(session, GatewayRequest.created_at, GatewayRequest.id, decoded)
        )
    rows = (
        await session.execute(
            statement.order_by(GatewayRequest.created_at.desc(), GatewayRequest.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    visible = rows[:limit]
    items = []
    for request, reservation in visible:
        summary = (
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
                tenant=TenantSummary(id=identity.tenant.id, name=identity.tenant.name),
                model=request.model,
                stream=request.stream,
                status=request.status,
                usage_json=_safe_usage(request.usage_json),
                error_code=request.error_code,
                created_at=request.created_at,
                reservation=summary,
            )
        )
    has_more = len(rows) > limit
    return Page(
        items=items,
        has_more=has_more,
        next_cursor=_encode_cursor(visible[-1][0].created_at, visible[-1][0].id)
        if has_more
        else None,
    )


@router.get("/portal/ledger/transfers", response_model=Page[LedgerTransferRead])
async def portal_transfers(
    response: Response,
    session: SessionDep,
    identity: PortalDep,
    limit: Limit = 25,
    cursor: Cursor = None,
) -> Page[LedgerTransferRead]:
    response.headers["Cache-Control"] = "no-store"
    own_entry = (
        select(LedgerEntry.id)
        .join(LedgerAccount)
        .where(
            LedgerEntry.transfer_id == LedgerTransfer.id,
            LedgerAccount.tenant_id == identity.tenant.id,
        )
    )
    statement = select(LedgerTransfer).where(exists(own_entry), LedgerTransfer.posted.is_(True))
    decoded = _decode_cursor(cursor)
    if decoded:
        statement = statement.where(
            _cursor_filter(session, LedgerTransfer.created_at, LedgerTransfer.id, decoded)
        )
    transfers = (
        await session.scalars(
            statement.order_by(LedgerTransfer.created_at.desc(), LedgerTransfer.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    visible = transfers[:limit]
    ids = [transfer.id for transfer in visible]
    entries: dict[UUID, list[LedgerEntryRead]] = {item_id: [] for item_id in ids}
    if ids:
        rows = (
            await session.execute(
                select(LedgerEntry, LedgerAccount)
                .join(LedgerAccount)
                .where(
                    LedgerEntry.transfer_id.in_(ids), LedgerAccount.tenant_id == identity.tenant.id
                )
                .order_by(LedgerEntry.created_at, LedgerEntry.id)
            )
        ).all()
        for entry, account in rows:
            entries[entry.transfer_id].append(
                LedgerEntryRead(
                    account_kind=account.kind,
                    account_scope=account.scope,
                    amount_micro_cny=entry.amount_micro_cny,
                )
            )
    has_more = len(transfers) > limit
    return Page(
        items=[
            LedgerTransferRead(
                transfer=LedgerTransferSummary(
                    id=t.id,
                    kind=t.kind,
                    external_ref=t.external_ref,
                    posted=t.posted,
                    created_at=t.created_at,
                ),
                reversal=LedgerReversalRead(reversal_of_id=t.reversal_of_id, reversed_by_id=None)
                if t.reversal_of_id
                else None,
                entries=entries[t.id],
            )
            for t in visible
        ],
        next_cursor=_encode_cursor(visible[-1].created_at, visible[-1].id) if has_more else None,
        has_more=has_more,
    )


@router.get("/portal/models", response_model=Page[ModelRead])
async def portal_models(
    response: Response,
    session: SessionDep,
    identity: PortalDep,
    limit: Limit = 25,
    cursor: Cursor = None,
) -> Page[ModelRead]:
    del identity
    response.headers["Cache-Control"] = "no-store"
    statement = select(ModelCatalog).where(ModelCatalog.active.is_(True))
    decoded = _decode_cursor(cursor)
    if decoded:
        statement = statement.where(
            _cursor_filter(session, ModelCatalog.created_at, ModelCatalog.id, decoded)
        )
    models = (
        await session.scalars(
            statement.order_by(ModelCatalog.created_at.desc(), ModelCatalog.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    visible = models[:limit]
    ids = [m.id for m in visible]
    prices: dict[UUID, list[PriceVersionRead]] = {item_id: [] for item_id in ids}
    if ids:
        for p in (
            await session.scalars(
                select(PriceVersion)
                .where(PriceVersion.model_id.in_(ids))
                .order_by(PriceVersion.effective_at, PriceVersion.id)
            )
        ).all():
            prices[p.model_id].append(
                PriceVersionRead(
                    id=p.id,
                    input_micro_cny_per_million=p.input_micro_cny_per_million,
                    output_micro_cny_per_million=p.output_micro_cny_per_million,
                    effective_at=p.effective_at,
                )
            )
    has_more = len(models) > limit
    return Page(
        items=[
            ModelRead(
                id=m.id,
                public_name=m.public_name,
                litellm_model=m.litellm_model,
                route_order=m.route_order,
                active=m.active,
                created_at=m.created_at,
                prices=prices[m.id],
            )
            for m in visible
        ],
        next_cursor=_encode_cursor(visible[-1].created_at, visible[-1].id) if has_more else None,
        has_more=has_more,
    )
