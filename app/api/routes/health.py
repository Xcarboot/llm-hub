from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from app.api.deps import SessionDep

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request, session: SessionDep) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
        if not await request.app.state.redis.ping():
            raise RuntimeError("redis ping failed")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="dependency unavailable") from exc
    return {"status": "ready"}
