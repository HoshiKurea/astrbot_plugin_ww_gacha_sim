"""
UI资源渲染模块
负责抽卡相关的UI资源（如图像、音效、动画文件）的渲染相关逻辑
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import cast

from PIL import Image
from astrbot.api.star import StarTools

from ..web.path_security import managed_path

from .local_file_cache_manager import LocalFileCacheManager
from .proxy_config import ProxyConfig
from .resource_loader import ResourceLoader
from .image_encoding import encode_portrait

PLUGIN_PATH = Path(__file__).resolve().parent.parent.parent


def safe_json_load(file_path: Path) -> dict:
    """安全的JSON加载工具函数"""
    try:
        with open(file_path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"文件 {file_path} 不存在")
    except json.JSONDecodeError:
        raise json.JSONDecodeError("JSON 格式错误", "", 0)


class UIResourceManager:
    """UI资源管理类"""

    def __init__(
        self,
        resources_loader: ResourceLoader,
        cache_manager: LocalFileCacheManager,
        proxy_config: ProxyConfig,
        resource_dir: Path = PLUGIN_PATH / "src" / "assets",
        portrait_store=None,
    ):
        # 初始化日志记录器
        self.logger = logging.getLogger(self.__class__.__name__)

        self.resource_dir = resource_dir
        if resources_loader is None or cache_manager is None or proxy_config is None:
            raise ValueError("UIResourceManager requires owned resources, cache and proxy")
        self.resources_downloader = resources_loader
        self.cache_manager = cache_manager
        self.proxy_config = proxy_config
        self.portrait_store = portrait_store

        # 加载精灵表配置
        self.sprite_atlas = safe_json_load(self.resource_dir / "gacha_atlas.json")

        # 确保目录存在
        self.resource_dir.mkdir(exist_ok=True)

    def get_sprite_from_atlas(
        self, sprite_name: str, remove_transparent_border: bool = False
    ) -> Image.Image | None:
        """从精灵表中提取指定精灵，确保保留完整的透明通道信息"""
        if not self.sprite_atlas or "frames" not in self.sprite_atlas:
            return None

        if sprite_name not in self.sprite_atlas["frames"]:
            return None

        # 检查缓存
        cache_key = f"sprite_{sprite_name}"
        cached_sprite = self.cache_manager.get_cached_image(cache_key)
        if cached_sprite:
            # 确保返回的图像具有完整的透明通道信息
            if cached_sprite.mode != "RGBA":
                cached_sprite = cached_sprite.convert("RGBA")

            # 如果需要移除透明边界
            if remove_transparent_border:
                cached_sprite = self._remove_transparent_border(cached_sprite)

            return cached_sprite

        # 加载精灵表图像
        atlas_path = self.resource_dir / "gacha_atlas.webp"
        if not atlas_path.exists():
            atlas_path = self.resource_dir / "gacha_atlas.png"
        try:
            atlas_img = Image.open(atlas_path)
            # 确保精灵表图像是RGBA模式以保留透明度信息
            if atlas_img.mode != "RGBA":
                atlas_img = atlas_img.convert("RGBA")
        except FileNotFoundError:
            self.logger.warning(f"精灵表图像 {atlas_path} 不存在")
            return None

        # 获取精灵帧信息
        frame_info = self.sprite_atlas["frames"][sprite_name]
        frame = frame_info["frame"]

        # 从精灵表中裁剪出精灵，保留完整的透明通道信息
        x, y, w, h = frame["x"], frame["y"], frame["w"], frame["h"]
        sprite_img = atlas_img.crop((x, y, x + w, y + h))

        # 确保提取的精灵具有完整的透明通道信息
        if sprite_img.mode != "RGBA":
            sprite_img = sprite_img.convert("RGBA")

        # 如果需要移除透明边界
        if remove_transparent_border:
            sprite_img = self._remove_transparent_border(sprite_img)

        # 缓存提取的精灵
        try:
            self.cache_manager.cache_image(sprite_img, cache_key)
        except Exception as e:
            self.logger.warning(f"Error caching sprite {sprite_name}: {e}")
            # 即使缓存失败，也要返回图像

        return sprite_img

    def _ensure_transparency_consistency(self, img: Image.Image) -> Image.Image:
        """确保图像的透明度一致性，消除可能的棋盘格背景"""
        if img.mode != "RGBA":
            # 如果不是RGBA模式，转换为RGBA模式
            img = img.convert("RGBA")

        # 检查图像中是否有透明像素
        alpha_channel = img.split()[-1]  # 获取透明通道
        alpha_data = alpha_channel.getextrema()  # 获取透明度范围

        # 如果透明度范围显示有透明区域，确保透明区域完全透明
        if isinstance(alpha_data, tuple) and len(alpha_data) >= 2:
            # 使用 cast 告诉类型检查器 alpha_data 是元组
            alpha_tuple = cast(tuple[int, int], alpha_data)
            min_alpha = int(alpha_tuple[0])
            if min_alpha < 255:  # 存在透明像素
                # 确保透明区域完全透明（值为0），避免棋盘格背景
                alpha_channel = alpha_channel.point(lambda x: 0 if x < 128 else x)  # pyright: ignore[reportOperatorIssue]
                r, g, b = img.split()[:3]
                img = Image.merge("RGBA", (r, g, b, alpha_channel))

        return img

    def _remove_transparent_border(self, img: Image.Image) -> Image.Image:
        """移除图像周围的透明边界，返回实际内容区域"""
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        # 获取alpha通道
        alpha = img.split()[-1]

        # 获取非透明区域的边界框
        bbox = alpha.getbbox()

        if bbox:
            # 裁剪到实际内容区域
            return img.crop(bbox)
        else:
            # 如果整个图像都是透明的，返回原始图像
            return img

    def _composite_sprites_with_transparency(
        self,
        base_img: Image.Image,
        overlay_img: Image.Image,
        position: tuple,
        opacity: float = 1.0,
    ) -> Image.Image:
        """使用正确的透明度混合叠加精灵图层"""
        # 确保基础图像和覆盖图像都是RGBA模式
        if base_img.mode != "RGBA":
            base_img = base_img.convert("RGBA")
        if overlay_img.mode != "RGBA":
            overlay_img = overlay_img.convert("RGBA")

        # 如果需要调整透明度
        if opacity < 1.0:
            # 调整覆盖图像的透明度
            r, g, b, a = overlay_img.split()
            # 根据指定的不透明度调整alpha通道
            a = a.point(lambda x: int(x * opacity))
            overlay_img = Image.merge("RGBA", (r, g, b, a))

        # 创建新的基础图像副本用于叠加
        result_img = base_img.copy()

        # 使用正确的透明度混合方法进行叠加
        result_img.paste(overlay_img, position, overlay_img)

        return result_img

    def get_background_for_quality(self, quality: int) -> Image.Image | str:
        """根据品质获取背景路径"""
        # 从精灵表中提取背景精灵
        sprite_name = f"bg_star_{quality}.png"
        sprite_img = self.get_sprite_from_atlas(
            sprite_name, remove_transparent_border=True
        )

        if sprite_img is not None:
            return sprite_img
        else:
            # 如果无法从精灵表提取，返回默认的背景路径
            default_bg_path = f"assets/backgrounds/bg_{quality}star.png"
            return default_bg_path

    def _get_default_resource(self) -> str:
        """获取默认资源路径（占位图）"""
        # 返回默认占位图路径
        default_path = self.resource_dir / "placeholder.png"
        if not default_path.exists():
            # 如果默认占位图不存在，创建一个简单的占位图
            try:
                img = Image.new("RGBA", (200, 200), (200, 200, 200, 100))  # 灰色占位图
                img.save(default_path)
            except ImportError:
                # 如果PIL不可用，返回空字符串
                return ""
        return str(default_path)

    def get_halftone_pattern(self) -> Image.Image | None:
        """获取半调图案"""
        sprite_name = "bandiao.png"
        sprite_img = self.get_sprite_from_atlas(
            sprite_name, remove_transparent_border=False
        )
        if sprite_img:
            return sprite_img

        # 如果精灵表中不存在，则从文件系统加载
        halftone_path = self.resource_dir / "bandiao.png"
        if halftone_path.exists():
            return Image.open(halftone_path).convert("RGBA")
        return None

    def get_icon_path(self, icon_name: str) -> str | None:
        """获取图标路径"""
        # 构建图标路径
        icon_path = self.resource_dir / "icons" / f"T_{icon_name}.png"
        if icon_path.exists():
            return str(icon_path)
        else:
            # 尝试默认图标
            default_icon_path = self.resource_dir / "icons" / "T_Spectro.png"
            if default_icon_path.exists():
                return str(default_icon_path)
        return None

    def get_background_path(self) -> str | None:
        """获取背景路径"""
        bg_path = self.resource_dir / "T_LuckdrawBg.webp"
        if not bg_path.exists():
            bg_path = self.resource_dir / "T_LuckdrawBg.png"
        if bg_path.exists():
            return str(bg_path)
        return None

    def get_item_portrait(self, item) -> Image.Image:
        """
        获取物品立绘图像

        Args:
            item: 物品对象

        Returns:
            Image.Image: 立绘图像对象

        Raises:
            Exception: 当所有资源获取方式都失败时抛出异常
        """
        # Network access belongs to PortraitService, never a render worker.
        portrait_url = item.portrait_url or ""
        if getattr(self, "portrait_store", None) is not None and portrait_url:
            content = self.portrait_store.cached_bytes(portrait_url)
            if content is not None:
                import io
                with Image.open(io.BytesIO(content)) as image:
                    if image.width * image.height > 20_000_000:
                        raise ValueError("立绘尺寸过大")
                    image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                    return image.convert("RGBA")
        if portrait_url.startswith("local:"):
            try:
                root = Path(StarTools.get_data_dir("astrbot_plugin_ww_gacha_sim")) / "portraits"
                path = managed_path(root, portrait_url.removeprefix("local:"))
                with Image.open(path) as image:
                    image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                    return image.convert("RGBA")
            except (ValueError, OSError) as exc:
                self.logger.error("无法读取受管立绘 %s: %s", portrait_url, exc)
                raise ValueError("受管立绘不存在或无效") from exc

        # 2. 检查缓存
        cached_image = self._cached_portrait(item)
        if cached_image is not None:
            self.logger.debug("立绘缓存命中: %s", self.portrait_cache_key(item))
            return cached_image

        raise ValueError(f"立绘尚未准备好: {item.name} (ID: {item.external_id})")

    @staticmethod
    def portrait_cache_key(item):
        source = ResourceLoader._canonical_url(item.portrait_url or "")
        return "portrait_" + hashlib.sha256(source.encode()).hexdigest()

    def _cached_portrait(self, item):
        picture = self.cache_manager.get_cached_image(self.portrait_cache_key(item))
        if picture is None:
            # Reuse existing PNG downloads when upgrading from the old cache.
            legacy_key = hashlib.md5(f"{item.external_id}\0{item.portrait_url or ''}".encode()).hexdigest()
            picture = self.cache_manager.get_cached_image(legacy_key)
        return picture

    def has_item_portrait(self, item):
        if not item.portrait_url:
            return True
        if getattr(self, "portrait_store", None) is not None and self.portrait_store.has(item.portrait_url):
            return True
        if item.portrait_url.startswith("local:"):
            root = Path(StarTools.get_data_dir("astrbot_plugin_ww_gacha_sim")) / "portraits"
            return managed_path(root, item.portrait_url.removeprefix("local:")).is_file()
        picture = self._cached_portrait(item)
        if picture is None:
            return False
        picture.close()
        return True

    def download_item_portrait(self, item, stop_event=None):
        if self.has_item_portrait(item):
            return True
        if not item.portrait_url or item.portrait_url.startswith("local:"):
            return False
        if getattr(self, "portrait_store", None) is not None:
            try:
                self.portrait_store.ensure(item.portrait_url)
                return True
            except (ValueError, OSError) as exc:
                self.logger.warning("立绘准备失败：%s", exc)
                return False
        return self._download_from_url(item.portrait_url, self.portrait_cache_key(item),
                                       stop_event=stop_event) is not None

    def _download_from_url(self, url: str, cache_key: str, stop_event=None) -> bytes | None:
        """
        从URL下载资源并缓存

        Args:
            url: 资源URL
            cache_key: 缓存键

        Returns:
            下载内容，失败返回None
        """
        try:
            # 获取代理配置
            proxy_dict = self.proxy_config.get_proxy_dict()

            # 使用资源下载器下载资源
            content = self.resources_downloader.download_with_retry(
                url, proxy=proxy_dict, max_retries=1, timeout=8,
                stop_event=stop_event, total_budget_seconds=30,
            )

            if content:
                compact = encode_portrait(content)
                self.cache_manager.cache_file(compact, cache_key, expire_time=30 * 86400)
                return compact
            else:
                self.logger.warning(f"网络下载失败: {url}")
                return None
        except Exception as e:
            self.logger.error(f"网络下载时发生异常: {url}, 错误: {e}")
            return None
