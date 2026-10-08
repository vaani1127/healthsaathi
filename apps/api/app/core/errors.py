"""Errors as RFC 7807 problem+json."""

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var

logger = logging.getLogger(__name__)

PROBLEM_JSON = "application/problem+json"


class ProblemError(Exception):
    """Raise from services; turned into a problem+json response."""

    def __init__(
        self,
        status: int,
        code: str,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers = headers
        self.extra = extra


def problem_response(
    status: int,
    code: str,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://healthsaathi.dev/problems/{code}",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "code": code,
    }
    if detail:
        body["detail"] = detail
    request_id = request_id_var.get()
    if request_id:
        body["request_id"] = request_id
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(_: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(exc.status, exc.code, exc.detail, exc.headers, **exc.extra)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = HTTPStatus(exc.status_code).phrase.lower().replace(" ", "-")
        detail = exc.detail if isinstance(exc.detail, str) else None
        return problem_response(exc.status_code, code, detail, getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return problem_response(422, "validation-error", "Request is not valid.", errors=errors)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return problem_response(500, "internal-error")
