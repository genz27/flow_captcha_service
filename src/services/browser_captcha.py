"""
基于 RT 的本地 reCAPTCHA 打码服务 (终极闭环版 - 无 fake_useragent 纯净版)
支持：自动刷新 Session Token、外部触发指纹切换、死磕重试
"""
import os
import sys
import subprocess
import signal
# 修复 Windows 上 playwright 的 asyncio 兼容性问题
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

import asyncio
import hashlib
import time
import re
import random
import uuid
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Set, Union
from datetime import datetime
from urllib.parse import urlparse, unquote, parse_qs

from ..core.logger import debug_logger
from ..core.config import config


# ==================== Docker 环境检测 ====================
def _is_running_in_docker() -> bool:
    """检测是否在 Docker 容器中运行"""
    # 方法1: 检查 /.dockerenv 文件
    if os.path.exists('/.dockerenv'):
        return True
    # 方法2: 检查 cgroup
    try:
        with open('/proc/1/cgroup', 'r') as f:
            content = f.read()
            if 'docker' in content or 'kubepods' in content or 'containerd' in content:
                return True
    except:
        pass
    # 方法3: 检查环境变量
    if os.environ.get('DOCKER_CONTAINER') or os.environ.get('KUBERNETES_SERVICE_HOST'):
        return True
    return False


IS_DOCKER = _is_running_in_docker()


def _is_truthy_env(name: str) -> bool:
    """判断环境变量是否为 true。"""
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


ALLOW_DOCKER_HEADED = (
    _is_truthy_env("ALLOW_DOCKER_HEADED_CAPTCHA")
    or _is_truthy_env("ALLOW_DOCKER_BROWSER_CAPTCHA")
)
DOCKER_HEADED_BLOCKED = IS_DOCKER and not ALLOW_DOCKER_HEADED


# ==================== playwright 自动安装 ====================
def _run_pip_install(package: str, use_mirror: bool = False) -> bool:
    """运行 pip install 命令"""
    cmd = [sys.executable, '-m', 'pip', 'install', package]
    if use_mirror:
        cmd.extend(['-i', 'https://pypi.tuna.tsinghua.edu.cn/simple'])
    
    try:
        debug_logger.log_info(f"[BrowserCaptcha] 正在安装 {package}...")
        print(f"[BrowserCaptcha] 正在安装 {package}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            debug_logger.log_info(f"[BrowserCaptcha] ✅ {package} 安装成功")
            print(f"[BrowserCaptcha] ✅ {package} 安装成功")
            return True
        else:
            debug_logger.log_warning(f"[BrowserCaptcha] {package} 安装失败: {result.stderr[:200]}")
            return False
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] {package} 安装异常: {e}")
        return False


def _run_playwright_install(use_mirror: bool = False) -> bool:
    """安装 playwright chromium 浏览器"""
    cmd = [sys.executable, '-m', 'playwright', 'install', 'chromium']
    env = os.environ.copy()
    
    if use_mirror:
        # 使用国内镜像
        env['PLAYWRIGHT_DOWNLOAD_HOST'] = 'https://npmmirror.com/mirrors/playwright'
    
    try:
        debug_logger.log_info("[BrowserCaptcha] 正在安装 chromium 浏览器...")
        print("[BrowserCaptcha] 正在安装 chromium 浏览器...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
        if result.returncode == 0:
            debug_logger.log_info("[BrowserCaptcha] ✅ chromium 浏览器安装成功")
            print("[BrowserCaptcha] ✅ chromium 浏览器安装成功")
            return True
        else:
            debug_logger.log_warning(f"[BrowserCaptcha] chromium 安装失败: {result.stderr[:200]}")
            return False
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] chromium 安装异常: {e}")
        return False


