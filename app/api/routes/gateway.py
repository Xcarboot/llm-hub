import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import GatewayDep, SessionDep, TenantDep
from app.core.responses import ClosingStreamingResponse
from app.db.models import ModelCatalog
from app.schemas.gateway import ChatCompletionRequest, ModelItem, ModelList
from app.services.idempotency import claim_request

router = APIRouter(prefix="/v1", tags=["openai-compatible"])


@router.get("/models", response_model=ModelList)
async def models(session: SessionDep, tenant: TenantDep) -> ModelList:
    del tenant
    names = (
        await session.scalars(
            select(ModelCatalog.public_name)
            .where(ModelCatalog.active.is_(True), ModelCatalog.route_order == 0)
            .order_by(ModelCatalog.public_name)
        )
    ).all()
    return ModelList(data=[ModelItem(id=name) for name in names])


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest,
    session: SessionDep,
    tenant: TenantDep,
    gateway: GatewayDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> JSONResponse | ClosingStreamingResponse:
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    payload_hash = hashlib.sha256(canonical.encode()).hexdigest()
    try:
        claim = await claim_request(
            session,
            tenant.id,
            idempotency_key,
            payload_hash,
            body.model,
            body.stream,
        )
    except IntegrityError:
        await session.rollback()
        claim = await claim_request(
            session,
            tenant.id,
            idempotency_key,
            payload_hash,
            body.model,
            body.stream,
        )
    if claim.replay_response is not None:
        await session.rollback()
        return JSONResponse(claim.replay_response)
    if body.stream:
        stream = await gateway.prepare_stream(session, tenant.id, claim, body)
        return ClosingStreamingResponse(stream, media_type="text/event-stream")
    response = await gateway.complete_nonstream(session, tenant.id, claim, body)
    return JSONResponse(response)
