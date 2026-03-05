from __future__ import annotations

import urllib.parse
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.auth import issue_admin_token, revoke_admin_token, verify_admin_token
from ..core.config import config
from ..core.database import Database
from ..core.logger import debug_logger
from ..core.models import (
    ClusterNodeUpdateRequest,
    CreateApiKeyRequest,
    LoginRequest,
    UpdateAdminCredentialsRequest,
    UpdateApiKeyRequest,
    UpdateCaptchaConfigRequest,
    UpdateSystemConfigRequest,
)
from ..services.captcha_runtime import CaptchaRuntime
from ..services.cluster_manager import ClusterManager

router = APIRouter(prefix="/api/admin", tags=["admin"])

_db: Optional[Database] = None
_runtime: Optional[CaptchaRuntime] = None
_cluster: Optional[ClusterManager] = None

RESTART_REQUIRED_CONFIG_KEYS = {
    "server.host",
    "server.port",
    "storage.db_path",
    "cluster.role",
}


def _assert_master_role(feature_name: str):
    if config.cluster_role != "master":
        raise HTTPException(status_code=400, detail=f"仅 master 角色可使用：{feature_name}")


def _assert_local_captcha_role():
    if config.cluster_role == "master":
        raise HTTPException(status_code=400, detail="master 角色不执行本地打码，无需配置运行时打码参数")


async def _build_setup_guide_payload() -> Dict[str, Any]:
    if _db is None:
        return {
            "role": config.cluster_role,
            "is_first_deploy": False,
            "has_blocker": False,
            "items": [],
            "next_steps": [],
        }

    role = config.cluster_role
    items: list[Dict[str, Any]] = []
    next_steps: list[str] = []

    default_admin = await _db.verify_admin_credentials("admin", "admin")
    if default_admin:
        items.append(
            {
                "level": "critical",
                "title": "请先修改默认管理员账号",
                "detail": "当前仍可使用 admin/admin 登录，存在明显安全风险。",
                "key": "admin.default_credentials",
            }
        )
        next_steps.append("先在“管理员账号”中修改用户名和密码，再继续其他配置。")

    if role == "master":
        stats = await _db.get_service_stats()
        if int(stats.get("api_key_total") or 0) <= 0:
            items.append(
                {
                    "level": "required",
                    "title": "主节点还没有可用 API Key",
                    "detail": "flow2api 调用主节点前，需要先创建至少 1 个启用状态的 API Key。",
                    "key": "master.api_key_missing",
                }
            )
        if int(stats.get("cluster_node_total") or 0) <= 0:
            items.append(
                {
                    "level": "required",
                    "title": "主节点还没有注册子节点",
                    "detail": "请先启动 subnode，并配置 master 地址与集群密钥。",
                    "key": "master.subnode_missing",
                }
            )
        next_steps.append("确认主节点端口已对外可达，再让子节点连接注册。")
    elif role == "subnode":
        if not config.cluster_master_base_url:
            items.append(
                {
                    "level": "required",
                    "title": "未配置主节点地址",
                    "detail": "请填写 cluster.master_base_url，例如 http://master:8060。",
                    "key": "subnode.master_base_url_missing",
                }
            )
        if not config.cluster_master_cluster_key:
            items.append(
                {
                    "level": "required",
                    "title": "未配置集群密钥",
                    "detail": "请填写 cluster.master_cluster_key，需与主节点 Cluster Key 完全一致。",
                    "key": "subnode.cluster_key_missing",
                }
            )
        if not config.cluster_node_public_base_url:
            items.append(
                {
                    "level": "required",
                    "title": "未配置子节点对外地址",
                    "detail": "请填写 cluster.node_public_base_url，主节点将通过该地址回调子节点。",
                    "key": "subnode.public_base_url_missing",
                }
            )
        if not config.node_api_key:
            items.append(
                {
                    "level": "required",
                    "title": "未配置子节点 API Key",
                    "detail": "请填写 cluster.node_api_key，主节点调用子节点时会校验该 key。",
                    "key": "subnode.node_api_key_missing",
                }
            )
        next_steps.append("保存后重启 subnode，观察主节点“子节点状态”是否为健康。")
    else:
        next_steps.append("standalone 模式可直接使用本地有头打码，无需集群配置。")

    has_blocker = any(item["level"] in {"critical", "required"} for item in items)
    is_first_deploy = default_admin or has_blocker
    if not items:
        next_steps.append("当前基础配置已完整，可直接投入调用。")

    return {
        "role": role,
        "is_first_deploy": is_first_deploy,
        "has_blocker": has_blocker,
        "items": items,
        "next_steps": next_steps,
    }


