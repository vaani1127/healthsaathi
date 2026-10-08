import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.core.db import get_session

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


class ReadyResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    database: Literal["ok", "unavailable"]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={503: {"model": ReadyResponse}},
)
async def ready(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReadyResponse | JSONResponse:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("database check failed")
        body = ReadyResponse(status="unavailable", database="unavailable")
        return JSONResponse(status_code=503, content=body.model_dump())
    return ReadyResponse(status="ok", database="ok")