def _ensure_playwright_installed() -> bool:
    """确保 playwright 已安装"""
    try:
        import playwright
        debug_logger.log_info("[BrowserCaptcha] playwright 已安装")
        return True
    except ImportError:
        pass
    
    debug_logger.log_info("[BrowserCaptcha] playwright 未安装，开始自动安装...")
    print("[BrowserCaptcha] playwright 未安装，开始自动安装...")
    
    # 先尝试官方源
    if _run_pip_install('playwright', use_mirror=False):
        return True
    
    # 官方源失败，尝试国内镜像
    debug_logger.log_info("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    print("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    if _run_pip_install('playwright', use_mirror=True):
        return True
    
    debug_logger.log_error("[BrowserCaptcha] ❌ playwright 自动安装失败，请手动安装: pip install playwright")
    print("[BrowserCaptcha] ❌ playwright 自动安装失败，请手动安装: pip install playwright")
    return False


def _ensure_browser_installed() -> bool:
    """确保 chromium 浏览器已安装"""
    try:
        detect_script = (
            "from playwright.sync_api import sync_playwright\n"
            "with sync_playwright() as p:\n"
            "    print(p.chromium.executable_path or '')\n"
        )
        env = os.environ.copy()
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "0") or "0")
        result = subprocess.run(
            [sys.executable, "-c", detect_script],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        browser_path = (result.stdout or "").strip().splitlines()
        browser_path = browser_path[-1].strip() if browser_path else ""
        if result.returncode == 0 and browser_path and os.path.exists(browser_path):
            debug_logger.log_info(f"[BrowserCaptcha] chromium 浏览器已安装: {browser_path}")
            return True
    except Exception as e:
        debug_logger.log_info(f"[BrowserCaptcha] 检测浏览器时出错: {e}")
    
    debug_logger.log_info("[BrowserCaptcha] chromium 浏览器未安装，开始自动安装...")
    print("[BrowserCaptcha] chromium 浏览器未安装，开始自动安装...")
    
    # 先尝试官方源
    if _run_playwright_install(use_mirror=False):
        return True
    
    # 官方源失败，尝试国内镜像
    debug_logger.log_info("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    print("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    if _run_playwright_install(use_mirror=True):
        return True
    
    debug_logger.log_error("[BrowserCaptcha] ❌ chromium 浏览器自动安装失败，请手动安装: python -m playwright install chromium")
    print("[BrowserCaptcha] ❌ chromium 浏览器自动安装失败，请手动安装: python -m playwright install chromium")
    return False


# 尝试导入 playwright
async_playwright = None
Route = None
BrowserContext = None
PLAYWRIGHT_AVAILABLE = False

if DOCKER_HEADED_BLOCKED:
    debug_logger.log_warning(
        "[BrowserCaptcha] 检测到 Docker 环境，默认禁用有头浏览器打码。"
        "如需启用请设置 ALLOW_DOCKER_HEADED_CAPTCHA=true，并提供 DISPLAY/Xvfb。"
    )
    print("[BrowserCaptcha] ⚠️ 检测到 Docker 环境，默认禁用有头浏览器打码")
    print("[BrowserCaptcha] 如需启用请设置 ALLOW_DOCKER_HEADED_CAPTCHA=true，并提供 DISPLAY/Xvfb")
else:
    if IS_DOCKER and ALLOW_DOCKER_HEADED:
        debug_logger.log_warning(
            "[BrowserCaptcha] Docker 有头浏览器打码白名单已启用，请确保 DISPLAY/Xvfb 可用"
        )
        print("[BrowserCaptcha] ✅ Docker 有头浏览器打码白名单已启用")
    if _ensure_playwright_installed():
        try:
            from playwright.async_api import async_playwright, Route, BrowserContext
            PLAYWRIGHT_AVAILABLE = True
            # 检查并安装浏览器
            _ensure_browser_installed()
        except ImportError as e:
            debug_logger.log_error(f"[BrowserCaptcha] playwright 导入失败: {e}")
            print(f"[BrowserCaptcha] ❌ playwright 导入失败: {e}")


# 配置
LABS_URL = "https://labs.google/fx/tools/flow"

# ==========================================
# 代理解析工具函数
# ==========================================
def parse_proxy_url(proxy_url: str) -> Optional[Dict[str, str]]:
    """解析代理URL"""
    if not proxy_url: return None
    if not re.match(r'^(http|https|socks5)://', proxy_url): proxy_url = f"http://{proxy_url}"
    match = re.match(r'^(socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$', proxy_url)
    if match:
        protocol, username, password, host, port = match.groups()
        proxy_config = {'server': f'{protocol}://{host}:{port}'}
        if username and password:
            proxy_config['username'] = username
            proxy_config['password'] = password
        return proxy_config
    return None

def normalize_browser_proxy_url(proxy_url: str) -> tuple[Optional[str], Optional[str]]:
    """将浏览器代理标准化为 Playwright/Chromium 可接受的格式。

    Chromium 不支持带账号密码的 socks5 代理认证。
    对于 `socks5://user:pass@host:port`，自动降级为 `http://user:pass@host:port`，
    方便兼容同时提供 HTTP/SOCKS5 双入口的代理服务商。

    Returns:
        (normalized_proxy_url, warning_message)
    """
    if not proxy_url:
        return None, None

    proxy_url = proxy_url.strip()
    match = re.match(r'^(socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$', proxy_url)
    if not match:
        if not re.match(r'^(http|https|socks5)://', proxy_url):
            proxy_url = f"http://{proxy_url}"
        return proxy_url, None

    protocol, username, password, host, port = match.groups()
    if protocol == "socks5" and username and password:
        normalized = f"http://{username}:{password}@{host}:{port}"
        warning = (
            "检测到带认证的 SOCKS5 代理。"
            "Chromium 不支持 socks5 用户名密码认证，"
            f"已自动改用 HTTP 代理启动浏览器: http://{host}:{port}"
        )
        return normalized, warning

    return proxy_url, None

def split_browser_proxy_pool(proxy_value: str) -> List[str]:
    """Split a proxy pool string into a list using newlines, commas, or semicolons."""
    if not proxy_value:
        return []
    parts = re.split(r"[\n,;]+", str(proxy_value))
    return [part.strip() for part in parts if part and part.strip()]


def normalize_browser_proxy_pool(proxy_value: str) -> tuple[List[str], List[str]]:
    """Normalize the proxy pool and return any warning messages."""
    normalized: List[str] = []
    warnings: List[str] = []
    for index, raw_proxy in enumerate(split_browser_proxy_pool(proxy_value), start=1):
        normalized_proxy, warning_message = normalize_browser_proxy_url(raw_proxy)
        if not normalized_proxy:
            continue
        normalized.append(normalized_proxy)
        if warning_message:
            warnings.append(f"Proxy #{index}: {warning_message}")
    return normalized, warnings


def validate_browser_proxy_url(proxy_url: str) -> tuple[bool, str]:
    proxy_pool = split_browser_proxy_pool(proxy_url)
    if not proxy_pool:
        return True, None

    for index, raw_proxy in enumerate(proxy_pool, start=1):
        normalized_proxy_url, _ = normalize_browser_proxy_url(raw_proxy)
        parsed = parse_proxy_url(normalized_proxy_url)
        if not parsed:
            return False, f"Proxy pool entry {index} has an invalid format"

    return True, None


def _build_user_agent_pool(base_user_agents: List[str], *, extra_count: int = 100) -> List[str]:
    """在保留已验证 UA 的基础上，扩充更多候选，减少指纹重复率。"""
    normalized_pool: List[str] = []
    seen: Set[str] = set()
    for raw_user_agent in base_user_agents:
        if not isinstance(raw_user_agent, str):
            continue
        user_agent = raw_user_agent.strip()
        if not user_agent or user_agent in seen:
            continue
        normalized_pool.append(user_agent)
        seen.add(user_agent)

    target_total = len(normalized_pool) + max(0, int(extra_count))
    if len(normalized_pool) >= target_total:
        return normalized_pool

    candidate_pool: List[str] = []

    windows_platforms = [
        "Windows NT 10.0; Win64; x64",
        "Windows NT 10.0; WOW64",
    ]
    windows_chrome_builds = [
        "132.0.6834.84",
        "132.0.6834.111",
        "132.0.6834.159",
        "131.0.6778.141",
        "131.0.6778.205",
        "131.0.6778.243",
        "130.0.6723.91",
        "130.0.6723.117",
        "129.0.6668.70",
        "129.0.6668.101",
        "128.0.6613.84",
        "128.0.6613.120",
    ]
    for platform in windows_platforms:
        for chrome_build in windows_chrome_builds:
            candidate_pool.append(
                f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{chrome_build} Safari/537.36"
            )

    windows_edge_pairs = [
        ("132.0.6834.159", "132.0.2957.115"),
        ("132.0.6834.111", "132.0.2957.140"),
        ("131.0.6778.243", "131.0.2903.99"),
        ("131.0.6778.205", "131.0.2903.112"),
        ("130.0.6723.117", "130.0.2849.80"),
        ("130.0.6723.91", "130.0.2849.96"),
        ("129.0.6668.101", "129.0.2792.65"),
        ("128.0.6613.120", "128.0.2739.79"),
    ]
    for platform in windows_platforms:
        for chrome_build, edge_build in windows_edge_pairs:
            candidate_pool.append(
                f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{chrome_build} Safari/537.36 Edg/{edge_build}"
            )

    firefox_versions = [
        "134.0.1",
        "134.0.2",
        "133.0.3",
        "132.0.2",
        "131.0",
        "130.0.1",
    ]
    for platform in windows_platforms:
        for firefox_version in firefox_versions:
            candidate_pool.append(
                f"Mozilla/5.0 ({platform}; rv:{firefox_version}) Gecko/20100101 Firefox/{firefox_version}"
            )

    mac_platforms = [
        "Macintosh; Intel Mac OS X 14_5_0",
        "Macintosh; Intel Mac OS X 14_4_0",
        "Macintosh; Intel Mac OS X 13_6_6",
        "Macintosh; Intel Mac OS X 12_7_6",
    ]
    mac_chrome_builds = [
        "132.0.6834.84",
        "132.0.6834.159",
        "131.0.6778.141",
        "131.0.6778.243",
        "130.0.6723.117",
        "129.0.6668.101",
    ]
    for platform in mac_platforms:
        for chrome_build in mac_chrome_builds:
            candidate_pool.append(
                f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{chrome_build} Safari/537.36"
            )

    mac_edge_pairs = [
        ("132.0.6834.159", "132.0.2957.115"),
        ("131.0.6778.243", "131.0.2903.112"),
        ("130.0.6723.117", "130.0.2849.96"),
        ("129.0.6668.101", "129.0.2792.65"),
    ]
    for platform in mac_platforms:
        for chrome_build, edge_build in mac_edge_pairs:
            candidate_pool.append(
                f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{chrome_build} Safari/537.36 Edg/{edge_build}"
            )

    android_profiles = [
        ("Android 14; Pixel 8", "132.0.6834.111"),
        ("Android 14; SM-S9280", "132.0.6834.159"),
        ("Android 14; OnePlus 12", "131.0.6778.243"),
        ("Android 13; Pixel 7 Pro", "131.0.6778.205"),
        ("Android 13; Xiaomi 14", "130.0.6723.117"),
        ("Android 13; V2318A", "130.0.6723.91"),
        ("Android 12; CPH2451", "129.0.6668.101"),
        ("Android 12; RMX3840", "128.0.6613.120"),
    ]
    for device_token, chrome_build in android_profiles:
        candidate_pool.append(
            f"Mozilla/5.0 (Linux; {device_token}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chrome_build} Mobile Safari/537.36"
        )

    for candidate in candidate_pool:
        if candidate in seen:
            continue
        normalized_pool.append(candidate)
        seen.add(candidate)
        if len(normalized_pool) >= target_total:
            break

    return normalized_pool


def _classify_user_agent_platform(user_agent: str) -> str:
    normalized = str(user_agent or "").lower()
    if "iphone" in normalized or "ipad" in normalized or "crios" in normalized or "edgios" in normalized:
        return "ios"
    if "android" in normalized or "mobile" in normalized or "samsungbrowser" in normalized or "edga/" in normalized:
        return "android"
    if "mac os x" in normalized or "macintosh" in normalized:
        return "mac"
    if "linux" in normalized:
        return "linux"
    return "windows"


@dataclass(frozen=True)
class BrowserProfile:
    user_agent: str
    viewport: Dict[str, int]
    locale: str
    timezone_id: str
    accept_language: str
    device_scale_factor: float = 1.0
    is_mobile: bool = False
    has_touch: bool = False
    profile_family: str = "desktop"


def _build_browser_profile_pool(
    user_agents: List[str],
    *,
    desktop_resolutions: List[tuple[int, int]],
) -> List[BrowserProfile]:
    """为 UA 绑定更完整的 profile，避免只随机 UA 和 viewport。"""
    desktop_regions = [
        ("zh-CN", "zh-CN,zh;q=0.9,en;q=0.8", "Asia/Shanghai"),
        ("en-US", "en-US,en;q=0.9", "America/Los_Angeles"),
        ("en-GB", "en-GB,en;q=0.9", "Europe/London"),
        ("ja-JP", "ja-JP,ja;q=0.9,en;q=0.7", "Asia/Tokyo"),
    ]
    mobile_regions = [
        ("zh-CN", "zh-CN,zh;q=0.9,en;q=0.8", "Asia/Shanghai"),
        ("en-US", "en-US,en;q=0.9", "America/Los_Angeles"),
        ("en-SG", "en-SG,en;q=0.9,zh-CN;q=0.6", "Asia/Singapore"),
        ("ja-JP", "ja-JP,ja;q=0.9,en;q=0.7", "Asia/Tokyo"),
    ]
    iphone_viewports = [
        {"width": 393, "height": 852},
        {"width": 430, "height": 932},
        {"width": 390, "height": 844},
    ]
    android_viewports = [
        {"width": 412, "height": 915},
        {"width": 384, "height": 854},
        {"width": 360, "height": 800},
        {"width": 412, "height": 892},
    ]
    tablet_viewports = [
        {"width": 820, "height": 1180},
        {"width": 768, "height": 1024},
        {"width": 800, "height": 1280},
    ]

    profiles: List[BrowserProfile] = []
    for user_agent in user_agents:
        digest = int(hashlib.sha256(user_agent.encode("utf-8")).hexdigest()[:8], 16)
        platform_family = _classify_user_agent_platform(user_agent)

        if platform_family == "ios":
            viewport = dict(iphone_viewports[digest % len(iphone_viewports)])
            locale, accept_language, timezone_id = mobile_regions[digest % len(mobile_regions)]
            profiles.append(
                BrowserProfile(
                    user_agent=user_agent,
                    viewport=viewport,
                    locale=locale,
                    timezone_id=timezone_id,
                    accept_language=accept_language,
                    device_scale_factor=3.0,
                    is_mobile=True,
                    has_touch=True,
                    profile_family="mobile",
                )
            )
            continue

        if platform_family == "android":
            viewport_source = android_viewports if "mobile" in user_agent.lower() else tablet_viewports
            viewport = dict(viewport_source[digest % len(viewport_source)])
            locale, accept_language, timezone_id = mobile_regions[digest % len(mobile_regions)]
            profiles.append(
                BrowserProfile(
                    user_agent=user_agent,
                    viewport=viewport,
                    locale=locale,
                    timezone_id=timezone_id,
                    accept_language=accept_language,
                    device_scale_factor=3.0 if viewport["width"] <= 430 else 2.0,
                    is_mobile=True,
                    has_touch=True,
                    profile_family="mobile" if viewport["width"] <= 430 else "tablet",
                )
            )
            continue

        resolution = desktop_resolutions[digest % len(desktop_resolutions)]
        locale, accept_language, timezone_id = desktop_regions[digest % len(desktop_regions)]
        width, height = resolution
        height = max(640, height - (digest % 96))
        device_scale_factor = 2.0 if platform_family == "mac" and width >= 1400 else 1.0
        profiles.append(
            BrowserProfile(
                user_agent=user_agent,
                viewport={"width": width, "height": height},
                locale=locale,
                timezone_id=timezone_id,
                accept_language=accept_language,
                device_scale_factor=device_scale_factor,
                is_mobile=False,
                has_touch=False,
                profile_family="desktop",
            )
        )

    return profiles


@dataclass
class TokenAcquireResult:
    token: Optional[str]
    browser_ref: Optional[Union[int, str]]
    browser_id: Optional[int]
    fingerprint: Optional[Dict[str, Any]] = None
    source: str = "live"
    elapsed_ms: int = 0
    browser_epoch: int = 0
    timings: Optional[Dict[str, int]] = None


@dataclass
class StandbyTokenEntry:
    token: str
    browser_id: int
    fingerprint: Optional[Dict[str, Any]]
    browser_epoch: int
    project_id: str
    action: str
    proxy_signature: str
    created_monotonic: float
    expires_monotonic: float


class TokenBrowser:
    """简化版浏览器：每次获取 token 时启动新浏览器，用完即关
    
    每次都是新的随机 UA，避免长时间运行导致的各种问题
    """
    # 保留原始已验证 UA，同时在类定义末尾自动扩充 100 条候选。
    _BASE_UA_LIST = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.265 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.177 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36 Edg/132.0.2957.171",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.265 Safari/537.36 Edg/131.0.2903.146",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Safari/537.36 Edg/130.0.2849.142",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.177 Safari/537.36 Edg/129.0.2792.124",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Safari/537.36 Edg/128.0.2739.111",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.265 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36 Edg/132.0.2957.171",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.265 Safari/537.36 Edg/131.0.2903.146",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Safari/537.36 Edg/130.0.2849.142",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.177 Safari/537.36 Edg/129.0.2792.124",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Safari/537.36 Edg/128.0.2739.111",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:134.0) Gecko/20100101 Firefox/134.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.3; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.2; rv:132.0) Gecko/20100101 Firefox/132.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.1; rv:131.0) Gecko/20100101 Firefox/131.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:129.0) Gecko/20100101 Firefox/129.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.163 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 14; SM-S9180) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.260 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 12; M2102J20SG) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.177 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 11; M2012K11AC) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 14; SM-S9180) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.260 Mobile Safari/537.36 EdgA/131.0.2903.146",
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Mobile Safari/537.36 EdgA/130.0.2849.142",
        "Mozilla/5.0 (Linux; Android 12; M2102J20SG) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.177 Mobile Safari/537.36 EdgA/129.0.2792.124",
        "Mozilla/5.0 (Linux; Android 11; M2012K11AC) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Mobile Safari/537.36 EdgA/128.0.2739.111",
        "Mozilla/5.0 (Linux; Android 14; SM-S9180) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/28.0 Chrome/132.0.6834.163 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 13; SM-S9110) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/27.0 Chrome/130.0.6723.172 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 12; SM-G9910) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/26.0 Chrome/128.0.6613.186 Mobile Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/132.0.6834.95 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/131.0.6778.112 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/132.2957.171 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/131.2903.146 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36 Edg/132.0.2957.171",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36 Edg/132.0.2957.171",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36 OPR/117.0.0.0",
    ]
    UA_POOL_EXTRA_COUNT = 100
    UA_LIST = _build_user_agent_pool(_BASE_UA_LIST, extra_count=UA_POOL_EXTRA_COUNT)
    
    # 分辨率池
    RESOLUTIONS = [
        (1920, 1080), (2560, 1440), (3840, 2160), (1366, 768), (1536, 864),
        (1600, 900), (1280, 720), (1360, 768), (1920, 1200),
        (1440, 900), (1680, 1050), (1280, 800), (2560, 1600),
        (2880, 1800), (3024, 1890), (3456, 2160),
        (1280, 1024), (1024, 768), (1400, 1050),
        (1920, 1280), (2736, 1824), (2880, 1920), (3000, 2000),
        (2256, 1504), (2496, 1664), (3240, 2160),
        (3200, 1800), (2304, 1440), (1800, 1200),
    ]
    DEFAULT_PROFILE_POOL = tuple(_build_browser_profile_pool(UA_LIST, desktop_resolutions=RESOLUTIONS))
    PROFILE_POOL_CACHE: Dict[int, tuple[BrowserProfile, ...]] = {
        UA_POOL_EXTRA_COUNT: DEFAULT_PROFILE_POOL,
    }
    
    def __init__(self, token_id: int, user_data_dir: str, db=None):
        self.token_id = token_id
        self.user_data_dir = user_data_dir
        self.db = db
        self._semaphore = asyncio.Semaphore(1)  # Only one active solve task is allowed per slot.
        self._solve_count = 0
        self._error_count = 0
        self._last_fingerprint: Optional[Dict[str, Any]] = None
        self._browser_proxy_active = False
        # Delay browser release after solve and track it by request_ref.
        self._pending_release_entries: Dict[str, Dict[str, Any]] = {}
        self._pending_release_lock = asyncio.Lock()
        # Browser mode keeps a shared in-memory browser instead of a persistent profile.
        self._shared_browser_lock = asyncio.Lock()
        self._shared_playwright = None
        self._shared_browser = None
        self._shared_context = None
        self._shared_keepalive_page = None
        self._shared_ready_page = None
        self._shared_ready_key: Optional[str] = None
        self._shared_custom_pages: Dict[str, Any] = {}
        self._shared_custom_page_last_used: Dict[str, float] = {}
        # 这里记录的是 Playwright driver 的 PID；Chromium 进程树通过 slot marker 扫描。
        self._shared_driver_pid: Optional[int] = None
        self._shared_driver_proc = None
        self._pid_dir = os.path.join(os.getcwd(), "tmp", "browser_pids")
        self._pid_file = os.path.join(self._pid_dir, f"slot_{self.token_id}.pid")
        os.makedirs(self._pid_dir, exist_ok=True)
        self._shared_proxy_url: Optional[str] = None
        self._shared_launch_count = 0
        self._shared_reuse_count = 0
        self._consecutive_browser_failures = 0
        self._solve_inflight = 0
        self._last_idle_since = time.monotonic()
        self._browser_epoch = 0
        self._profile_pool = self._build_profile_pool()
        self._active_profile: Optional[BrowserProfile] = None
        self._refresh_browser_profile()

    def _fingerprint_pool_extra_count(self) -> int:
        try:
            raw_value = getattr(config, "browser_fingerprint_pool_extra_count", self.UA_POOL_EXTRA_COUNT)
            if raw_value is None or raw_value == "":
                return self.UA_POOL_EXTRA_COUNT
            return max(0, int(raw_value))
        except Exception:
            return self.UA_POOL_EXTRA_COUNT

    def _build_profile_pool(self) -> tuple[BrowserProfile, ...]:
        extra_count = self._fingerprint_pool_extra_count()
        cached_pool = self.PROFILE_POOL_CACHE.get(extra_count)
        if cached_pool is not None:
            return cached_pool

        user_agents = _build_user_agent_pool(self._BASE_UA_LIST, extra_count=extra_count)
        profile_pool = tuple(_build_browser_profile_pool(user_agents, desktop_resolutions=self.RESOLUTIONS))
        self.PROFILE_POOL_CACHE[extra_count] = profile_pool
        return profile_pool

    def _refresh_browser_profile(self):
        """Refresh the in-memory browser fingerprint profile."""
        if not self._profile_pool:
            self._profile_pool = self._build_profile_pool()
        profile = random.choice(self._profile_pool)
        self._active_profile = profile
        self._profile_user_agent = profile.user_agent
        self._profile_viewport = dict(profile.viewport)
        self._profile_locale = profile.locale
        self._profile_timezone_id = profile.timezone_id
        self._profile_accept_language = profile.accept_language
        self._profile_device_scale_factor = float(profile.device_scale_factor)
        self._profile_is_mobile = bool(profile.is_mobile)
        self._profile_has_touch = bool(profile.has_touch)
        self._profile_family = profile.profile_family

    def _retry_max_attempts(self) -> int:
        try:
            return max(1, int(getattr(config, "browser_retry_max_attempts", 3) or 3))
        except Exception:
            return 3

    def _retry_backoff_seconds(self) -> float:
        try:
            raw_value = getattr(config, "browser_retry_backoff_seconds", 1.0)
            if raw_value is None or raw_value == "":
                return 1.0
            return max(0.0, float(raw_value))
        except Exception:
            return 1.0

    def _execute_timeout_seconds(self, *, fallback: float) -> float:
        try:
            return max(5.0, float(getattr(config, "browser_execute_timeout_seconds", fallback) or fallback))
        except Exception:
            return fallback

    def _execute_script_timeout_ms(self, *, fallback: float) -> int:
        return max(5000, int(self._execute_timeout_seconds(fallback=fallback) * 1000) - 5000)

    def _reload_wait_timeout_seconds(self) -> float:
        try:
            raw_value = getattr(config, "browser_reload_wait_timeout_seconds", 12.0)
            if raw_value is None or raw_value == "":
                return 12.0
            return max(0.0, float(raw_value))
        except Exception:
            return 12.0

    def _clr_wait_timeout_seconds(self) -> float:
        try:
            raw_value = getattr(config, "browser_clr_wait_timeout_seconds", 12.0)
            if raw_value is None or raw_value == "":
                return 12.0
            return max(0.0, float(raw_value))
        except Exception:
            return 12.0

    def _request_finish_image_wait_seconds(self, *, flow_timeout: int, upsample_timeout: int) -> int:
        fallback = max(max(flow_timeout, upsample_timeout) + 180, 900)
        try:
            return max(60, int(getattr(config, "browser_request_finish_image_wait_seconds", fallback) or fallback))
        except Exception:
            return fallback

    def _request_finish_non_image_wait_seconds(self, *, flow_timeout: int) -> int:
        fallback = max(flow_timeout + 300, 1800)
        try:
            return max(60, int(getattr(config, "browser_request_finish_non_image_wait_seconds", fallback) or fallback))
        except Exception:
            return fallback

    def _custom_page_cache_max_pages(self) -> int:
        try:
            return max(1, int(getattr(config, "browser_custom_page_cache_max_pages", 3) or 3))
        except Exception:
            return 3

    def _custom_page_idle_ttl_seconds(self) -> float:
        try:
            return max(30.0, float(getattr(config, "browser_custom_page_idle_ttl_seconds", 240.0) or 240.0))
        except Exception:
            return 240.0

    def _get_slot_marker(self) -> str:
        return f"--flow2api-browser-slot={self.token_id}"

    def _read_pid_file(self) -> Optional[int]:
        try:
            if not os.path.exists(self._pid_file):
                return None
            with open(self._pid_file, 'r', encoding='utf-8') as handle:
                raw = (handle.read() or '').strip()
            return int(raw or '0') or None
        except Exception:
            return None

    def _write_pid_file(self, pid: Optional[int]):
        self._shared_driver_pid = pid
        try:
            if pid:
                with open(self._pid_file, 'w', encoding='utf-8') as handle:
                    handle.write(str(pid))
            elif os.path.exists(self._pid_file):
                os.remove(self._pid_file)
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} failed to write PID file: {e}")

    def _get_pid_command_line(self, pid: Optional[int]) -> str:
        if not pid:
            return ""
        try:
            if sys.platform.startswith('win'):
                result = subprocess.run(
                    [
                        'powershell',
                        '-NoProfile',
                        '-Command',
                        f'(Get-CimInstance Win32_Process -Filter "ProcessId = {pid}").CommandLine'
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                return (result.stdout or '').strip()

            cmdline_path = f'/proc/{pid}/cmdline'
            if os.path.exists(cmdline_path):
                with open(cmdline_path, 'rb') as handle:
                    return handle.read().decode('utf-8', errors='ignore').replace('\x00', ' ')

            result = subprocess.run(
                ['ps', '-p', str(pid), '-o', 'command='],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return (result.stdout or '').strip()
        except Exception:
            return ""

    def _get_posix_process_state(self, pid: Optional[int]) -> Optional[str]:
        if not pid or sys.platform.startswith('win'):
            return None
        try:
            status_path = f'/proc/{pid}/status'
            if not os.path.exists(status_path):
                return None
            with open(status_path, 'r', encoding='utf-8', errors='ignore') as handle:
                for line in handle:
                    if not line.startswith('State:'):
                        continue
                    parts = line.split()
                    if len(parts) >= 2:
                        return parts[1].strip()
                    return None
        except Exception:
            return None
        return None

    def _reap_pid_if_direct_child(self, pid: Optional[int]) -> bool:
        if not pid or sys.platform.startswith('win'):
            return False
        try:
            waited_pid, _ = os.waitpid(pid, os.WNOHANG)
            return waited_pid == pid
        except ChildProcessError:
            return False
        except ProcessLookupError:
            return False
        except Exception:
            return False

    def _is_pid_running(self, pid: Optional[int]) -> bool:
        if not pid:
            return False
        try:
            if sys.platform.startswith('win'):
                result = subprocess.run(
                    ['tasklist', '/FI', f'PID eq {pid}'],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return str(pid) in (result.stdout or '')
            state = self._get_posix_process_state(pid)
            if state == 'Z':
                self._reap_pid_if_direct_child(pid)
                return False
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    def _pid_matches_slot(self, pid: Optional[int]) -> bool:
        if not pid:
            return False
        return self._get_slot_marker() in self._get_pid_command_line(pid)

    async def _wait_pid_exit(self, pid: Optional[int], timeout_seconds: float = 5.0) -> bool:
        if not pid:
            return True
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if not self._is_pid_running(pid):
                return True
            await asyncio.sleep(0.2)
        return not self._is_pid_running(pid)

    def _pid_looks_like_playwright_driver(self, pid: Optional[int]) -> bool:
        if not pid:
            return False
        command_line = self._get_pid_command_line(pid).lower()
        if not command_line:
            return False
        return 'run-driver' in command_line and 'playwright' in command_line

    def _extract_driver_proc(self, playwright=None, browser=None):
        candidates = [
            lambda: playwright._impl_obj._connection._transport._proc if playwright and getattr(playwright, "_impl_obj", None) else None,
            lambda: browser._impl_obj._connection._transport._proc if browser and getattr(browser, "_impl_obj", None) else None,
        ]
        for getter in candidates:
            try:
                proc = getter()
                if proc is not None:
                    return proc
            except Exception:
                continue
        return None

    def _extract_driver_pid(self, playwright=None, browser=None, proc=None) -> Optional[int]:
        try:
            if proc is None:
                proc = self._extract_driver_proc(playwright=playwright, browser=browser)
            pid = getattr(proc, "pid", None)
            if isinstance(pid, int) and pid > 0:
                return pid
        except Exception:
            pass
        return None

    def _list_slot_process_pids(self) -> List[int]:
        marker = self._get_slot_marker()
        matched_pids: Set[int] = set()
        try:
            if sys.platform.startswith('win'):
                command = (
                    f"$marker = '{marker}'; "
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($marker) } | "
                    "ForEach-Object { $_.ProcessId }"
                )
                result = subprocess.run(
                    ['powershell', '-NoProfile', '-Command', command],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                for line in (result.stdout or '').splitlines():
                    text = line.strip()
                    if text.isdigit():
                        matched_pids.add(int(text))
                return sorted(matched_pids)

            proc_root = '/proc'
            if os.path.isdir(proc_root):
                for entry in os.listdir(proc_root):
                    if not entry.isdigit():
                        continue
                    pid = int(entry)
                    if marker in self._get_pid_command_line(pid):
                        matched_pids.add(pid)
                return sorted(matched_pids)

            result = subprocess.run(
                ['ps', '-ax', '-o', 'pid=', '-o', 'command='],
                capture_output=True,
                text=True,
                timeout=20,
            )
            for line in (result.stdout or '').splitlines():
                text = line.strip()
                if not text:
                    continue
                parts = text.split(None, 1)
                if len(parts) != 2 or not parts[0].isdigit():
                    continue
                if marker in parts[1]:
                    matched_pids.add(int(parts[0]))
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} failed to scan slot processes: {e}")
        return sorted(matched_pids)

    async def _wait_process_exit(self, proc, timeout_seconds: float = 5.0) -> bool:
        if proc is None:
            return True
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
            return True
        except asyncio.TimeoutError:
            return getattr(proc, "returncode", None) is not None
        except ProcessLookupError:
            return True
        except Exception:
            return getattr(proc, "returncode", None) is not None

    async def _terminate_pid(self, pid: Optional[int], reason: str, timeout_seconds: float = 3.0):
        if not pid:
            return
        if not self._is_pid_running(pid):
            self._reap_pid_if_direct_child(pid)
            return

        try:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} terminating PID={pid}, reason={reason}"
            )
            if sys.platform.startswith('win'):
                subprocess.run(
                    ['taskkill', '/PID', str(pid), '/T', '/F'],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                await self._wait_pid_exit(pid, timeout_seconds=timeout_seconds)
                return

            os.kill(pid, signal.SIGTERM)
            if await self._wait_pid_exit(pid, timeout_seconds=timeout_seconds):
                self._reap_pid_if_direct_child(pid)
                return

            os.kill(pid, signal.SIGKILL)
            await self._wait_pid_exit(pid, timeout_seconds=max(1.0, timeout_seconds / 2))
            self._reap_pid_if_direct_child(pid)
        except ProcessLookupError:
            self._reap_pid_if_direct_child(pid)
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} failed to terminate PID={pid}: {e}")

    async def _terminate_driver_proc(self, proc, reason: str, timeout_seconds: float = 3.0):
        if proc is None:
            return

        driver_pid = self._extract_driver_pid(proc=proc)
        if getattr(proc, "returncode", None) is not None:
            self._reap_pid_if_direct_child(driver_pid)
            return

        try:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} terminating Playwright driver PID={driver_pid or 'unknown'}, reason={reason}"
            )
            if sys.platform.startswith('win') and driver_pid:
                subprocess.run(
                    ['taskkill', '/PID', str(driver_pid), '/T', '/F'],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            else:
                proc.terminate()
        except ProcessLookupError:
            self._reap_pid_if_direct_child(driver_pid)
            return
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} driver terminate failed PID={driver_pid or 'unknown'}: {e}"
            )

        if await self._wait_process_exit(proc, timeout_seconds=timeout_seconds):
            self._reap_pid_if_direct_child(driver_pid)
            return

        try:
            if sys.platform.startswith('win') and driver_pid:
                subprocess.run(
                    ['taskkill', '/PID', str(driver_pid), '/T', '/F'],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            else:
                proc.kill()
        except ProcessLookupError:
            self._reap_pid_if_direct_child(driver_pid)
            return
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} driver kill failed PID={driver_pid or 'unknown'}: {e}"
            )

        await self._wait_process_exit(proc, timeout_seconds=max(1.0, timeout_seconds / 2))
        self._reap_pid_if_direct_child(driver_pid)

    async def _cleanup_stale_slot_process(self):
        candidate_pids: Set[int] = set(self._list_slot_process_pids())
        stale_pid = self._read_pid_file()

        if stale_pid and self._is_pid_running(stale_pid):
            if self._pid_matches_slot(stale_pid) or self._pid_looks_like_playwright_driver(stale_pid):
                candidate_pids.add(stale_pid)
            else:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} PID file points to an unrelated process; ignoring PID={stale_pid}"
                )

        if candidate_pids:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} detected stale slot processes: {sorted(candidate_pids)}"
            )
        for pid in sorted(candidate_pids):
            await self._terminate_pid(pid, reason='stale_slot_process')
        self._write_pid_file(None)

    async def _ensure_shared_keepalive_page(self):
        """Ensure the shared browser always keeps one keepalive page alive."""
        keepalive_page = self._shared_keepalive_page
        try:
            if keepalive_page and not keepalive_page.is_closed():
                return keepalive_page
        except Exception:
            keepalive_page = None

        if not self._shared_context:
            return None

        keepalive_page = await self._shared_context.new_page()
        try:
            await keepalive_page.goto("about:blank", wait_until="load", timeout=5000)
        except Exception:
            pass
        self._shared_keepalive_page = keepalive_page
        debug_logger.log_info(
            f"[BrowserCaptcha] Token-{self.token_id} keepalive page created"
        )
        return keepalive_page

    def _build_ready_page_key(self, project_id: str, website_key: str) -> str:
        primary_host = "https://www.recaptcha.net" if self._browser_proxy_active else "https://www.google.com"
        return f"{project_id}|{website_key}|{primary_host}"

    async def _close_page_quietly(self, page):
        if not page:
            return
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass

    async def _drop_shared_ready_page(self):
        ready_page = self._shared_ready_page
        self._shared_ready_page = None
        self._shared_ready_key = None
        await self._close_page_quietly(ready_page)

    async def _drop_shared_custom_page(self, custom_key: str):
        custom_page = self._shared_custom_pages.pop(custom_key, None)
        self._shared_custom_page_last_used.pop(custom_key, None)
        await self._close_page_quietly(custom_page)

    async def _drop_all_shared_custom_pages(self):
        custom_pages = list(self._shared_custom_pages.values())
        self._shared_custom_pages = {}
        self._shared_custom_page_last_used = {}
        for custom_page in custom_pages:
            await self._close_page_quietly(custom_page)

    def _build_custom_page_key(
        self,
        website_url: str,
        website_key: str,
        captcha_type: str,
        enterprise: bool,
    ) -> str:
        primary_host = "https://www.recaptcha.net" if self._browser_proxy_active else "https://www.google.com"
        normalized_type = str(captcha_type or "").strip().lower() or "recaptcha_v3"
        return "|".join(
            [
                normalized_type,
                "1" if enterprise else "0",
                str(website_key or "").strip(),
                str(website_url or "").strip(),
                primary_host,
            ]
        )

    def _build_custom_page_runtime(
        self,
        *,
        website_key: str,
        captcha_type: str,
        enterprise: bool,
    ) -> Dict[str, Any]:
        primary_host = "https://www.recaptcha.net" if self._browser_proxy_active else "https://www.google.com"
        secondary_host = "https://www.google.com" if primary_host == "https://www.recaptcha.net" else "https://www.recaptcha.net"
        normalized_type = str(captcha_type or "").strip().lower() or "recaptcha_v3"
        is_turnstile = "turnstile" in normalized_type
        is_recaptcha_v2 = "recaptcha_v2" in normalized_type or normalized_type.endswith("v2")
        script_path = "recaptcha/enterprise.js" if enterprise else "recaptcha/api.js"
        execute_target = "grecaptcha.enterprise.execute" if enterprise else "grecaptcha.execute"
        ready_target = "grecaptcha.enterprise.ready" if enterprise else "grecaptcha.ready"
        if is_turnstile:
            wait_expression = "typeof turnstile !== 'undefined' && typeof turnstile.render === 'function'"
            api_label = "turnstile.js"
        elif is_recaptcha_v2:
            wait_expression = (
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && "
                "typeof grecaptcha.enterprise.render === 'function'"
            ) if enterprise else (
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.render === 'function'"
            )
            api_label = "enterprise.js" if enterprise else "api.js"
        else:
            wait_expression = (
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && "
                "typeof grecaptcha.enterprise.execute === 'function'"
            ) if enterprise else (
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'"
            )
            api_label = "enterprise.js" if enterprise else "api.js"
        render_value = "explicit" if is_recaptcha_v2 else str(website_key or "").strip()
        return {
            "primary_host": primary_host,
            "secondary_host": secondary_host,
            "normalized_type": normalized_type,
            "is_turnstile": is_turnstile,
            "is_recaptcha_v2": is_recaptcha_v2,
            "script_path": script_path,
            "execute_target": execute_target,
            "ready_target": ready_target,
            "wait_expression": wait_expression,
            "api_label": api_label,
            "render_value": render_value,
        }

    def _custom_page_is_stale(self, custom_key: str, *, now_value: Optional[float] = None) -> bool:
        last_used = float(self._shared_custom_page_last_used.get(custom_key, 0.0) or 0.0)
        if last_used <= 0:
            return False
        current = float(now_value if now_value is not None else time.monotonic())
        return (current - last_used) >= self._custom_page_idle_ttl_seconds()

    async def _inject_custom_page_scripts(self, page, runtime: Dict[str, Any]):
        if runtime["is_turnstile"]:
            await page.evaluate(
                """
                    (scriptUrl) => {
                        const existing = Array.from(document.scripts || []).some((script) => {
                            const src = script?.src || "";
                            return src.includes('turnstile/v0/api.js');
                        });
                        if (existing) return;
                        const script = document.createElement('script');
                        script.src = scriptUrl;
                        script.async = true;
                        script.defer = true;
                        document.head.appendChild(script);
                    }
                """,
                "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit",
            )
            return

        await page.evaluate(
            """
                (primaryUrl, secondaryUrl) => {
                    const existing = Array.from(document.scripts || []).some((script) => {
                        const src = script?.src || "";
                        return src.includes('/recaptcha/');
                    });
                    if (existing) return;
                    const urls = [primaryUrl, secondaryUrl];
                    const loadScript = (index) => {
                        if (index >= urls.length) return;
                        const script = document.createElement('script');
                        script.src = urls[index];
                        script.async = true;
                        script.onerror = () => loadScript(index + 1);
                        document.head.appendChild(script);
                    };
                    loadScript(0);
                }
            """,
            f"{runtime['primary_host']}/{runtime['script_path']}?render={runtime['render_value']}",
            f"{runtime['secondary_host']}/{runtime['script_path']}?render={runtime['render_value']}",
        )

    async def _prepare_custom_page(
        self,
        page,
        *,
        website_url: str,
        runtime: Dict[str, Any],
        warmup: bool,
    ) -> Dict[str, Any]:
        page_loaded = False
        stage_started = time.perf_counter()
        goto_timeout_ms = int(self._execute_timeout_seconds(fallback=30.0) * 1000)
        ready_timeout_ms = int(self._execute_timeout_seconds(fallback=15.0) * 1000)

        try:
            goto_started = time.perf_counter()
            await page.goto(website_url, wait_until="domcontentloaded", timeout=goto_timeout_ms)
            goto_ms = int((time.perf_counter() - goto_started) * 1000)
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} 自定义 page.goto 失败: {type(e).__name__}: {str(e)[:200]}"
            )
            raise

        for _ in range(20):
            try:
                ready_state = await page.evaluate("document.readyState")
                if ready_state == "complete":
                    page_loaded = True
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        if not page_loaded:
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} 自定义页面 readyState 未达到 complete，继续尝试预热")

        try:
            await page.mouse.move(320, 220)
            await page.mouse.move(520, 320, steps=12)
            await page.mouse.wheel(0, 240)
            await page.bring_to_front()
            await page.evaluate("""
                (() => {
                    try {
                        window.focus();
                        window.dispatchEvent(new Event('focus'));
                        document.dispatchEvent(new MouseEvent('mousemove', {
                            bubbles: true,
                            clientX: Math.max(32, Math.floor((window.innerWidth || 1280) * 0.4)),
                            clientY: Math.max(32, Math.floor((window.innerHeight || 720) * 0.35))
                        }));
                        window.scrollTo(0, Math.min(280, document.body?.scrollHeight || 280));
                    } catch (e) {}
                })()
            """)
        except Exception:
            pass

        if warmup:
            warmup_seconds = float(getattr(config, "browser_score_test_warmup_seconds", 12) or 12)
            if warmup_seconds > 0:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} 真实页面预热 {warmup_seconds:.1f}s 后再执行自定义打码"
                )
                await asyncio.sleep(warmup_seconds)

        try:
            ready_started = time.perf_counter()
            await page.wait_for_function(runtime["wait_expression"], timeout=ready_timeout_ms)
            ready_ms = int((time.perf_counter() - ready_started) * 1000)
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} 自定义 grecaptcha 未就绪，尝试补注入脚本: {type(e).__name__}: {str(e)[:200]}"
            )
            try:
                await self._inject_custom_page_scripts(page, runtime)
                ready_started = time.perf_counter()
                await page.wait_for_function(runtime["wait_expression"], timeout=ready_timeout_ms)
                ready_ms = int((time.perf_counter() - ready_started) * 1000)
            except Exception as inject_error:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} 自定义 grecaptcha 最终未就绪: {type(inject_error).__name__}: {str(inject_error)[:200]}"
                )
                raise

        return {
            "goto_ms": goto_ms,
            "grecaptcha_ready_ms": ready_ms,
            "ready_page_prepare_ms": int((time.perf_counter() - stage_started) * 1000),
        }

    async def _trim_shared_custom_pages(self, *, keep_key: Optional[str] = None, max_pages: Optional[int] = None):
        safe_max_pages = max(1, int(max_pages or self._custom_page_cache_max_pages()))
        now_value = time.monotonic()

        stale_keys = [
            page_key
            for page_key in list(self._shared_custom_pages.keys())
            if page_key != keep_key and self._custom_page_is_stale(page_key, now_value=now_value)
        ]
        for stale_key in stale_keys:
            await self._drop_shared_custom_page(stale_key)

        while len(self._shared_custom_pages) > safe_max_pages:
            evictable = [
                (page_key, self._shared_custom_page_last_used.get(page_key, 0.0))
                for page_key in self._shared_custom_pages
                if page_key != keep_key
            ]
            if not evictable:
                return
            evict_key = min(evictable, key=lambda item: item[1])[0]
            await self._drop_shared_custom_page(evict_key)

    async def _get_or_create_custom_page(
        self,
        context,
        *,
        website_url: str,
        website_key: str,
        captcha_type: str,
        enterprise: bool,
    ):
        custom_key = self._build_custom_page_key(website_url, website_key, captcha_type, enterprise)
        runtime = self._build_custom_page_runtime(
            website_key=website_key,
            captcha_type=captcha_type,
            enterprise=enterprise,
        )
        custom_page = self._shared_custom_pages.get(custom_key)
        ready_hit = False

        await self._trim_shared_custom_pages(keep_key=custom_key)

        try:
            if custom_page and not custom_page.is_closed():
                if self._custom_page_is_stale(custom_key):
                    await self._drop_shared_custom_page(custom_key)
                    custom_page = None
                else:
                    ready_ok = await custom_page.evaluate(runtime["wait_expression"])
                    if ready_ok:
                        ready_hit = True
                        self._shared_custom_page_last_used[custom_key] = time.monotonic()
                        return custom_page, custom_key, runtime, ready_hit
        except Exception:
            pass

        await self._drop_shared_custom_page(custom_key)
        custom_page = await context.new_page()
        await custom_page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        custom_page.on("requestfailed", lambda request: None)
        await self._prepare_custom_page(
            custom_page,
            website_url=website_url,
            runtime=runtime,
            warmup=True,
        )
        self._shared_custom_pages[custom_key] = custom_page
        self._shared_custom_page_last_used[custom_key] = time.monotonic()
        await self._trim_shared_custom_pages(keep_key=custom_key)
        return custom_page, custom_key, runtime, ready_hit

    async def _create_ready_page(self, context, project_id: str, website_key: str):
        stage_started = time.perf_counter()
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

        page_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
        primary_host = "https://www.recaptcha.net" if self._browser_proxy_active else "https://www.google.com"
        secondary_host = "https://www.google.com" if primary_host == "https://www.recaptcha.net" else "https://www.recaptcha.net"

        async def handle_route(route):
            if route.request.url.rstrip('/') == page_url.rstrip('/'):
                html = f"""<html><head><script>
                (() => {{
                    const urls = [
                        '{primary_host}/recaptcha/enterprise.js?render={website_key}',
                        '{secondary_host}/recaptcha/enterprise.js?render={website_key}'
                    ];
                    const loadScript = (index) => {{
                        if (index >= urls.length) return;
                        const script = document.createElement('script');
                        script.src = urls[index];
                        script.async = true;
                        script.onerror = () => loadScript(index + 1);
                        document.head.appendChild(script);
                    }};
                    loadScript(0);
                }})();
                </script></head><body></body></html>"""
                await route.fulfill(status=200, content_type="text/html", body=html)
            elif any(d in route.request.url for d in ["google.com", "gstatic.com", "recaptcha.net"]):
                await route.continue_()
            else:
                await route.abort()

        def handle_request_failed(request):
            try:
                failed_url = request.url or ""
                if not any(d in failed_url for d in ["google.com", "gstatic.com", "recaptcha.net"]):
                    return
                failure = request.failure or ""
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} 资源加载失败: url={failed_url[:200]}, error={failure}"
                )
            except Exception:
                pass

        await page.route("**/*", handle_route)
        page.on("requestfailed", handle_request_failed)

        try:
            goto_started = time.perf_counter()
            await page.goto(
                page_url,
                wait_until="domcontentloaded",
                timeout=int(self._execute_timeout_seconds(fallback=30.0) * 1000),
            )
            goto_ms = int((time.perf_counter() - goto_started) * 1000)
            ready_started = time.perf_counter()
            await page.wait_for_function(
                "typeof grecaptcha !== 'undefined' && "
                "typeof grecaptcha.enterprise !== 'undefined' && "
                "typeof grecaptcha.enterprise.execute === 'function'",
                timeout=int(self._execute_timeout_seconds(fallback=15.0) * 1000),
            )
            ready_ms = int((time.perf_counter() - ready_started) * 1000)
        except Exception:
            await self._close_page_quietly(page)
            raise
        return page, {
            "goto_ms": goto_ms,
            "grecaptcha_ready_ms": ready_ms,
            "ready_page_prepare_ms": int((time.perf_counter() - stage_started) * 1000),
        }

    async def _get_or_create_ready_page(self, context, project_id: str, website_key: str):
        ready_key = self._build_ready_page_key(project_id, website_key)
        ready_page = self._shared_ready_page
        ready_hit = False

        try:
            if (
                ready_page
                and self._shared_ready_key == ready_key
                and not ready_page.is_closed()
            ):
                ready_ok = await ready_page.evaluate(
                    "typeof grecaptcha !== 'undefined' && "
                    "typeof grecaptcha.enterprise !== 'undefined' && "
                    "typeof grecaptcha.enterprise.execute === 'function'"
                )
                if ready_ok:
                    ready_hit = True
                    return ready_page, ready_hit, {
                        "goto_ms": 0,
                        "grecaptcha_ready_ms": 0,
                        "ready_page_prepare_ms": 0,
                    }
        except Exception:
            pass

        await self._drop_shared_ready_page()
        ready_page, stage_timings = await self._create_ready_page(context, project_id, website_key)
        self._shared_ready_page = ready_page
        self._shared_ready_key = ready_key
        return ready_page, ready_hit, stage_timings

    def get_browser_epoch(self) -> int:
        return int(self._browser_epoch)

    async def _resolve_proxy_runtime_config(self, token_proxy_url: Optional[str] = None) -> tuple:
        """Resolve runtime proxy configuration."""
        proxy_option = None
        raw_proxy_url = None
        proxy_source = "none"
        self._browser_proxy_active = False
        try:
            candidate_proxy_url = None
            if token_proxy_url and token_proxy_url.strip():
                candidate_proxy_url = token_proxy_url.strip()
                proxy_source = "token"
            elif self.db:
                captcha_config = await self.db.get_captcha_config()
                if captcha_config.browser_proxy_enabled and captcha_config.browser_proxy_url:
                    candidate_proxy_url = captcha_config.browser_proxy_url.strip()
                    proxy_source = "global"

            if candidate_proxy_url:
                normalized_proxy_url, proxy_warning = normalize_browser_proxy_url(candidate_proxy_url)
                if proxy_warning:
                    debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} {proxy_warning}")
                proxy_option = parse_proxy_url(normalized_proxy_url)
                if proxy_option:
                    raw_proxy_url = normalized_proxy_url
                    self._browser_proxy_active = True
                    debug_logger.log_info(
                        f"[BrowserCaptcha] Token-{self.token_id} using {proxy_source} proxy: {proxy_option['server']}"
                    )
                else:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} {proxy_source} proxy format is invalid and has been ignored"
                    )
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} failed to read proxy configuration: {e}")

        return proxy_option, raw_proxy_url, proxy_source

    async def _create_browser(self, token_proxy_url: Optional[str] = None, manage_slot_pid: bool = True) -> tuple:
        """Create a browser instance; shared-slot browsers track the Playwright driver PID."""
        random_ua = self._profile_user_agent
        width = self._profile_viewport["width"]
        height = self._profile_viewport["height"]
        viewport = {"width": width, "height": height}
        launch_in_background = bool(getattr(config, "browser_launch_background", True))

        if manage_slot_pid:
            await self._cleanup_stale_slot_process()
        playwright = await async_playwright().start()
        browser_executable_path = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip() or None
        proxy_option, raw_proxy_url, _ = await self._resolve_proxy_runtime_config(token_proxy_url=token_proxy_url)

        # Record the initial fingerprint; sec-ch-* values are filled later from the page.
        self._last_fingerprint = {
            "user_agent": random_ua,
            "accept_language": self._profile_accept_language,
            "locale": self._profile_locale,
            "timezone_id": self._profile_timezone_id,
            "device_scale_factor": self._profile_device_scale_factor,
            "is_mobile": self._profile_is_mobile,
            "has_touch": self._profile_has_touch,
            "profile_family": self._profile_family,
            "viewport": dict(viewport),
            "proxy_url": raw_proxy_url if raw_proxy_url else None,
        }

        try:
            browser_args = [
                '--disable-blink-features=AutomationControlled',
                '--disable-quic',
                '--disable-features=UseDnsHttpsSvcb',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-setuid-sandbox',
                '--no-first-run',
                '--no-zygote',
                f'--window-size={width},{height}',
                '--disable-infobars',
                '--hide-scrollbars',
            ]
            if manage_slot_pid:
                browser_args.append(self._get_slot_marker())

            if launch_in_background:
                browser_args.extend([
                    '--start-minimized',
                    '--disable-background-timer-throttling',
                    '--disable-renderer-backgrounding',
                    '--disable-backgrounding-occluded-windows',
                ])
                if sys.platform.startswith("win"):
                    browser_args.append('--window-position=-32000,-32000')
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} headed browser will launch in background mode"
                )

            if browser_executable_path:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} using custom browser executable: {browser_executable_path}"
                )

            browser = await playwright.chromium.launch(
                headless=False,
                executable_path=browser_executable_path,
                proxy=proxy_option,
                args=browser_args,
            )
            context = await browser.new_context(
                user_agent=random_ua,
                viewport=viewport,
                locale=self._profile_locale,
                timezone_id=self._profile_timezone_id,
                device_scale_factor=self._profile_device_scale_factor,
                is_mobile=self._profile_is_mobile,
                has_touch=self._profile_has_touch,
                extra_http_headers={"Accept-Language": self._profile_accept_language},
            )
            driver_proc = self._extract_driver_proc(playwright=playwright, browser=browser)
            driver_pid = self._extract_driver_pid(proc=driver_proc)
            if manage_slot_pid:
                self._shared_driver_proc = driver_proc
                self._write_pid_file(driver_pid)
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} shared browser started (proxy={'yes' if raw_proxy_url else 'no'})"
            )
            return playwright, browser, context
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] Token-{self.token_id} browser launch failed: {type(e).__name__}: {str(e)[:200]}")
            try:
                if playwright:
                    await playwright.stop()
            except Exception:
                pass
            if manage_slot_pid:
                self._shared_driver_proc = None
                self._write_pid_file(None)
            raise

    async def _recycle_browser_locked(self, reason: str = "unknown", rotate_profile: bool = True):
        """Recycle the shared browser instance and reset its state."""
        playwright = self._shared_playwright
        browser = self._shared_browser
        context = self._shared_context
        keepalive_page = self._shared_keepalive_page
        ready_page = self._shared_ready_page
        custom_pages = list(self._shared_custom_pages.values())
        driver_proc = self._shared_driver_proc
        driver_pid = self._shared_driver_pid or self._read_pid_file()
        had_browser = bool(playwright or browser or context or keepalive_page or ready_page or custom_pages or driver_proc or driver_pid)

        self._shared_playwright = None
        self._shared_browser = None
        self._shared_context = None
        self._shared_keepalive_page = None
        self._shared_ready_page = None
        self._shared_ready_key = None
        self._shared_custom_pages = {}
        self._shared_custom_page_last_used = {}
        self._shared_driver_pid = None
        self._shared_driver_proc = None
        self._shared_proxy_url = None
        self._consecutive_browser_failures = 0
        self._shared_reuse_count = 0

        if rotate_profile:
            self._refresh_browser_profile()

        if had_browser:
            self._browser_epoch += 1
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} shared browser recycled, reason={reason}"
            )
        await self._close_browser(playwright, browser, context, driver_pid=driver_pid, driver_proc=driver_proc)

    async def recycle_browser(self, reason: str = "unknown", rotate_profile: bool = True):
        """Recycle the current shared browser."""
        async with self._shared_browser_lock:
            await self._recycle_browser_locked(reason=reason, rotate_profile=rotate_profile)

    async def _get_or_create_shared_browser(self, token_proxy_url: Optional[str] = None) -> tuple:
        """Get or create the shared browser for this slot."""
        _, expected_proxy_url, _ = await self._resolve_proxy_runtime_config(token_proxy_url=token_proxy_url)

        async with self._shared_browser_lock:
            has_shared_browser = bool(self._shared_playwright and self._shared_browser and self._shared_context)

            if has_shared_browser:
                is_connected = True
                try:
                    checker = getattr(self._shared_browser, "is_connected", None)
                    if callable(checker):
                        is_connected = bool(checker())
                except Exception:
                    is_connected = False

                if not is_connected:
                    await self._recycle_browser_locked(reason="browser_disconnected", rotate_profile=False)
                    has_shared_browser = False

            if has_shared_browser and self._shared_proxy_url != expected_proxy_url:
                # If the proxy configuration changed, recycle the slot before reusing it.
                await self._recycle_browser_locked(reason="proxy_changed", rotate_profile=False)
                has_shared_browser = False

            if has_shared_browser:
                try:
                    await self._ensure_shared_keepalive_page()
                except Exception:
                    await self._recycle_browser_locked(reason="keepalive_page_broken", rotate_profile=False)
                    has_shared_browser = False

            if has_shared_browser:
                self._shared_reuse_count += 1
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} reusing shared browser (reuse={self._shared_reuse_count})"
                )
                return self._shared_playwright, self._shared_browser, self._shared_context

            playwright, browser, context = await self._create_browser(token_proxy_url=token_proxy_url)
            self._shared_playwright = playwright
            self._shared_browser = browser
            self._shared_context = context
            self._browser_epoch += 1
            await self._ensure_shared_keepalive_page()
            self._shared_proxy_url = (self._last_fingerprint or {}).get("proxy_url")
            self._shared_launch_count += 1
            self._shared_reuse_count = 0
            self.note_idle()
            return playwright, browser, context

    async def _capture_page_fingerprint(self, page):
        """从浏览器页面提取 UA 与客户端提示头，确保与打码浏览器一致。"""
        try:
            fingerprint = await page.evaluate("""
                () => {
                    const ua = navigator.userAgent || "";
                    const lang = navigator.language || "";
                    const uaData = navigator.userAgentData || null;
                    let secChUa = "";
                    let secChUaMobile = "";
                    let secChUaPlatform = "";

                    if (uaData) {
                        if (Array.isArray(uaData.brands) && uaData.brands.length > 0) {
                            secChUa = uaData.brands
                                .map((item) => `"${item.brand}";v="${item.version}"`)
                                .join(", ");
                        }
                        secChUaMobile = uaData.mobile ? "?1" : "?0";
                        if (uaData.platform) {
                            secChUaPlatform = `"${uaData.platform}"`;
                        }
                    }

                    return {
                        user_agent: ua,
                        accept_language: lang,
                        sec_ch_ua: secChUa,
                        sec_ch_ua_mobile: secChUaMobile,
                        sec_ch_ua_platform: secChUaPlatform,
                        timezone_id: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
                        device_scale_factor: Number(window.devicePixelRatio || 1),
                        has_touch: Number(navigator.maxTouchPoints || 0) > 0,
                        viewport: {
                            width: Number(window.innerWidth || 0),
                            height: Number(window.innerHeight || 0),
                        },
                    };
                }
            """)

            if not isinstance(fingerprint, dict):
                return

            if self._last_fingerprint is None:
                self._last_fingerprint = {}

            for key in ("user_agent", "accept_language", "sec_ch_ua", "sec_ch_ua_mobile", "sec_ch_ua_platform"):
                value = fingerprint.get(key)
                if isinstance(value, str) and value:
                    self._last_fingerprint[key] = value
            timezone_id = fingerprint.get("timezone_id")
            if isinstance(timezone_id, str) and timezone_id:
                self._last_fingerprint["timezone_id"] = timezone_id
            device_scale_factor = fingerprint.get("device_scale_factor")
            if isinstance(device_scale_factor, (int, float)) and device_scale_factor > 0:
                self._last_fingerprint["device_scale_factor"] = float(device_scale_factor)
            has_touch = fingerprint.get("has_touch")
            if isinstance(has_touch, bool):
                self._last_fingerprint["has_touch"] = has_touch
            viewport = fingerprint.get("viewport")
            if isinstance(viewport, dict):
                width = viewport.get("width")
                height = viewport.get("height")
                if isinstance(width, (int, float)) and isinstance(height, (int, float)) and width > 0 and height > 0:
                    self._last_fingerprint["viewport"] = {
                        "width": int(width),
                        "height": int(height),
                    }
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} 提取浏览器指纹失败: {type(e).__name__}: {str(e)[:200]}")

    async def _verify_score_in_page(self, page, token: str, verify_url: str) -> Dict[str, Any]:
        """直接读取测试页面展示的分数，避免 verify.php 与页面显示口径不一致。"""
        _ = token
        _ = verify_url
        started_at = time.time()
        timeout_seconds = 25.0
        refresh_clicked = False
        last_snapshot: Dict[str, Any] = {}

        try:
            timeout_seconds = float(getattr(config, "browser_score_dom_wait_seconds", 25) or 25)
        except Exception:
            pass

        while (time.time() - started_at) < timeout_seconds:
            try:
                result = await page.evaluate(
                    """
                        () => {
                            const bodyText = ((document.body && document.body.innerText) || "")
                                .replace(/\\u00a0/g, " ")
                                .replace(/\\r/g, "");
                            const patterns = [
                                { source: "current_score", regex: /Your score is:\\s*([01](?:\\.\\d+)?)/i },
                                { source: "selected_score", regex: /Selected Score Test:[\\s\\S]{0,400}?Score:\\s*([01](?:\\.\\d+)?)/i },
                                { source: "history_score", regex: /(?:^|\\n)\\s*Score:\\s*([01](?:\\.\\d+)?)\\s*;/i },
                            ];
                            let score = null;
                            let source = "";
                            for (const item of patterns) {
                                const match = bodyText.match(item.regex);
                                if (!match) continue;
                                const parsed = Number(match[1]);
                                if (!Number.isNaN(parsed) && parsed >= 0 && parsed <= 1) {
                                    score = parsed;
                                    source = item.source;
                                    break;
                                }
                            }
                            const uaMatch = bodyText.match(/Current User Agent:\\s*([^\\n]+)/i);
                            const ipMatch = bodyText.match(/Current IP Address:\\s*([^\\n]+)/i);
                            return {
                                score,
                                source,
                                raw_text: bodyText.slice(0, 4000),
                                current_user_agent: uaMatch ? uaMatch[1].trim() : "",
                                current_ip_address: ipMatch ? ipMatch[1].trim() : "",
                                title: document.title || "",
                                url: location.href || "",
                            };
                        }
                    """
                )
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {str(e)[:200]}"}

            if isinstance(result, dict):
                last_snapshot = result
                score = result.get("score")
                if isinstance(score, (int, float)):
                    elapsed_ms = int((time.time() - started_at) * 1000)
                    return {
                        "verify_mode": "browser_page_dom",
                        "verify_elapsed_ms": elapsed_ms,
                        "verify_http_status": None,
                        "verify_result": {
                            "success": True,
                            "score": score,
                            "source": result.get("source") or "antcpt_dom",
                            "raw_text": result.get("raw_text") or "",
                            "current_user_agent": result.get("current_user_agent") or "",
                            "current_ip_address": result.get("current_ip_address") or "",
                            "page_title": result.get("title") or "",
                            "page_url": result.get("url") or "",
                        },
                    }

            if not refresh_clicked and (time.time() - started_at) >= 2:
                refresh_clicked = True
                try:
                    await page.evaluate(
                        """
                            () => {
                                const nodes = Array.from(
                                    document.querySelectorAll('button, input[type="button"], input[type="submit"], a')
                                );
                                const target = nodes.find((node) => {
                                    const text = (node.innerText || node.textContent || node.value || "").trim();
                                    return /Refresh score now!?/i.test(text);
                                });
                                if (target) {
                                    target.click();
                                    return true;
                                }
                                return false;
                            }
                        """
                    )
                except Exception:
                    pass

            await asyncio.sleep(0.5)

        elapsed_ms = int((time.time() - started_at) * 1000)
        if not isinstance(last_snapshot, dict):
            last_snapshot = {"raw": last_snapshot}

        return {
            "verify_mode": "browser_page_dom",
            "verify_elapsed_ms": elapsed_ms,
            "verify_http_status": None,
            "verify_result": {
                "success": False,
                "score": None,
                "source": "antcpt_dom_timeout",
                "raw_text": last_snapshot.get("raw_text") or "",
                "current_user_agent": last_snapshot.get("current_user_agent") or "",
                "current_ip_address": last_snapshot.get("current_ip_address") or "",
                "page_title": last_snapshot.get("title") or "",
                "page_url": last_snapshot.get("url") or "",
                "error": last_snapshot.get("error") or "未在页面中读取到分数",
            },
        }
    
    async def _close_browser(
        self,
        playwright,
        browser,
        context,
        driver_pid: Optional[int] = None,
        driver_proc=None,
        clear_slot_pid: bool = True,
    ):
        """Close a browser instance and clean both the Playwright driver and Chromium tree."""
        is_shared_browser = any([
            context is not None and context is self._shared_context,
            browser is not None and browser is self._shared_browser,
            playwright is not None and playwright is self._shared_playwright,
        ])
        effective_driver_proc = driver_proc or self._extract_driver_proc(playwright=playwright, browser=browser)
        effective_driver_pid = driver_pid or self._extract_driver_pid(
            playwright=playwright,
            browser=browser,
            proc=effective_driver_proc,
        )
        if clear_slot_pid and not effective_driver_pid:
            effective_driver_pid = self._shared_driver_pid or self._read_pid_file()
        if effective_driver_pid and effective_driver_proc is None and not self._pid_looks_like_playwright_driver(effective_driver_pid):
            effective_driver_pid = None
        if is_shared_browser:
            self._shared_playwright = None
            self._shared_browser = None
            self._shared_context = None
            self._shared_keepalive_page = None
            self._shared_ready_page = None
            self._shared_ready_key = None
            self._shared_custom_pages = {}
            self._shared_custom_page_last_used = {}
            self._shared_driver_pid = None
            self._shared_driver_proc = None
            self._shared_proxy_url = None
        try:
            if context:
                await asyncio.wait_for(context.close(), timeout=10)
        except Exception:
            pass
        try:
            if browser:
                await asyncio.wait_for(browser.close(), timeout=10)
        except Exception:
            pass
        try:
            if playwright:
                await asyncio.wait_for(playwright.stop(), timeout=10)
        except Exception:
            pass
        if effective_driver_proc:
            await self._terminate_driver_proc(
                effective_driver_proc,
                reason='driver_close_timeout_or_orphan',
                timeout_seconds=4,
            )
        elif effective_driver_pid:
            await self._terminate_pid(
                effective_driver_pid,
                reason='driver_close_timeout_or_orphan',
                timeout_seconds=4,
            )

        if clear_slot_pid:
            for slot_pid in self._list_slot_process_pids():
                await self._terminate_pid(
                    slot_pid,
                    reason='slot_process_tree_cleanup',
                    timeout_seconds=3,
                )
            self._write_pid_file(None)

    async def _wait_and_close_after_request(
        self,
        request_ref: str,
        release_event: asyncio.Event,
        wait_timeout: int,
        playwright,
        browser,
        context,
        action: str
    ):
        """等待上游请求结束后再关闭浏览器（超时兜底）。"""
        close_reason = "上游请求完成"
        try:
            await asyncio.wait_for(release_event.wait(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            close_reason = f"等待上游请求完成超时({wait_timeout}s)"
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} {close_reason}，执行兜底关闭"
            )
        except Exception as e:
            close_reason = f"等待上游请求完成异常: {type(e).__name__}"
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} {close_reason}，执行兜底关闭"
            )
        finally:
            await self._close_browser(playwright, browser, context)
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} {close_reason}，浏览器已关闭 (action={action}, request_ref={request_ref[:8]})"
            )
            async with self._pending_release_lock:
                self._pending_release_entries.pop(request_ref, None)

    async def _defer_browser_close_until_request_done(
        self,
        playwright,
        browser,
        context,
        action: str
    ) -> str:
        """打码成功后延迟关闭浏览器，等待 Flow 请求结束通知。"""
        flow_timeout = int(getattr(config, "flow_timeout", 300) or 300)
        upsample_timeout = int(getattr(config, "upsample_timeout", 300) or 300)
        if action == "IMAGE_GENERATION":
            wait_timeout = self._request_finish_image_wait_seconds(
                flow_timeout=flow_timeout,
                upsample_timeout=upsample_timeout,
            )
        else:
            wait_timeout = self._request_finish_non_image_wait_seconds(flow_timeout=flow_timeout)
        request_ref = uuid.uuid4().hex
        release_event = asyncio.Event()
        release_task = asyncio.create_task(
            self._wait_and_close_after_request(
                request_ref=request_ref,
                release_event=release_event,
                wait_timeout=wait_timeout,
                playwright=playwright,
                browser=browser,
                context=context,
                action=action,
            )
        )

        async with self._pending_release_lock:
            self._pending_release_entries[request_ref] = {
                "event": release_event,
                "task": release_task,
            }
        debug_logger.log_info(
            f"[BrowserCaptcha] Token-{self.token_id} 打码成功后进入延迟关闭，等待上游请求完成 "
            f"(action={action}, timeout={wait_timeout}s, request_ref={request_ref[:8]})"
        )
        return request_ref

    async def notify_generation_request_finished(self, request_ref: Optional[str] = None):
        """通知当前 Token 对应的上游图片/视频请求已结束。"""
        async with self._pending_release_lock:
            release_event = None
            matched_ref = request_ref
            if matched_ref and matched_ref in self._pending_release_entries:
                entry = self._pending_release_entries.pop(matched_ref)
                release_event = entry.get("event")
            elif not matched_ref and self._pending_release_entries:
                # 兼容旧调用方（无 request_ref），仅回收最早待释放项，避免一次性影响全部请求。
                matched_ref = next(iter(self._pending_release_entries.keys()))
                entry = self._pending_release_entries.pop(matched_ref)
                release_event = entry.get("event")
        if release_event and not release_event.is_set():
            release_event.set()
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} 收到上游请求完成通知，开始关闭浏览器 "
                f"(request_ref={(matched_ref or 'unknown')[:8]})"
            )

    async def force_close_pending_browser(self, request_ref: Optional[str] = None, close_all: bool = False):
        """Force close pending browsers tracked by this slot."""
        async with self._pending_release_lock:
            entries: List[Dict[str, Any]] = []
            if close_all:
                entries = list(self._pending_release_entries.values())
                self._pending_release_entries.clear()
            elif request_ref and request_ref in self._pending_release_entries:
                entry = self._pending_release_entries.pop(request_ref)
                entries = [entry]
            elif self._pending_release_entries:
                first_ref = next(iter(self._pending_release_entries.keys()))
                entry = self._pending_release_entries.pop(first_ref)
                entries = [entry]

        release_events = [entry.get("event") for entry in entries if isinstance(entry, dict)]
        release_tasks = [entry.get("task") for entry in entries if isinstance(entry, dict)]

        for release_event in release_events:
            if not release_event:
                continue
            if not release_event.is_set():
                release_event.set()
        for release_task in release_tasks:
            if not release_task:
                continue
            try:
                await asyncio.wait_for(release_task, timeout=5)
            except Exception:
                pass

        if close_all:
            await self.recycle_browser(reason="force_close_all", rotate_profile=False)

    async def _execute_captcha(self, context, project_id: str, website_key: str, action: str) -> Optional[TokenAcquireResult]:
        """在给定 context 中执行打码逻辑"""
        page = None
        handle_response = None
        ready_hit = False
        stage_timings: Dict[str, int] = {}
        total_started = time.perf_counter()
        try:
            page, ready_hit, ready_stage_timings = await self._get_or_create_ready_page(context, project_id, website_key)
            stage_timings.update(ready_stage_timings)
            stage_timings["ready_page_hit"] = 1 if ready_hit else 0
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} 复用热页面={ready_hit}"
            )
            reload_ok_event = asyncio.Event()
            clr_ok_event = asyncio.Event()

            def handle_response(response):
                try:
                    if response.status != 200:
                        return
                    parsed = urlparse(response.url)
                    path = parsed.path or ""
                    if "recaptcha/enterprise/reload" not in path and "recaptcha/enterprise/clr" not in path:
                        return
                    query = parse_qs(parsed.query or "")
                    key = (query.get("k") or [None])[0]
                    if key != website_key:
                        return
                    if "recaptcha/enterprise/reload" in path:
                        reload_ok_event.set()
                    elif "recaptcha/enterprise/clr" in path:
                        clr_ok_event.set()
                except Exception:
                    pass

            page.on("response", handle_response)

            # 记录本次打码页面的真实 UA/客户端提示头
            await self._capture_page_fingerprint(page)

            execute_started = time.perf_counter()
            execute_timeout_seconds = self._execute_timeout_seconds(fallback=30.0)
            execute_script_timeout_ms = self._execute_script_timeout_ms(fallback=30.0)
            token = await asyncio.wait_for(
                page.evaluate(f"""
                    (actionName) => {{
                        return new Promise((resolve, reject) => {{
                            const timeout = setTimeout(() => reject(new Error('timeout')), {execute_script_timeout_ms});
                            grecaptcha.enterprise.execute('{website_key}', {{action: actionName}})
                                .then(t => {{ resolve(t); }})
                                .catch(e => {{ reject(e); }});
                        }});
                    }}
                """, action),
                timeout=execute_timeout_seconds
            )
            stage_timings["execute_ms"] = int((time.perf_counter() - execute_started) * 1000)

            # 按要求：等待 enterprise/reload 与 enterprise/clr 均出现并返回 200
            reload_wait_timeout_seconds = self._reload_wait_timeout_seconds()
            if reload_wait_timeout_seconds > 0:
                try:
                    reload_started = time.perf_counter()
                    await asyncio.wait_for(reload_ok_event.wait(), timeout=reload_wait_timeout_seconds)
                    stage_timings["reload_wait_ms"] = int((time.perf_counter() - reload_started) * 1000)
                except asyncio.TimeoutError:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} 等待 recaptcha enterprise/reload 200 超时"
                    )
                    return None
            else:
                stage_timings["reload_wait_ms"] = 0

            clr_wait_timeout_seconds = self._clr_wait_timeout_seconds()
            if clr_wait_timeout_seconds > 0:
                try:
                    clr_started = time.perf_counter()
                    await asyncio.wait_for(clr_ok_event.wait(), timeout=clr_wait_timeout_seconds)
                    stage_timings["clr_wait_ms"] = int((time.perf_counter() - clr_started) * 1000)
                except asyncio.TimeoutError:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} 等待 recaptcha enterprise/clr 200 超时"
                    )
                    return None
            else:
                stage_timings["clr_wait_ms"] = 0

            # 即使 reload/clr 都已返回 200，也额外等待几秒，确保 enterprise 请求链路完全稳定。
            post_wait_seconds = float(getattr(config, "browser_recaptcha_settle_seconds", 3) or 3)
            if post_wait_seconds > 0:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} reload/clr 已就绪，额外等待 {post_wait_seconds:.1f}s 后返回 token"
                )
                settle_started = time.perf_counter()
                await asyncio.sleep(post_wait_seconds)
                stage_timings["settle_ms"] = int((time.perf_counter() - settle_started) * 1000)
            else:
                stage_timings["settle_ms"] = 0

            total_ms = int((time.perf_counter() - total_started) * 1000)
            stage_timings["total_ms"] = total_ms
            return TokenAcquireResult(
                token=token,
                browser_ref=self.token_id,
                browser_id=self.token_id,
                fingerprint=self.get_last_fingerprint(),
                source="live",
                elapsed_ms=total_ms,
                browser_epoch=self.get_browser_epoch(),
                timings=stage_timings,
            )
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)}"
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} 打码失败: {msg[:200]}")
            await self._drop_shared_ready_page()
            return None
        finally:
            if page and handle_response:
                try:
                    page.remove_listener("response", handle_response)
                except Exception:
                    pass

    async def _execute_custom_captcha(
        self,
        context,
        website_url: str,
        website_key: str,
        action: str,
        verify_url: Optional[str] = None,
        enterprise: bool = False,
        captcha_type: str = "recaptcha_v3",
        is_invisible: bool = True,
        reuse_ready_page: bool = False,
    ) -> Any:
        """在任意站点执行 reCAPTCHA，用于分数测试等非 Flow 场景。"""
        page = None
        custom_key: Optional[str] = None
        runtime = self._build_custom_page_runtime(
            website_key=website_key,
            captcha_type=captcha_type,
            enterprise=enterprise,
        )
        try:
            if reuse_ready_page:
                page, custom_key, runtime, ready_hit = await self._get_or_create_custom_page(
                    context,
                    website_url=website_url,
                    website_key=website_key,
                    captcha_type=captcha_type,
                    enterprise=enterprise,
                )
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} 复用自定义热页面={ready_hit} "
                    f"type={runtime['normalized_type']} url={website_url}"
                )
            else:
                page = await context.new_page()
                await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} 加载真实自定义页面 {runtime['api_label']}: "
                    f"type={runtime['normalized_type']}, primary={runtime['primary_host']}, "
                    f"secondary={runtime['secondary_host']}, url={website_url}"
                )
                await self._prepare_custom_page(
                    page,
                    website_url=website_url,
                    runtime=runtime,
                    warmup=True,
                )

            await self._capture_page_fingerprint(page)
            execute_timeout_seconds = self._execute_timeout_seconds(fallback=45.0)
            execute_script_timeout_ms = self._execute_script_timeout_ms(fallback=45.0)

            if runtime["is_turnstile"]:
                token = await asyncio.wait_for(
                    page.evaluate(
                        """
                            ({ siteKey, actionName }) => {
                                return new Promise((resolve, reject) => {
                                    const api = window.turnstile;
                                    if (!api || typeof api.render !== 'function') {
                                        reject(new Error('turnstile_unavailable'));
                                        return;
                                    }

                                    let settled = false;
                                    const timeout = setTimeout(() => {
                                        if (settled) return;
                                        settled = true;
                                        reject(new Error('timeout'));
                                    }, """ + str(execute_script_timeout_ms) + """);

                                    const done = (token) => {
                                        if (settled || !token) return;
                                        settled = true;
                                        clearTimeout(timeout);
                                        resolve(token);
                                    };

                                    const fail = (reason) => {
                                        if (settled) return;
                                        settled = true;
                                        clearTimeout(timeout);
                                        reject(new Error(String(reason || 'turnstile_failed')));
                                    };

                                    try {
                                        const host = document.createElement('div');
                                        host.id = `fcs-turnstile-${Date.now()}-${Math.floor(Math.random() * 10000)}`;
                                        host.style.position = 'fixed';
                                        host.style.left = '-9999px';
                                        host.style.top = '0';
                                        host.style.width = '320px';
                                        host.style.height = '65px';
                                        document.body.appendChild(host);

                                        const widgetId = api.render(host, {
                                            sitekey: siteKey,
                                            action: actionName || undefined,
                                            execution: 'execute',
                                            appearance: 'execute',
                                            callback: done,
                                            'error-callback': fail,
                                            'expired-callback': () => fail('expired'),
                                            'timeout-callback': () => fail('timeout'),
                                        });

                                        if (typeof api.execute === 'function') {
                                            try { api.execute(widgetId); } catch (e) {}
                                            try { api.execute(host); } catch (e) {}
                                        }

                                        const probe = () => {
                                            if (settled) return;
                                            const input = document.querySelector('input[name="cf-turnstile-response"]');
                                            const currentToken = input && input.value ? String(input.value).trim() : '';
                                            if (currentToken) {
                                                done(currentToken);
                                                return;
                                            }
                                            setTimeout(probe, 500);
                                        };
                                        setTimeout(probe, 600);
                                    } catch (e) {
                                        fail(e && e.message ? e.message : String(e));
                                    }
                                });
                            }
                        """,
                        {"siteKey": website_key, "actionName": action},
                    ),
                    timeout=execute_timeout_seconds,
                )
            elif runtime["is_recaptcha_v2"]:
                token = await asyncio.wait_for(
                    page.evaluate(
                        """
                            ({ siteKey, actionName, enterpriseMode, invisibleMode }) => {
                                return new Promise((resolve, reject) => {
                                    const apiRoot = enterpriseMode
                                        ? (window.grecaptcha && window.grecaptcha.enterprise)
                                        : window.grecaptcha;
                                    if (!apiRoot || typeof apiRoot.render !== 'function') {
                                        reject(new Error('recaptcha_render_unavailable'));
                                        return;
                                    }

                                    let settled = false;
                                    const timeout = setTimeout(() => {
                                        if (settled) return;
                                        settled = true;
                                        reject(new Error('timeout'));
                                    }, """ + str(execute_script_timeout_ms) + """);

                                    const done = (token) => {
                                        if (settled || !token) return;
                                        settled = true;
                                        clearTimeout(timeout);
                                        resolve(token);
                                    };

                                    const fail = (reason) => {
                                        if (settled) return;
                                        settled = true;
                                        clearTimeout(timeout);
                                        reject(new Error(String(reason || 'recaptcha_v2_failed')));
                                    };

                                    try {
                                        const host = document.createElement('div');
                                        host.id = `fcs-recaptcha-${Date.now()}-${Math.floor(Math.random() * 10000)}`;
                                        host.style.position = 'fixed';
                                        host.style.left = '-9999px';
                                        host.style.top = '0';
                                        document.body.appendChild(host);

                                        const widgetId = apiRoot.render(host, {
                                            sitekey: siteKey,
                                            size: invisibleMode ? 'invisible' : 'normal',
                                            badge: 'bottomright',
                                            callback: done,
                                            'error-callback': () => fail('error-callback'),
                                            'expired-callback': () => fail('expired-callback'),
                                            action: actionName || undefined,
                                        });

                                        if (typeof apiRoot.execute === 'function') {
                                            try {
                                                const executeResult = apiRoot.execute(widgetId);
                                                if (executeResult && typeof executeResult.then === 'function') {
                                                    executeResult
                                                        .then((resultToken) => {
                                                            if (resultToken) done(resultToken);
                                                        })
                                                        .catch((error) => fail(error && error.message ? error.message : String(error)));
                                                }
                                            } catch (e) {
                                                if (invisibleMode) {
                                                    fail(e && e.message ? e.message : String(e));
                                                    return;
                                                }
                                            }
                                        }

                                        const probe = () => {
                                            if (settled) return;
                                            const input = document.querySelector('textarea[name="g-recaptcha-response"]');
                                            const currentToken = input && input.value ? String(input.value).trim() : '';
                                            if (currentToken) {
                                                done(currentToken);
                                                return;
                                            }
                                            setTimeout(probe, 500);
                                        };
                                        setTimeout(probe, 800);
                                    } catch (e) {
                                        fail(e && e.message ? e.message : String(e));
                                    }
                                });
                            }
                        """,
                        {
                            "siteKey": website_key,
                            "actionName": action,
                            "enterpriseMode": enterprise,
                            "invisibleMode": bool(is_invisible),
                        },
                    ),
                    timeout=execute_timeout_seconds,
                )
            else:
                token = await asyncio.wait_for(
                    page.evaluate(
                        f"""
                            (actionName) => {{
                                return new Promise((resolve, reject) => {{
                                    const timeout = setTimeout(() => reject(new Error('timeout')), {execute_script_timeout_ms});
                                    try {{
                                        {runtime['ready_target']}(function() {{
                                            {runtime['execute_target']}('{website_key}', {{action: actionName}})
                                                .then(t => {{
                                                    clearTimeout(timeout);
                                                    resolve(t);
                                                }})
                                                .catch(e => {{
                                                    clearTimeout(timeout);
                                                    reject(e);
                                                }});
                                        }});
                                    }} catch (e) {{
                                        clearTimeout(timeout);
                                        reject(e);
                                    }}
                                }});
                            }}
                        """,
                        action,
                    ),
                    timeout=self._execute_timeout_seconds(fallback=30.0),
                )

            post_wait_seconds = float(getattr(config, "browser_recaptcha_settle_seconds", 3) or 3)
            if post_wait_seconds > 0:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} 自定义打码已完成，额外等待 {post_wait_seconds:.1f}s 后返回 token"
                )
                await asyncio.sleep(post_wait_seconds)

            if verify_url:
                verify_payload = await self._verify_score_in_page(page, token, verify_url)
                return {
                    "token": token,
                    **verify_payload,
                }

            return token
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)}"
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} 自定义打码失败: {msg[:200]}")
            if reuse_ready_page and custom_key:
                await self._drop_shared_custom_page(custom_key)
            return None
        finally:
            if page and not reuse_ready_page:
                try:
                    await page.close()
                except:
                    pass

    def is_busy(self) -> bool:
        return self._solve_inflight > 0

    def note_idle(self):
        if self._solve_inflight <= 0:
            self._last_idle_since = time.monotonic()

    def idle_seconds(self) -> float:
        if self.is_busy():
            return 0.0
        return max(0.0, time.monotonic() - self._last_idle_since)

    def has_shared_browser(self) -> bool:
        return bool(
            self._shared_browser
            or self._shared_context
            or self._shared_keepalive_page
            or self._shared_ready_page
            or self._shared_custom_pages
        )

    def get_last_fingerprint(self) -> Optional[Dict[str, Any]]:
        """返回最近一次打码浏览器的指纹快照。"""
        if not self._last_fingerprint:
            return None
        sanitized = dict(self._last_fingerprint)
        sanitized.pop("proxy_url", None)
        return sanitized
    
    async def get_token(
        self,
        project_id: str,
        website_key: str,
        action: str = "IMAGE_GENERATION",
        token_proxy_url: Optional[str] = None
    ) -> TokenAcquireResult:
        """Get a token from the shared browser unless a fatal browser error occurs."""
        async with self._semaphore:
            self._solve_inflight += 1
            max_retries = self._retry_max_attempts()
            retry_backoff_seconds = self._retry_backoff_seconds()

            try:
                for attempt in range(max_retries):
                    try:
                        start_ts = time.perf_counter()
                        _, _, context = await self._get_or_create_shared_browser(token_proxy_url=token_proxy_url)

                        result = await self._execute_captcha(context, project_id, website_key, action)
                        if result and result.token:
                            self._solve_count += 1
                            self._consecutive_browser_failures = 0
                            elapsed_ms = result.elapsed_ms or int((time.perf_counter() - start_ts) * 1000)
                            ready_hit = bool((result.timings or {}).get("ready_page_hit"))
                            result.browser_ref = self.token_id
                            result.browser_id = self.token_id
                            result.fingerprint = result.fingerprint or self.get_last_fingerprint()
                            result.browser_epoch = self.get_browser_epoch()
                            result.elapsed_ms = elapsed_ms
                            debug_logger.log_info(
                                f"[BrowserCaptcha] Token-{self.token_id} token acquired "
                                f"({elapsed_ms}ms, launches={self._shared_launch_count}, reuse={self._shared_reuse_count}, ready_page_hit={ready_hit})"
                            )
                            if result.timings:
                                debug_logger.log_info(
                                    f"[BrowserCaptcha] Token-{self.token_id} solve timings "
                                    f"goto={result.timings.get('goto_ms', 0)}ms "
                                    f"ready={result.timings.get('grecaptcha_ready_ms', 0)}ms "
                                    f"execute={result.timings.get('execute_ms', 0)}ms "
                                    f"reload={result.timings.get('reload_wait_ms', 0)}ms "
                                    f"clr={result.timings.get('clr_wait_ms', 0)}ms "
                                    f"settle={result.timings.get('settle_ms', 0)}ms"
                                )
                            return result

                        self._error_count += 1
                        self._consecutive_browser_failures += 1
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] Token-{self.token_id} token attempt {attempt + 1}/{max_retries} failed"
                        )
                        await self._drop_shared_ready_page()
                        if self._consecutive_browser_failures >= 2:
                            await self.recycle_browser(reason=f"captcha_failed_{attempt + 1}", rotate_profile=False)
                    except Exception as e:
                        self._error_count += 1
                        self._consecutive_browser_failures += 1
                        error_message = f"{type(e).__name__}: {str(e)}"
                        debug_logger.log_error(
                            f"[BrowserCaptcha] Token-{self.token_id} browser error: {error_message[:200]}"
                        )
                        error_lower = error_message.lower()
                        if any(keyword in error_lower for keyword in [
                            "context or browser has been closed",
                            "target closed",
                            "browser has been closed",
                            "connection closed",
                            "crash",
                            "closed",
                        ]):
                            await self.recycle_browser(reason="browser_runtime_error", rotate_profile=False)
                        else:
                            await self._drop_shared_ready_page()

                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_backoff_seconds)

                return TokenAcquireResult(
                    token=None,
                    browser_ref=None,
                    browser_id=self.token_id,
                    fingerprint=None,
                    source="live",
                    elapsed_ms=0,
                    browser_epoch=self.get_browser_epoch(),
                )
            finally:
                self._solve_inflight = max(0, self._solve_inflight - 1)
                self.note_idle()

    async def get_custom_token(
        self,
        website_url: str,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
        token_proxy_url: Optional[str] = None,
        captcha_type: str = "recaptcha_v3",
        is_invisible: bool = True,
    ) -> Optional[str]:
        """Get a custom reCAPTCHA token using the shared browser whenever possible."""
        async with self._semaphore:
            self._solve_inflight += 1
            max_retries = self._retry_max_attempts()
            retry_backoff_seconds = self._retry_backoff_seconds()

            try:
                for attempt in range(max_retries):
                    try:
                        start_ts = time.time()
                        _, _, context = await self._get_or_create_shared_browser(token_proxy_url=token_proxy_url)
                        token = await self._execute_custom_captcha(
                            context=context,
                            website_url=website_url,
                            website_key=website_key,
                            action=action,
                            enterprise=enterprise,
                            captcha_type=captcha_type,
                            is_invisible=is_invisible,
                            reuse_ready_page=True,
                        )

                        if token:
                            self._solve_count += 1
                            self._consecutive_browser_failures = 0
                            debug_logger.log_info(
                                f"[BrowserCaptcha] Token-{self.token_id} custom token acquired "
                                f"({(time.time()-start_ts)*1000:.0f}ms, launches={self._shared_launch_count}, reuse={self._shared_reuse_count})"
                            )
                            return token

                        self._error_count += 1
                        self._consecutive_browser_failures += 1
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] Token-{self.token_id} custom token attempt {attempt+1}/{max_retries} failed"
                        )
                        if self._consecutive_browser_failures >= 2:
                            await self.recycle_browser(reason=f"custom_token_failed_{attempt + 1}", rotate_profile=False)
                    except Exception as e:
                        self._error_count += 1
                        self._consecutive_browser_failures += 1
                        debug_logger.log_error(
                            f"[BrowserCaptcha] Token-{self.token_id} custom browser error: {type(e).__name__}: {str(e)[:200]}"
                        )
                        error_lower = str(e).lower()
                        if any(keyword in error_lower for keyword in [
                            "context or browser has been closed",
                            "target closed",
                            "browser has been closed",
                            "connection closed",
                            "crash",
                            "closed",
                        ]):
                            await self.recycle_browser(reason="custom_browser_runtime_error", rotate_profile=False)

                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_backoff_seconds)

                return None
            finally:
                self._solve_inflight = max(0, self._solve_inflight - 1)
                self.note_idle()

    async def get_custom_score(
        self,
        website_url: str,
        website_key: str,
        verify_url: str,
        action: str = "homepage",
        enterprise: bool = False,
        token_proxy_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get a custom token and verify its score using the shared browser whenever possible."""
        async with self._semaphore:
            self._solve_inflight += 1
            max_retries = self._retry_max_attempts()
            retry_backoff_seconds = self._retry_backoff_seconds()

            try:
                for attempt in range(max_retries):
                    try:
                        started_at = time.time()
                        _, _, context = await self._get_or_create_shared_browser(token_proxy_url=token_proxy_url)
                        payload = await self._execute_custom_captcha(
                            context=context,
                            website_url=website_url,
                            website_key=website_key,
                            action=action,
                            verify_url=verify_url,
                            enterprise=enterprise,
                            reuse_ready_page=True,
                        )

                        if isinstance(payload, dict) and payload.get("token"):
                            self._solve_count += 1
                            self._consecutive_browser_failures = 0
                            payload.setdefault("token_elapsed_ms", int((time.time() - started_at) * 1000))
                            debug_logger.log_info(
                                f"[BrowserCaptcha] Token-{self.token_id} in-page score verification succeeded "
                                f"({(time.time()-started_at)*1000:.0f}ms, launches={self._shared_launch_count}, reuse={self._shared_reuse_count})"
                            )
                            return payload

                        self._error_count += 1
                        self._consecutive_browser_failures += 1
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] Token-{self.token_id} in-page score attempt {attempt+1}/{max_retries} failed"
                        )
                        if self._consecutive_browser_failures >= 2:
                            await self.recycle_browser(reason=f"custom_score_failed_{attempt + 1}", rotate_profile=False)
                    except Exception as e:
                        self._error_count += 1
                        self._consecutive_browser_failures += 1
                        debug_logger.log_error(
                            f"[BrowserCaptcha] Token-{self.token_id} in-page score browser error: {type(e).__name__}: {str(e)[:200]}"
                        )
                        error_lower = str(e).lower()
                        if any(keyword in error_lower for keyword in [
                            "context or browser has been closed",
                            "target closed",
                            "browser has been closed",
                            "connection closed",
                            "crash",
                            "closed",
                        ]):
                            await self.recycle_browser(reason="custom_score_browser_runtime_error", rotate_profile=False)

                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_backoff_seconds)

                return {
                    "token": None,
                    "verify_mode": "browser_page",
                    "verify_elapsed_ms": 0,
                    "verify_http_status": None,
                    "verify_result": {}
                }
            finally:
                self._solve_inflight = max(0, self._solve_inflight - 1)
                self.note_idle()


class BrowserCaptchaService:
    """多浏览器轮询打码服务（单例模式）
    
    支持配置浏览器数量，每个浏览器只开 1 个标签页，请求轮询分配
    """
    
    _instance: Optional['BrowserCaptchaService'] = None
    _lock = asyncio.Lock()
    
    def __init__(self, db=None):
        self.db = db
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.base_user_data_dir = os.path.join(os.getcwd(), "browser_data_rt")
        self._browsers: Dict[int, TokenBrowser] = {}
        self._browsers_lock = asyncio.Lock()
        
        # Browser slot configuration
        self._browser_count = 1  # Default to 1; loaded from the database later
        self._round_robin_index = 0  # Round-robin cursor
        self._proxy_pool_cursor_by_key: Dict[str, int] = {}
        self._proxy_pool_lock = asyncio.Lock()
        self._project_slot_affinity: Dict[str, List[int]] = {}
        self._project_slot_last_used: Dict[str, float] = {}
        self._project_slot_lock = asyncio.Lock()
        self._standby_tokens: Dict[str, List[StandbyTokenEntry]] = {}
        self._standby_bucket_last_used: Dict[str, float] = {}
        self._standby_lock = asyncio.Lock()
        self._standby_refill_tasks: Dict[str, asyncio.Task] = {}
        self._foreground_solves_inflight = 0
        
        # Metrics
        self._stats = {
            "req_total": 0,
            "gen_ok": 0,
            "gen_fail": 0,
            "api_403": 0,
            "standby_hit": 0,
            "standby_miss": 0,
            "standby_fill_ok": 0,
            "standby_fill_fail": 0,
        }
        
        # The concurrency limit is initialized by _load_browser_count.
        self._token_semaphore = None
        self._idle_reaper_task: Optional[asyncio.Task] = None

    def _idle_reaper_interval_seconds(self) -> float:
        try:
            return max(1.0, float(getattr(config, "browser_idle_reaper_interval_seconds", 15.0) or 15.0))
        except Exception:
            return 15.0
    
    async def _ensure_idle_reaper(self):
        if self._idle_reaper_task is None or self._idle_reaper_task.done():
            self._idle_reaper_task = asyncio.create_task(self._idle_reaper_loop())

    async def _idle_reaper_loop(self):
        while True:
            try:
                await asyncio.sleep(self._idle_reaper_interval_seconds())
                idle_ttl = int(getattr(config, "browser_idle_ttl_seconds", 600) or 600)
                browsers = []
                async with self._browsers_lock:
                    browsers = list(self._browsers.values())
                for browser in browsers:
                    try:
                        if browser.is_busy():
                            continue
                        if not browser.has_shared_browser():
                            continue
                        if browser.idle_seconds() < idle_ttl:
                            continue
                        await browser.recycle_browser(reason=f"idle_ttl_{idle_ttl}s", rotate_profile=False)
                    except Exception as e:
                        debug_logger.log_warning(f"[BrowserCaptcha] idle reaper failed: {e}")
            except asyncio.CancelledError:
                return
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] idle reaper loop error: {e}")

    @classmethod
    async def get_instance(cls, db=None) -> 'BrowserCaptchaService':
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
                    # 从数据库加载 browser_count 配置
                    await cls._instance._load_browser_count()
                    await cls._instance._ensure_idle_reaper()
        return cls._instance
    
    def _check_available(self):
        """检查服务是否可用"""
        if DOCKER_HEADED_BLOCKED:
            raise RuntimeError(
                "检测到 Docker 环境，默认禁用有头浏览器打码。"
                "如需启用请设置环境变量 ALLOW_DOCKER_HEADED_CAPTCHA=true，并提供 DISPLAY/Xvfb。"
            )
        if IS_DOCKER and not os.environ.get("DISPLAY"):
            raise RuntimeError(
                "Docker 有头浏览器打码已启用，但 DISPLAY 未设置。"
                "请设置 DISPLAY（例如 :99）并启动 Xvfb。"
            )
        if not PLAYWRIGHT_AVAILABLE or async_playwright is None:
            raise RuntimeError(
                "playwright 未安装或不可用。"
                "请手动安装: pip install playwright && python -m playwright install chromium"
            )
    
    async def _load_browser_count(self):
        """从数据库加载浏览器数量配置"""
        if self.db:
            try:
                captcha_config = await self.db.get_captcha_config()
                self._browser_count = max(1, captcha_config.browser_count)
                debug_logger.log_info(f"[BrowserCaptcha] 浏览器数量配置: {self._browser_count}")
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 加载 browser_count 配置失败: {e}，使用默认值 1")
                self._browser_count = 1
        # 并发限制 = 浏览器数量，不再硬编码限制
        self._token_semaphore = asyncio.Semaphore(self._browser_count)
        debug_logger.log_info(f"[BrowserCaptcha] 并发上限: {self._browser_count}")
    
    async def reload_browser_count(self):
        """???????????????????????"""
        old_count = self._browser_count
        await self._load_browser_count()
        
        browsers_to_close: List[TokenBrowser] = []
        await self._ensure_idle_reaper()
        if self._browser_count < old_count:
            async with self._browsers_lock:
                for browser_id in list(self._browsers.keys()):
                    if browser_id >= self._browser_count:
                        browsers_to_close.append(self._browsers.pop(browser_id))
                        debug_logger.log_info(f"[BrowserCaptcha] ????????? {browser_id}")

        for browser in browsers_to_close:
            try:
                await browser.force_close_pending_browser(close_all=True)
                await browser.recycle_browser(reason="browser_slot_removed", rotate_profile=False)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] ???????????: {e}")

        async with self._project_slot_lock:
            pruned: Dict[str, List[int]] = {}
            for project_key, slots in self._project_slot_affinity.items():
                valid_slots = [slot for slot in slots if 0 <= slot < self._browser_count]
                if valid_slots:
                    pruned[project_key] = valid_slots
            self._project_slot_affinity = pruned
            self._project_slot_last_used = {
                project_key: float(self._project_slot_last_used.get(project_key, time.monotonic()))
                for project_key in self._project_slot_affinity
            }
            self._trim_project_affinity_locked()

    def _log_stats(self):
        total = self._stats["req_total"]
        gen_fail = self._stats["gen_fail"]
        api_403 = self._stats["api_403"]
        gen_ok = self._stats["gen_ok"]
        
        valid_success = gen_ok - api_403
        if valid_success < 0: valid_success = 0
        
        rate = (valid_success / total * 100) if total > 0 else 0.0

    
    async def _warmup_browser_slot(self, browser_id: int):
        browser = await self._get_or_create_browser(browser_id)
        try:
            await browser._get_or_create_shared_browser()
            debug_logger.log_info(f"[BrowserCaptcha] warmed browser slot {browser_id}")
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] warmup for slot {browser_id} failed: {e}")

    async def warmup_browser_slots(self):
        tasks = [self._warmup_browser_slot(browser_id) for browser_id in range(self._browser_count)]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _project_affinity_max_keys(self) -> int:
        fallback = max(32, self._browser_count * 16)
        try:
            return max(1, int(getattr(config, "browser_project_affinity_max_keys", fallback) or fallback))
        except Exception:
            return fallback

    def _project_affinity_ttl_seconds(self) -> float:
        try:
            return max(60.0, float(getattr(config, "browser_project_affinity_ttl_seconds", 1800.0) or 1800.0))
        except Exception:
            return 1800.0

    def _standby_bucket_max_count(self) -> int:
        fallback = max(32, self._browser_count * 12)
        try:
            return max(1, int(getattr(config, "browser_standby_bucket_max_count", fallback) or fallback))
        except Exception:
            return fallback

    def _standby_bucket_idle_ttl_seconds(self) -> float:
        fallback = max(self._standby_token_ttl_seconds() * 2.0, 180.0)
        try:
            return max(30.0, float(getattr(config, "browser_standby_bucket_idle_ttl_seconds", fallback) or fallback))
        except Exception:
            return fallback

    @staticmethod
    def _compact_standby_fingerprint(fingerprint: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(fingerprint, dict):
            return fingerprint
        compact: Dict[str, Any] = {}
        for key in (
            "user_agent",
            "userAgent",
            "accept_language",
            "sec_ch_ua",
            "sec_ch_ua_mobile",
            "sec_ch_ua_platform",
            "locale",
            "timezone_id",
            "profile_family",
        ):
            value = fingerprint.get(key)
            if isinstance(value, str) and value:
                compact[key] = value
        for key in ("device_scale_factor",):
            value = fingerprint.get(key)
            if isinstance(value, (int, float)) and value > 0:
                compact[key] = value
        for key in ("is_mobile", "has_touch"):
            value = fingerprint.get(key)
            if isinstance(value, bool):
                compact[key] = value
        viewport = fingerprint.get("viewport")
        if isinstance(viewport, dict):
            width = viewport.get("width")
            height = viewport.get("height")
            if isinstance(width, int) and isinstance(height, int):
                compact["viewport"] = {"width": width, "height": height}
        return compact or None

    def _touch_project_affinity_locked(self, project_key: str, *, now_value: Optional[float] = None):
        normalized_key = str(project_key or "").strip()
        if not normalized_key:
            return
        self._project_slot_last_used[normalized_key] = float(now_value if now_value is not None else time.monotonic())

    def _trim_project_affinity_locked(self):
        now_value = time.monotonic()
        ttl_seconds = self._project_affinity_ttl_seconds()
        stale_keys = [
            project_key
            for project_key, last_used in self._project_slot_last_used.items()
            if (now_value - float(last_used or 0.0)) >= ttl_seconds
        ]
        for project_key in stale_keys:
            self._project_slot_affinity.pop(project_key, None)
            self._project_slot_last_used.pop(project_key, None)

        max_keys = self._project_affinity_max_keys()
        while len(self._project_slot_affinity) > max_keys:
            evictable = [
                (project_key, float(self._project_slot_last_used.get(project_key, 0.0) or 0.0))
                for project_key in self._project_slot_affinity
            ]
            if not evictable:
                break
            evict_key = min(evictable, key=lambda item: item[1])[0]
            self._project_slot_affinity.pop(evict_key, None)
            self._project_slot_last_used.pop(evict_key, None)

    def _trim_standby_buckets_locked(self, *, now_value: Optional[float] = None) -> List[asyncio.Task]:
        current = float(now_value if now_value is not None else time.monotonic())
        idle_ttl = self._standby_bucket_idle_ttl_seconds()
        next_state: Dict[str, List[StandbyTokenEntry]] = {}

        for bucket_key, entries in list(self._standby_tokens.items()):
            kept = [entry for entry in entries if self._is_standby_entry_valid(entry, now_monotonic=current)]
            last_used = float(self._standby_bucket_last_used.get(bucket_key, 0.0) or 0.0)
            is_idle = last_used > 0 and (current - last_used) >= idle_ttl
            if kept and not is_idle:
                next_state[bucket_key] = kept
            else:
                self._standby_bucket_last_used.pop(bucket_key, None)

        self._standby_tokens = next_state

        cancelled_tasks: List[asyncio.Task] = []
        max_buckets = self._standby_bucket_max_count()
        while len(self._standby_tokens) > max_buckets:
            evictable = [
                (bucket_key, float(self._standby_bucket_last_used.get(bucket_key, 0.0) or 0.0))
                for bucket_key in self._standby_tokens
            ]
            if not evictable:
                break
            evict_key = min(evictable, key=lambda item: item[1])[0]
            self._standby_tokens.pop(evict_key, None)
            self._standby_bucket_last_used.pop(evict_key, None)
            refill_task = self._standby_refill_tasks.pop(evict_key, None)
            if refill_task is not None and not refill_task.done():
                cancelled_tasks.append(refill_task)

        return cancelled_tasks

    async def _select_browser_id(self, project_id: Optional[str]) -> int:
        project_key = str(project_id or '').strip()
        affinity_slots: List[int] = []
        if project_key:
            async with self._project_slot_lock:
                self._trim_project_affinity_locked()
                affinity_slots = [slot for slot in self._project_slot_affinity.get(project_key, []) if 0 <= slot < self._browser_count]
                self._project_slot_affinity[project_key] = affinity_slots
                self._touch_project_affinity_locked(project_key)

        async with self._browsers_lock:
            def is_slot_idle(slot_id: int) -> bool:
                browser = self._browsers.get(slot_id)
                return browser is None or not getattr(browser, 'is_busy', lambda: False)()

            for slot_id in affinity_slots:
                if is_slot_idle(slot_id):
                    return slot_id

            for offset in range(self._browser_count):
                slot_id = (self._round_robin_index + offset) % self._browser_count
                if is_slot_idle(slot_id):
                    self._round_robin_index = (slot_id + 1) % self._browser_count
                    if project_key:
                        async with self._project_slot_lock:
                            self._trim_project_affinity_locked()
                            slots = [slot for slot in self._project_slot_affinity.get(project_key, []) if 0 <= slot < self._browser_count]
                            if slot_id not in slots:
                                slots.append(slot_id)
                            self._project_slot_affinity[project_key] = slots
                            self._touch_project_affinity_locked(project_key)
                    return slot_id

        slot_id = self._get_next_browser_id()
        if project_key:
            async with self._project_slot_lock:
                self._trim_project_affinity_locked()
                slots = [slot for slot in self._project_slot_affinity.get(project_key, []) if 0 <= slot < self._browser_count]
                if slot_id not in slots:
                    slots.append(slot_id)
                self._project_slot_affinity[project_key] = slots
                self._touch_project_affinity_locked(project_key)
        return slot_id

    async def _get_or_create_browser(self, browser_id: int) -> TokenBrowser:
        """获取或创建指定 ID 的浏览器实例"""
        async with self._browsers_lock:
            if browser_id not in self._browsers:
                user_data_dir = os.path.join(self.base_user_data_dir, f"browser_{browser_id}")
                browser = TokenBrowser(browser_id, user_data_dir, db=self.db)
                self._browsers[browser_id] = browser
                debug_logger.log_info(f"[BrowserCaptcha] 创建浏览器实例 {browser_id}")
            return self._browsers[browser_id]
    
    def _get_next_browser_id(self) -> int:
        """轮询获取下一个浏览器 ID"""
        browser_id = self._round_robin_index % self._browser_count
        self._round_robin_index += 1
        return browser_id

    @staticmethod
    def _compose_browser_ref(browser_id: int, request_ref: Optional[str]) -> Union[int, str]:
        """将 browser_id 与 request_ref 合并为可回传的请求句柄。"""
        if request_ref:
            return f"{browser_id}:{request_ref}"
        return browser_id

    @staticmethod
    def _parse_browser_ref(browser_ref: Optional[Union[int, str]]) -> tuple[Optional[int], Optional[str]]:
        """解析请求句柄，兼容旧的纯 int browser_id。"""
        if browser_ref is None:
            return None, None

        if isinstance(browser_ref, int):
            return browser_ref, None

        if isinstance(browser_ref, str):
            raw = browser_ref.strip()
            if raw.isdigit():
                return int(raw), None
            browser_id_part, sep, request_ref = raw.partition(":")
            if sep and browser_id_part.isdigit() and request_ref:
                return int(browser_id_part), request_ref

        return None, None

    async def _resolve_token_proxy_url(self, token_id: Optional[int]) -> Optional[str]:
        """Read token-level proxy configuration with proxy-pool rotation."""
        if not token_id or not self.db:
            return None
        try:
            token = await self.db.get_token(token_id)
            if token and token.captcha_proxy_url and token.captcha_proxy_url.strip():
                return await self._pick_proxy_from_pool(
                    token.captcha_proxy_url.strip(),
                    cursor_key=f"token:{token_id}",
                )
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] failed to read token({token_id}) proxy config: {e}")
        return None

    async def _resolve_global_proxy_url(self) -> Optional[str]:
        """Read the global proxy configuration with proxy-pool rotation."""
        if not self.db:
            return None
        try:
            captcha_config = await self.db.get_captcha_config()
            if not captcha_config.browser_proxy_enabled:
                return None
            if not captcha_config.browser_proxy_url:
                return None
            return await self._pick_proxy_from_pool(
                captcha_config.browser_proxy_url.strip(),
                cursor_key="global",
            )
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] failed to read the global proxy pool: {e}")
            return None

    async def _pick_proxy_from_pool(self, proxy_value: str, cursor_key: str) -> Optional[str]:
        """Resolve runtime proxy configuration."""
        normalized_pool, warning_messages = normalize_browser_proxy_pool(proxy_value)
        for warning in warning_messages:
            debug_logger.log_warning(f"[BrowserCaptcha] {warning}")

        valid_pool: List[str] = []
        for index, normalized_proxy in enumerate(normalized_pool, start=1):
            if parse_proxy_url(normalized_proxy):
                valid_pool.append(normalized_proxy)
            else:
                debug_logger.log_warning(f"[BrowserCaptcha] proxy pool entry {index} has an invalid format and was ignored")

        if not valid_pool:
            return None

        async with self._proxy_pool_lock:
            cursor = int(self._proxy_pool_cursor_by_key.get(cursor_key, 0))
            selected_proxy = valid_pool[cursor % len(valid_pool)]
            self._proxy_pool_cursor_by_key[cursor_key] = (cursor + 1) % len(valid_pool)
        return selected_proxy

    async def _resolve_effective_proxy_url(self, token_id: Optional[int]) -> Optional[str]:
        """Prefer the token proxy pool; otherwise fall back to the global proxy pool."""
        token_proxy = await self._resolve_token_proxy_url(token_id)
        if token_proxy:
            return token_proxy
        return await self._resolve_global_proxy_url()

    def _build_standby_bucket_key(
        self,
        project_id: str,
        action: str,
        token_proxy_url: Optional[str],
    ) -> str:
        project_key = str(project_id or "").strip()
        action_key = str(action or "IMAGE_GENERATION").strip().upper()
        proxy_key = str(token_proxy_url or "").strip()
        if proxy_key:
            proxy_key = hashlib.sha1(proxy_key.encode("utf-8")).hexdigest()[:16]
        else:
            proxy_key = "direct"
        return f"{project_key}|{action_key}|{proxy_key}"

    def _standby_pool_enabled(self) -> bool:
        try:
            if not bool(getattr(config, "browser_standby_token_pool_enabled", True)):
                return False
            return max(0, int(getattr(config, "browser_standby_token_pool_depth", 2) or 2)) > 0
        except Exception:
            return True

    def _standby_pool_depth(self) -> int:
        try:
            return max(0, int(getattr(config, "browser_standby_token_pool_depth", 2) or 2))
        except Exception:
            return 2

    def _standby_token_ttl_seconds(self) -> float:
        try:
            return max(5.0, float(getattr(config, "browser_standby_token_ttl_seconds", 45) or 45))
        except Exception:
            return 45.0

    def _standby_refill_wait_seconds(self) -> float:
        try:
            return max(0.05, float(getattr(config, "browser_standby_refill_idle_seconds", 0.8) or 0.8))
        except Exception:
            return 0.8

    def _standby_refill_retry_rounds(self) -> int:
        depth = max(1, self._standby_pool_depth())
        return max(4, self._browser_count * depth * 2)

    def _get_browser_epoch_for_standby(self, browser_id: int) -> Optional[int]:
        browser = self._browsers.get(browser_id)
        if not browser:
            return None
        if not getattr(browser, "has_shared_browser", lambda: False)():
            return None
        getter = getattr(browser, "get_browser_epoch", None)
        if callable(getter):
            try:
                return int(getter())
            except Exception:
                return None
        return None

    def _is_standby_entry_valid(self, entry: StandbyTokenEntry, now_monotonic: Optional[float] = None) -> bool:
        if not entry or not entry.token:
            return False
        now_value = now_monotonic if now_monotonic is not None else time.monotonic()
        if entry.expires_monotonic <= now_value:
            return False
        current_epoch = self._get_browser_epoch_for_standby(entry.browser_id)
        if current_epoch is None:
            return True
        return int(current_epoch) == int(entry.browser_epoch)

    async def _select_idle_browser_id_for_refill(
        self,
        project_id: str,
        preferred_browser_id: Optional[int],
    ) -> Optional[int]:
        project_key = str(project_id or "").strip()
        affinity_slots: List[int] = []
        if project_key:
            async with self._project_slot_lock:
                self._trim_project_affinity_locked()
                affinity_slots = [
                    slot
                    for slot in self._project_slot_affinity.get(project_key, [])
                    if 0 <= slot < self._browser_count
                ]

        async with self._browsers_lock:
            def is_slot_idle(slot_id: int) -> bool:
                browser = self._browsers.get(slot_id)
                return browser is None or not getattr(browser, "is_busy", lambda: False)()

            if preferred_browser_id is not None and 0 <= preferred_browser_id < self._browser_count:
                if is_slot_idle(preferred_browser_id):
                    return preferred_browser_id

            for slot_id in affinity_slots:
                if slot_id == preferred_browser_id:
                    continue
                if is_slot_idle(slot_id):
                    return slot_id

            for offset in range(self._browser_count):
                slot_id = (self._round_robin_index + offset) % self._browser_count
                if slot_id == preferred_browser_id:
                    continue
                if is_slot_idle(slot_id):
                    return slot_id

        return None

    async def _take_standby_token(self, bucket_key: str) -> Optional[TokenAcquireResult]:
        now_value = time.monotonic()
        selected: Optional[StandbyTokenEntry] = None
        cancelled_tasks: List[asyncio.Task] = []

        async with self._standby_lock:
            cancelled_tasks = self._trim_standby_buckets_locked(now_value=now_value)
            entries = list(self._standby_tokens.get(bucket_key, []))
            remaining: List[StandbyTokenEntry] = []
            for entry in entries:
                if not self._is_standby_entry_valid(entry, now_monotonic=now_value):
                    continue
                if selected is None:
                    selected = entry
                    continue
                remaining.append(entry)

            if remaining:
                self._standby_tokens[bucket_key] = remaining
                self._standby_bucket_last_used[bucket_key] = now_value
            else:
                self._standby_tokens.pop(bucket_key, None)
                self._standby_bucket_last_used.pop(bucket_key, None)

        for task in cancelled_tasks:
            task.cancel()

        if not selected:
            return None

        return TokenAcquireResult(
            token=selected.token,
            browser_ref=selected.browser_id,
            browser_id=selected.browser_id,
            fingerprint=dict(selected.fingerprint) if isinstance(selected.fingerprint, dict) else selected.fingerprint,
            source="standby",
            elapsed_ms=0,
            browser_epoch=selected.browser_epoch,
        )

    async def _store_standby_token(
        self,
        bucket_key: str,
        result: TokenAcquireResult,
        project_id: str,
        action: str,
    ):
        if not self._standby_pool_enabled():
            return
        if not result or not result.token or result.browser_id is None:
            return

        now_value = time.monotonic()
        proxy_signature = bucket_key.rsplit("|", 1)[-1]
        entry = StandbyTokenEntry(
            token=result.token,
            browser_id=int(result.browser_id),
            fingerprint=self._compact_standby_fingerprint(
                dict(result.fingerprint) if isinstance(result.fingerprint, dict) else result.fingerprint
            ),
            browser_epoch=int(result.browser_epoch),
            project_id=str(project_id or "").strip(),
            action=str(action or "IMAGE_GENERATION").strip().upper(),
            proxy_signature=proxy_signature,
            created_monotonic=now_value,
            expires_monotonic=now_value + self._standby_token_ttl_seconds(),
        )

        async with self._standby_lock:
            cancelled_tasks = self._trim_standby_buckets_locked(now_value=now_value)
            entries = [
                existing
                for existing in self._standby_tokens.get(bucket_key, [])
                if self._is_standby_entry_valid(existing, now_monotonic=now_value)
            ]
            entries.append(entry)
            depth = self._standby_pool_depth()
            if depth > 0:
                entries = entries[-depth:]
            if entries:
                self._standby_tokens[bucket_key] = entries
                self._standby_bucket_last_used[bucket_key] = now_value
            else:
                self._standby_tokens.pop(bucket_key, None)
                self._standby_bucket_last_used.pop(bucket_key, None)

        for task in cancelled_tasks:
            task.cancel()

    async def _invalidate_standby_tokens_for_browser(self, browser_id: int):
        async with self._standby_lock:
            next_state: Dict[str, List[StandbyTokenEntry]] = {}
            for bucket_key, entries in self._standby_tokens.items():
                kept = [entry for entry in entries if entry.browser_id != browser_id]
                if kept:
                    next_state[bucket_key] = kept
                else:
                    self._standby_bucket_last_used.pop(bucket_key, None)
            self._standby_tokens = next_state

    async def _acquire_live_token(
        self,
        project_id: str,
        action: str,
        token_proxy_url: Optional[str],
        browser_id: Optional[int] = None,
    ) -> TokenAcquireResult:
        if browser_id is None:
            browser_id = await self._select_browser_id(project_id)
        browser = await self._get_or_create_browser(browser_id)
        result = await browser.get_token(
            project_id,
            self.website_key,
            action,
            token_proxy_url=token_proxy_url,
        )
        result.browser_id = browser_id
        result.browser_ref = self._compose_browser_ref(browser_id, None)
        return result

    async def _schedule_standby_refill(
        self,
        bucket_key: str,
        project_id: str,
        action: str,
        token_proxy_url: Optional[str],
        preferred_browser_id: Optional[int],
    ):
        if not self._standby_pool_enabled():
            return

        async with self._standby_lock:
            cancelled_tasks = self._trim_standby_buckets_locked()
            existing_task = self._standby_refill_tasks.get(bucket_key)
            if existing_task and not existing_task.done():
                for task in cancelled_tasks:
                    task.cancel()
                return
            current_depth = len(self._standby_tokens.get(bucket_key, []))
            if current_depth >= self._standby_pool_depth():
                for task in cancelled_tasks:
                    task.cancel()
                return
            task = asyncio.create_task(
                self._refill_standby_token(
                    bucket_key=bucket_key,
                    project_id=project_id,
                    action=action,
                    token_proxy_url=token_proxy_url,
                    preferred_browser_id=preferred_browser_id,
                )
            )
            self._standby_refill_tasks[bucket_key] = task
            self._standby_bucket_last_used[bucket_key] = time.monotonic()

        for task in cancelled_tasks:
            task.cancel()

    async def _refill_standby_token(
        self,
        bucket_key: str,
        project_id: str,
        action: str,
        token_proxy_url: Optional[str],
        preferred_browser_id: Optional[int],
    ):
        try:
            refill_wait_seconds = self._standby_refill_wait_seconds()
            next_preferred_browser_id = preferred_browser_id

            for _ in range(self._standby_refill_retry_rounds()):
                await asyncio.sleep(refill_wait_seconds)

                now_value = time.monotonic()
                async with self._standby_lock:
                    current_depth = len(
                        [
                            entry
                            for entry in self._standby_tokens.get(bucket_key, [])
                            if self._is_standby_entry_valid(entry, now_monotonic=now_value)
                        ]
                    )
                target_depth = self._standby_pool_depth()
                if target_depth <= 0 or current_depth >= target_depth:
                    return

                refill_browser_id = await self._select_idle_browser_id_for_refill(
                    project_id=project_id,
                    preferred_browser_id=next_preferred_browser_id,
                )
                if refill_browser_id is None:
                    continue

                result = await self._acquire_live_token(
                    project_id=project_id,
                    action=action,
                    token_proxy_url=token_proxy_url,
                    browser_id=refill_browser_id,
                )

                if not result.token:
                    self._stats["standby_fill_fail"] += 1
                    return

                result.source = "standby_fill"
                await self._store_standby_token(
                    bucket_key=bucket_key,
                    result=result,
                    project_id=project_id,
                    action=action,
                )
                self._stats["standby_fill_ok"] += 1
                next_preferred_browser_id = result.browser_id
                debug_logger.log_info(
                    f"[BrowserCaptcha] standby token refilled bucket={bucket_key[:120]} browser={result.browser_id}"
                )
        except Exception as e:
            self._stats["standby_fill_fail"] += 1
            debug_logger.log_warning(f"[BrowserCaptcha] standby refill failed bucket={bucket_key[:120]}: {e}")
        finally:
            async with self._standby_lock:
                self._standby_refill_tasks.pop(bucket_key, None)

    async def get_token(self, project_id: str, action: str = "IMAGE_GENERATION", token_id: int = None) -> TokenAcquireResult:
        """Get a reCAPTCHA token and recycle the shared browser only after fatal browser errors.
        
        Args:
            project_id: project ID
            action: reCAPTCHA action
            token_id: business token ID used to resolve token-level proxy settings
        
        Returns:
            token result with browser_ref/fingerprint/source metadata
        """
        self._check_available()

        self._stats["req_total"] += 1
        token_proxy_url = await self._resolve_effective_proxy_url(token_id)
        bucket_key = self._build_standby_bucket_key(project_id, action, token_proxy_url)
        standby_result = await self._take_standby_token(bucket_key) if self._standby_pool_enabled() else None
        if standby_result and standby_result.token:
            self._stats["gen_ok"] += 1
            self._stats["standby_hit"] += 1
            await self._schedule_standby_refill(
                bucket_key=bucket_key,
                project_id=project_id,
                action=action,
                token_proxy_url=token_proxy_url,
                preferred_browser_id=standby_result.browser_id,
            )
            self._log_stats()
            return standby_result

        if self._standby_pool_enabled():
            self._stats["standby_miss"] += 1

        self._foreground_solves_inflight += 1
        try:
            if self._token_semaphore:
                async with self._token_semaphore:
                    live_result = await self._acquire_live_token(
                        project_id=project_id,
                        action=action,
                        token_proxy_url=token_proxy_url,
                    )
            else:
                live_result = await self._acquire_live_token(
                    project_id=project_id,
                    action=action,
                    token_proxy_url=token_proxy_url,
                )
        finally:
            self._foreground_solves_inflight = max(0, self._foreground_solves_inflight - 1)

        if live_result.token:
            self._stats["gen_ok"] += 1
            await self._schedule_standby_refill(
                bucket_key=bucket_key,
                project_id=project_id,
                action=action,
                token_proxy_url=token_proxy_url,
                preferred_browser_id=live_result.browser_id,
            )
        else:
            self._stats["gen_fail"] += 1

        self._log_stats()
        return live_result

    async def get_custom_token(
        self,
        website_url: str,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
        captcha_type: str = "recaptcha_v3",
        is_invisible: bool = True,
    ) -> tuple[Optional[str], int]:
        """获取任意站点的 reCAPTCHA token，用于分数测试。"""
        self._check_available()
        token_proxy_url = await self._resolve_global_proxy_url()
        custom_slot_key = f"custom:{captcha_type}:{1 if enterprise else 0}:{website_key}:{website_url}"

        if self._token_semaphore:
            async with self._token_semaphore:
                browser_id = await self._select_browser_id(custom_slot_key)
                browser = await self._get_or_create_browser(browser_id)
                token = await browser.get_custom_token(
                    website_url=website_url,
                    website_key=website_key,
                    action=action,
                    enterprise=enterprise,
                    token_proxy_url=token_proxy_url,
                    captcha_type=captcha_type,
                    is_invisible=is_invisible,
                )
            return token, browser_id

        browser_id = await self._select_browser_id(custom_slot_key)
        browser = await self._get_or_create_browser(browser_id)
        token = await browser.get_custom_token(
            website_url=website_url,
            website_key=website_key,
            action=action,
            enterprise=enterprise,
            token_proxy_url=token_proxy_url,
            captcha_type=captcha_type,
            is_invisible=is_invisible,
        )
        return token, browser_id

    async def get_custom_score(
        self,
        website_url: str,
        website_key: str,
        verify_url: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> tuple[Dict[str, Any], int]:
        """在浏览器页面内完成 token 获取与分数校验。"""
        self._check_available()
        token_proxy_url = await self._resolve_global_proxy_url()
        custom_slot_key = f"custom_score:{1 if enterprise else 0}:{website_key}:{website_url}"

        if self._token_semaphore:
            async with self._token_semaphore:
                browser_id = await self._select_browser_id(custom_slot_key)
                browser = await self._get_or_create_browser(browser_id)
                payload = await browser.get_custom_score(
                    website_url=website_url,
                    website_key=website_key,
                    verify_url=verify_url,
                    action=action,
                    enterprise=enterprise,
                    token_proxy_url=token_proxy_url,
                )
            return payload, browser_id

        browser_id = await self._select_browser_id(custom_slot_key)
        browser = await self._get_or_create_browser(browser_id)
        payload = await browser.get_custom_score(
            website_url=website_url,
            website_key=website_key,
            verify_url=verify_url,
            action=action,
            enterprise=enterprise,
            token_proxy_url=token_proxy_url,
        )
        return payload, browser_id

    async def get_fingerprint(self, browser_ref: Optional[Union[int, str]]) -> Optional[Dict[str, Any]]:
        """获取指定浏览器最近一次打码时的指纹快照。"""
        browser_id, _ = self._parse_browser_ref(browser_ref)
        if browser_id is None:
            return None

        async with self._browsers_lock:
            browser = self._browsers.get(browser_id)
            if not browser:
                return None
            return browser.get_last_fingerprint()

    async def report_error(self, browser_ref: Optional[Union[int, str]] = None, error_reason: Optional[str] = None):
        """Handle upstream errors; recycle the browser only for explicit reCAPTCHA evaluation failures."""
        browser_id, _ = self._parse_browser_ref(browser_ref)

        async with self._browsers_lock:
            browser = self._browsers.get(browser_id) if browser_id is not None else None
            error_text = error_reason or ""
            error_lower = error_text.lower()
            has_recaptcha = "recaptcha" in error_lower
            should_recycle = has_recaptcha and (
                "evaluation failed" in error_lower
                or "verification failed" in error_lower or "验证失败" in error_text
                or "failed" in error_lower
            )
            if should_recycle:
                self._stats["api_403"] += 1
            if browser_id is not None:
                debug_logger.log_info(
                    f"[BrowserCaptcha] browser {browser_id} failure reported, reason={error_reason or 'unknown'}, recycle={should_recycle}"
                )

        if browser and should_recycle:
            try:
                await browser.recycle_browser(
                    reason=error_reason or "recaptcha_evaluation_failed",
                    rotate_profile=True,
                )
                await self._invalidate_standby_tokens_for_browser(browser_id)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] browser {browser_id} recycle failed: {e}")

    async def report_request_finished(self, browser_ref: Optional[Union[int, str]] = None):
        """上层通知本次请求已完成；browser 模式仅保留常驻浏览器，不在成功后主动关闭。"""
        browser_id, _ = self._parse_browser_ref(browser_ref)
        if browser_id is None:
            return

        async with self._browsers_lock:
            browser = self._browsers.get(browser_id)

        if browser:
            keepalive_alive = False
            keepalive_page = getattr(browser, '_shared_keepalive_page', None)
            try:
                keepalive_alive = bool(keepalive_page and not keepalive_page.is_closed())
            except Exception:
                keepalive_alive = False
            debug_logger.log_info(
                f"[BrowserCaptcha] browser {browser_id} request finished; keepalive_alive={keepalive_alive}"
            )

    async def remove_browser(self, browser_id: int):
        browser = None
        async with self._browsers_lock:
            browser = self._browsers.pop(browser_id, None)

        if browser:
            try:
                await self._invalidate_standby_tokens_for_browser(browser_id)
                await browser.force_close_pending_browser(close_all=True)
                await browser.recycle_browser(reason="browser_slot_removed", rotate_profile=False)
            except Exception:
                pass

    async def close(self):
        async with self._browsers_lock:
            browsers = list(self._browsers.values())
            self._browsers.clear()

        if self._idle_reaper_task and not self._idle_reaper_task.done():
            self._idle_reaper_task.cancel()
            try:
                await self._idle_reaper_task
            except asyncio.CancelledError:
                pass

        async with self._standby_lock:
            refill_tasks = list(self._standby_refill_tasks.values())
            self._standby_refill_tasks.clear()
            self._standby_tokens.clear()
            self._standby_bucket_last_used.clear()

        async with self._project_slot_lock:
            self._project_slot_affinity.clear()
            self._project_slot_last_used.clear()

        for task in refill_tasks:
            if not task or task.done():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        for browser in browsers:
            try:
                await browser.force_close_pending_browser(close_all=True)
                await browser.recycle_browser(reason="service_shutdown", rotate_profile=False)
            except Exception:
                pass
            
    async def open_login_browser(self): return {"success": False, "error": "Not implemented"}
    async def create_browser_for_token(self, t, s=None): pass
    def get_stats(self): 
        browsers = list(self._browsers.values())
        busy_browser_count = sum(1 for browser in browsers if getattr(browser, "is_busy", lambda: False)())
        base_stats = {
            "total_solve_count": self._stats["gen_ok"],
            "total_error_count": self._stats["gen_fail"],
            "risk_403_count": self._stats["api_403"],
            "browser_count": len(self._browsers),
            "configured_browser_count": self._browser_count,
            "busy_browser_count": busy_browser_count,
            "idle_browser_count": max(self._browser_count - busy_browser_count, 0),
            "project_affinity_count": len(self._project_slot_affinity),
            "standby_hit": self._stats["standby_hit"],
            "standby_miss": self._stats["standby_miss"],
            "standby_fill_ok": self._stats["standby_fill_ok"],
            "standby_fill_fail": self._stats["standby_fill_fail"],
            "standby_bucket_count": len(self._standby_tokens),
            "browsers": []
        }
        return base_stats

