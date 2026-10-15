from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import verify_admin_token, verify_api_key
from app.db.models import ApiKey, Tenant
from app.db.session import get_session
from app.infra.litellm_client import LiteLLMClient
from app.services.gateway import GatewayService

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def current_tenant(
    session: SessionDep, authorization: Annotated[str | None, Header()] = None
) -> Tenant:
    if authorization is None or not authorization.startswith("Bearer gw_"):
        raise HTTPException(status_code=401, detail="invalid bearer API key")
    raw_key = authorization.removeprefix("Bearer ")
    prefix = raw_key[:12]
    candidates = (
        await session.scalars(
            select(ApiKey).where(ApiKey.prefix == prefix, ApiKey.revoked.is_(False))
        )
    ).all()
    matched = next(
        (
            candidate
            for candidate in candidates
            if verify_api_key(raw_key, candidate.key_hash, settings.api_key_hmac_secret)
        ),
        None,
    )
    if matched is None:
        raise HTTPException(status_code=401, detail="invalid bearer API key")
    tenant = await session.get(Tenant, matched.tenant_id)
    if tenant is None or not tenant.active:
        raise HTTPException(status_code=403, detail="tenant is inactive")
    return tenant


async def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    if not verify_admin_token(x_admin_token, settings.admin_token):
        raise HTTPException(status_code=401, detail="invalid admin token")


def gateway_service(request: Request) -> GatewayService:
    return GatewayService(
        settings,
        request.app.state.rate_limiter,
        LiteLLMClient(settings.litellm_config_path),
    )

TenantDep = Annotated[Tenant, Depends(current_tenant)]
AdminDep = Annotated[None, Depends(require_admin)]
GatewayDep = Annotated[GatewayService, Depends(gateway_service)]
