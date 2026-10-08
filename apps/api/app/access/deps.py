from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import RequestInfo, access_session
from app.core.logging import request_id_var
from app.identity.deps import ClinicPrincipal, CurrentClinicPrincipal


async def get_db(principal: CurrentClinicPrincipal) -> AsyncIterator[AsyncSession]:
    """Tenant session for the request. Commits on success; logs access denials on 403."""
    async with access_session(principal) as db:
        yield db


def request_info(request: Request) -> RequestInfo:
    return RequestInfo(
        request_id=request_id_var.get(),
        client_ip=request.client.host if request.client else None,
    )


# scope="function": commit (or log a denial) before the response is sent, so the client's next
# request always sees this request's writes.
Db = Annotated[AsyncSession, Depends(get_db, scope="function")]
ReqInfo = Annotated[RequestInfo, Depends(request_info)]
Staff = CurrentClinicPrincipal
__all__ = ["ClinicPrincipal", "Db", "ReqInfo", "Staff", "get_db", "request_info"]
