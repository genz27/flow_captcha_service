from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

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


class Config:
    def __init__(self):
        self._root_dir = Path(__file__).resolve().parents[2]
        default_path = self._root_dir / "config" / "setting.toml"
        env_path = os.getenv("FCS_CONFIG_FILE", "").strip()
        self._config_path = Path(env_path) if env_path else default_path
        self._config = self._load_config()

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
            "captcha": {
                "browser_count": 1,
                "browser_proxy_enabled": False,
                "browser_proxy_url": "",
                "browser_launch_background": True,
                "browser_score_dom_wait_seconds": 25,
                "browser_recaptcha_settle_seconds": 3,
                "browser_score_test_warmup_seconds": 12,
                "flow_timeout": 300,
                "upsample_timeout": 300,
                "session_ttl_seconds": 7200,
                "node_name": "standalone-node",
            },
            "log": {
                "level": "INFO",
            },
            "cluster": {
                "role": "standalone",
            },
        }

    def _load_config(self) -> Dict[str, Any]:
        config_data = self._defaults()
        if self._config_path.exists():
            raw_text = self._config_path.read_text(encoding="utf-8-sig")
            user_cfg = tomli.loads(raw_text)
            if isinstance(user_cfg, dict):
                config_data = _deep_merge(config_data, user_cfg)
        return config_data

    def _get(self, section: str, key: str, default: Any = None) -> Any:
        return self._config.get(section, {}).get(key, default)

    @property
    def root_dir(self) -> Path:
        return self._root_dir

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
    def browser_launch_background(self) -> bool:
        value = os.getenv("FCS_BROWSER_LAUNCH_BACKGROUND")
        if value:
            return _as_bool(value, True)
        return _as_bool(self._get("captcha", "browser_launch_background", True), True)

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
    def browser_score_test_warmup_seconds(self) -> float:
        value = os.getenv("FCS_BROWSER_SCORE_TEST_WARMUP_SECONDS")
        if value:
            return float(value)
        return float(self._get("captcha", "browser_score_test_warmup_seconds", 12))

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
            return int(value)
        return int(self._get("captcha", "session_ttl_seconds", 7200))

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
    def cluster_role(self) -> str:
        return str(os.getenv("FCS_CLUSTER_ROLE", self._get("cluster", "role", "standalone"))).strip().lower()


config = Config()
