from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from hmac import compare_digest
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.api.deps import SessionDep
from app.db.models import CustomerSession, CustomerUser, Tenant
from app.services.customer_auth import COOKIE_NAME, check_origin, csrf_token, digest, failure


class NoStoreRoute(APIRoute):
    """Cover both successful responses and authentication/authorization errors."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def no_store(request: Request) -> Response:
            try:
                response = await handler(request)
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
                raise
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store


@dataclass(frozen=True)
class PortalIdentity:
    user: CustomerUser
    tenant: Tenant
    session: CustomerSession


async def current_customer(request: Request, session: SessionDep) -> PortalIdentity:
    from datetime import UTC, datetime

    raw = request.cookies.get(COOKIE_NAME)
    if not raw or len(raw) > 512:
        raise failure(401, "invalid portal session")
    row = (
        await session.execute(
            select(CustomerSession, CustomerUser, Tenant)
            .join(CustomerUser, CustomerUser.id == CustomerSession.user_id)
            .join(Tenant, Tenant.id == CustomerUser.tenant_id)
            .where(
                CustomerSession.token_hash == digest(raw),
                CustomerSession.revoked.is_(False),
                CustomerSession.expires_at > datetime.now(UTC),
                CustomerUser.active.is_(True),
                Tenant.active.is_(True),
            )
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise failure(401, "invalid portal session")
    portal_session, user, tenant = row
    return PortalIdentity(user=user, tenant=tenant, session=portal_session)


PortalDep = Annotated[PortalIdentity, Depends(current_customer)]


async def require_customer_write(
    request: Request, session: SessionDep, identity: PortalDep
) -> PortalIdentity:
    del session  # The dependency deliberately shares the request's transaction.
    check_origin(request)
    supplied = request.headers.get("x-csrf-token", "")
    raw = request.cookies.get(COOKIE_NAME, "")
    expected = csrf_token(raw)
    if (
        not supplied
        or not compare_digest(digest(supplied), identity.session.csrf_hash)
        or not compare_digest(digest(expected), identity.session.csrf_hash)
    ):
        raise failure(403, "invalid CSRF token")
    return identity


PortalWriteDep = Annotated[PortalIdentity, Depends(require_customer_write)]
