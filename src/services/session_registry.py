from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional, List, Union


@dataclass
class SessionEntry:
    session_id: str
    browser_id: Union[int, str]
    api_key_id: int
    project_id: str
    action: str
    status: str = "pending"
    created_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    error_reason: Optional[str] = None


class SessionRegistry:
    def __init__(self):
        self._sessions: Dict[str, SessionEntry] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        session_id: str,
        browser_id: Union[int, str],
        api_key_id: int,
        project_id: str,
        action: str,
    ) -> SessionEntry:
        entry = SessionEntry(
            session_id=session_id,
            browser_id=browser_id,
            api_key_id=api_key_id,
            project_id=project_id,
            action=action,
        )
        async with self._lock:
            self._sessions[session_id] = entry
        return entry

    async def get(self, session_id: str) -> Optional[SessionEntry]:
        async with self._lock:
            return self._sessions.get(session_id)

    async def finish(self, session_id: str) -> Optional[SessionEntry]:
        async with self._lock:
            entry = self._sessions.get(session_id)
            if not entry:
                return None
            if entry.status == "pending":
                entry.status = "finished"
                entry.finished_at = datetime.utcnow()
            return entry

    async def mark_error(self, session_id: str, error_reason: str) -> Optional[SessionEntry]:
        async with self._lock:
            entry = self._sessions.get(session_id)
            if not entry:
                return None
            if entry.status == "pending":
                entry.status = "error"
                entry.error_reason = error_reason
                entry.finished_at = datetime.utcnow()
            return entry

    async def remove(self, session_id: str):
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def list_expired(
        self,
        ttl_seconds: int,
        ttl_resolver: Optional[Callable[[SessionEntry], int]] = None,
    ) -> List[SessionEntry]:
        now = datetime.utcnow()
        expired: List[SessionEntry] = []

        async with self._lock:
            for session_id, entry in list(self._sessions.items()):
                current_ttl = max(1, int(ttl_seconds))
                if ttl_resolver is not None:
                    try:
                        current_ttl = max(1, int(ttl_resolver(entry)))
                    except Exception:
                        current_ttl = max(1, int(ttl_seconds))

                deadline = now - timedelta(seconds=current_ttl)
                if entry.status == "pending" and entry.created_at < deadline:
                    expired.append(entry)
                    entry.status = "expired"
                    entry.error_reason = "session_timeout"
                    entry.finished_at = now
                elif entry.status in {"finished", "error", "expired"}:
                    # 清理已经结束超过 60 秒的会话
                    if entry.finished_at and (now - entry.finished_at).total_seconds() > 60:
                        self._sessions.pop(session_id, None)

        return expired

    async def active_count(self) -> int:
        async with self._lock:
            return sum(1 for s in self._sessions.values() if s.status == "pending")

    async def total_count(self) -> int:
        async with self._lock:
            return len(self._sessions)
