import uuid
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import ANONYMOUS, tenant_session
from app.core.errors import ProblemError, problem_response
from app.core.ratelimit import LOGIN, client_ip, limiter
from app.db.enums import Role
from app.db.models import User
from app.identity import schemas, service
from app.identity.deps import ClinicPrincipal, CurrentPrincipal, require_roles

router = APIRouter(prefix="/auth", tags=["auth"])
staff_router = APIRouter(prefix="/staff", tags=["staff"])

REFRESH_COOKIE = "hs_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"


def _device(request: Request) -> service.DeviceInfo:
    return service.DeviceInfo(
        fingerprint=request.headers.get("x-device-id"),
        user_agent=request.headers.get("user-agent"),
    )


def _set_refresh_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=settings.refresh_token_days * 86400,
        path=REFRESH_COOKIE_PATH,
        domain=settings.cookie_domain,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _clear_refresh_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        domain=settings.cookie_domain,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _token_response(response: Response, issued: service.IssuedTokens) -> schemas.TokenResponse:
    if issued.refresh_token:
        _set_refresh_cookie(response, issued.refresh_token)
    return schemas.TokenResponse(
        access_token=issued.access_token,
        expires_at=issued.access_expires_at,
        session_id=issued.session_id,
        clinic_id=issued.clinic_id,
        role=Role(issued.role) if issued.role else None,
    )


@router.post("/login", response_model=schemas.LoginResponse)
@limiter.limit(LOGIN, key_func=client_ip)
async def login(request: Request, body: schemas.LoginRequest) -> schemas.LoginResponse:
    challenge = await service.login_with_password(body.email, body.password)
    return schemas.LoginResponse(
        status=challenge.status,
        challenge_token=challenge.challenge_token,
        expires_at=challenge.expires_at,
    )


@router.post("/totp/verify", response_model=schemas.TokenResponse)
@limiter.limit(LOGIN, key_func=client_ip)
async def totp_verify(
    request: Request, response: Response, body: schemas.TotpVerifyRequest
) -> schemas.TokenResponse:
    issued = await service.verify_totp(body.challenge_token, body.code, _device(request))
    return _token_response(response, issued)


@router.post("/totp/enroll", response_model=schemas.TotpEnrollResponse)
async def totp_enroll(body: schemas.TotpEnrollRequest) -> schemas.TotpEnrollResponse:
    secret, uri = await service.start_totp_enrolment(body.enroll_token)
    return schemas.TotpEnrollResponse(secret=secret, otpauth_uri=uri)


@router.post("/totp/activate", response_model=schemas.TokenResponse)
async def totp_activate(
    request: Request, response: Response, body: schemas.TotpActivateRequest
) -> schemas.TokenResponse:
    issued = await service.activate_totp(body.enroll_token, body.code, _device(request))
    return _token_response(response, issued)


@router.post("/otp/request", status_code=status.HTTP_202_ACCEPTED)
async def email_code_request(body: schemas.EmailCodeRequest) -> dict[str, str]:
    await service.request_email_code(body.email)
    return {"status": "sent_if_registered"}


@router.post("/otp/verify", response_model=schemas.TokenResponse)
@limiter.limit(LOGIN, key_func=client_ip)
async def email_code_verify(
    request: Request, response: Response, body: schemas.EmailCodeVerify
) -> schemas.TokenResponse:
    issued = await service.verify_email_code(body.email, body.code, _device(request))
    return _token_response(response, issued)


@router.post("/refresh", response_model=schemas.TokenResponse)
async def refresh(
    response: Response,
    body: schemas.RefreshRequest | None = None,
    hs_refresh: Annotated[str | None, Cookie()] = None,
) -> schemas.TokenResponse | JSONResponse:
    if not hs_refresh:
        raise ProblemError(401, "no-refresh-token", "Sign in again.")
    body = body or schemas.RefreshRequest()
    try:
        issued = await service.refresh(hs_refresh, body.clinic_id, body.role)
    except ProblemError as exc:
        if exc.status != 401:
            raise
        failed = problem_response(exc.status, exc.code, exc.detail)
        _clear_refresh_cookie(failed)
        return failed
    return _token_response(response, issued)


@router.post("/select-clinic", response_model=schemas.TokenResponse)
async def select_clinic(
    principal: CurrentPrincipal, body: schemas.SelectClinicRequest, response: Response
) -> schemas.TokenResponse:
    issued = await service.select_clinic(
        principal.user_id, principal.session_id, list(principal.amr), body.clinic_id, body.role
    )
    return _token_response(response, issued)


@router.get("/me", response_model=schemas.MeResponse)
async def me(principal: CurrentPrincipal) -> schemas.MeResponse:
    async with tenant_session(ANONYMOUS) as db:
        user = await db.scalar(select(User).where(User.id == principal.user_id))
    if user is None:
        raise ProblemError(401, "invalid-token")
    memberships = await service.active_memberships(principal.user_id)
    return schemas.MeResponse(
        user_id=user.id,
        email=user.email,
        name=user.name,
        session_id=principal.session_id,
        clinic_id=principal.clinic_id,
        role=principal.role,
        mfa=principal.mfa,
        memberships=[schemas.MembershipOut(**m.__dict__) for m in memberships],
    )


@router.get("/sessions", response_model=list[schemas.SessionOut])
async def list_sessions(principal: CurrentPrincipal) -> list[schemas.SessionOut]:
    return [
        schemas.SessionOut(**s.__dict__, current=s.id == principal.session_id)
        for s in await service.list_sessions(principal.user_id)
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(principal: CurrentPrincipal, session_id: uuid.UUID) -> Response:
    if not await service.revoke_session(principal.user_id, session_id):
        raise ProblemError(404, "session-not-found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(principal: CurrentPrincipal) -> Response:
    await service.revoke_session(principal.user_id, principal.session_id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(response)
    return response


@router.post("/invite/accept", status_code=status.HTTP_204_NO_CONTENT)
async def accept_invite(body: schemas.AcceptInviteRequest) -> Response:
    await service.accept_invite(body.email, body.token, body.password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@staff_router.post(
    "/invites", response_model=schemas.InviteResponse, status_code=status.HTTP_201_CREATED
)
async def invite_staff(
    principal: Annotated[ClinicPrincipal, Depends(require_roles(Role.CLINIC_ADMIN))],
    body: schemas.InviteRequest,
) -> schemas.InviteResponse:
    user_id, membership_id = await service.invite_staff(
        principal.tenant(), body.email, body.name, body.role
    )
    return schemas.InviteResponse(user_id=user_id, membership_id=membership_id)


@staff_router.get("/members", response_model=list[schemas.StaffMemberOut])
async def list_staff(
    principal: Annotated[ClinicPrincipal, Depends(require_roles(Role.CLINIC_ADMIN))],
) -> list[schemas.StaffMemberOut]:
    rows = await service.list_staff(principal.tenant())
    return [schemas.StaffMemberOut(**r.__dict__) for r in rows]


@staff_router.patch("/members/{membership_id}", status_code=status.HTTP_204_NO_CONTENT)
async def update_membership(
    principal: Annotated[ClinicPrincipal, Depends(require_roles(Role.CLINIC_ADMIN))],
    membership_id: uuid.UUID,
    body: schemas.MembershipUpdate,
) -> Response:
    await service.set_membership_active(principal.tenant(), membership_id, body.is_active)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
