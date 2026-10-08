"""Shared full-size artwork for previews, draws, imports and offline packages."""

import base64
import hashlib
import io
import os
import threading
import uuid
from concurrent.futures import Future
from urllib.parse import urlsplit

from PIL import Image

from .image_encoding import encode_portrait
from .resource_loader import ResourceLoader
from ..web.path_security import managed_path


class PortraitStore:
    def __init__(self, root, cache, loader, proxy=None):
        self.root, self.cache, self.loader, self.proxy = root, cache, loader, proxy
        self.lock = threading.Lock()
        self.inflight = {}
        self.stop = threading.Event()

    def source(self, source):
        if not isinstance(source, str) or not source or len(source) > 2048:
            raise ValueError("立绘地址无效")
        source = ResourceLoader._canonical_url(source)
        if source.startswith("local:"):
            managed_path(self.root, source[6:])
        else:
            parsed = urlsplit(source)
            if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                    or parsed.username or parsed.password or any(ord(c) < 32 for c in source)):
                raise ValueError("立绘地址无效")
        return source

    def digest(self, source):
        return hashlib.sha256(self.source(source).encode()).hexdigest()

    def key(self, source):
        return "portrait_" + self.digest(source)

    def pinned_path(self, source):
        return managed_path(self.root, "offline/" + self.digest(source) + ".webp")

    def is_pinned(self, source):
        return self.pinned_path(source).is_file()

    def cached_bytes(self, source):
        source = self.source(source)
        pinned = self.pinned_path(source)
        if pinned.is_file():
            return pinned.read_bytes()
        if source.startswith("local:"):
            path = managed_path(self.root, source[6:])
            if path.is_file() and path.stat().st_size <= 10 * 1024 * 1024:
                return path.read_bytes()
            return None
        # Hold the cache lock until read, so cleanup cannot unlink this file.
        with self.cache._meta_lock:
            path = self.cache.get_cached_file_path(self.key(source))
            return path.read_bytes() if path else None

    def has(self, source):
        source = self.source(source)
        if self.is_pinned(source):
            return True
        if source.startswith("local:"):
            return managed_path(self.root, source[6:]).is_file()
        return self.cache.get_cached_file_path(self.key(source)) is not None

    def ensure(self, source):
        source = self.source(source)
        cached = self.cached_bytes(source)
        if cached is not None:
            return cached
        if self.stop.is_set():
            raise ValueError("资源服务正在关闭")
        if source.startswith("local:"):
            raise ValueError("本地立绘不存在，请上传立绘或导入对应离线资源包")
        with self.lock:
            future = self.inflight.get(source)
            owner = future is None
            if owner:
                if len(self.inflight) >= 32:
                    raise ValueError("资源下载繁忙，请稍后重试")
                future = Future()
                self.inflight[source] = future
        if not owner:
            return future.result()
        try:
            raw = self.loader.download_with_retry(source, proxy=self.proxy, max_retries=1,
                                                 timeout=8, total_budget_seconds=30,
                                                 stop_event=self.stop)
            if not raw:
                raise ValueError("立绘下载失败，请检查网络或导入离线资源包")
            compact = encode_portrait(raw)
            self.cache.cache_file(compact, self.key(source), expire_time=30 * 86400)
            future.set_result(compact)
            return compact
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.lock:
                self.inflight.pop(source, None)

    def publish(self, source, compact):
        target = self.pinned_path(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name("." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(compact)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def pin(self, source):
        if self.is_pinned(source):
            return self.pinned_path(source)
        raw = self.ensure(source)
        # Avoid re-encoding an already compact shared WebP a second time.
        with Image.open(io.BytesIO(raw)) as image:
            compact = raw if image.format == "WEBP" and max(image.size) <= 1280 else encode_portrait(raw)
        return self.publish(source, compact)

    def preview(self, source):
        raw = self.ensure(source)
        key = "preview_" + hashlib.sha256(raw).hexdigest()
        with self.cache._meta_lock:
            cached = self.cache.get_cached_file_path(key)
            encoded = cached.read_bytes() if cached else None
        if encoded is None:
            with Image.open(io.BytesIO(raw)) as original:
                if original.width * original.height > 20_000_000:
                    raise ValueError("立绘尺寸过大")
                picture = original.convert("RGBA")
                bounds = picture.getbbox()
                if bounds:
                    picture = picture.crop(bounds)
                picture.thumbnail((360, 360))
                output = io.BytesIO()
                picture.save(output, format="WEBP", quality=82)
                encoded = output.getvalue()
            self.cache.cache_file(encoded, key, expire_time=30 * 86400)
        return {"data_url": "data:image/webp;base64," + base64.b64encode(encoded).decode("ascii")}

    def close(self):
        self.stop.set()
