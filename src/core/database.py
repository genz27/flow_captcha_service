from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from .config import config
from .models import CaptchaConfig


class Database:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or config.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _hash_secret(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    async def init_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS captcha_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    browser_proxy_enabled BOOLEAN DEFAULT 0,
                    browser_proxy_url TEXT,
                    browser_count INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS service_admin (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    username TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS service_api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    key_prefix TEXT NOT NULL,
                    enabled BOOLEAN DEFAULT 1,
                    quota_remaining INTEGER,
                    quota_used INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP
                )
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS captcha_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    api_key_id INTEGER,
                    project_id TEXT,
                    action TEXT,
                    status TEXT,
                    error_reason TEXT,
                    duration_ms INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(api_key_id) REFERENCES service_api_keys(id)
                )
                """
            )

            await db.execute("CREATE INDEX IF NOT EXISTS idx_captcha_jobs_created_at ON captcha_jobs(created_at DESC)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_captcha_jobs_status ON captcha_jobs(status)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_service_api_keys_enabled ON service_api_keys(enabled)")

            await db.commit()

        await self._ensure_defaults()

    async def _ensure_defaults(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute("SELECT id FROM captcha_config WHERE id = 1")
            row = await cursor.fetchone()
            if not row:
                await db.execute(
                    """
                    INSERT INTO captcha_config (id, browser_proxy_enabled, browser_proxy_url, browser_count)
                    VALUES (1, ?, ?, ?)
                    """,
                    (1 if config.browser_proxy_enabled else 0, config.browser_proxy_url or None, config.browser_count),
                )

            cursor = await db.execute("SELECT id FROM service_admin WHERE id = 1")
            row = await cursor.fetchone()
            if not row:
                await db.execute(
                    """
                    INSERT INTO service_admin (id, username, password_hash)
                    VALUES (1, ?, ?)
                    """,
                    (config.admin_username, self._hash_secret(config.admin_password)),
                )

            await db.commit()

    async def verify_admin_credentials(self, username: str, password: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT username, password_hash FROM service_admin WHERE id = 1"
            )
            row = await cursor.fetchone()
            if not row:
                return False
            if row["username"] != username:
                return False
            return row["password_hash"] == self._hash_secret(password)

    async def get_captcha_config(self) -> CaptchaConfig:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM captcha_config WHERE id = 1")
            row = await cursor.fetchone()
            if not row:
                return CaptchaConfig()
            return CaptchaConfig(
                id=row["id"],
                browser_proxy_enabled=bool(row["browser_proxy_enabled"]),
                browser_proxy_url=row["browser_proxy_url"],
                browser_count=max(1, int(row["browser_count"] or 1)),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    async def update_captcha_config(
        self,
        browser_proxy_enabled: bool,
        browser_proxy_url: Optional[str],
        browser_count: int,
    ):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE captcha_config
                SET browser_proxy_enabled = ?,
                    browser_proxy_url = ?,
                    browser_count = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                (1 if browser_proxy_enabled else 0, browser_proxy_url, max(1, int(browser_count))),
            )
            await db.commit()

    async def create_api_key(self, name: str, quota_remaining: Optional[int]) -> Tuple[str, Dict[str, Any]]:
        raw_key = f"fcs_{secrets.token_urlsafe(32)}"
        key_hash = self._hash_secret(raw_key)
        key_prefix = raw_key[:12]

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO service_api_keys (name, key_hash, key_prefix, enabled, quota_remaining)
                VALUES (?, ?, ?, 1, ?)
                """,
                (name, key_hash, key_prefix, quota_remaining),
            )
            api_key_id = cursor.lastrowid
            await db.commit()

        row = await self.get_api_key(api_key_id)
        return raw_key, row

    async def get_api_key(self, api_key_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, name, key_prefix, enabled, quota_remaining, quota_used,
                       created_at, updated_at, last_used_at
                FROM service_api_keys
                WHERE id = ?
                """,
                (api_key_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return dict(row)

    async def list_api_keys(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, name, key_prefix, enabled, quota_remaining, quota_used,
                       created_at, updated_at, last_used_at
                FROM service_api_keys
                ORDER BY id ASC
                """
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def update_api_key(
        self,
        api_key_id: int,
        name: Optional[str] = None,
        enabled: Optional[bool] = None,
        quota_remaining: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        current = await self.get_api_key(api_key_id)
        if not current:
            return None

        new_name = name if name is not None else current["name"]
        new_enabled = int(enabled) if enabled is not None else int(bool(current["enabled"]))
        new_quota = quota_remaining if quota_remaining is not None else current["quota_remaining"]

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE service_api_keys
                SET name = ?, enabled = ?, quota_remaining = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (new_name, new_enabled, new_quota, api_key_id),
            )
            await db.commit()

        return await self.get_api_key(api_key_id)

    async def resolve_service_api_key(self, raw_key: str) -> Optional[Dict[str, Any]]:
        key_hash = self._hash_secret(raw_key)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, name, key_prefix, enabled, quota_remaining, quota_used,
                       created_at, updated_at, last_used_at
                FROM service_api_keys
                WHERE key_hash = ?
                """,
                (key_hash,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return dict(row)

    async def ensure_api_key_available(self, api_key_id: int) -> Tuple[bool, str]:
        api_key = await self.get_api_key(api_key_id)
        if not api_key:
            return False, "API Key 不存在"

        if not bool(api_key["enabled"]):
            return False, "API Key 已禁用"

        quota_remaining = api_key["quota_remaining"]
        if quota_remaining is not None and int(quota_remaining) <= 0:
            return False, "API Key 可用次数已耗尽"

        return True, ""

    async def consume_api_key_quota(self, api_key_id: int) -> Tuple[bool, str]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT enabled, quota_remaining, quota_used FROM service_api_keys WHERE id = ?",
                    (api_key_id,),
                )
                row = await cursor.fetchone()
                if not row:
                    await db.execute("ROLLBACK")
                    return False, "API Key 不存在"

                if not bool(row["enabled"]):
                    await db.execute("ROLLBACK")
                    return False, "API Key 已禁用"

                quota_remaining = row["quota_remaining"]
                if quota_remaining is None:
                    await db.execute(
                        """
                        UPDATE service_api_keys
                        SET quota_used = quota_used + 1,
                            last_used_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (api_key_id,),
                    )
                else:
                    if int(quota_remaining) <= 0:
                        await db.execute("ROLLBACK")
                        return False, "API Key 可用次数已耗尽"
                    await db.execute(
                        """
                        UPDATE service_api_keys
                        SET quota_remaining = quota_remaining - 1,
                            quota_used = quota_used + 1,
                            last_used_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND quota_remaining > 0
                        """,
                        (api_key_id,),
                    )
                await db.commit()
                return True, ""
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def create_job_log(
        self,
        session_id: Optional[str],
        api_key_id: Optional[int],
        project_id: Optional[str],
        action: Optional[str],
        status: str,
        error_reason: Optional[str],
        duration_ms: Optional[int],
    ):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO captcha_jobs (session_id, api_key_id, project_id, action, status, error_reason, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, api_key_id, project_id, action, status, error_reason, duration_ms),
            )
            await db.commit()

    async def list_job_logs(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        safe_offset = max(0, int(offset))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT j.id, j.session_id, j.api_key_id, k.name AS api_key_name, k.key_prefix,
                       j.project_id, j.action, j.status, j.error_reason, j.duration_ms, j.created_at
                FROM captcha_jobs j
                LEFT JOIN service_api_keys k ON k.id = j.api_key_id
                ORDER BY j.id DESC
                LIMIT ? OFFSET ?
                """,
                (safe_limit, safe_offset),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_service_stats(self) -> Dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
                FROM captcha_jobs
                """
            )
            summary = await cursor.fetchone()

            cursor = await db.execute(
                """
                SELECT
                    COUNT(*) AS key_count,
                    SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS key_enabled_count
                FROM service_api_keys
                """
            )
            key_summary = await cursor.fetchone()

            return {
                "jobs_total": int(summary["total"] or 0),
                "jobs_success": int(summary["success"] or 0),
                "jobs_failed": int(summary["failed"] or 0),
                "api_key_total": int(key_summary["key_count"] or 0),
                "api_key_enabled_total": int(key_summary["key_enabled_count"] or 0),
            }

    async def get_token(self, token_id: int):
        """兼容 browser_captcha.py 的调用；独立打码服务默认不维护业务 token。"""
        return None
