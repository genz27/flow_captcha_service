from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from ..core.config import config
from ..core.database import Database
from ..core.logger import debug_logger
from .session_registry import SessionRegistry, SessionEntry


class CaptchaRuntime:
    def __init__(self, db: Database):
        self.db = db
        self.registry = SessionRegistry()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._browser_service = None

    async def start(self):
        if self._cleanup_task and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _get_browser_service(self):
        if config.cluster_role == "master":
            raise RuntimeError("master 角色不执行本地打码")

        if self._browser_service is None:
            from .browser_captcha import BrowserCaptchaService

            self._browser_service = await BrowserCaptchaService.get_instance(self.db)
        return self._browser_service

    async def solve(
        self,
        project_id: str,
        action: str,
        token_id: Optional[int],
        api_key_id: int,
    ) -> Dict[str, Any]:
        service = await self._get_browser_service()
        token, browser_id = await service.get_token(project_id, action, token_id=token_id)

        if not token or browser_id is None:
            raise RuntimeError("有头打码失败，未获取到 token")

        fingerprint = await service.get_fingerprint(browser_id)
        session_id = str(uuid.uuid4())
        await self.registry.create(
            session_id=session_id,
            browser_id=browser_id,
            api_key_id=api_key_id,
            project_id=project_id,
            action=action,
        )

        return {
            "session_id": session_id,
            "token": token,
            "fingerprint": fingerprint,
            "node_name": config.node_name,
            "expires_in_seconds": config.session_ttl_seconds,
        }

    async def custom_score(
        self,
        website_url: str,
        website_key: str,
        verify_url: str,
        action: str,
        enterprise: bool,
    ) -> Dict[str, Any]:
        service = await self._get_browser_service()
        payload, browser_id = await service.get_custom_score(
            website_url=website_url,
            website_key=website_key,
            verify_url=verify_url,
            action=action,
            enterprise=enterprise,
        )
        payload = payload if isinstance(payload, dict) else {}
        payload["browser_id"] = browser_id
        payload["node_name"] = config.node_name
        return payload

    async def finish(self, session_id: str) -> Tuple[bool, str, Optional[SessionEntry]]:
        entry = await self.registry.get(session_id)
        if not entry:
            return False, "session_not_found", None

        if entry.status != "pending":
            return True, f"session_already_{entry.status}", entry

        service = await self._get_browser_service()
        await service.report_request_finished(entry.browser_id)
        finished_entry = await self.registry.finish(session_id)
        return True, "ok", finished_entry

    async def mark_error(self, session_id: str, error_reason: str) -> Tuple[bool, str, Optional[SessionEntry]]:
        entry = await self.registry.get(session_id)
        if not entry:
            return False, "session_not_found", None

        if entry.status != "pending":
            return True, f"session_already_{entry.status}", entry

        service = await self._get_browser_service()
        await service.report_error(entry.browser_id, error_reason=error_reason)
        error_entry = await self.registry.mark_error(session_id, error_reason)
        return True, "ok", error_entry

    async def reload_browser_count(self):
        if config.cluster_role == "master":
            return
        if self._browser_service is None:
            return
        try:
            await self._browser_service.reload_browser_count()
        except Exception as e:
            debug_logger.log_warning(f"[CaptchaRuntime] reload_browser_count failed: {e}")

    async def get_stats(self) -> Dict[str, Any]:
        pending_sessions = await self.registry.active_count()
        total_sessions = await self.registry.total_count()

        browser_stats: Dict[str, Any] = {
            "total_solve_count": 0,
            "total_error_count": 0,
            "risk_403_count": 0,
            "browser_count": 0,
            "configured_browser_count": config.browser_count,
            "busy_browser_count": 0,
            "idle_browser_count": config.browser_count,
        }

        if self._browser_service is not None:
            try:
                browser_stats = self._browser_service.get_stats()
            except Exception as e:
                debug_logger.log_warning(f"[CaptchaRuntime] get browser stats failed: {e}")
        elif config.cluster_role != "master":
            try:
                captcha_cfg = await self.db.get_captcha_config()
                browser_stats["configured_browser_count"] = max(1, int(captcha_cfg.browser_count or 1))
                browser_stats["idle_browser_count"] = browser_stats["configured_browser_count"]
            except Exception as e:
                debug_logger.log_warning(f"[CaptchaRuntime] read browser_count from db failed: {e}")

        configured_count = max(1, int(browser_stats.get("configured_browser_count") or config.browser_count or 1))
        busy_count = max(0, int(browser_stats.get("busy_browser_count") or 0))
        browser_stats["thread_total"] = configured_count
        browser_stats["thread_idle"] = max(configured_count - busy_count, 0)
        browser_stats["thread_active"] = busy_count

        return {
            "node_name": config.node_name,
            "role": config.cluster_role,
            "active_sessions": busy_count,
            "pending_sessions": pending_sessions,
            "cached_sessions": total_sessions,
            "local_solve_enabled": config.cluster_role != "master",
            "browser": browser_stats,
        }

    def _resolve_session_timeout_seconds(self, action: str) -> int:
        configured_ttl = max(120, int(getattr(config, "session_ttl_seconds", 1200) or 1200))
        flow_timeout = max(10, int(getattr(config, "flow_timeout", 300) or 300))
        upsample_timeout = max(10, int(getattr(config, "upsample_timeout", 300) or 300))

        action_name = str(action or "").strip().upper()
        if action_name == "IMAGE_GENERATION":
            expected = max(flow_timeout, upsample_timeout) + 120
        elif action_name == "VIDEO_GENERATION":
            expected = max(flow_timeout + 240, upsample_timeout + 180, 600)
        else:
            expected = max(flow_timeout + 180, upsample_timeout + 120, 480)

        return max(120, min(configured_ttl, int(expected)))

    def _resolve_entry_ttl(self, entry: SessionEntry) -> int:
        return self._resolve_session_timeout_seconds(entry.action)

    async def _cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(30)
                expired_entries = await self.registry.list_expired(
                    config.session_ttl_seconds,
                    ttl_resolver=self._resolve_entry_ttl,
                )
                if not expired_entries:
                    continue

                if self._browser_service is None:
                    continue

                started = time.perf_counter()
                for entry in expired_entries:
                    try:
                        await self._browser_service.report_error(
                            entry.browser_id,
                            error_reason=entry.error_reason or "session_timeout",
                        )
                    except Exception as e:
                        debug_logger.log_warning(f"[CaptchaRuntime] expired session cleanup failed: {e}")
                elapsed = int((time.perf_counter() - started) * 1000)
                debug_logger.log_info(
                    f"[CaptchaRuntime] cleaned {len(expired_entries)} expired session(s) in {elapsed}ms"
                )
            except asyncio.CancelledError:
                return
            except Exception as e:
                debug_logger.log_warning(f"[CaptchaRuntime] cleanup loop error: {e}")

    async def close(self):
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if self._browser_service is not None:
            try:
                await self._browser_service.close()
            except Exception as e:
                debug_logger.log_warning(f"[CaptchaRuntime] close browser service failed: {e}")
