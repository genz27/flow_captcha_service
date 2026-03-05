from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

from ..core.config import config
from ..core.database import Database
from ..core.logger import debug_logger


class ClusterManager:
    def __init__(self, db: Database, runtime):
        self.db = db
        self.runtime = runtime
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._dispatch_cursor = 0
        self._dispatch_lock = asyncio.Lock()
        # 记录短时调度预留槽位，用于覆盖“心跳上报滞后”窗口，避免瞬时超发。
        self._dispatch_reservations: Dict[int, List[float]] = {}

    async def start(self):
        if config.cluster_role == "subnode":
            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def close(self):
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

    async def dispatch_solve(self, request_payload: Dict[str, Any]) -> Dict[str, Any]:
        nodes = await self._select_candidate_nodes()
        if not nodes:
            raise RuntimeError("暂无可用子节点")

        last_error = ""
        attempted = False
        for node in nodes:
            reserved = await self._try_reserve_dispatch_slot(node)
            if not reserved:
                continue

            attempted = True
            node_id = int(node.get("id") or 0)
            try:
                result = await self._post_to_node(
                    node=node,
                    path="/api/v1/solve",
                    json_payload=request_payload,
                    timeout=config.cluster_master_dispatch_timeout_seconds,
                )
                child_session = str(result.get("session_id") or "").strip()
                token = str(result.get("token") or "").strip()
                if not child_session or not token:
                    raise RuntimeError("子节点响应缺少 session_id/token")

                result["session_id"] = f"{node['id']}:{child_session}"
                result["node_name"] = node["node_name"]
                return result
            except Exception as e:
                await self._release_dispatch_slot(node_id)
                last_error = str(e)
                await self.db.mark_cluster_node_error(int(node["id"]), last_error)
                debug_logger.log_warning(f"[ClusterManager] dispatch solve node={node['node_name']} failed: {last_error}")

        if not attempted:
            raise RuntimeError("暂无可用子节点")
        raise RuntimeError(f"子节点打码失败: {last_error or 'unknown'}")

    async def dispatch_finish(self, routed_session_id: str, status: str) -> Dict[str, Any]:
        node, child_session = await self._resolve_routed_session(routed_session_id)
        payload = {"status": status}
        return await self._post_to_node(
            node=node,
            path=f"/api/v1/sessions/{child_session}/finish",
            json_payload=payload,
            timeout=20,
        )

    async def dispatch_error(self, routed_session_id: str, error_reason: str) -> Dict[str, Any]:
        node, child_session = await self._resolve_routed_session(routed_session_id)
        payload = {"error_reason": error_reason}
        return await self._post_to_node(
            node=node,
            path=f"/api/v1/sessions/{child_session}/error",
            json_payload=payload,
            timeout=20,
        )

    async def dispatch_custom_score(self, request_payload: Dict[str, Any]) -> Dict[str, Any]:
        nodes = await self._select_candidate_nodes()
        if not nodes:
            raise RuntimeError("暂无可用子节点")

        last_error = ""
        attempted = False
        for node in nodes:
            reserved = await self._try_reserve_dispatch_slot(node)
            if not reserved:
                continue

            attempted = True
            node_id = int(node.get("id") or 0)
            try:
                result = await self._post_to_node(
                    node=node,
                    path="/api/v1/custom-score",
                    json_payload=request_payload,
                    timeout=config.cluster_master_dispatch_timeout_seconds,
                )
                await self._release_dispatch_slot(node_id)
                return result
            except Exception as e:
                await self._release_dispatch_slot(node_id)
                last_error = str(e)
                await self.db.mark_cluster_node_error(int(node["id"]), last_error)
                debug_logger.log_warning(f"[ClusterManager] dispatch custom-score node={node['node_name']} failed: {last_error}")

        if not attempted:
            raise RuntimeError("暂无可用子节点")
        raise RuntimeError(f"子节点分数校验失败: {last_error or 'unknown'}")

    async def register_node(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        node = await self.db.upsert_cluster_node(
            node_name=payload["node_name"],
            base_url=payload["base_url"],
            node_api_key=payload["node_api_key"],
            weight=int(payload.get("weight") or 100),
            max_concurrency=int(payload.get("max_concurrency") or 1),
            active_sessions=int(payload.get("active_sessions") or 0),
            cached_sessions=int(payload.get("cached_sessions") or 0),
            healthy=bool(payload.get("healthy", True)),
        )
        return {
            "success": True,
            "node": node,
            "cluster_role": config.cluster_role,
        }

    async def heartbeat_node(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        node = await self.db.heartbeat_cluster_node(
            node_name=payload["node_name"],
            base_url=payload["base_url"],
            active_sessions=int(payload.get("active_sessions") or 0),
            cached_sessions=int(payload.get("cached_sessions") or 0),
            healthy=bool(payload.get("healthy", True)),
        )
        if not node:
            return {
                "success": False,
                "message": "node_not_registered",
            }
        return {
            "success": True,
            "node": node,
        }

    async def _resolve_routed_session(self, routed_session_id: str) -> Tuple[Dict[str, Any], str]:
        raw = (routed_session_id or "").strip()
        if ":" not in raw:
            raise RuntimeError("master 模式 session_id 必须为 nodeId:childSessionId")
        node_part, child_session = raw.split(":", 1)
        if not node_part.isdigit() or not child_session:
            raise RuntimeError("session_id 路由格式无效")

        node_id = int(node_part)
        node = await self.db.get_cluster_node(node_id)
        if not node:
            raise RuntimeError("路由节点不存在")
        if not bool(node.get("enabled", 0)):
            raise RuntimeError("路由节点已禁用")

        return node, child_session

    async def _select_candidate_nodes(self) -> List[Dict[str, Any]]:
        nodes = await self.db.get_available_cluster_nodes(config.cluster_master_node_stale_seconds)
        if not nodes:
            return []

        filtered_nodes: List[Dict[str, Any]] = []
        for node in nodes:
            base_url = str(node.get("base_url") or "")
            parsed = urllib.parse.urlparse(base_url)
            host = (parsed.hostname or "").strip().lower()
            if parsed.scheme not in {"http", "https"} or not host:
                debug_logger.log_warning(
                    f"[ClusterManager] 跳过无效子节点地址 node={node.get('node_name')} base_url={base_url}"
                )
                continue
            if host in {"0.0.0.0", "127.0.0.1", "localhost", "::1", "::"}:
                debug_logger.log_warning(
                    f"[ClusterManager] 跳过不可达子节点地址 node={node.get('node_name')} base_url={base_url}"
                )
                continue
            filtered_nodes.append(node)

        if not filtered_nodes:
            return []

        async with self._dispatch_lock:
            self._prune_dispatch_reservations_locked()

            decorated: List[Dict[str, Any]] = []
            for node in filtered_nodes:
                node_id = int(node.get("id") or 0)
                reserved = len(self._dispatch_reservations.get(node_id, []))
                decorated.append(self.decorate_node_capacity(node, extra_active=reserved))

            with_idle = [node for node in decorated if int(node.get("thread_idle") or 0) > 0]
            if not with_idle:
                # 全部节点都无空闲容量时，直接返回空，避免继续向已满节点灌流量。
                return []

            with_idle.sort(
                key=lambda node: (
                    -int(node.get("thread_idle") or 0),
                    int(node.get("thread_active") or 0),
                    -int(node.get("weight") or 100),
                    int(node.get("id") or 0),
                )
            )

            weighted_ring: List[Dict[str, Any]] = []
            for node in with_idle:
                idle = max(1, int(node.get("thread_idle") or 0))
                weight = max(1, int(node.get("weight") or 100))
                weight_factor = max(1, round(weight / 100.0))
                tickets = min(200, max(1, idle * weight_factor))
                weighted_ring.extend([node] * tickets)

            start = self._dispatch_cursor % len(weighted_ring)
            self._dispatch_cursor = (self._dispatch_cursor + 1) % len(weighted_ring)

            ordered_idle: List[Dict[str, Any]] = []
            seen_node_ids = set()
            for offset in range(len(weighted_ring)):
                node = weighted_ring[(start + offset) % len(weighted_ring)]
                node_id = int(node.get("id") or 0)
                if node_id in seen_node_ids:
                    continue
                seen_node_ids.add(node_id)
                ordered_idle.append(node)
                if len(ordered_idle) >= len(with_idle):
                    break

            return ordered_idle

    def _dispatch_reservation_window_seconds(self) -> float:
        heartbeat_window = max(10, int(config.cluster_heartbeat_interval_seconds) * 2)
        dispatch_window = max(10, int(config.cluster_master_dispatch_timeout_seconds) + 5)
        return float(max(heartbeat_window, dispatch_window))

    def _prune_dispatch_reservations_locked(self):
        if not self._dispatch_reservations:
            return

        expire_before = time.time() - self._dispatch_reservation_window_seconds()
        stale_node_ids: List[int] = []
        for node_id, slots in self._dispatch_reservations.items():
            valid_slots = [ts for ts in slots if ts >= expire_before]
            if valid_slots:
                self._dispatch_reservations[node_id] = valid_slots
            else:
                stale_node_ids.append(node_id)

        for node_id in stale_node_ids:
            self._dispatch_reservations.pop(node_id, None)

    async def _try_reserve_dispatch_slot(self, node: Dict[str, Any]) -> bool:
        node_id = int(node.get("id") or 0)
        if node_id <= 0:
            return False

        total_capacity = max(1, int(node.get("thread_total") or node.get("max_concurrency") or 1))
        reported_active = max(0, int(node.get("reported_active_sessions") or node.get("active_sessions") or 0))

        async with self._dispatch_lock:
            self._prune_dispatch_reservations_locked()
            reserved = len(self._dispatch_reservations.get(node_id, []))
            if reported_active + reserved >= total_capacity:
                return False

            self._dispatch_reservations.setdefault(node_id, []).append(time.time())
            return True

    async def _release_dispatch_slot(self, node_id: int):
        if node_id <= 0:
            return

        async with self._dispatch_lock:
            self._prune_dispatch_reservations_locked()
            slots = self._dispatch_reservations.get(node_id)
            if not slots:
                return

            slots.pop()
            if slots:
                self._dispatch_reservations[node_id] = slots
            else:
                self._dispatch_reservations.pop(node_id, None)

    @staticmethod
    def decorate_node_capacity(node: Dict[str, Any], extra_active: int = 0) -> Dict[str, Any]:
        reported_active = max(0, int(node.get("active_sessions") or 0))
        active = reported_active + max(0, int(extra_active or 0))
        total = max(1, int(node.get("max_concurrency") or 1))
        idle = max(total - active, 0)
        decorated = dict(node)
        decorated["thread_total"] = total
        decorated["thread_active"] = active
        decorated["thread_idle"] = idle
        decorated["reported_active_sessions"] = reported_active
        decorated["dispatch_reserved"] = max(0, int(extra_active or 0))
        return decorated

    def decorate_nodes_capacity(self, nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [self.decorate_node_capacity(node) for node in (nodes or [])]

    async def _post_to_node(
        self,
        node: Dict[str, Any],
        path: str,
        json_payload: Dict[str, Any],
        timeout: int,
    ) -> Dict[str, Any]:
        base_url = str(node.get("base_url") or "").rstrip("/")
        api_key = str(node.get("node_api_key") or "").strip()
        if not base_url or not api_key:
            raise RuntimeError("节点配置缺少 base_url 或 node_api_key")

        url = f"{base_url}{path}"
        headers = {"Authorization": f"Bearer {api_key}"}
        status_code, payload, response_text = await asyncio.to_thread(
            self._sync_json_http_request,
            "POST",
            url,
            headers,
            json_payload,
            timeout,
        )

        if status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if not detail:
                detail = (response_text or "").strip()[:300]
            raise RuntimeError(f"HTTP {status_code}: {detail or payload}")

        if isinstance(payload, dict):
            return payload
        raise RuntimeError("子节点响应不是 JSON 对象")

    @staticmethod
    def _sync_json_http_request(
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout: int,
    ) -> tuple[int, Optional[Any], str]:
        req_headers = dict(headers or {})
        req_headers.setdefault("Accept", "application/json")

        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req_headers["Content-Type"] = "application/json; charset=utf-8"

        request = urllib.request.Request(
            url=url,
            data=data,
            headers=req_headers,
            method=(method or "GET").upper(),
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status_code = int(response.getcode() or 0)
                raw_body = response.read()
        except urllib.error.HTTPError as e:
            status_code = int(getattr(e, "code", 500))
            raw_body = e.read() if hasattr(e, "read") else b""
        except Exception as e:
            raise RuntimeError(f"HTTP 请求失败: {e}") from e

        text = raw_body.decode("utf-8", errors="replace") if raw_body else ""
        parsed: Optional[Any] = None
        if text:
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None

        return status_code, parsed, text

    async def _heartbeat_loop(self):
        debug_logger.log_info("[ClusterManager] subnode heartbeat loop started")
        while True:
            try:
                await self._send_subnode_heartbeat()
            except asyncio.CancelledError:
                return
            except Exception as e:
                debug_logger.log_warning(f"[ClusterManager] heartbeat error: {e}")

            await asyncio.sleep(config.cluster_heartbeat_interval_seconds)

    async def _send_subnode_heartbeat(self):
        master_base = config.cluster_master_base_url
        cluster_key = config.cluster_master_cluster_key
        node_api_key = config.node_api_key

        if not master_base or not cluster_key or not node_api_key:
            debug_logger.log_warning(
                "[ClusterManager] subnode mode 缺少 master_base_url/master_cluster_key/node_api_key，跳过心跳"
            )
            return

        public_base_url = config.cluster_node_public_base_url
        if not public_base_url:
            debug_logger.log_warning(
                "[ClusterManager] subnode mode 缺少 node_public_base_url，跳过心跳。"
                "请填写主节点可以访问到的子节点地址，例如 http://subnode:8060 或 http://公网IP:8061"
            )
            return

        parsed_public = urllib.parse.urlparse(public_base_url)
        public_host = (parsed_public.hostname or "").strip().lower()
        if parsed_public.scheme not in {"http", "https"} or not public_host:
            debug_logger.log_warning(
                f"[ClusterManager] node_public_base_url 无效: {public_base_url}"
            )
            return

        if public_host in {"0.0.0.0", "127.0.0.1", "localhost", "::1", "::"}:
            debug_logger.log_warning(
                "[ClusterManager] node_public_base_url 不能是 0.0.0.0 / 127.0.0.1 / localhost。"
                f"当前值: {public_base_url}"
            )
            return

        runtime_stats = await self.runtime.get_stats()
        active_sessions = int(runtime_stats.get("active_sessions") or 0)
        cached_sessions = int(runtime_stats.get("cached_sessions") or 0)
        browser_stats = runtime_stats.get("browser") if isinstance(runtime_stats, dict) else {}
        configured_browser_count = 0
        if isinstance(browser_stats, dict):
            configured_browser_count = max(0, int(browser_stats.get("configured_browser_count") or 0))
        configured_browser_count = max(1, configured_browser_count or int(config.browser_count))
        configured_dispatch_limit = max(1, int(config.cluster_node_max_concurrency))
        effective_capacity = max(1, min(configured_browser_count, configured_dispatch_limit))

        register_payload = {
            "node_name": config.node_name,
            "base_url": public_base_url,
            "node_api_key": node_api_key,
            "weight": config.cluster_node_weight,
            "max_concurrency": effective_capacity,
            "active_sessions": active_sessions,
            "cached_sessions": cached_sessions,
            "healthy": True,
        }
        heartbeat_payload = {
            "node_name": config.node_name,
            "base_url": public_base_url,
            "active_sessions": active_sessions,
            "cached_sessions": cached_sessions,
            "healthy": True,
        }

        headers = {"X-Cluster-Key": cluster_key}
        register_url = f"{master_base}/api/cluster/register"
        hb_url = f"{master_base}/api/cluster/heartbeat"

        register_status, _, register_text = await asyncio.to_thread(
            self._sync_json_http_request,
            "POST",
            register_url,
            headers,
            register_payload,
            20,
        )
        if register_status >= 400:
            raise RuntimeError(f"register failed: {register_status}, {(register_text or '')[:200]}")

        hb_status, _, hb_text = await asyncio.to_thread(
            self._sync_json_http_request,
            "POST",
            hb_url,
            headers,
            heartbeat_payload,
            20,
        )
        if hb_status >= 400:
            raise RuntimeError(f"heartbeat failed: {hb_status}, {(hb_text or '')[:200]}")

    async def get_cluster_runtime_summary(self) -> Dict[str, Any]:
        nodes = self.decorate_nodes_capacity(await self.db.list_cluster_nodes())
        total_thread_capacity = sum(max(0, int(node.get("thread_total") or 0)) for node in nodes)
        total_idle_capacity = sum(max(0, int(node.get("thread_idle") or 0)) for node in nodes)
        total_active_capacity = sum(max(0, int(node.get("thread_active") or 0)) for node in nodes)
        healthy_node_count = sum(1 for node in nodes if bool(node.get("healthy")) and bool(node.get("enabled")))
        return {
            "role": config.cluster_role,
            "node_name": config.node_name,
            "node_count": len(nodes),
            "healthy_node_count": healthy_node_count,
            "total_thread_capacity": total_thread_capacity,
            "total_idle_capacity": total_idle_capacity,
            "total_active_capacity": total_active_capacity,
            "nodes": nodes,
        }
