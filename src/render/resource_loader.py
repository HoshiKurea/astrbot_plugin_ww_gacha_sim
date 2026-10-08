"""
网络资源访问模块
针对国内网络环境优化资源访问
"""

import ipaddress
import socket
import threading
import time
from contextlib import nullcontext
from urllib.parse import quote, urlparse, urlsplit

import httpx

from astrbot.api import logger


class ResourceLoader:
    """网络资源访问优化器"""

    def __init__(self, reuse_connections=False):
        """
        初始化资源加载器
        """
        # 请求头
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        self.reuse_connections = reuse_connections
        self._clients = {}
        self._client_lock = threading.Lock()
        self._preferred_host = None
        self._closed = False

    def close(self):
        with self._client_lock:
            self._closed = True
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()

    def _pooled_client(self, client_kwargs):
        key = client_kwargs.get("proxy")
        with self._client_lock:
            if self._closed:
                raise RuntimeError("资源下载器已关闭")
            if key not in self._clients:
                self._clients[key] = httpx.Client(
                    **client_kwargs,
                    limits=httpx.Limits(max_connections=6, max_keepalive_connections=6,
                                        keepalive_expiry=30),
                )
            return self._clients[key]

    # Public CDNs expose this repository without requiring a local proxy.
    # Keep the fallback limited to the bundled artwork repository.
    _MIRROR_REPOSITORY = "TomyJan/WutheringWaves-UIResources"
    _MIRROR_HOSTS = {"cdn.jsdelivr.net", "fastly.jsdelivr.net"}
    _LEGACY_PROXY_HOSTS = {"gh-proxy.com", "v6.gh-proxy.org"}

    @classmethod
    def _canonical_url(cls, url: str) -> str:
        """Unwrap legacy proxy URLs for this fixed artwork repository."""
        parsed = urlsplit(url)
        if parsed.scheme == "https" and parsed.netloc in cls._LEGACY_PROXY_HOSTS:
            original = parsed.path.lstrip("/")
            prefix = f"https://raw.githubusercontent.com/{cls._MIRROR_REPOSITORY}/"
            if original.startswith(prefix):
                return original
        return url

    @classmethod
    def _mirror_urls(cls, url: str) -> tuple[str, ...]:
        parsed = urlsplit(url)
        prefix = f"/{cls._MIRROR_REPOSITORY}/"
        if parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com":
            return ()
        if not parsed.path.startswith(prefix):
            return ()
        parts = parsed.path[len(prefix):].split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return ()
        ref, path = parts
        encoded_ref = quote(ref, safe="")
        encoded_path = quote(path, safe="/")
        return tuple(
            f"https://{host}/gh/{cls._MIRROR_REPOSITORY}@{encoded_ref}/{encoded_path}"
            for host in ("cdn.jsdelivr.net", "fastly.jsdelivr.net")
        )

    @classmethod
    def _download_candidates(cls, url: str) -> tuple[str, ...]:
        """Return the origin followed by the allowlisted public mirrors."""
        url = cls._canonical_url(url)
        return (url, *cls._mirror_urls(url))

    def download_with_retry(
        self,
        url: str,
        max_retries: int = 3,
        timeout: int = 15,
        proxy: dict[str, str] | None = None,
        max_bytes: int = 10 * 1024 * 1024,
        stop_event=None,
        total_budget_seconds: float | None = None,
    ) -> bytes | None:
        """
        简单的资源下载功能，支持代理配置

        Args:
            url: 资源URL
            max_retries: 最大重试次数
            timeout: 超时时间（秒）
            proxy: 代理配置字典（可选）

        Returns:
            下载的内容，失败返回None
        """
        if self._closed:
            return None
        deadline = (time.monotonic() + total_budget_seconds
                    if total_budget_seconds is not None else None)

        def stopped():
            return ((stop_event is not None and stop_event.is_set())
                    or (deadline is not None and time.monotonic() >= deadline))

        url = self._canonical_url(url)
        if not self.is_valid_resource_url(url):
            logger.warning(f"❌ 无效的URL: {url}")
            return None

        candidates = self._download_candidates(url)
        if len(candidates) > 1 and self._preferred_host:
            candidates = tuple(sorted(candidates, key=lambda candidate:
                                      urlsplit(candidate).netloc != self._preferred_host))
        for candidate_index, candidate in enumerate(candidates):
            if stopped():
                return None
            if not self.is_valid_resource_url(candidate):
                continue
            if candidate_index:
                logger.info("Trying alternate artwork source: %s", candidate)
            # A blocked GitHub connection should hand over to the CDN promptly;
            # non-mirrored URLs retain the caller's normal retry count.
            candidate_retries = 1 if candidate_index == 0 and len(candidates) > 1 else max_retries
            for attempt in range(candidate_retries):
                if stopped():
                    return None
                try:
                    logger.info(f"Download attempt {attempt + 1}/{candidate_retries}: {candidate}")

                    request_timeout = min(timeout, max(0.1, deadline - time.monotonic())) if deadline else timeout
                    client_kwargs = {
                        "timeout": httpx.Timeout(timeout=request_timeout, connect=min(4, request_timeout)),
                        "verify": True,
                        "http2": False,  # 禁用 HTTP/2 避免部分代理兼容性问题
                        "follow_redirects": False,
                    }
                    if proxy:
                        # 适配新版 httpx，使用 proxy 参数而不是 proxies
                        proxy_url = None
                        if isinstance(proxy, dict):
                            proxy_url = (
                                proxy.get("all://")
                                or proxy.get("http://")
                                or proxy.get("https://")
                            )
                            if not proxy_url and len(proxy) > 0:
                                proxy_url = next(iter(proxy.values()))

                        if proxy_url:
                            client_kwargs["proxy"] = proxy_url

                    client_context = (nullcontext(self._pooled_client(client_kwargs))
                                      if self.reuse_connections else httpx.Client(**client_kwargs))
                    with client_context as client:
                        with client.stream("GET", candidate, headers=self.headers,
                                           timeout=client_kwargs["timeout"]) as response:
                            if response.status_code == 200:
                                content = bytearray()
                                for chunk in response.iter_bytes():
                                    if stopped():
                                        return None
                                    content.extend(chunk)
                                    if len(content) > max_bytes:
                                        logger.warning("下载资源超过 %s 字节: %s", max_bytes, candidate)
                                        return None
                                logger.info(f"✅ 下载成功: {candidate}")
                                if len(candidates) > 1:
                                    self._preferred_host = urlsplit(candidate).netloc
                                return bytes(content)
                            logger.warning(
                                f"❌ 下载失败，状态码: {response.status_code}, URL: {candidate}"
                            )
                except httpx.TimeoutException:
                    logger.warning(f"❌ 下载超时，尝试 {attempt + 1}/{candidate_retries}: {candidate}")
                except httpx.RequestError as e:
                    logger.error(
                        f"❌ 下载请求错误，尝试 {attempt + 1}/{candidate_retries}: {candidate}, Error: {e}"
                    )
                except Exception as e:
                    logger.error(f"❌ 下载尝试 {attempt + 1} 失败: {e}, URL: {candidate}")

                if attempt < candidate_retries - 1:
                    wait_time = 2**attempt  # 指数退避
                    logger.info(f"⏱️  等待 {wait_time} 秒后重试...")
                    if stop_event is not None:
                        if stop_event.wait(wait_time):
                            return None
                    else:
                        time.sleep(wait_time)

        logger.error(f"❌ 所有下载尝试都失败: {url}")
        return None

    def is_valid_resource_url(self, url: str) -> bool:
        """检查资源URL是否有效 (仅支持 http 和 https)"""
        try:
            parsed = urlparse(url)
            is_valid = all([parsed.scheme, parsed.netloc]) and parsed.scheme in (
                "http",
                "https",
            )
            if is_valid:
                if parsed.username or parsed.password or not parsed.hostname:
                    return False
                # This fixed artwork repository is also used by the versioned
                # admin gallery. TLS host verification and disabled redirects
                # keep the origin fixed when a local proxy uses fake-IP DNS.
                if (parsed.scheme == "https"
                        and parsed.netloc == "raw.githubusercontent.com"
                        and parsed.path.startswith("/TomyJan/WutheringWaves-UIResources/")):
                    return True
                if (parsed.scheme == "https"
                        and parsed.netloc in self._MIRROR_HOSTS
                        and parsed.path.startswith("/gh/TomyJan/WutheringWaves-UIResources@")):
                    return True
                addresses = socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
                is_valid = bool(addresses) and all(
                    ipaddress.ip_address(address[4][0]).is_global
                    for address in addresses
                )
            if not is_valid:
                logger.warning(f"Invalid URL format or scheme: {url}")
            return is_valid
        except (ValueError, OSError) as e:
            logger.error(f"Error parsing URL: {url}, Error: {e}")
            return False
