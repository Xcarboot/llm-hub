from uuid import UUID

from fastapi import APIRouter, Request, Response

from app.api.customer_deps import NoStoreRoute, PortalDep, PortalWriteDep
from app.api.deps import AdminDep, SessionDep
from app.core.config import settings
from app.db.models import AuditEvent, CustomerUser, Tenant
from app.schemas.commerce import AccountActivationCreate, ActivateBody, CustomerProfile, LoginBody
from app.services import customer_auth as auth

router = APIRouter(tags=["customer-auth"], route_class=NoStoreRoute)


def _profile(user: CustomerUser, tenant: Tenant, raw: str) -> CustomerProfile:
    return CustomerProfile(
        id=user.id,
        tenant_id=tenant.id,
        email=user.email,
        tenant_name=tenant.name,
        csrf_token=auth.csrf_token(raw),
    )


def _set_cookie(response: Response, raw: str) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        auth.COOKIE_NAME,
        raw,
        httponly=True,
        samesite="lax",
        path="/",
        secure=settings.customer_cookie_secure,
        max_age=settings.customer_session_ttl_seconds,
    )


@router.get("/portal/session", response_model=CustomerProfile)
async def portal_session(
    request: Request, response: Response, identity: PortalDep
) -> CustomerProfile:
    response.headers["Cache-Control"] = "no-store"
    return _profile(identity.user, identity.tenant, request.cookies[auth.COOKIE_NAME])


@router.post("/portal/login", response_model=CustomerProfile)
async def portal_login(
    body: LoginBody, request: Request, response: Response, session: SessionDep
) -> CustomerProfile:
    auth.check_origin(request)
    await auth.rate_limit(request, "email:" + body.email)
    user, tenant, raw = await auth.login(session, body.email, body.password)
    _set_cookie(response, raw)
    return _profile(user, tenant, raw)


@router.post("/portal/activate", response_model=CustomerProfile)
async def portal_activate(
    body: ActivateBody, request: Request, response: Response, session: SessionDep
) -> CustomerProfile:
    auth.check_origin(request)
    await auth.rate_limit(request, "activation:" + body.token[:16])
    user, tenant, raw = await auth.activate(session, body.token, body.password)
    _set_cookie(response, raw)
    return _profile(user, tenant, raw)


@router.post("/portal/logout", status_code=204)
async def portal_logout(response: Response, session: SessionDep, identity: PortalWriteDep) -> None:
    identity.session.revoked = True
    await session.commit()
    response.delete_cookie(
        auth.COOKIE_NAME,
        path="/",
        secure=settings.customer_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"


@router.post("/admin/tenants/{tenant_id}/customer-activation")
async def create_customer_activation(
    tenant_id: UUID,
    body: AccountActivationCreate,
    response: Response,
    session: SessionDep,
    admin: AdminDep,
) -> dict[str, str]:
    del admin
    url, expires_at, user = await auth.issue_activation(session, tenant_id, body.email)
    session.add(AuditEvent(actor="admin", action="customer.activation", target=str(user.id)))
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return {
        "activation_url": url,
        "expires_at": expires_at.isoformat(),
        "email": user.email,
        "tenant_id": str(user.tenant_id),
    }
