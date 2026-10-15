import asyncio
from datetime import UTC, datetime

import typer
from sqlalchemy import select

from app.core.config import settings
from app.core.security import generate_api_key
from app.db.models import ApiKey, ModelCatalog, PriceVersion, Tenant
from app.db.session import SessionLocal
from app.services.ledger import create_tenant_accounts, ensure_platform_accounts, manual_recharge

app = typer.Typer(no_args_is_help=True)


async def _bootstrap() -> None:
    async with SessionLocal() as session:
        if await session.scalar(select(Tenant).where(Tenant.name == "example-tenant")):
            raise RuntimeError("example tenant already exists; bootstrap is intentionally one-shot")
        tenant = Tenant(name="example-tenant")
        session.add(tenant)
        await session.flush()
        await create_tenant_accounts(session, tenant.id)
        await ensure_platform_accounts(session)
        primary = ModelCatalog(
            public_name="enterprise-chat",
            litellm_model="primary",
            route_order=0,
        )
        fallback = ModelCatalog(
            public_name="enterprise-chat",
            litellm_model="fallback",
            route_order=1,
        )
        session.add_all([primary, fallback])
        await session.flush()
        now = datetime.now(UTC)
        session.add_all(
            [
                PriceVersion(
                    model_id=primary.id,
                    input_micro_cny_per_million=2_000_000,
                    output_micro_cny_per_million=8_000_000,
                    effective_at=now,
                ),
                PriceVersion(
                    model_id=fallback.id,
                    input_micro_cny_per_million=1_500_000,
                    output_micro_cny_per_million=6_000_000,
                    effective_at=now,
                ),
            ]
        )
        raw, prefix, digest = generate_api_key(settings.api_key_hmac_secret)
        session.add(ApiKey(tenant_id=tenant.id, prefix=prefix, key_hash=digest))
        await session.commit()
        typer.echo(f"tenant_id={tenant.id}")
        typer.echo(f"api_key={raw}")
        typer.echo("This plaintext key is shown once; store it securely.")


@app.command()
def bootstrap() -> None:
    """Create one example tenant, primary/fallback routes, prices, accounts, and key."""
    asyncio.run(_bootstrap())


async def _recharge(tenant_id: str, amount: int, external_ref: str) -> None:
    from uuid import UUID

    async with SessionLocal() as session:
        await ensure_platform_accounts(session)
        transfer = await manual_recharge(session, UUID(tenant_id), amount, external_ref)
        await session.commit()
        typer.echo(f"transfer_id={transfer.id}")


@app.command()
def recharge(
    tenant_id: str,
    amount_micro_cny: int,
    external_ref: str,
) -> None:
    """Manually credit a tenant in integer micro-CNY."""
    asyncio.run(_recharge(tenant_id, amount_micro_cny, external_ref))


if __name__ == "__main__":
    app()
