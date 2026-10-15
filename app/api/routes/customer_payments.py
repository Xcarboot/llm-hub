from collections.abc import Awaitable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.customer_deps import NoStoreRoute, PortalDep, PortalWriteDep
from app.api.deps import AdminDep, SessionDep
from app.core.config import settings
from app.core.errors import GatewayError
from app.db.models import PaymentOrder, RefundRequest
from app.schemas.commerce import PaymentCreate, RefundCreate, RefundDecision
from app.services import customer_payments as payments

router = APIRouter(tags=["customer-payments"], route_class=NoStoreRoute)
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(min_length=1, max_length=1024)]
Status = Annotated[str | None, Query(min_length=1, max_length=32)]


async def _run(
    session: AsyncSession, work: Awaitable[dict[str, Any]], *, write: bool = False
) -> dict[str, Any]:
    try:
        result = await work
        if write:
            await session.commit()
        return result
    except GatewayError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=exc.status_code, detail=exc.message, headers={"Cache-Control": "no-store"}
        ) from exc
    except BaseException:
        await session.rollback()
        raise


async def _payment_result(session: AsyncSession, work: Awaitable[PaymentOrder]) -> dict[str, Any]:
    row = await work
    return await payments.get_payment(session, row.tenant_id, row.id)


async def _refund_result(session: AsyncSession, work: Awaitable[RefundRequest]) -> dict[str, Any]:
    row = await work
    return await payments.get_refund(session, row.tenant_id, row.id)


@router.get("/portal/payments/config")
async def portal_payment_config(identity: PortalDep) -> dict[str, Any]:
    del identity
    return payments.payment_config(settings)


@router.post("/portal/payments")
async def portal_create_payment(
    body: PaymentCreate, session: SessionDep, identity: PortalWriteDep
) -> dict[str, Any]:
    return await _run(
        session,
        _payment_result(
            session,
            payments.create_payment(
                session,
                identity.tenant.id,
                body.amount_micro_cny,
                body.idempotency_key,
                settings,
                actor=f"customer:{identity.user.id}",
            ),
        ),
        write=True,
    )


@router.get("/portal/payments")
async def portal_payments(
    session: SessionDep, identity: PortalDep, limit: Limit = 50, cursor: Cursor = None
) -> dict[str, Any]:
    return await _run(
        session,
        payments.list_records(
            session,
            tenant_id=identity.tenant.id,
            limit=limit,
            cursor=cursor,
        ),
    )


@router.get("/portal/payments/{order_id}")
async def portal_payment(
    order_id: UUID, session: SessionDep, identity: PortalDep
) -> dict[str, Any]:
    return await _run(session, payments.get_payment(session, identity.tenant.id, order_id))


@router.post("/portal/payments/{order_id}/mock-confirm")
async def portal_confirm_payment(
    order_id: UUID, session: SessionDep, identity: PortalWriteDep
) -> dict[str, Any]:
    return await _run(
        session,
        _payment_result(
            session,
            payments.confirm_mock_payment(
                session,
                identity.tenant.id,
                order_id,
                settings,
                actor=f"customer:{identity.user.id}",
            ),
        ),
        write=True,
    )


@router.post("/portal/refunds")
async def portal_create_refund(
    body: RefundCreate, session: SessionDep, identity: PortalWriteDep
) -> dict[str, Any]:
    return await _run(
        session,
        _refund_result(
            session,
            payments.create_refund(
                session,
                identity.tenant.id,
                body.payment_order_id,
                body.amount_micro_cny,
                body.reason,
                body.idempotency_key,
                settings,
                actor=f"customer:{identity.user.id}",
            ),
        ),
        write=True,
    )


@router.get("/portal/refunds")
async def portal_refunds(
    session: SessionDep, identity: PortalDep, limit: Limit = 50, cursor: Cursor = None
) -> dict[str, Any]:
    return await _run(
        session,
        payments.list_records(
            session,
            refunds=True,
            tenant_id=identity.tenant.id,
            limit=limit,
            cursor=cursor,
        ),
    )


@router.get("/portal/refunds/{refund_id}")
async def portal_refund(
    refund_id: UUID, session: SessionDep, identity: PortalDep
) -> dict[str, Any]:
    return await _run(session, payments.get_refund(session, identity.tenant.id, refund_id))


@router.get("/admin/payments")
async def admin_payments(
    session: SessionDep,
    admin: AdminDep,
    tenant_id: Annotated[UUID | None, Query()] = None,
    status: Status = None,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> dict[str, Any]:
    del admin
    return await _run(
        session,
        payments.list_records(
            session,
            admin=True,
            tenant_id=tenant_id,
            status=status,
            limit=limit,
            cursor=cursor,
        ),
    )


@router.get("/admin/refunds")
async def admin_refunds(
    session: SessionDep,
    admin: AdminDep,
    tenant_id: Annotated[UUID | None, Query()] = None,
    status: Status = None,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> dict[str, Any]:
    del admin
    return await _run(
        session,
        payments.list_records(
            session,
            refunds=True,
            admin=True,
            tenant_id=tenant_id,
            status=status,
            limit=limit,
            cursor=cursor,
        ),
    )


@router.post("/admin/refunds/{refund_id}/approve")
async def admin_approve_refund(
    refund_id: UUID, body: RefundDecision, session: SessionDep, admin: AdminDep
) -> dict[str, Any]:
    del admin
    return await _run(
        session,
        _refund_result(
            session,
            payments.approve_refund(
                session,
                refund_id,
                body.reason,
                settings,
            ),
        ),
        write=True,
    )


@router.post("/admin/refunds/{refund_id}/reject")
async def admin_reject_refund(
    refund_id: UUID, body: RefundDecision, session: SessionDep, admin: AdminDep
) -> dict[str, Any]:
    del admin
    return await _run(
        session,
        _refund_result(
            session,
            payments.reject_refund(
                session,
                refund_id,
                body.reason,
            ),
        ),
        write=True,
    )
