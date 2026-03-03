from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any

from pydantic import BaseModel, Field


class CaptchaConfig(BaseModel):
    id: int = 1
    browser_proxy_enabled: bool = False
    browser_proxy_url: Optional[str] = None
    browser_count: int = 1
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ServiceApiKey(BaseModel):
    id: int
    name: str
    key_prefix: str
    enabled: bool = True
    quota_remaining: Optional[int] = None
    quota_used: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None


class SolveRequest(BaseModel):
    project_id: str = Field(min_length=1)
    action: str = "IMAGE_GENERATION"
    token_id: Optional[int] = None


class SolveResponse(BaseModel):
    success: bool = True
    session_id: str
    token: str
    fingerprint: Optional[Dict[str, Any]] = None
    node_name: str
    expires_in_seconds: int = 7200


class FinishRequest(BaseModel):
    status: str = "success"


class ErrorRequest(BaseModel):
    error_reason: str = "upstream_error"


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    quota_remaining: Optional[int] = Field(default=None, ge=0)


class UpdateApiKeyRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    enabled: Optional[bool] = None
    quota_remaining: Optional[int] = Field(default=None, ge=0)


class UpdateCaptchaConfigRequest(BaseModel):
    browser_proxy_enabled: bool = False
    browser_proxy_url: Optional[str] = None
    browser_count: int = Field(default=1, ge=1)


@dataclass
class SessionRecord:
    session_id: str
    browser_id: int
    api_key_id: int
    project_id: str
    action: str
    status: str = "pending"
    created_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    error_reason: Optional[str] = None
