from __future__ import annotations

import hashlib
import secrets
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

    @staticmethod
    def _generate_cluster_key() -> str:
        return f"fcs_cluster_{secrets.token_urlsafe(24)}"

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

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS cluster_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cluster_key TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS cluster_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    node_api_key TEXT NOT NULL,
                    enabled BOOLEAN DEFAULT 1,
                    healthy BOOLEAN DEFAULT 1,
                    active_sessions INTEGER DEFAULT 0,
                    cached_sessions INTEGER DEFAULT 0,
                    max_concurrency INTEGER DEFAULT 1,
                    weight INTEGER DEFAULT 100,
                    last_heartbeat_at TIMESTAMP,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            # 历史版本把 node_name 设为 UNIQUE，会导致同名子节点覆盖；这里统一迁移为非唯一。
            await self._migrate_cluster_nodes_schema(db)

            await db.execute("CREATE INDEX IF NOT EXISTS idx_captcha_jobs_created_at ON captcha_jobs(created_at DESC)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_captcha_jobs_status ON captcha_jobs(status)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_service_api_keys_enabled ON service_api_keys(enabled)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cluster_nodes_enabled ON cluster_nodes(enabled)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cluster_nodes_heartbeat ON cluster_nodes(last_heartbeat_at DESC)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cluster_nodes_base_url ON cluster_nodes(base_url)")

            await db.commit()

        await self._ensure_defaults()

    async def _migrate_cluster_nodes_schema(self, db: aiosqlite.Connection):
        cursor = await db.execute("PRAGMA index_list(cluster_nodes)")
        indexes = await cursor.fetchall()

        has_node_name_unique = False
        for idx in indexes:
            idx_name = idx[1]
            is_unique = bool(idx[2])
            if not is_unique:
                continue

            idx_cursor = await db.execute(f"PRAGMA index_info('{idx_name}')")
            idx_columns = await idx_cursor.fetchall()
            if len(idx_columns) == 1 and str(idx_columns[0][2] or "") == "node_name":
                has_node_name_unique = True
                break

        if not has_node_name_unique:
            return

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS cluster_nodes_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                node_api_key TEXT NOT NULL,
                enabled BOOLEAN DEFAULT 1,
                healthy BOOLEAN DEFAULT 1,
                active_sessions INTEGER DEFAULT 0,
                cached_sessions INTEGER DEFAULT 0,
                max_concurrency INTEGER DEFAULT 1,
                weight INTEGER DEFAULT 100,
                last_heartbeat_at TIMESTAMP,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            INSERT INTO cluster_nodes_v2 (
                id, node_name, base_url, node_api_key, enabled, healthy,
                active_sessions, cached_sessions, max_concurrency, weight,
                last_heartbeat_at, last_error, created_at, updated_at
            )
            SELECT
                id, node_name, base_url, node_api_key, enabled, healthy,
                active_sessions, cached_sessions, max_concurrency, weight,
                last_heartbeat_at, last_error, created_at, updated_at
            FROM cluster_nodes
            ORDER BY id ASC
            """
        )
        await db.execute("DROP TABLE cluster_nodes")
        await db.execute("ALTER TABLE cluster_nodes_v2 RENAME TO cluster_nodes")

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

            cursor = await db.execute("SELECT id FROM cluster_settings WHERE id = 1")
            row = await cursor.fetchone()
            if not row:
                await db.execute(
                    """
                    INSERT INTO cluster_settings (id, cluster_key)
                    VALUES (1, ?)
                    """,
                    (self._generate_cluster_key(),),
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

    async def get_admin_profile(self) -> Dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT username, created_at, updated_at FROM service_admin WHERE id = 1"
            )
            row = await cursor.fetchone()
            if not row:
                return {
                    "username": config.admin_username,
                    "created_at": None,
                    "updated_at": None,
                }
            return dict(row)

    async def update_admin_credentials(
        self,
        current_password: str,
        new_username: Optional[str] = None,
        new_password: Optional[str] = None,
    ) -> tuple[bool, str, Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT username, password_hash, created_at, updated_at FROM service_admin WHERE id = 1"
            )
            row = await cursor.fetchone()
            if not row:
                return False, "管理员账号不存在", {}

            if row["password_hash"] != self._hash_secret(current_password):
                return False, "当前密码错误", {}

            username = (new_username or row["username"] or "").strip()
            if not username:
                return False, "用户名不能为空", {}

            password_hash = row["password_hash"]
            if new_password:
                password_hash = self._hash_secret(new_password)

            await db.execute(
                """
                UPDATE service_admin
                SET username = ?,
                    password_hash = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                (username, password_hash),
            )
            await db.commit()

        profile = await self.get_admin_profile()
        return True, "管理员账号更新成功", profile

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
        if api_key_id <= 0:
            return True, ""

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
        if api_key_id <= 0:
            return True, ""

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

            cursor = await db.execute(
                """
                SELECT
                    COUNT(*) AS node_count,
                    SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS node_enabled_count
                FROM cluster_nodes
                """
            )
            node_summary = await cursor.fetchone()

            return {
                "jobs_total": int(summary["total"] or 0),
                "jobs_success": int(summary["success"] or 0),
                "jobs_failed": int(summary["failed"] or 0),
                "api_key_total": int(key_summary["key_count"] or 0),
                "api_key_enabled_total": int(key_summary["key_enabled_count"] or 0),
                "cluster_node_total": int(node_summary["node_count"] or 0),
                "cluster_node_enabled_total": int(node_summary["node_enabled_count"] or 0),
            }

    async def get_cluster_key(self) -> str:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT cluster_key FROM cluster_settings WHERE id = 1")
            row = await cursor.fetchone()
            if row and row["cluster_key"]:
                return row["cluster_key"]

        new_key = self._generate_cluster_key()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO cluster_settings (id, cluster_key, updated_at) VALUES (1, ?, CURRENT_TIMESTAMP)",
                (new_key,),
            )
            await db.commit()
        return new_key

    async def rotate_cluster_key(self) -> str:
        new_key = self._generate_cluster_key()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE cluster_settings SET cluster_key = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                (new_key,),
            )
            await db.commit()
        return new_key

    async def validate_cluster_key(self, raw_key: str) -> bool:
        current = await self.get_cluster_key()
        return bool(raw_key) and secrets.compare_digest(raw_key, current)

    async def upsert_cluster_node(
        self,
        node_name: str,
        base_url: str,
        node_api_key: str,
        weight: int,
        max_concurrency: int,
        active_sessions: int,
        cached_sessions: int,
        healthy: bool,
    ) -> Dict[str, Any]:
        normalized_name = node_name.strip()
        normalized_url = base_url.strip().rstrip("/")

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id FROM cluster_nodes WHERE base_url = ? ORDER BY id ASC LIMIT 1",
                (normalized_url,),
            )
            row = await cursor.fetchone()

            if row:
                node_id = int(row["id"])
                await db.execute(
                    """
                    UPDATE cluster_nodes
                    SET node_name = ?, node_api_key = ?, weight = ?, max_concurrency = ?,
                        active_sessions = ?, cached_sessions = ?, healthy = ?,
                        last_heartbeat_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        normalized_name,
                        node_api_key,
                        max(1, int(weight)),
                        max(1, int(max_concurrency)),
                        max(0, int(active_sessions)),
                        max(0, int(cached_sessions)),
                        1 if healthy else 0,
                        node_id,
                    ),
                )
            else:
                cursor = await db.execute(
                    """
                    INSERT INTO cluster_nodes (
                        node_name, base_url, node_api_key, enabled, healthy,
                        active_sessions, cached_sessions, max_concurrency, weight,
                        last_heartbeat_at
                    )
                    VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        normalized_name,
                        normalized_url,
                        node_api_key,
                        1 if healthy else 0,
                        max(0, int(active_sessions)),
                        max(0, int(cached_sessions)),
                        max(1, int(max_concurrency)),
                        max(1, int(weight)),
                    ),
                )
                node_id = int(cursor.lastrowid)

            await db.execute(
                "DELETE FROM cluster_nodes WHERE base_url = ? AND id <> ?",
                (normalized_url, node_id),
            )
            await db.commit()

        node = await self.get_cluster_node(node_id)
        if not node:
            raise RuntimeError("节点保存失败")
        return node

    async def heartbeat_cluster_node(
        self,
        node_name: str,
        base_url: str,
        active_sessions: int,
        cached_sessions: int,
        healthy: bool,
    ) -> Optional[Dict[str, Any]]:
        normalized_name = node_name.strip()
        normalized_url = base_url.strip().rstrip("/")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id FROM cluster_nodes WHERE base_url = ? ORDER BY id ASC LIMIT 1",
                (normalized_url,),
            )
            row = await cursor.fetchone()
            if not row:
                return None

            node_id = int(row["id"])
            await db.execute(
                """
                UPDATE cluster_nodes
                SET node_name = ?,
                    active_sessions = ?,
                    cached_sessions = ?,
                    healthy = ?,
                    last_heartbeat_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    normalized_name,
                    max(0, int(active_sessions)),
                    max(0, int(cached_sessions)),
                    1 if healthy else 0,
                    node_id,
                ),
            )
            await db.execute(
                "DELETE FROM cluster_nodes WHERE base_url = ? AND id <> ?",
                (normalized_url, node_id),
            )
            await db.commit()

        return await self.get_cluster_node(node_id)

    async def list_cluster_nodes(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, node_name, base_url, enabled, healthy,
                       active_sessions, cached_sessions, max_concurrency, weight,
                       last_heartbeat_at, last_error, created_at, updated_at
                FROM cluster_nodes
                ORDER BY id ASC
                """
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_cluster_node(self, node_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM cluster_nodes WHERE id = ?", (node_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_cluster_node_by_name(self, node_name: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM cluster_nodes WHERE node_name = ?", (node_name,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_cluster_node_by_base_url(self, base_url: str) -> Optional[Dict[str, Any]]:
        normalized_url = base_url.strip().rstrip("/")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM cluster_nodes WHERE base_url = ? ORDER BY id ASC LIMIT 1",
                (normalized_url,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_cluster_node(
        self,
        node_id: int,
        enabled: Optional[bool] = None,
        weight: Optional[int] = None,
        max_concurrency: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        current = await self.get_cluster_node(node_id)
        if not current:
            return None

        new_enabled = int(enabled) if enabled is not None else int(bool(current["enabled"]))
        new_weight = max(1, int(weight)) if weight is not None else max(1, int(current["weight"] or 100))
        new_max = (
            max(1, int(max_concurrency))
            if max_concurrency is not None
            else max(1, int(current["max_concurrency"] or 1))
        )

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE cluster_nodes
                SET enabled = ?,
                    weight = ?,
                    max_concurrency = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (new_enabled, new_weight, new_max, node_id),
            )
            await db.commit()

        return await self.get_cluster_node(node_id)

    async def delete_cluster_node(self, node_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM cluster_nodes WHERE id = ?", (node_id,))
            await db.commit()
            return (cursor.rowcount or 0) > 0

    async def mark_cluster_node_error(self, node_id: int, error_message: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE cluster_nodes
                SET last_error = ?,
                    healthy = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error_message[:500], node_id),
            )
            await db.commit()

    async def get_available_cluster_nodes(self, stale_seconds: int) -> List[Dict[str, Any]]:
        stale_seconds = max(10, int(stale_seconds))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT *
                FROM cluster_nodes
                WHERE enabled = 1
                  AND healthy = 1
                  AND last_heartbeat_at IS NOT NULL
                  AND last_heartbeat_at >= datetime('now', '-' || ? || ' seconds')
                ORDER BY id ASC
                """,
                (stale_seconds,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_token(self, token_id: int):
        """兼容 browser_captcha.py 的调用；独立打码服务默认不维护业务 token。"""
        return None