def _validate_subnode_fields_before_persist(updates: Dict[str, Dict[str, Any]]):
    cluster_updates = updates.get("cluster", {}) if isinstance(updates.get("cluster"), dict) else {}
    env_overrides = config.get_active_env_overrides()

    role_from_update = str(cluster_updates.get("role") or "").strip().lower()
    if "FCS_CLUSTER_ROLE" in env_overrides:
        effective_role = config.cluster_role
    else:
        effective_role = role_from_update or config.cluster_role

    if effective_role != "subnode":
        return

    def _pick_value(update_key: str, env_key: str, runtime_value: str) -> str:
        if env_key in env_overrides:
            return str(runtime_value or "").strip()
        if update_key in cluster_updates:
            return str(cluster_updates.get(update_key) or "").strip()
        return str(runtime_value or "").strip()

    master_base_url = _pick_value(
        update_key="master_base_url",
        env_key="FCS_CLUSTER_MASTER_BASE_URL",
        runtime_value=config.cluster_master_base_url,
    )
    master_cluster_key = _pick_value(
        update_key="master_cluster_key",
        env_key="FCS_CLUSTER_MASTER_CLUSTER_KEY",
        runtime_value=config.cluster_master_cluster_key,
    )
    node_public_base_url = _pick_value(
        update_key="node_public_base_url",
        env_key="FCS_CLUSTER_NODE_PUBLIC_BASE_URL",
        runtime_value=config.cluster_node_public_base_url,
    )
    node_api_key = _pick_value(
        update_key="node_api_key",
        env_key="FCS_CLUSTER_NODE_API_KEY",
        runtime_value=config.node_api_key,
    )

    missing_fields: list[str] = []
    if not master_base_url:
        missing_fields.append("cluster.master_base_url")
    if not master_cluster_key:
        missing_fields.append("cluster.master_cluster_key")
    if not node_public_base_url:
        missing_fields.append("cluster.node_public_base_url")
    if not node_api_key:
        missing_fields.append("cluster.node_api_key")
    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"subnode 模式缺少必填配置: {', '.join(missing_fields)}",
        )

    parsed_public = urllib.parse.urlparse(node_public_base_url)
    public_host = (parsed_public.hostname or "").strip().lower()
    if parsed_public.scheme not in {"http", "https"} or not public_host:
        raise HTTPException(status_code=400, detail="cluster.node_public_base_url 格式无效")
    if public_host in {"0.0.0.0", "127.0.0.1", "localhost", "::1", "::"}:
        raise HTTPException(
            status_code=400,
            detail=(
                "cluster.node_public_base_url 不能使用 0.0.0.0 / 127.0.0.1 / localhost，"
                "必须是主节点可访问地址"
            ),
        )


def _as_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise HTTPException(status_code=400, detail=f"{field_name} 必须是布尔值")


def _as_int(value: Any, field_name: str, min_value: int, max_value: int) -> int:
    try:
        iv = int(value)
    except Exception:
        raise HTTPException(status_code=400, detail=f"{field_name} 必须是整数")
    if iv < min_value or iv > max_value:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} 必须在 [{min_value}, {max_value}] 范围内",
        )
    return iv


def _as_float(value: Any, field_name: str, min_value: float, max_value: float) -> float:
    try:
        fv = float(value)
    except Exception:
        raise HTTPException(status_code=400, detail=f"{field_name} 必须是数字")
    if fv < min_value or fv > max_value:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} 必须在 [{min_value}, {max_value}] 范围内",
        )
    return fv


