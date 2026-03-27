from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, Optional

import tomli


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _toml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value)
    if value is None:
        return '""'
    return _toml_quote(str(value))




def _positive_int_or_fallback(value: Any, fallback: int) -> int:
    if value is None:
        return max(1, int(fallback))
    text = str(value).strip()
    if text == "":
        return max(1, int(fallback))
    try:
        number = int(text)
    except Exception:
        return max(1, int(fallback))
    if number <= 0:
        return max(1, int(fallback))
    return number


def _bounded_int_or_fallback(value: Any, fallback: int, minimum: int) -> int:
    if value is None:
        return max(minimum, int(fallback))
    text = str(value).strip()
    if text == "":
        return max(minimum, int(fallback))
    try:
        number = int(text)
    except Exception:
        return max(minimum, int(fallback))
    return max(minimum, number)

class Config:
    ORDERED_TOP_SECTIONS = ("server", "storage", "admin", "portal", "captcha", "log", "cluster")
    ENV_OVERRIDE_KEYS = (
        "FCS_CONFIG_FILE",
        "FCS_SERVER_HOST",
        "FCS_SERVER_PORT",
        "FCS_DB_PATH",
        "FCS_ADMIN_USERNAME",
        "FCS_ADMIN_PASSWORD",
        "FCS_BROWSER_LAUNCH_BACKGROUND",
        "FCS_BROWSER_FINGERPRINT_POOL_EXTRA_COUNT",
        "FCS_BROWSER_CUSTOM_PAGE_CACHE_MAX_PAGES",
        "FCS_BROWSER_CUSTOM_PAGE_IDLE_TTL_SECONDS",
        "FCS_BROWSER_PROJECT_AFFINITY_MAX_KEYS",
        "FCS_BROWSER_PROJECT_AFFINITY_TTL_SECONDS",
        "FCS_BROWSER_SCORE_DOM_WAIT_SECONDS",
        "FCS_BROWSER_RECAPTCHA_SETTLE_SECONDS",
        "FCS_BROWSER_STANDBY_TOKEN_POOL_ENABLED",
        "FCS_BROWSER_STANDBY_TOKEN_TTL_SECONDS",
        "FCS_BROWSER_STANDBY_TOKEN_POOL_DEPTH",
        "FCS_BROWSER_STANDBY_BUCKET_MAX_COUNT",
        "FCS_BROWSER_STANDBY_BUCKET_IDLE_TTL_SECONDS",
        "FCS_BROWSER_STANDBY_REFILL_IDLE_SECONDS",
        "FCS_BROWSER_SCORE_TEST_WARMUP_SECONDS",
        "FCS_BROWSER_IDLE_TTL_SECONDS",
        "FCS_BROWSER_RETRY_MAX_ATTEMPTS",
        "FCS_BROWSER_RETRY_BACKOFF_SECONDS",
        "FCS_BROWSER_EXECUTE_TIMEOUT_SECONDS",
        "FCS_BROWSER_RELOAD_WAIT_TIMEOUT_SECONDS",
        "FCS_BROWSER_CLR_WAIT_TIMEOUT_SECONDS",
        "FCS_BROWSER_IDLE_REAPER_INTERVAL_SECONDS",
        "FCS_BROWSER_REQUEST_FINISH_IMAGE_WAIT_SECONDS",
        "FCS_BROWSER_REQUEST_FINISH_NON_IMAGE_WAIT_SECONDS",
        "FCS_FLOW_TIMEOUT",
        "FCS_UPSAMPLE_TIMEOUT",
        "FCS_SESSION_TTL_SECONDS",
        "FCS_NODE_NAME",
        "FCS_BROWSER_COUNT",
        "FCS_BROWSER_PROXY_ENABLED",
        "FCS_BROWSER_PROXY_URL",
        "FCS_LOG_LEVEL",
        "FCS_LOG_STORAGE_BACKEND",
        "FCS_LOG_REDIS_URL",
        "FCS_LOG_REDIS_KEY_PREFIX",
        "FCS_LOG_REDIS_MAX_ENTRIES",
        "FCS_LOG_STARTUP_CLEAR_ON_BOOT",
        "FCS_LOG_AUTO_CLEAR_INTERVAL_MINUTES",
        "FCS_CLUSTER_ROLE",
        "FCS_CLUSTER_MASTER_BASE_URL",
        "FCS_CLUSTER_MASTER_CLUSTER_KEY",
        "FCS_CLUSTER_NODE_PUBLIC_BASE_URL",
        "FCS_CLUSTER_NODE_API_KEY",
        "FCS_CLUSTER_HEARTBEAT_INTERVAL_SECONDS",
        "FCS_CLUSTER_NODE_WEIGHT",
        "FCS_CLUSTER_NODE_MAX_CONCURRENCY",
        "FCS_CLUSTER_MASTER_NODE_STALE_SECONDS",
        "FCS_CLUSTER_MASTER_DISPATCH_TIMEOUT_SECONDS",
    )

    def __init__(self):
        self._root_dir = Path(__file__).resolve().parents[2]
        default_path = self._root_dir / "data" / "setting.toml"
        self._legacy_config_path = self._root_dir / "config" / "setting.toml"
        self._example_config_path = self._root_dir / "config" / "setting_example.toml"
        env_path = os.getenv("FCS_CONFIG_FILE", "").strip()
        self._config_path = Path(env_path) if env_path else default_path
        self._config_path = self._resolve_config_path(self._config_path)
        self._config = self._load_config()

    def _resolve_config_path(self, preferred_path: Path) -> Path:
        resolved_path = preferred_path if preferred_path.is_absolute() else (self._root_dir / preferred_path)
        resolved_path.parent.mkdir(parents=True, exist_ok=True)

        # Respect explicit custom config paths.
        if resolved_path.exists() or os.getenv("FCS_CONFIG_FILE", "").strip():
            return resolved_path

        # One-time migration: move the old config into the persistent data directory.
        if resolved_path == (self._root_dir / "data" / "setting.toml"):
            if self._legacy_config_path.exists():
                resolved_path.write_text(self._legacy_config_path.read_text(encoding="utf-8-sig"), encoding="utf-8")
                return resolved_path
            if self._example_config_path.exists():
                resolved_path.write_text(self._example_config_path.read_text(encoding="utf-8-sig"), encoding="utf-8")
                return resolved_path

        return resolved_path

    def _defaults(self) -> Dict[str, Any]:
        return {
            "server": {
                "host": "0.0.0.0",
                "port": 8060,
            },
            "storage": {
                "db_path": "data/captcha_service.db",
            },
            "admin": {
                "username": "admin",
                "password": "admin",
            },
            "portal": {
                "public_base_url": "",
                "oidc_enabled": False,
                "oidc_base_url": "",
                "oidc_well_known_url": "",
                "oidc_client_id": "",
                "oidc_client_secret": "",
                "oidc_scope": "openid profile email",
                "oauth_only": False,
                "register_bonus_quota": 0,
                "checkin_min_quota": 0,
                "checkin_max_quota": 0,
            },
            "captcha": {
                "browser_count": 1,
                "browser_proxy_enabled": False,
                "browser_proxy_url": "",
                "browser_launch_background": True,
                "browser_fingerprint_pool_extra_count": 100,
                "browser_custom_page_cache_max_pages": 3,
                "browser_custom_page_idle_ttl_seconds": 240,
                "browser_project_affinity_max_keys": 0,
                "browser_project_affinity_ttl_seconds": 1800,
                "browser_score_dom_wait_seconds": 25,
                "browser_recaptcha_settle_seconds": 3,
                "browser_standby_token_pool_enabled": True,
                "browser_standby_token_ttl_seconds": 90,
                "browser_standby_token_pool_depth": 2,
                "browser_standby_bucket_max_count": 0,
                "browser_standby_bucket_idle_ttl_seconds": 0,
                "browser_standby_refill_idle_seconds": 0.8,
                "browser_score_test_warmup_seconds": 12,
                "browser_idle_ttl_seconds": 600,
                "browser_retry_max_attempts": 3,
                "browser_retry_backoff_seconds": 1,
                "browser_execute_timeout_seconds": 0,
                "browser_reload_wait_timeout_seconds": 12,
                "browser_clr_wait_timeout_seconds": 12,
                "browser_idle_reaper_interval_seconds": 15,
                "browser_request_finish_image_wait_seconds": 0,
                "browser_request_finish_non_image_wait_seconds": 0,
                "flow_timeout": 300,
                "upsample_timeout": 300,
                "session_ttl_seconds": 1200,
                "node_name": "standalone-node",
            },
            "log": {
                "level": "INFO",
                "storage_backend": "sqlite",
                "redis_url": "",
                "redis_key_prefix": "fcs",
                "redis_max_entries": 20000,
                "startup_clear_on_boot": True,
                "auto_clear_interval_minutes": 0,
            },
            "cluster": {
                "role": "standalone",
                "master_base_url": "",
                "master_cluster_key": "",
                "node_public_base_url": "",
                "node_api_key": "",
                "heartbeat_interval_seconds": 15,
                "node_weight": 100,
                "node_max_concurrency": 0,
                "master_node_stale_seconds": 120,
                "master_dispatch_timeout_seconds": 45,
            },
        }

    def _read_user_config(self) -> Dict[str, Any]:
        if not self._config_path.exists():
            return {}
        raw_text = self._config_path.read_text(encoding="utf-8-sig")
        parsed = tomli.loads(raw_text)
        if not isinstance(parsed, dict):
            return {}
        return parsed

    def _load_config(self) -> Dict[str, Any]:
        return _deep_merge(self._defaults(), self._read_user_config())

    def _get(self, section: str, key: str, default: Any = None) -> Any:
        return self._config.get(section, {}).get(key, default)

    def _dump_toml(self, data: Dict[str, Any]) -> str:
        lines: list[str] = []
        top_keys = list(self.ORDERED_TOP_SECTIONS)
        top_keys.extend(sorted(k for k in data.keys() if k not in self.ORDERED_TOP_SECTIONS))

        for top_key in top_keys:
            section_data = data.get(top_key)
            if not isinstance(section_data, dict):
                continue
            self._append_toml_section(lines, top_key, section_data)

        return "\n".join(lines).rstrip() + "\n"

    def _append_toml_section(self, lines: list[str], path: str, section_data: Dict[str, Any]):
        lines.append(f"[{path}]")
        for key, value in section_data.items():
            if isinstance(value, dict):
                continue
            lines.append(f"{key} = {_toml_literal(value)}")
        lines.append("")

        for key, value in section_data.items():
            if isinstance(value, dict):
                self._append_toml_section(lines, f"{path}.{key}", value)

    def _normalize_top_level_config(self, payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        normalized: Dict[str, Dict[str, Any]] = {}
        for section, value in payload.items():
            if not isinstance(section, str) or not isinstance(value, dict):
                continue
            clean_section = section.strip()
            if not clean_section:
                continue
            normalized[clean_section] = dict(value)
        return normalized

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    @property
    def config_path(self) -> Path:
        return self._config_path

    def reload_config(self):
        self._config = self._load_config()

    def get_merged_config(self) -> Dict[str, Any]:
        return copy.deepcopy(self._config)

    def get_active_env_overrides(self) -> Dict[str, str]:
        active: Dict[str, str] = {}
        for env_key in self.ENV_OVERRIDE_KEYS:
            value = os.getenv(env_key)
            if value is None:
                continue
            text = str(value).strip()
            if text == "":
                continue
            active[env_key] = text
        return active

    def update_config_sections(self, sections: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_top_level_config(sections)
        user_cfg = self._read_user_config()

        for section, payload in normalized.items():
            current = user_cfg.get(section)
            if not isinstance(current, dict):
                current = {}
            for key, value in payload.items():
                current[key] = value
            user_cfg[section] = current

        merged_to_write = _deep_merge(self._defaults(), user_cfg)
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(self._dump_toml(merged_to_write), encoding="utf-8")
        self._config = merged_to_write
        return self.get_merged_config()

    @property
    def server_host(self) -> str:
        return str(os.getenv("FCS_SERVER_HOST", self._get("server", "host", "0.0.0.0")))

    @property
    def server_port(self) -> int:
        value = os.getenv("FCS_SERVER_PORT")
        if value:
            return int(value)
        return int(self._get("server", "port", 8060))

    @property
    def db_path(self) -> Path:
        env_path = os.getenv("FCS_DB_PATH", "").strip()
        raw = env_path or str(self._get("storage", "db_path", "data/captcha_service.db"))
        path = Path(raw)
        if not path.is_absolute():
            path = self._root_dir / path
        return path

    @property
    def admin_username(self) -> str:
        return str(os.getenv("FCS_ADMIN_USERNAME", self._get("admin", "username", "admin")))

    @property
    def admin_password(self) -> str:
        return str(os.getenv("FCS_ADMIN_PASSWORD", self._get("admin", "password", "admin")))

    @property
    def portal_oidc_enabled(self) -> bool:
        return _as_bool(self._get("portal", "oidc_enabled", False), False)

    @property
    def portal_public_base_url(self) -> str:
        return str(self._get("portal", "public_base_url", "")).strip().rstrip("/")

    @property
    def portal_oidc_base_url(self) -> str:
        return str(self._get("portal", "oidc_base_url", "")).strip().rstrip("/")

    @property
    def portal_oidc_well_known_url(self) -> str:
        return str(self._get("portal", "oidc_well_known_url", "")).strip()

    @property
    def portal_oidc_client_id(self) -> str:
        return str(self._get("portal", "oidc_client_id", "")).strip()

    @property
    def portal_oidc_client_secret(self) -> str:
        return str(self._get("portal", "oidc_client_secret", "")).strip()

    @property
    def portal_oidc_scope(self) -> str:
        scope = str(self._get("portal", "oidc_scope", "openid profile email")).strip()
        return scope or "openid profile email"

    @property
    def portal_oauth_only(self) -> bool:
        return _as_bool(self._get("portal", "oauth_only", False), False)

    @property
    def portal_register_bonus_quota(self) -> int:
        try:
            return max(0, int(self._get("portal", "register_bonus_quota", 0)))
        except Exception:
            return 0

    @property
    def portal_checkin_min_quota(self) -> int:
        try:
            return max(0, int(self._get("portal", "checkin_min_quota", 0)))
        except Exception:
            return 0

    @property
    def portal_checkin_max_quota(self) -> int:
        try:
            return max(0, int(self._get("portal", "checkin_max_quota", 0)))
        except Exception:
            return 0

    @property
    def browser_launch_background(self) -> bool:
        value = os.getenv("FCS_BROWSER_LAUNCH_BACKGROUND")
        if value:
            return _as_bool(value, True)
        return _as_bool(self._get("captcha", "browser_launch_background", True), True)

    @property
    def browser_fingerprint_pool_extra_count(self) -> int:
        return _bounded_int_or_fallback(
            os.getenv(
                "FCS_BROWSER_FINGERPRINT_POOL_EXTRA_COUNT",
                self._get("captcha", "browser_fingerprint_pool_extra_count", 100),
            ),
            100,
            0,
        )

    @property
    def browser_custom_page_cache_max_pages(self) -> int:
        return _bounded_int_or_fallback(
            os.getenv(
                "FCS_BROWSER_CUSTOM_PAGE_CACHE_MAX_PAGES",
                self._get("captcha", "browser_custom_page_cache_max_pages", 3),
            ),
            3,
            1,
        )

    @property
    def browser_custom_page_idle_ttl_seconds(self) -> float:
        value = os.getenv("FCS_BROWSER_CUSTOM_PAGE_IDLE_TTL_SECONDS")
        if value:
            try:
                return max(30.0, float(value))
            except Exception:
                return 240.0
        try:
            return max(30.0, float(self._get("captcha", "browser_custom_page_idle_ttl_seconds", 240)))
        except Exception:
            return 240.0

    @property
    def browser_project_affinity_max_keys(self) -> int:
        return _bounded_int_or_fallback(
            os.getenv(
                "FCS_BROWSER_PROJECT_AFFINITY_MAX_KEYS",
                self._get("captcha", "browser_project_affinity_max_keys", 0),
            ),
            0,
            0,
        )

    @property
    def browser_project_affinity_ttl_seconds(self) -> float:
        value = os.getenv("FCS_BROWSER_PROJECT_AFFINITY_TTL_SECONDS")
        if value:
            try:
                return max(60.0, float(value))
            except Exception:
                return 1800.0
        try:
            return max(60.0, float(self._get("captcha", "browser_project_affinity_ttl_seconds", 1800)))
        except Exception:
            return 1800.0

    @property
    def browser_score_dom_wait_seconds(self) -> float:
        value = os.getenv("FCS_BROWSER_SCORE_DOM_WAIT_SECONDS")
        if value:
            return float(value)
        return float(self._get("captcha", "browser_score_dom_wait_seconds", 25))

    @property
    def browser_recaptcha_settle_seconds(self) -> float:
        value = os.getenv("FCS_BROWSER_RECAPTCHA_SETTLE_SECONDS")
        if value:
            return float(value)
        return float(self._get("captcha", "browser_recaptcha_settle_seconds", 3))

    @property
    def browser_standby_token_pool_enabled(self) -> bool:
        value = os.getenv("FCS_BROWSER_STANDBY_TOKEN_POOL_ENABLED")
        if value:
            return _as_bool(value, True)
        return _as_bool(self._get("captcha", "browser_standby_token_pool_enabled", True), True)

    @property
    def browser_standby_token_ttl_seconds(self) -> float:
        value = os.getenv("FCS_BROWSER_STANDBY_TOKEN_TTL_SECONDS")
        if value:
            try:
                return max(5.0, float(value))
            except Exception:
                return 45.0
        try:
            return max(5.0, float(self._get("captcha", "browser_standby_token_ttl_seconds", 90)))
        except Exception:
            return 90.0

    @property
    def browser_standby_token_pool_depth(self) -> int:
        value = os.getenv("FCS_BROWSER_STANDBY_TOKEN_POOL_DEPTH")
        if value:
            try:
                return max(0, min(8, int(value)))
            except Exception:
                return 2
        try:
            return max(0, min(8, int(self._get("captcha", "browser_standby_token_pool_depth", 2))))
        except Exception:
            return 2

    @property
    def browser_standby_bucket_max_count(self) -> int:
        return _bounded_int_or_fallback(
            os.getenv(
                "FCS_BROWSER_STANDBY_BUCKET_MAX_COUNT",
                self._get("captcha", "browser_standby_bucket_max_count", 0),
            ),
            0,
            0,
        )

    @property
    def browser_standby_bucket_idle_ttl_seconds(self) -> float:
        value = os.getenv("FCS_BROWSER_STANDBY_BUCKET_IDLE_TTL_SECONDS")
        if value is not None:
            text = str(value).strip()
            if text == "":
                return 0.0
            try:
                number = float(text)
            except Exception:
                return 0.0
            if number <= 0:
                return 0.0
            return max(30.0, number)
        try:
            raw_value = self._get("captcha", "browser_standby_bucket_idle_ttl_seconds", 0)
            return max(0.0, float(raw_value))
        except Exception:
            return 0.0

    @property
    def browser_standby_refill_idle_seconds(self) -> float:
        value = os.getenv("FCS_BROWSER_STANDBY_REFILL_IDLE_SECONDS")
        if value:
            try:
                return max(0.0, float(value))
            except Exception:
                return 0.8
        try:
            return max(0.0, float(self._get("captcha", "browser_standby_refill_idle_seconds", 0.8)))
        except Exception:
            return 0.8

    @property
    def browser_score_test_warmup_seconds(self) -> float:
        value = os.getenv("FCS_BROWSER_SCORE_TEST_WARMUP_SECONDS")
        if value:
            return float(value)
        return float(self._get("captcha", "browser_score_test_warmup_seconds", 12))

    @property
    def browser_idle_ttl_seconds(self) -> int:
        value = os.getenv("FCS_BROWSER_IDLE_TTL_SECONDS")
        if value:
            try:
                return max(60, int(value))
            except Exception:
                return 600
        try:
            return max(60, int(self._get("captcha", "browser_idle_ttl_seconds", 600)))
        except Exception:
            return 600

    @property
    def browser_retry_max_attempts(self) -> int:
        return _bounded_int_or_fallback(
            os.getenv(
                "FCS_BROWSER_RETRY_MAX_ATTEMPTS",
                self._get("captcha", "browser_retry_max_attempts", 3),
            ),
            3,
            1,
        )

    @property
    def browser_retry_backoff_seconds(self) -> float:
        value = os.getenv("FCS_BROWSER_RETRY_BACKOFF_SECONDS")
        if value:
            try:
                return max(0.0, float(value))
            except Exception:
                return 1.0
        try:
            return max(0.0, float(self._get("captcha", "browser_retry_backoff_seconds", 1)))
        except Exception:
            return 1.0

    @property
    def browser_execute_timeout_seconds(self) -> float:
        value = os.getenv("FCS_BROWSER_EXECUTE_TIMEOUT_SECONDS")
        if value is not None:
            text = str(value).strip()
            if text == "":
                return 0.0
            try:
                number = float(text)
            except Exception:
                return 0.0
            if number <= 0:
                return 0.0
            return max(5.0, number)
        try:
            raw_value = self._get("captcha", "browser_execute_timeout_seconds", 0)
            return max(0.0, float(raw_value))
        except Exception:
            return 0.0

    @property
    def browser_reload_wait_timeout_seconds(self) -> float:
        value = os.getenv("FCS_BROWSER_RELOAD_WAIT_TIMEOUT_SECONDS")
        if value:
            try:
                return max(1.0, float(value))
            except Exception:
                return 12.0
        try:
            return max(1.0, float(self._get("captcha", "browser_reload_wait_timeout_seconds", 12)))
        except Exception:
            return 12.0

    @property
    def browser_clr_wait_timeout_seconds(self) -> float:
        value = os.getenv("FCS_BROWSER_CLR_WAIT_TIMEOUT_SECONDS")
        if value:
            try:
                return max(1.0, float(value))
            except Exception:
                return 12.0
        try:
            return max(1.0, float(self._get("captcha", "browser_clr_wait_timeout_seconds", 12)))
        except Exception:
            return 12.0

    @property
    def browser_idle_reaper_interval_seconds(self) -> float:
        value = os.getenv("FCS_BROWSER_IDLE_REAPER_INTERVAL_SECONDS")
        if value:
            try:
                return max(1.0, float(value))
            except Exception:
                return 15.0
        try:
            return max(1.0, float(self._get("captcha", "browser_idle_reaper_interval_seconds", 15)))
        except Exception:
            return 15.0

    @property
    def browser_request_finish_image_wait_seconds(self) -> int:
        return _bounded_int_or_fallback(
            os.getenv(
                "FCS_BROWSER_REQUEST_FINISH_IMAGE_WAIT_SECONDS",
                self._get("captcha", "browser_request_finish_image_wait_seconds", 0),
            ),
            0,
            0,
        )

    @property
    def browser_request_finish_non_image_wait_seconds(self) -> int:
        return _bounded_int_or_fallback(
            os.getenv(
                "FCS_BROWSER_REQUEST_FINISH_NON_IMAGE_WAIT_SECONDS",
                self._get("captcha", "browser_request_finish_non_image_wait_seconds", 0),
            ),
            0,
            0,
        )

    @property
    def flow_timeout(self) -> int:
        value = os.getenv("FCS_FLOW_TIMEOUT")
        if value:
            return int(value)
        return int(self._get("captcha", "flow_timeout", 300))

    @property
    def upsample_timeout(self) -> int:
        value = os.getenv("FCS_UPSAMPLE_TIMEOUT")
        if value:
            return int(value)
        return int(self._get("captcha", "upsample_timeout", 300))

    @property
    def session_ttl_seconds(self) -> int:
        value = os.getenv("FCS_SESSION_TTL_SECONDS")
        if value:
            return max(120, int(value))
        return max(120, int(self._get("captcha", "session_ttl_seconds", 1200)))

    @property
    def node_name(self) -> str:
        return str(os.getenv("FCS_NODE_NAME", self._get("captcha", "node_name", "standalone-node")))

    @property
    def browser_count(self) -> int:
        value = os.getenv("FCS_BROWSER_COUNT")
        if value:
            return max(1, int(value))
        return max(1, int(self._get("captcha", "browser_count", 1)))

    @property
    def browser_proxy_enabled(self) -> bool:
        value = os.getenv("FCS_BROWSER_PROXY_ENABLED")
        if value:
            return _as_bool(value, False)
        return _as_bool(self._get("captcha", "browser_proxy_enabled", False), False)

    @property
    def browser_proxy_url(self) -> str:
        return str(os.getenv("FCS_BROWSER_PROXY_URL", self._get("captcha", "browser_proxy_url", "")))

    @property
    def log_level(self) -> str:
        return str(os.getenv("FCS_LOG_LEVEL", self._get("log", "level", "INFO"))).upper()

    @property
    def log_storage_backend(self) -> str:
        value = str(
            os.getenv(
                "FCS_LOG_STORAGE_BACKEND",
                self._get("log", "storage_backend", "sqlite"),
            )
        ).strip().lower()
        return value if value in {"sqlite", "redis"} else "sqlite"

    @property
    def log_redis_url(self) -> str:
        return str(os.getenv("FCS_LOG_REDIS_URL", self._get("log", "redis_url", ""))).strip()

    @property
    def log_redis_key_prefix(self) -> str:
        return str(
            os.getenv(
                "FCS_LOG_REDIS_KEY_PREFIX",
                self._get("log", "redis_key_prefix", "fcs"),
            )
        ).strip() or "fcs"

    @property
    def log_redis_max_entries(self) -> int:
        return _bounded_int_or_fallback(
            os.getenv("FCS_LOG_REDIS_MAX_ENTRIES", self._get("log", "redis_max_entries", 20000)),
            20000,
            100,
        )

    @property
    def log_startup_clear_on_boot(self) -> bool:
        return _as_bool(
            os.getenv(
                "FCS_LOG_STARTUP_CLEAR_ON_BOOT",
                self._get("log", "startup_clear_on_boot", True),
            ),
            True,
        )

    @property
    def log_auto_clear_interval_minutes(self) -> int:
        return _bounded_int_or_fallback(
            os.getenv(
                "FCS_LOG_AUTO_CLEAR_INTERVAL_MINUTES",
                self._get("log", "auto_clear_interval_minutes", 0),
            ),
            0,
            0,
        )

    @property
    def log_redis_enabled(self) -> bool:
        return self.log_storage_backend == "redis"

    @property
    def cluster_role(self) -> str:
        return str(os.getenv("FCS_CLUSTER_ROLE", self._get("cluster", "role", "standalone"))).strip().lower()

    @property
    def cluster_master_base_url(self) -> str:
        return str(
            os.getenv(
                "FCS_CLUSTER_MASTER_BASE_URL",
                self._get("cluster", "master_base_url", ""),
            )
        ).strip().rstrip("/")

    @property
    def cluster_master_cluster_key(self) -> str:
        return str(
            os.getenv(
                "FCS_CLUSTER_MASTER_CLUSTER_KEY",
                self._get("cluster", "master_cluster_key", ""),
            )
        ).strip()

    @property
    def cluster_node_public_base_url(self) -> str:
        return str(
            os.getenv(
                "FCS_CLUSTER_NODE_PUBLIC_BASE_URL",
                self._get("cluster", "node_public_base_url", ""),
            )
        ).strip().rstrip("/")

    @property
    def node_api_key(self) -> str:
        return str(
            os.getenv(
                "FCS_CLUSTER_NODE_API_KEY",
                self._get("cluster", "node_api_key", ""),
            )
        ).strip()

    @property
    def cluster_heartbeat_interval_seconds(self) -> int:
        value = os.getenv("FCS_CLUSTER_HEARTBEAT_INTERVAL_SECONDS")
        if value:
            return max(5, int(value))
        return max(5, int(self._get("cluster", "heartbeat_interval_seconds", 15)))

    @property
    def cluster_node_weight(self) -> int:
        value = os.getenv("FCS_CLUSTER_NODE_WEIGHT")
        if value:
            return max(1, int(value))
        return max(1, int(self._get("cluster", "node_weight", 100)))

    @property
    def cluster_node_max_concurrency(self) -> int:
        fallback = self.browser_count
        env_value = os.getenv("FCS_CLUSTER_NODE_MAX_CONCURRENCY")
        if env_value is not None and str(env_value).strip() != "":
            return _positive_int_or_fallback(env_value, fallback)

        raw_user_cfg = self._read_user_config()
        cluster_cfg = raw_user_cfg.get("cluster", {}) if isinstance(raw_user_cfg, dict) else {}
        explicit_value = cluster_cfg.get("node_max_concurrency") if isinstance(cluster_cfg, dict) else None
        return _positive_int_or_fallback(explicit_value, fallback)

    @property
    def cluster_master_node_stale_seconds(self) -> int:
        value = os.getenv("FCS_CLUSTER_MASTER_NODE_STALE_SECONDS")
        if value:
            return max(10, int(value))
        return max(10, int(self._get("cluster", "master_node_stale_seconds", 120)))

    @property
    def cluster_master_dispatch_timeout_seconds(self) -> int:
        value = os.getenv("FCS_CLUSTER_MASTER_DISPATCH_TIMEOUT_SECONDS")
        if value:
            return max(5, int(value))
        return max(5, int(self._get("cluster", "master_dispatch_timeout_seconds", 45)))

    @property
    def cluster_node_identifier(self) -> str:
        return os.getenv("CLUSTER_NODE_IDENTIFIER", "node0")

    @property
    def browser_fingerprint_mode(self) -> str:
        return os.getenv("BROWSER_FINGERPRINT_MODE", "random")

    @property
    def browser_captcha_dwell_seconds(self) -> float:
        try:
            return max(0.0, float(os.getenv("BROWSER_CAPTCHA_DWELL_SECONDS", "0")))
        except Exception:
            return 0.0

    @property
    def browser_dwell_background_only(self) -> bool:
        return os.getenv("BROWSER_DWELL_BACKGROUND_ONLY", "true").lower() != "false"

    @property
    def browser_solve_min_interval_seconds(self) -> float:
        try:
            return max(0.0, float(os.getenv("BROWSER_SOLVE_MIN_INTERVAL_SECONDS", "0")))
        except Exception:
            return 0.0


config = Config()
