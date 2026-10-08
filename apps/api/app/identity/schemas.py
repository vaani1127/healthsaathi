import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.db.enums import Role

# Plain format check. Demo accounts use the reserved .test domain, which strict validators reject.
EmailStr = Annotated[str, Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    status: Literal["mfa_required", "mfa_enrollment_required"]
    challenge_token: str
    expires_at: datetime


class TotpVerifyRequest(BaseModel):
    challenge_token: str
    code: str = Field(min_length=6, max_length=8)


class TotpEnrollRequest(BaseModel):
    enroll_token: str


class TotpEnrollResponse(BaseModel):
    secret: str
    otpauth_uri: str


class TotpActivateRequest(BaseModel):
    enroll_token: str
    code: str = Field(min_length=6, max_length=8)


class EmailCodeRequest(BaseModel):
    email: EmailStr


class EmailCodeVerify(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_at: datetime
    session_id: uuid.UUID
    clinic_id: uuid.UUID | None = None
    role: Role | None = None


class SelectClinicRequest(BaseModel):
    clinic_id: uuid.UUID
    role: Role


class RefreshRequest(BaseModel):
    clinic_id: uuid.UUID | None = None
    role: Role | None = None


class MembershipOut(BaseModel):
    clinic_id: uuid.UUID
    clinic_name: str
    role: Role


class MeResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    name: str
    session_id: uuid.UUID
    clinic_id: uuid.UUID | None
    role: Role | None
    mfa: bool
    memberships: list[MembershipOut]


class SessionOut(BaseModel):
    id: uuid.UUID
    started_at: datetime
    last_used_at: datetime
    expires_at: datetime
    user_agent: str | None
    current: bool


class InviteRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=2, max_length=120)
    role: Role


class InviteResponse(BaseModel):
    user_id: uuid.UUID
    membership_id: uuid.UUID


class AcceptInviteRequest(BaseModel):
    email: EmailStr
    token: str = Field(min_length=10, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class StaffMemberOut(BaseModel):
    membership_id: uuid.UUID
    user_id: uuid.UUID
    name: str
    email: str
    role: Role
    is_active: bool
    mfa_enabled: bool


class MembershipUpdate(BaseModel):
    is_active: bool
