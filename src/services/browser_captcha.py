"""
基于 RT 的本地 reCAPTCHA 打码服务 (终极闭环版 - 无 fake_useragent 纯净版)
支持：自动刷新 Session Token、外部触发指纹切换、死磕重试
"""
import os
import sys
import subprocess
# 修复 Windows 上 playwright 的 asyncio 兼容性问题
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

import asyncio
import time
import re
import random
from pathlib import Path
from typing import Optional, Dict, Any, List
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
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # 尝试获取浏览器路径，如果失败说明未安装
            browser_path = p.chromium.executable_path
            if browser_path and os.path.exists(browser_path):
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

def validate_browser_proxy_url(proxy_url: str) -> tuple[bool, str]:
    if not proxy_url: return True, None
    normalized_proxy_url, _ = normalize_browser_proxy_url(proxy_url)
    parsed = parse_proxy_url(normalized_proxy_url)
    if not parsed: return False, "代理格式错误"
    return True, None

class TokenBrowser:
    """简化版浏览器：每次获取 token 时启动新浏览器，用完即关
    
    每次都是新的随机 UA，避免长时间运行导致的各种问题
    """
    # UA ???? 2026-03-01 ??????? score >= 0.3 ? UA?
    UA_LIST = [
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
    
    def __init__(self, token_id: int, user_data_dir: str, db=None):
        self.token_id = token_id
        self.user_data_dir = user_data_dir
        self.db = db
        self._semaphore = asyncio.Semaphore(1)  # 同时只能有一个任务
        self._solve_count = 0
        self._error_count = 0
        self._last_fingerprint: Optional[Dict[str, Any]] = None
        self._browser_proxy_active = False
        # 打码成功后延迟关闭浏览器：等待上游图片/视频请求完成通知
        self._pending_release_events: List[asyncio.Event] = []
        self._pending_release_tasks: List[asyncio.Task] = []
        self._pending_release_lock = asyncio.Lock()
    
    async def _create_browser(self, token_proxy_url: Optional[str] = None) -> tuple:
        """创建新浏览器实例（新 UA），返回 (playwright, browser, context)"""
        import random
        
        random_ua = random.choice(self.UA_LIST)
        base_w, base_h = random.choice(self.RESOLUTIONS)
        width, height = base_w, base_h - random.randint(0, 80)
        viewport = {"width": width, "height": height}
        launch_in_background = bool(getattr(config, "browser_launch_background", True))
        
        playwright = await async_playwright().start()
        Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
        
        # 代理配置
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
                        f"[BrowserCaptcha] Token-{self.token_id} 使用{proxy_source}代理: {proxy_option['server']}"
                    )
                else:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} {proxy_source}代理格式无效，已忽略"
                    )
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} 读取代理配置失败: {e}")
        
        # 先记录创建时的指纹，后续会在页面中补齐 sec-ch-* 等信息
        self._last_fingerprint = {
            "user_agent": random_ua,
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
                    f"[BrowserCaptcha] Token-{self.token_id} 有头浏览器将以后台模式启动"
                )

            browser = await playwright.chromium.launch(
                headless=False,
                proxy=proxy_option,
                args=browser_args
            )
            context = await browser.new_context(
                user_agent=random_ua,
                viewport=viewport,
            )
            return playwright, browser, context
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] Token-{self.token_id} 启动浏览器失败: {type(e).__name__}: {str(e)[:200]}")
            # 确保清理已创建的对象
            try:
                if playwright:
                    await playwright.stop()
            except: pass
            raise

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
    
    async def _close_browser(self, playwright, browser, context):
        """关闭浏览器实例"""
        try:
            if context:
                await context.close()
        except: pass
        try:
            if browser:
                await browser.close()
        except: pass
        try:
            if playwright:
                await playwright.stop()
        except: pass

    async def _wait_and_close_after_request(
        self,
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
                f"[BrowserCaptcha] Token-{self.token_id} {close_reason}，浏览器已关闭 (action={action})"
            )
            async with self._pending_release_lock:
                current_task = asyncio.current_task()
                if current_task in self._pending_release_tasks:
                    self._pending_release_tasks.remove(current_task)
                if release_event in self._pending_release_events:
                    self._pending_release_events.remove(release_event)

    async def _defer_browser_close_until_request_done(
        self,
        playwright,
        browser,
        context,
        action: str
    ):
        """打码成功后延迟关闭浏览器，等待 Flow 请求结束通知。"""
        flow_timeout = int(getattr(config, "flow_timeout", 300) or 300)
        upsample_timeout = int(getattr(config, "upsample_timeout", 300) or 300)
        if action == "IMAGE_GENERATION":
            # 图片链路可能包含放大请求，等待上限至少覆盖 flow/upsample 超时
            base_timeout = max(flow_timeout, upsample_timeout)
            wait_timeout = max(base_timeout + 180, 900)
        else:
            # 视频请求默认超时更长，给更大的缓冲避免“请求未结束就关闭”
            wait_timeout = max(flow_timeout + 300, 1800)
        release_event = asyncio.Event()
        release_task = asyncio.create_task(
            self._wait_and_close_after_request(
                release_event=release_event,
                wait_timeout=wait_timeout,
                playwright=playwright,
                browser=browser,
                context=context,
                action=action,
            )
        )

        async with self._pending_release_lock:
            self._pending_release_events.append(release_event)
            self._pending_release_tasks.append(release_task)
        debug_logger.log_info(
            f"[BrowserCaptcha] Token-{self.token_id} 打码成功后进入延迟关闭，等待上游请求完成 (action={action}, timeout={wait_timeout}s)"
        )

    async def notify_generation_request_finished(self):
        """通知当前 Token 对应的上游图片/视频请求已结束。"""
        async with self._pending_release_lock:
            release_event = self._pending_release_events.pop(0) if self._pending_release_events else None
        if release_event and not release_event.is_set():
            release_event.set()
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} 收到上游请求完成通知，开始关闭浏览器"
            )

    async def force_close_pending_browser(self):
        """强制关闭待释放浏览器（服务关闭时调用）。"""
        async with self._pending_release_lock:
            release_events = list(self._pending_release_events)
            release_tasks = list(self._pending_release_tasks)
            self._pending_release_events.clear()
            self._pending_release_tasks.clear()

        for release_event in release_events:
            if not release_event.is_set():
                release_event.set()
        for release_task in release_tasks:
            try:
                await asyncio.wait_for(release_task, timeout=5)
            except Exception:
                pass
    
    async def _execute_captcha(self, context, project_id: str, website_key: str, action: str) -> Optional[str]:
        """在给定 context 中执行打码逻辑"""
        page = None
        try:
            page = await context.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            
            page_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
            primary_host = "https://www.recaptcha.net" if self._browser_proxy_active else "https://www.google.com"
            secondary_host = "https://www.google.com" if primary_host == "https://www.recaptcha.net" else "https://www.recaptcha.net"
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} 加载 enterprise.js: primary={primary_host}, secondary={secondary_host}"
            )
            
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
            try:
                await page.goto(page_url, wait_until="load", timeout=30000)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} page.goto 失败: {type(e).__name__}: {str(e)[:200]}")
                return None
            
            try:
                await page.wait_for_function("typeof grecaptcha !== 'undefined'", timeout=15000)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} grecaptcha 未就绪: {type(e).__name__}: {str(e)[:200]}")
                return None

            # 记录本次打码页面的真实 UA/客户端提示头
            await self._capture_page_fingerprint(page)
            
            token = await asyncio.wait_for(
                page.evaluate(f"""
                    (actionName) => {{
                        return new Promise((resolve, reject) => {{
                            const timeout = setTimeout(() => reject(new Error('timeout')), 25000);
                            grecaptcha.enterprise.execute('{website_key}', {{action: actionName}})
                                .then(t => {{ resolve(t); }})
                                .catch(e => {{ reject(e); }});
                        }});
                    }}
                """, action),
                timeout=30
            )

            # 按要求：等待 enterprise/reload 与 enterprise/clr 均出现并返回 200
            try:
                await asyncio.wait_for(reload_ok_event.wait(), timeout=12)
            except asyncio.TimeoutError:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} 等待 recaptcha enterprise/reload 200 超时"
                )
                return None

            try:
                await asyncio.wait_for(clr_ok_event.wait(), timeout=12)
            except asyncio.TimeoutError:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} 等待 recaptcha enterprise/clr 200 超时"
                )
                return None

            # 即使 reload/clr 都已返回 200，也额外等待几秒，确保 enterprise 请求链路完全稳定。
            post_wait_seconds = float(getattr(config, "browser_recaptcha_settle_seconds", 3) or 3)
            if post_wait_seconds > 0:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} reload/clr 已就绪，额外等待 {post_wait_seconds:.1f}s 后返回 token"
                )
                await asyncio.sleep(post_wait_seconds)

            return token
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)}"
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} 打码失败: {msg[:200]}")
            return None
        finally:
            if page:
                try:
                    await page.close()
                except:
                    pass

    async def _execute_custom_captcha(
        self,
        context,
        website_url: str,
        website_key: str,
        action: str,
        verify_url: Optional[str] = None,
        enterprise: bool = False,
    ) -> Any:
        """在任意站点执行 reCAPTCHA，用于分数测试等非 Flow 场景。"""
        page = None
        try:
            page = await context.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

            primary_host = "https://www.recaptcha.net" if self._browser_proxy_active else "https://www.google.com"
            secondary_host = "https://www.google.com" if primary_host == "https://www.recaptcha.net" else "https://www.recaptcha.net"
            script_path = "recaptcha/enterprise.js" if enterprise else "recaptcha/api.js"
            execute_target = "grecaptcha.enterprise.execute" if enterprise else "grecaptcha.execute"
            ready_target = "grecaptcha.enterprise.ready" if enterprise else "grecaptcha.ready"
            wait_expression = (
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && "
                "typeof grecaptcha.enterprise.execute === 'function'"
            ) if enterprise else (
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'"
            )
            api_label = "enterprise.js" if enterprise else "api.js"

            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} 加载真实自定义页面 {api_label}: primary={primary_host}, secondary={secondary_host}, url={website_url}"
            )

            def handle_request_failed(request):
                try:
                    failed_url = request.url or ""
                    if not any(d in failed_url for d in ["google.com", "gstatic.com", "recaptcha.net", "antcpt.com"]):
                        return
                    failure = request.failure or ""
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} 自定义资源加载失败: url={failed_url[:200]}, error={failure}"
                    )
                except Exception:
                    pass

            page.on("requestfailed", handle_request_failed)

            try:
                await page.goto(website_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} 自定义 page.goto 失败: {type(e).__name__}: {str(e)[:200]}"
                )
                return None

            page_loaded = False
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

            # 模拟更自然的前台交互，避免冷启动空白上下文直接 execute。
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

            warmup_seconds = float(getattr(config, "browser_score_test_warmup_seconds", 12) or 12)
            if warmup_seconds > 0:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} 真实页面预热 {warmup_seconds:.1f}s 后再执行自定义打码"
                )
                await asyncio.sleep(warmup_seconds)

            try:
                await page.wait_for_function(wait_expression, timeout=15000)
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} 自定义 grecaptcha 未就绪，尝试补注入脚本: {type(e).__name__}: {str(e)[:200]}"
                )
                try:
                    await page.evaluate(f"""
                        (primaryUrl, secondaryUrl) => {{
                            const existing = Array.from(document.scripts || []).some((script) => {{
                                const src = script?.src || "";
                                return src.includes('/recaptcha/');
                            }});
                            if (existing) return;
                            const urls = [primaryUrl, secondaryUrl];
                            const loadScript = (index) => {{
                                if (index >= urls.length) return;
                                const script = document.createElement('script');
                                script.src = urls[index];
                                script.async = true;
                                script.onerror = () => loadScript(index + 1);
                                document.head.appendChild(script);
                            }};
                            loadScript(0);
                        }}
                    """, f"{primary_host}/{script_path}?render={website_key}", f"{secondary_host}/{script_path}?render={website_key}")
                    await page.wait_for_function(wait_expression, timeout=15000)
                except Exception as inject_error:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} 自定义 grecaptcha 最终未就绪: {type(inject_error).__name__}: {str(inject_error)[:200]}"
                    )
                    return None

            await self._capture_page_fingerprint(page)

            token = await asyncio.wait_for(
                page.evaluate(
                    f"""
                        (actionName) => {{
                            return new Promise((resolve, reject) => {{
                                const timeout = setTimeout(() => reject(new Error('timeout')), 25000);
                                try {{
                                    {ready_target}(function() {{
                                        {execute_target}('{website_key}', {{action: actionName}})
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
                timeout=30,
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
            return None
        finally:
            if page:
                try:
                    await page.close()
                except:
                    pass

    def get_last_fingerprint(self) -> Optional[Dict[str, Any]]:
        """返回最近一次打码浏览器的指纹快照。"""
        if not self._last_fingerprint:
            return None
        return dict(self._last_fingerprint)
    
    async def get_token(
        self,
        project_id: str,
        website_key: str,
        action: str = "IMAGE_GENERATION",
        token_proxy_url: Optional[str] = None
    ) -> Optional[str]:
        """获取 Token：启动新浏览器 -> 打码 -> 关闭浏览器"""
        async with self._semaphore:
            MAX_RETRIES = 3
            
            for attempt in range(MAX_RETRIES):
                playwright = None
                browser = None
                context = None
                try:
                    start_ts = time.time()
                    
                    # 每次都启动新浏览器（新 UA）
                    playwright, browser, context = await self._create_browser(token_proxy_url=token_proxy_url)
                    
                    # 执行打码
                    token = await self._execute_captcha(context, project_id, website_key, action)
                    
                    if token:
                        self._solve_count += 1
                        debug_logger.log_info(f"[BrowserCaptcha] Token-{self.token_id} 获取成功 ({(time.time()-start_ts)*1000:.0f}ms)")
                        # 不立即关闭浏览器：等待图片/视频请求结束后再关闭
                        await self._defer_browser_close_until_request_done(
                            playwright=playwright,
                            browser=browser,
                            context=context,
                            action=action,
                        )
                        playwright = None
                        browser = None
                        context = None
                        return token
                    
                    self._error_count += 1
                    debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} 尝试 {attempt+1}/{MAX_RETRIES} 失败")
                    
                except Exception as e:
                    self._error_count += 1
                    debug_logger.log_error(f"[BrowserCaptcha] Token-{self.token_id} 浏览器错误: {type(e).__name__}: {str(e)[:200]}")
                finally:
                    # 无论成功失败都关闭浏览器
                    await self._close_browser(playwright, browser, context)
                
                # 重试前等待
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(1)
            
            return None

    async def get_custom_token(
        self,
        website_url: str,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Optional[str]:
        """获取任意站点的 reCAPTCHA token，成功后立即关闭浏览器。"""
        async with self._semaphore:
            max_retries = 3

            for attempt in range(max_retries):
                playwright = None
                browser = None
                context = None
                try:
                    start_ts = time.time()
                    playwright, browser, context = await self._create_browser()
                    token = await self._execute_custom_captcha(
                        context=context,
                        website_url=website_url,
                        website_key=website_key,
                        action=action,
                        enterprise=enterprise,
                    )

                    if token:
                        self._solve_count += 1
                        debug_logger.log_info(
                            f"[BrowserCaptcha] Token-{self.token_id} 自定义 token 获取成功 ({(time.time()-start_ts)*1000:.0f}ms)"
                        )
                        return token

                    self._error_count += 1
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} 自定义打码尝试 {attempt+1}/{max_retries} 失败"
                    )
                except Exception as e:
                    self._error_count += 1
                    debug_logger.log_error(
                        f"[BrowserCaptcha] Token-{self.token_id} 自定义浏览器错误: {type(e).__name__}: {str(e)[:200]}"
                    )
                finally:
                    await self._close_browser(playwright, browser, context)

                if attempt < max_retries - 1:
                    await asyncio.sleep(1)

            return None

    async def get_custom_score(
        self,
        website_url: str,
        website_key: str,
        verify_url: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Dict[str, Any]:
        """在同一个浏览器页面里获取 token 并直接校验分数。"""
        async with self._semaphore:
            max_retries = 3

            for attempt in range(max_retries):
                playwright = None
                browser = None
                context = None
                try:
                    started_at = time.time()
                    playwright, browser, context = await self._create_browser()
                    payload = await self._execute_custom_captcha(
                        context=context,
                        website_url=website_url,
                        website_key=website_key,
                        action=action,
                        verify_url=verify_url,
                        enterprise=enterprise,
                    )

                    if isinstance(payload, dict) and payload.get("token"):
                        self._solve_count += 1
                        payload.setdefault("token_elapsed_ms", int((time.time() - started_at) * 1000))
                        debug_logger.log_info(
                            f"[BrowserCaptcha] Token-{self.token_id} 页面内分数校验成功 ({(time.time()-started_at)*1000:.0f}ms)"
                        )
                        return payload

                    self._error_count += 1
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} 页面内分数校验尝试 {attempt+1}/{max_retries} 失败"
                    )
                except Exception as e:
                    self._error_count += 1
                    debug_logger.log_error(
                        f"[BrowserCaptcha] Token-{self.token_id} 页面内分数校验异常: {type(e).__name__}: {str(e)[:200]}"
                    )
                finally:
                    await self._close_browser(playwright, browser, context)

                if attempt < max_retries - 1:
                    await asyncio.sleep(1)

            return {
                "token": None,
                "verify_mode": "browser_page",
                "verify_elapsed_ms": 0,
                "verify_http_status": None,
                "verify_result": {}
            }
    

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
        
        # 浏览器数量配置
        self._browser_count = 1  # 默认 1 个，会从数据库加载
        self._round_robin_index = 0  # 轮询索引
        
        # 统计指标
        self._stats = {
            "req_total": 0,
            "gen_ok": 0,
            "gen_fail": 0,
            "api_403": 0
        }
        
        # 并发限制将在 _load_browser_count 中根据配置设置
        self._token_semaphore = None
    
    @classmethod
    async def get_instance(cls, db=None) -> 'BrowserCaptchaService':
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
                    # 从数据库加载 browser_count 配置
                    await cls._instance._load_browser_count()
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
        """重新加载浏览器数量配置（用于配置更新后热重载）"""
        old_count = self._browser_count
        await self._load_browser_count()
        
        # 如果数量减少，移除多余的浏览器实例
        if self._browser_count < old_count:
            async with self._browsers_lock:
                for browser_id in list(self._browsers.keys()):
                    if browser_id >= self._browser_count:
                        self._browsers.pop(browser_id)
                        debug_logger.log_info(f"[BrowserCaptcha] 移除多余浏览器实例 {browser_id}")
    
    def _log_stats(self):
        total = self._stats["req_total"]
        gen_fail = self._stats["gen_fail"]
        api_403 = self._stats["api_403"]
        gen_ok = self._stats["gen_ok"]
        
        valid_success = gen_ok - api_403
        if valid_success < 0: valid_success = 0
        
        rate = (valid_success / total * 100) if total > 0 else 0.0

    
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

    async def _resolve_token_proxy_url(self, token_id: Optional[int]) -> Optional[str]:
        """读取 token 级打码代理，为空时回退全局配置。"""
        if not token_id or not self.db:
            return None
        try:
            token = await self.db.get_token(token_id)
            if token and token.captcha_proxy_url and token.captcha_proxy_url.strip():
                return token.captcha_proxy_url.strip()
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 读取 token({token_id}) 打码代理失败: {e}")
        return None
    
    async def get_token(self, project_id: str, action: str = "IMAGE_GENERATION", token_id: int = None) -> tuple[Optional[str], int]:
        """获取 reCAPTCHA Token（轮询分配到不同浏览器）
        
        Args:
            project_id: 项目 ID
            action: reCAPTCHA action
            token_id: 业务 token id（仅用于读取 token 级打码代理）
        
        Returns:
            (token, browser_id) 元组，调用方失败时用 browser_id 调用 report_error
        """
        # 检查服务是否可用
        self._check_available()
        
        self._stats["req_total"] += 1
        token_proxy_url = await self._resolve_token_proxy_url(token_id)
        
        # 全局并发限制（如果已配置）
        if self._token_semaphore:
            async with self._token_semaphore:
                # 轮询选择浏览器
                browser_id = self._get_next_browser_id()
                browser = await self._get_or_create_browser(browser_id)
                
                token = await browser.get_token(
                    project_id,
                    self.website_key,
                    action,
                    token_proxy_url=token_proxy_url
                )
            
            if token:
                self._stats["gen_ok"] += 1
            else:
                self._stats["gen_fail"] += 1
                
            self._log_stats()
            return token, browser_id
        
        # 无并发限制时直接执行
        browser_id = self._get_next_browser_id()
        browser = await self._get_or_create_browser(browser_id)
        
        token = await browser.get_token(
            project_id,
            self.website_key,
            action,
            token_proxy_url=token_proxy_url
        )
        
        if token:
            self._stats["gen_ok"] += 1
        else:
            self._stats["gen_fail"] += 1
            
        self._log_stats()
        return token, browser_id

    async def get_custom_token(
        self,
        website_url: str,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> tuple[Optional[str], int]:
        """获取任意站点的 reCAPTCHA token，用于分数测试。"""
        self._check_available()

        if self._token_semaphore:
            async with self._token_semaphore:
                browser_id = self._get_next_browser_id()
                browser = await self._get_or_create_browser(browser_id)
                token = await browser.get_custom_token(
                    website_url=website_url,
                    website_key=website_key,
                    action=action,
                    enterprise=enterprise,
                )
            return token, browser_id

        browser_id = self._get_next_browser_id()
        browser = await self._get_or_create_browser(browser_id)
        token = await browser.get_custom_token(
            website_url=website_url,
            website_key=website_key,
            action=action,
            enterprise=enterprise,
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

        if self._token_semaphore:
            async with self._token_semaphore:
                browser_id = self._get_next_browser_id()
                browser = await self._get_or_create_browser(browser_id)
                payload = await browser.get_custom_score(
                    website_url=website_url,
                    website_key=website_key,
                    verify_url=verify_url,
                    action=action,
                    enterprise=enterprise,
                )
            return payload, browser_id

        browser_id = self._get_next_browser_id()
        browser = await self._get_or_create_browser(browser_id)
        payload = await browser.get_custom_score(
            website_url=website_url,
            website_key=website_key,
            verify_url=verify_url,
            action=action,
            enterprise=enterprise,
        )
        return payload, browser_id

    async def get_fingerprint(self, browser_id: int) -> Optional[Dict[str, Any]]:
        """获取指定浏览器最近一次打码时的指纹快照。"""
        async with self._browsers_lock:
            browser = self._browsers.get(browser_id)
            if not browser:
                return None
            return browser.get_last_fingerprint()

    async def report_error(self, browser_id: int = None, error_reason: Optional[str] = None):
        """上层举报当前请求失败，必要时提前回收待释放浏览器。
        
        Args:
            browser_id: 浏览器 ID（当前架构下每次都是新浏览器，此参数仅用于日志）
        """
        async with self._browsers_lock:
            browser = self._browsers.get(browser_id) if browser_id is not None else None
            error_lower = (error_reason or "").lower()
            if "403" in error_lower or "recaptcha" in error_lower:
                self._stats["api_403"] += 1
            if browser_id is not None:
                debug_logger.log_info(
                    f"[BrowserCaptcha] 浏览器 {browser_id} 的 token 验证失败，reason={error_reason or 'unknown'}"
                )

        if browser:
            try:
                await browser.force_close_pending_browser()
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 浏览器 {browser_id} 失败后提前关闭异常: {e}")

    async def report_request_finished(self, browser_id: int = None):
        """上层通知：图片/视频请求已完成，可关闭对应打码浏览器。"""
        if browser_id is None:
            return

        async with self._browsers_lock:
            browser = self._browsers.get(browser_id)

        if browser:
            await browser.notify_generation_request_finished()

    async def remove_browser(self, browser_id: int):
        async with self._browsers_lock:
            if browser_id in self._browsers:
                self._browsers.pop(browser_id)

    async def close(self):
        async with self._browsers_lock:
            browsers = list(self._browsers.values())
            self._browsers.clear()

        for browser in browsers:
            try:
                await browser.force_close_pending_browser()
            except Exception:
                pass
            
    async def open_login_browser(self): return {"success": False, "error": "Not implemented"}
    async def create_browser_for_token(self, t, s=None): pass
    def get_stats(self): 
        base_stats = {
            "total_solve_count": self._stats["gen_ok"],
            "total_error_count": self._stats["gen_fail"],
            "risk_403_count": self._stats["api_403"],
            "browser_count": len(self._browsers),
            "configured_browser_count": self._browser_count,
            "browsers": []
        }
        return base_stats

