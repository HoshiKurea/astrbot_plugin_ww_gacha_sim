import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from PIL import Image

from astrbot.api import logger
from astrbot.api.star import StarTools


class LocalFileCacheManager:
    """本地文件缓存管理器"""

    def __init__(self, cache_dir: Path | None = None, cleanup_interval: int = 24,
                 max_bytes: int = 512 * 1024 * 1024):
        """
        初始化缓存管理器

        Args:
            cache_dir: 缓存目录路径
            cleanup_interval: 缓存清理周期（单位：小时），默认24小时
        """
        if cache_dir is None:
            self.cache_dir = (
                Path(StarTools.get_data_dir("astrbot_plugin_ww_gacha_sim")) / "cache"
            )
        else:
            self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if max_bytes < 1:
            raise ValueError("缓存容量必须大于零")
        self.max_bytes = max_bytes
        self.meta_file = self.cache_dir / "cache_meta.json"
        self.cleanup_interval = cleanup_interval * 3600  # 转换为秒
        self.last_cleanup_time = 0
        self._cleanup_timer = None
        self._cleanup_lock = threading.Lock()
        self._meta_lock = threading.RLock()
        self._load_cache_meta()

    def _load_cache_meta(self):
        """加载缓存元数据"""
        if self.meta_file.exists():
            try:
                with open(self.meta_file, encoding="utf-8") as f:
                    self.cache_meta = json.load(f)
            except Exception:
                self.cache_meta = {}
        else:
            self.cache_meta = {}

        if not isinstance(self.cache_meta, dict):
            self.cache_meta = {}
        self.cache_meta = {
            key: value for key, value in self.cache_meta.items()
            if isinstance(key, str) and self._valid_key(key)
            and isinstance(value, dict)
            and isinstance(value.get("expires_at"), (int, float))
            and isinstance(value.get("created_at"), (int, float))
        }
        self.clear_expired_cache()
        self._start_scheduled_cleanup()

    @staticmethod
    def _valid_key(key: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", key)) and key not in (".", "..")

    @classmethod
    def _check_key(cls, key: str) -> None:
        if not cls._valid_key(key):
            raise ValueError("无效的缓存键")

    def _save_cache_meta(self):
        """保存缓存元数据"""
        with self._meta_lock:
            temporary = self.cache_dir / f".{uuid.uuid4().hex}.tmp"
            try:
                with open(temporary, "w", encoding="utf-8") as f:
                    json.dump(self.cache_meta, f, ensure_ascii=False, indent=2)
                os.replace(temporary, self.meta_file)
            finally:
                temporary.unlink(missing_ok=True)

    def _enforce_capacity(self):
        total = self.get_cache_size()
        for key, _ in sorted(self.cache_meta.items(),
                             key=lambda entry: entry[1].get("created_at", 0)):
            if total <= self.max_bytes:
                break
            path = self.cache_dir / f"{key}.cache"
            size = path.stat().st_size if path.exists() else 0
            self._remove_cache(key)
            total -= size

    def _generate_cache_key(self, content: str | bytes | Path) -> str:
        """
        生成缓存键

        Args:
            content: 内容（字符串、字节或路径）

        Returns:
            缓存键（哈希值）
        """
        if isinstance(content, Path):
            # 如果是路径，使用路径字符串和修改时间
            content_str = (
                f"{str(content)}_{content.stat().st_mtime if content.exists() else 0}"
            )
        elif isinstance(content, bytes):
            content_str = content.hex()
        else:
            content_str = str(content)

        return hashlib.md5(content_str.encode("utf-8")).hexdigest()

    def get_cached_file_path(self, key: str) -> Path | None:
        """
        获取缓存文件路径

        Args:
            key: 缓存键

        Returns:
            缓存文件路径（如果存在）
        """
        self._check_key(key)
        with self._meta_lock:
            cache_file = self.cache_dir / f"{key}.cache"
            if cache_file.exists():
                if self._is_cache_expired(key):
                    self._remove_cache(key)
                    return None
                return cache_file
            return None

    def _is_cache_expired(self, key: str) -> bool:
        """
        检查缓存是否过期

        Args:
            key: 缓存键

        Returns:
            是否过期
        """
        if key not in self.cache_meta:
            return True

        cache_info = self.cache_meta[key]
        if "expires_at" in cache_info:
            return time.time() > cache_info["expires_at"]
        return False

    def _remove_cache(self, key: str):
        """删除缓存"""
        with self._meta_lock:
            cache_file = self.cache_dir / f"{key}.cache"
            cache_file.unlink(missing_ok=True)
            if key in self.cache_meta:
                del self.cache_meta[key]
                self._save_cache_meta()

    def cache_file(
        self, content: str | bytes, key: str = None, expire_time: int = 3600
    ) -> Path:
        """
        缓存文件内容

        Args:
            content: 要缓存的内容
            key: 缓存键（如果未提供则自动生成）
            expire_time: 过期时间（秒）

        Returns:
            缓存文件路径
        """
        if key is None:
            key = self._generate_cache_key(content)
        self._check_key(key)

        content = content.encode("utf-8") if isinstance(content, str) else content
        if len(content) > self.max_bytes:
            raise ValueError("缓存文件超过容量上限")
        with self._meta_lock:
            cache_file = self.cache_dir / f"{key}.cache"
            temporary = self.cache_dir / f".{uuid.uuid4().hex}.tmp"
            try:
                temporary.write_bytes(content)
                os.replace(temporary, cache_file)
            finally:
                temporary.unlink(missing_ok=True)
            now = time.time()
            self.cache_meta[key] = {
                "created_at": now, "expires_at": now + expire_time,
                "size": len(content),
            }
            self._save_cache_meta()
            self._enforce_capacity()
            return cache_file

    def cache_image(
        self, image: Image.Image, key: str = None, expire_time: int = 3600
    ) -> Path:
        """
        缓存图片

        Args:
            image: PIL图片对象
            key: 缓存键
            expire_time: 过期时间（秒）

        Returns:
            缓存文件路径
        """
        if key is None:
            # 使用图片的哈希值作为键
            image_bytes = self._image_to_bytes(image)
            key = self._generate_cache_key(image_bytes)
        self._check_key(key)

        with self._meta_lock:
            cache_file = self.cache_dir / f"{key}.cache"
            temporary = self.cache_dir / f".{uuid.uuid4().hex}.tmp"
            try:
                image.save(temporary, format="PNG")
                size = temporary.stat().st_size
                if size > self.max_bytes:
                    raise ValueError("缓存图片超过容量上限")
                os.replace(temporary, cache_file)
            finally:
                temporary.unlink(missing_ok=True)
            now = time.time()
            self.cache_meta[key] = {
                "created_at": now, "expires_at": now + expire_time,
                "size": size, "type": "image",
            }
            self._save_cache_meta()
            self._enforce_capacity()
            return cache_file

    def _image_to_bytes(self, image: Image.Image) -> bytes:
        """将PIL图片对象转换为字节"""
        import io

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def get_cached_image(self, key: str) -> Image.Image | None:
        """
        获取缓存的图片

        Args:
            key: 缓存键

        Returns:
            PIL图片对象（如果存在且未过期）
        """
        with self._meta_lock:
            cache_file = self.get_cached_file_path(key)
            if cache_file:
                try:
                    with Image.open(cache_file) as image:
                        return image.copy()
                except Exception:
                    self._remove_cache(key)
            return None

    def clear_expired_cache(self):
        """清理过期缓存"""
        with self._meta_lock:
            current_time = time.time()
            expired_keys = [key for key, meta in self.cache_meta.items()
                            if current_time > meta.get("expires_at", float("inf"))]
            for key in expired_keys:
                self._remove_cache(key)
            for path in self.cache_dir.glob("*.cache"):
                if path.stem not in self.cache_meta:
                    path.unlink(missing_ok=True)
            self._enforce_capacity()
            self.last_cleanup_time = current_time
            if expired_keys:
                logger.info(f"清理了 {len(expired_keys)} 个过期缓存项")

    def clear_all_cache(self):
        """清理所有缓存"""

        with self._meta_lock:
            for cache_file in self.cache_dir.glob("*.cache"):
                cache_file.unlink(missing_ok=True)
            self.cache_meta = {}
            self._save_cache_meta()

    def get_cache_size(self) -> int:
        """获取缓存总大小"""
        with self._meta_lock:
            return sum(path.stat().st_size for path in self.cache_dir.glob("*.cache"))

    def _start_scheduled_cleanup(self):
        """启动定时清理任务"""
        if self._cleanup_timer is not None:
            return

        def _cleanup_task():
            """定时清理任务"""
            try:
                self.clear_expired_cache()
            except Exception as e:
                logger.error(f"定时清理缓存时发生错误: {e}")
            finally:
                with self._cleanup_lock:
                    if self._cleanup_timer is not None:
                        self._cleanup_timer = threading.Timer(
                            self.cleanup_interval, _cleanup_task
                        )
                        self._cleanup_timer.daemon = True
                        self._cleanup_timer.start()

        with self._cleanup_lock:
            if self._cleanup_timer is None:
                self._cleanup_timer = threading.Timer(
                    self.cleanup_interval, _cleanup_task
                )
                self._cleanup_timer.daemon = True
                self._cleanup_timer.start()
                logger.info(
                    f"已启动定时缓存清理任务，清理周期: {self.cleanup_interval / 3600} 小时"
                )

    def stop_scheduled_cleanup(self):
        """停止定时清理任务"""
        with self._cleanup_lock:
            if self._cleanup_timer is not None:
                self._cleanup_timer.cancel()
                self._cleanup_timer = None
                logger.info("已停止定时缓存清理任务")
