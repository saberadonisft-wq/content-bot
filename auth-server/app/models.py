from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, EmailStr, Field

UserRole = Literal["admin", "user"]
UserStatus = Literal["pending", "approved", "rejected", "banned"]
AuthProvider = Literal["email", "google"]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, description="Password must be at least 8 characters")
    display_name: str = Field(..., min_length=1, max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    role: UserRole
    status: UserStatus
    auth_provider: AuthProvider
    avatar_url: str | None = None
    created_at: datetime
    approved_at: datetime | None = None
    approved_by: str | None = None
    last_login_at: datetime | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class AdminUpdateRoleRequest(BaseModel):
    role: UserRole


class AdminUpdateStatusRequest(BaseModel):
    status: UserStatus


class MessageResponse(BaseModel):
    message: str
    status: UserStatus | None = None
    user: UserResponse | None = None


class PublicKeyResponse(BaseModel):
    public_key: str
    algorithm: str = "RS256"


ReleaseChannel = Literal["stable", "beta"]


class ReleaseCreateRequest(BaseModel):
    version: str = Field(..., min_length=1, max_length=32, description="Semantic version, e.g. 0.2.0")
    channel: ReleaseChannel = "stable"
    download_url: str = Field(..., min_length=5, description="Full download URL from R2 or storage")
    sha256: str = Field(..., min_length=64, max_length=64, description="SHA-256 hash of the zip package")
    file_size: int = Field(0, ge=0, description="Package size in bytes")
    changelog: str = Field("", description="Release notes / changelog in markdown")
    mandatory: bool = Field(False, description="Whether this update is mandatory")
    min_supported_version: str | None = Field(None, description="Minimum previous version that can update directly")


class ReleaseResponse(BaseModel):
    id: str
    version: str
    channel: ReleaseChannel
    download_url: str
    sha256: str
    file_size: int
    changelog: str
    mandatory: bool
    min_supported_version: str | None = None
    published_at: datetime
    published_by: str | None = None


class UpdateCheckResponse(BaseModel):
    update_available: bool
    current_version: str
    latest_version: str
    channel: ReleaseChannel = "stable"
    download_url: str | None = None
    sha256: str | None = None
    file_size: int | None = None
    changelog: str | None = None
    mandatory: bool = False
    published_at: datetime | None = None

