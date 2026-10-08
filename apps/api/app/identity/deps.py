import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.db import TenantContext
from app.core.errors import ProblemError
from app.db.enums import STAFF_ROLES, Role
from app.identity import service, tokens

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    session_id: uuid.UUID
    token_id: str
    amr: tuple[str, ...]
    clinic_id: uuid.UUID | None = None
    role: Role | None = None

    @property
    def mfa(self) -> bool:
        return "otp" in self.amr

    def tenant(self) -> TenantContext:
        return TenantContext(
            clinic_id=self.clinic_id,
            user_id=self.user_id,
            role=self.role.value if self.role else None,
        )


@dataclass(frozen=True)
class ClinicPrincipal(Principal):
    clinic_id: uuid.UUID
    role: Role


async def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ProblemError(401, "not-authenticated", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = tokens.decode_token(credentials.credentials, "access")
        principal = Principal(
            user_id=uuid.UUID(claims["sub"]),
            session_id=uuid.UUID(claims["sid"]),
            token_id=claims["jti"],
            amr=tuple(claims.get("amr") or ()),
            clinic_id=uuid.UUID(claims["clinic_id"]) if claims.get("clinic_id") else None,
            role=Role(claims["role"]) if claims.get("role") else None,
        )
    except (tokens.TokenError, KeyError, ValueError) as exc:
        raise ProblemError(
            401, "invalid-token", "Sign in again.", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    if not await service.session_is_active(principal.session_id):
        raise ProblemError(401, "session-ended", "This session was signed out.")
    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


async def get_clinic_principal(principal: CurrentPrincipal) -> ClinicPrincipal:
    if principal.clinic_id is None or principal.role is None:
        raise ProblemError(403, "clinic-not-selected", "Choose a clinic first.")
    if principal.role in STAFF_ROLES and not principal.mfa:
        raise ProblemError(403, "mfa-required")
    return ClinicPrincipal(
        user_id=principal.user_id,
        session_id=principal.session_id,
        token_id=principal.token_id,
        amr=principal.amr,
        clinic_id=principal.clinic_id,
        role=principal.role,
    )


CurrentClinicPrincipal = Annotated[ClinicPrincipal, Depends(get_clinic_principal)]


def require_roles(*roles: Role) -> Callable[[ClinicPrincipal], Awaitable[ClinicPrincipal]]:
    allowed = frozenset(roles)

    async def dependency(principal: CurrentClinicPrincipal) -> ClinicPrincipal:
        if principal.role not in allowed:
            raise ProblemError(403, "forbidden-role", "Your role cannot do this.")
        return principal

    return dependency


async def require_patient_data_access(principal: CurrentClinicPrincipal) -> ClinicPrincipal:
    """Gate for every route that returns patient data: staff must have passed TOTP."""
    if principal.role in STAFF_ROLES and not principal.mfa:
        raise ProblemError(403, "mfa-required")
    return principal


PatientDataPrincipal = Annotated[ClinicPrincipal, Depends(require_patient_data_access)]
