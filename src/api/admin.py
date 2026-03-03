from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.auth import issue_admin_token, revoke_admin_token, verify_admin_token
from ..core.database import Database
from ..core.models import (
    CreateApiKeyRequest,
    LoginRequest,
    UpdateApiKeyRequest,
    UpdateCaptchaConfigRequest,
)
from ..services.captcha_runtime import CaptchaRuntime

router = APIRouter(prefix="/api/admin", tags=["admin"])

_db: Optional[Database] = None
_runtime: Optional[CaptchaRuntime] = None


def set_dependencies(db: Database, runtime: CaptchaRuntime):
    global _db, _runtime
    _db = db
    _runtime = runtime


@router.post("/login")
async def admin_login(request: LoginRequest):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    ok = await _db.verify_admin_credentials(request.username, request.password)
    if not ok:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = issue_admin_token()
    return {
        "success": True,
        "token": token,
        "username": request.username,
    }


@router.post("/logout")
async def admin_logout(token: str = Depends(verify_admin_token)):
    revoke_admin_token(token)
    return {"success": True}


@router.get("/apikeys")
async def list_api_keys(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    items = await _db.list_api_keys()
    return {"success": True, "items": items}


@router.post("/apikeys")
async def create_api_key(request: CreateApiKeyRequest, token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    raw_key, item = await _db.create_api_key(request.name, request.quota_remaining)
    return {
        "success": True,
        "api_key": raw_key,
        "item": item,
        "message": "仅本次返回完整 API Key，请立即保存",
    }


@router.patch("/apikeys/{api_key_id}")
async def update_api_key(
    api_key_id: int,
    request: UpdateApiKeyRequest,
    token: str = Depends(verify_admin_token),
):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    item = await _db.update_api_key(
        api_key_id=api_key_id,
        name=request.name,
        enabled=request.enabled,
        quota_remaining=request.quota_remaining,
    )
    if not item:
        raise HTTPException(status_code=404, detail="API Key 不存在")

    return {"success": True, "item": item}


@router.get("/logs")
async def get_logs(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    token: str = Depends(verify_admin_token),
):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    items = await _db.list_job_logs(limit=limit, offset=offset)
    return {"success": True, "items": items, "limit": limit, "offset": offset}


@router.get("/stats")
async def get_stats(token: str = Depends(verify_admin_token)):
    if _db is None or _runtime is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    db_stats = await _db.get_service_stats()
    runtime_stats = await _runtime.get_stats()
    return {
        "success": True,
        "db": db_stats,
        "runtime": runtime_stats,
    }


@router.get("/captcha-config")
async def get_captcha_config(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    cfg = await _db.get_captcha_config()
    return {
        "success": True,
        "browser_proxy_enabled": cfg.browser_proxy_enabled,
        "browser_proxy_url": cfg.browser_proxy_url or "",
        "browser_count": cfg.browser_count,
    }


@router.post("/captcha-config")
async def update_captcha_config(
    request: UpdateCaptchaConfigRequest,
    token: str = Depends(verify_admin_token),
):
    if _db is None or _runtime is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    if request.browser_proxy_enabled and request.browser_proxy_url:
        from ..services.browser_captcha import validate_browser_proxy_url

        is_valid, message = validate_browser_proxy_url(request.browser_proxy_url)
        if not is_valid:
            raise HTTPException(status_code=400, detail=message)

    await _db.update_captcha_config(
        browser_proxy_enabled=request.browser_proxy_enabled,
        browser_proxy_url=request.browser_proxy_url if request.browser_proxy_enabled else None,
        browser_count=request.browser_count,
    )
    await _runtime.reload_browser_count()

    return {"success": True}