def _sanitize_system_config_updates(payload: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], list[str]]:
    allowed_sections = {"server", "storage", "captcha", "log", "cluster"}
    updates: Dict[str, Dict[str, Any]] = {}
    changed_keys: list[str] = []

    unknown_sections = [s for s in payload.keys() if s not in allowed_sections and s != "admin"]
    if unknown_sections:
        raise HTTPException(status_code=400, detail=f"存在不支持的配置分组: {unknown_sections}")

    server_cfg = payload.get("server")
    if isinstance(server_cfg, dict):
        section: Dict[str, Any] = {}
        if "host" in server_cfg:
            host = str(server_cfg.get("host") or "").strip()
            if not host:
                raise HTTPException(status_code=400, detail="server.host 不能为空")
            section["host"] = host
            changed_keys.append("server.host")
        if "port" in server_cfg:
            section["port"] = _as_int(server_cfg.get("port"), "server.port", 1, 65535)
            changed_keys.append("server.port")
        if section:
            updates["server"] = section

    storage_cfg = payload.get("storage")
    if isinstance(storage_cfg, dict):
        section = {}
        if "db_path" in storage_cfg:
            db_path = str(storage_cfg.get("db_path") or "").strip()
            if not db_path:
                raise HTTPException(status_code=400, detail="storage.db_path 不能为空")
            section["db_path"] = db_path
            changed_keys.append("storage.db_path")
        if section:
            updates["storage"] = section

    captcha_cfg = payload.get("captcha")
    if isinstance(captcha_cfg, dict):
        section = {}
        if "browser_launch_background" in captcha_cfg:
            section["browser_launch_background"] = _as_bool(
                captcha_cfg.get("browser_launch_background"),
                "captcha.browser_launch_background",
            )
            changed_keys.append("captcha.browser_launch_background")
        if "browser_score_dom_wait_seconds" in captcha_cfg:
            section["browser_score_dom_wait_seconds"] = _as_float(
                captcha_cfg.get("browser_score_dom_wait_seconds"),
                "captcha.browser_score_dom_wait_seconds",
                1.0,
                180.0,
            )
            changed_keys.append("captcha.browser_score_dom_wait_seconds")
        if "browser_recaptcha_settle_seconds" in captcha_cfg:
            section["browser_recaptcha_settle_seconds"] = _as_float(
                captcha_cfg.get("browser_recaptcha_settle_seconds"),
                "captcha.browser_recaptcha_settle_seconds",
                0.0,
                30.0,
            )
            changed_keys.append("captcha.browser_recaptcha_settle_seconds")
        if "browser_score_test_warmup_seconds" in captcha_cfg:
            section["browser_score_test_warmup_seconds"] = _as_float(
                captcha_cfg.get("browser_score_test_warmup_seconds"),
                "captcha.browser_score_test_warmup_seconds",
                0.0,
                300.0,
            )
            changed_keys.append("captcha.browser_score_test_warmup_seconds")
        if "flow_timeout" in captcha_cfg:
            section["flow_timeout"] = _as_int(
                captcha_cfg.get("flow_timeout"),
                "captcha.flow_timeout",
                10,
                7200,
            )
            changed_keys.append("captcha.flow_timeout")
        if "upsample_timeout" in captcha_cfg:
            section["upsample_timeout"] = _as_int(
                captcha_cfg.get("upsample_timeout"),
                "captcha.upsample_timeout",
                10,
                7200,
            )
            changed_keys.append("captcha.upsample_timeout")
        if "session_ttl_seconds" in captcha_cfg:
            section["session_ttl_seconds"] = _as_int(
                captcha_cfg.get("session_ttl_seconds"),
                "captcha.session_ttl_seconds",
                60,
                86400,
            )
            changed_keys.append("captcha.session_ttl_seconds")
        if "node_name" in captcha_cfg:
            node_name = str(captcha_cfg.get("node_name") or "").strip()
            if not node_name:
                raise HTTPException(status_code=400, detail="captcha.node_name 不能为空")
            section["node_name"] = node_name
            changed_keys.append("captcha.node_name")
        if section:
            updates["captcha"] = section

    log_cfg = payload.get("log")
    if isinstance(log_cfg, dict):
        section = {}
        if "level" in log_cfg:
            level = str(log_cfg.get("level") or "").strip().upper()
            if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
                raise HTTPException(status_code=400, detail="log.level 仅支持 DEBUG/INFO/WARNING/ERROR/CRITICAL")
            section["level"] = level
            changed_keys.append("log.level")
        if section:
            updates["log"] = section

    cluster_cfg = payload.get("cluster")
    if isinstance(cluster_cfg, dict):
        section = {}
        if "role" in cluster_cfg:
            role = str(cluster_cfg.get("role") or "").strip().lower()
            if role not in {"standalone", "master", "subnode"}:
                raise HTTPException(status_code=400, detail="cluster.role 仅支持 standalone/master/subnode")
            section["role"] = role
            changed_keys.append("cluster.role")
        if "master_base_url" in cluster_cfg:
            section["master_base_url"] = str(cluster_cfg.get("master_base_url") or "").strip().rstrip("/")
            changed_keys.append("cluster.master_base_url")
        if "master_cluster_key" in cluster_cfg:
            section["master_cluster_key"] = str(cluster_cfg.get("master_cluster_key") or "").strip()
            changed_keys.append("cluster.master_cluster_key")
        if "node_public_base_url" in cluster_cfg:
            section["node_public_base_url"] = str(cluster_cfg.get("node_public_base_url") or "").strip().rstrip("/")
            changed_keys.append("cluster.node_public_base_url")
        if "node_api_key" in cluster_cfg:
            section["node_api_key"] = str(cluster_cfg.get("node_api_key") or "").strip()
            changed_keys.append("cluster.node_api_key")
        if "heartbeat_interval_seconds" in cluster_cfg:
            section["heartbeat_interval_seconds"] = _as_int(
                cluster_cfg.get("heartbeat_interval_seconds"),
                "cluster.heartbeat_interval_seconds",
                5,
                3600,
            )
            changed_keys.append("cluster.heartbeat_interval_seconds")
        if "node_weight" in cluster_cfg:
            section["node_weight"] = _as_int(
                cluster_cfg.get("node_weight"),
                "cluster.node_weight",
                1,
                10000,
            )
            changed_keys.append("cluster.node_weight")
        if "node_max_concurrency" in cluster_cfg:
            section["node_max_concurrency"] = _as_int(
                cluster_cfg.get("node_max_concurrency"),
                "cluster.node_max_concurrency",
                1,
                200,
            )
            changed_keys.append("cluster.node_max_concurrency")
        if "master_node_stale_seconds" in cluster_cfg:
            section["master_node_stale_seconds"] = _as_int(
                cluster_cfg.get("master_node_stale_seconds"),
                "cluster.master_node_stale_seconds",
                10,
                3600,
            )
            changed_keys.append("cluster.master_node_stale_seconds")
        if "master_dispatch_timeout_seconds" in cluster_cfg:
            section["master_dispatch_timeout_seconds"] = _as_int(
                cluster_cfg.get("master_dispatch_timeout_seconds"),
                "cluster.master_dispatch_timeout_seconds",
                5,
                3600,
            )
            changed_keys.append("cluster.master_dispatch_timeout_seconds")
        if section:
            updates["cluster"] = section

    return updates, changed_keys


