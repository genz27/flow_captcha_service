from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..core.auth import verify_service_api_key
from ..core.database import Database
from ..core.models import ErrorRequest, FinishRequest, SolveRequest, SolveResponse
from ..services.captcha_runtime import CaptchaRuntime

router = APIRouter(prefix="/api/v1", tags=["captcha-service"])

_db: Optional[Database] = None
_runtime: Optional[CaptchaRuntime] = None


def set_dependencies(db: Database, runtime: CaptchaRuntime):
    global _db, _runtime
    _db = db
    _runtime = runtime


@router.get("/health")
async def health_check():
    return {"success": True, "status": "ok"}


@router.post("/solve", response_model=SolveResponse)
async def solve_captcha(
    request: SolveRequest,
    api_key: dict = Depends(verify_service_api_key),
):
    if _db is None or _runtime is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    available, message = await _db.ensure_api_key_available(api_key["id"])
    if not available:
        raise HTTPException(status_code=403, detail=message)

    started = time.perf_counter()
    try:
        result = await _runtime.solve(
            project_id=request.project_id,
            action=request.action,
            token_id=request.token_id,
            api_key_id=api_key["id"],
        )
        consumed, consume_message = await _db.consume_api_key_quota(api_key["id"])
        if not consumed:
            # 额度校验竞争失败时，主动标记 error 回收浏览器。
            await _runtime.mark_error(result["session_id"], "quota_conflict")
            raise HTTPException(status_code=403, detail=consume_message)

        elapsed = int((time.perf_counter() - started) * 1000)
        await _db.create_job_log(
            session_id=result["session_id"],
            api_key_id=api_key["id"],
            project_id=request.project_id,
            action=request.action,
            status="success",
            error_reason=None,
            duration_ms=elapsed,
        )
        return SolveResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        elapsed = int((time.perf_counter() - started) * 1000)
        await _db.create_job_log(
            session_id=None,
            api_key_id=api_key["id"],
            project_id=request.project_id,
            action=request.action,
            status="failed",
            error_reason=str(e),
            duration_ms=elapsed,
        )
        raise HTTPException(status_code=500, detail=f"打码失败: {e}")


@router.post("/sessions/{session_id}/finish")
async def finish_session(
    session_id: str,
    request: FinishRequest,
    api_key: dict = Depends(verify_service_api_key),
):
    if _db is None or _runtime is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    ok, message, entry = await _runtime.finish(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail=message)

    await _db.create_job_log(
        session_id=session_id,
        api_key_id=api_key["id"],
        project_id=entry.project_id if entry else None,
        action=entry.action if entry else None,
        status=f"finish:{request.status}",
        error_reason=None,
        duration_ms=None,
    )
    return {"success": True, "message": message}


@router.post("/sessions/{session_id}/error")
async def report_session_error(
    session_id: str,
    request: ErrorRequest,
    api_key: dict = Depends(verify_service_api_key),
):
    if _db is None or _runtime is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    ok, message, entry = await _runtime.mark_error(session_id, request.error_reason)
    if not ok:
        raise HTTPException(status_code=404, detail=message)

    await _db.create_job_log(
        session_id=session_id,
        api_key_id=api_key["id"],
        project_id=entry.project_id if entry else None,
        action=entry.action if entry else None,
        status="error_reported",
        error_reason=request.error_reason,
        duration_ms=None,
    )
    return {"success": True, "message": message}