def _build_system_config_payload(admin_profile: Dict[str, Any]) -> Dict[str, Any]:
    merged = config.get_merged_config()
    return {
        "config_path": str(config.config_path),
        "role": config.cluster_role,
        "env_overrides": config.get_active_env_overrides(),
        "config": {
            "server": merged.get("server", {}),
            "storage": merged.get("storage", {}),
            "captcha": merged.get("captcha", {}),
            "log": merged.get("log", {}),
            "cluster": merged.get("cluster", {}),
            "admin": {
                "username": admin_profile.get("username"),
                "password": "******",
            },
        },
    }


def set_dependencies(db: Database, runtime: CaptchaRuntime, cluster_manager: ClusterManager):
    global _db, _runtime, _cluster
    _db = db
    _runtime = runtime
    _cluster = cluster_manager


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
        "role": config.cluster_role,
    }


@router.post("/logout")
async def admin_logout(token: str = Depends(verify_admin_token)):
    revoke_admin_token(token)
    return {"success": True}


@router.get("/profile")
async def get_admin_profile(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    profile = await _db.get_admin_profile()
    return {"success": True, "profile": profile}


@router.post("/credentials")
async def update_admin_credentials(
    request: UpdateAdminCredentialsRequest,
    token: str = Depends(verify_admin_token),
):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    if not request.new_username and not request.new_password:
        raise HTTPException(status_code=400, detail="至少需要提供新用户名或新密码")

    ok, message, profile = await _db.update_admin_credentials(
        current_password=request.current_password,
        new_username=request.new_username,
        new_password=request.new_password,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"success": True, "message": message, "profile": profile}


@router.get("/system-config")
async def get_system_config(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    profile = await _db.get_admin_profile()
    setup_guide = await _build_setup_guide_payload()
    return {
        "success": True,
        "setup_guide": setup_guide,
        **_build_system_config_payload(profile),
    }


@router.post("/system-config")
async def update_system_config(
    request: UpdateSystemConfigRequest,
    token: str = Depends(verify_admin_token),
):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    request_payload = request.model_dump(exclude_none=True)
    updates, changed_keys = _sanitize_system_config_updates(request_payload)
    if not updates:
        raise HTTPException(status_code=400, detail="没有可更新的系统配置字段")

    _validate_subnode_fields_before_persist(updates)
    config.update_config_sections(updates)

    if "log.level" in changed_keys:
        debug_logger.refresh_level()

    restart_required = any(key in RESTART_REQUIRED_CONFIG_KEYS for key in changed_keys)
    message = "系统配置已保存并热重载"
    if restart_required:
        message += "；部分配置需要重启服务后完全生效"

    profile = await _db.get_admin_profile()
    setup_guide = await _build_setup_guide_payload()
    return {
        "success": True,
        "message": message,
        "restart_required": restart_required,
        "changed_keys": changed_keys,
        "setup_guide": setup_guide,
        **_build_system_config_payload(profile),
    }


@router.get("/setup-guide")
async def get_setup_guide(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    return {
        "success": True,
        **(await _build_setup_guide_payload()),
    }


@router.get("/apikeys")
async def list_api_keys(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    _assert_master_role("API Key 管理")
    items = await _db.list_api_keys()
    return {"success": True, "items": items}


@router.post("/apikeys")
async def create_api_key(request: CreateApiKeyRequest, token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    _assert_master_role("API Key 管理")

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
    _assert_master_role("API Key 管理")

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
    cluster_stats = await _cluster.get_cluster_runtime_summary() if _cluster else {}
    return {
        "success": True,
        "db": db_stats,
        "runtime": runtime_stats,
        "cluster": cluster_stats,
    }


@router.get("/captcha-config")
async def get_captcha_config(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    _assert_local_captcha_role()

    cfg = await _db.get_captcha_config()
    from ..services.browser_captcha import split_browser_proxy_pool

    proxy_pool_size = len(split_browser_proxy_pool(cfg.browser_proxy_url or ""))
    return {
        "success": True,
        "browser_proxy_enabled": cfg.browser_proxy_enabled,
        "browser_proxy_url": cfg.browser_proxy_url or "",
        "browser_count": cfg.browser_count,
        "browser_proxy_pool_size": proxy_pool_size,
    }


@router.post("/captcha-config")
async def update_captcha_config(
    request: UpdateCaptchaConfigRequest,
    token: str = Depends(verify_admin_token),
):
    if _db is None or _runtime is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    _assert_local_captcha_role()

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


@router.get("/cluster/config")
async def get_cluster_config(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    cluster_key = await _db.get_cluster_key()
    return {
        "success": True,
        "role": config.cluster_role,
        "cluster_key": cluster_key if config.cluster_role == "master" else "",
        "node_name": config.node_name,
        "master_base_url": config.cluster_master_base_url,
        "node_public_base_url": config.cluster_node_public_base_url,
        "heartbeat_interval_seconds": config.cluster_heartbeat_interval_seconds,
    }


@router.post("/cluster/config/rotate-key")
async def rotate_cluster_key(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    _assert_master_role("Cluster Key 轮换")

    new_key = await _db.rotate_cluster_key()
    return {
        "success": True,
        "cluster_key": new_key,
    }


@router.get("/cluster/nodes")
async def list_cluster_nodes(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    _assert_master_role("子节点管理")
    items = await _db.list_cluster_nodes()
    if _cluster is not None:
        items = _cluster.decorate_nodes_capacity(items)
    return {
        "success": True,
        "items": items,
    }


@router.patch("/cluster/nodes/{node_id}")
async def update_cluster_node(
    node_id: int,
    request: ClusterNodeUpdateRequest,
    token: str = Depends(verify_admin_token),
):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    _assert_master_role("子节点管理")

    updated = await _db.update_cluster_node(
        node_id=node_id,
        enabled=request.enabled,
        weight=request.weight,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="节点不存在")

    return {
        "success": True,
        "item": updated,
    }


@router.delete("/cluster/nodes/{node_id}")
async def delete_cluster_node(
    node_id: int,
    token: str = Depends(verify_admin_token),
):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    _assert_master_role("子节点管理")

    deleted = await _db.delete_cluster_node(node_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="节点不存在")

    return {
        "success": True,
        "message": f"子节点 #{node_id} 已删除",
    }
