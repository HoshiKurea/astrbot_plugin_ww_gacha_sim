"""
渲染器模块
负责实现抽卡结果的可视化渲染
"""

from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from astrbot.api import logger

from ..item_data.item_manager import Item
from .ui_resources_manager import UIResourceManager

if TYPE_CHECKING:
    from ..gacha.cardpool_manager import CardPoolConfig

# 系统中文字体候选列表（按平台优先级排列）
_SYSTEM_FONT_CANDIDATES = [
    "msyh.ttc",  # Windows: Microsoft YaHei
    "msyhbd.ttc",  # Windows: Microsoft YaHei Bold
    "simhei.ttf",  # Windows: SimHei
    "PingFang.ttc",  # macOS: PingFang
    "STHeiti.ttf",  # macOS: STHeiti
    "NotoSansCJK-Regular.ttc",  # Linux: Noto Sans CJK
    "wqy-microhei.ttc",  # Linux: WenQuanYi Micro Hei
]


# 布局常量配置
class LayoutConfig:
    # 通用
    CARD_WIDTH = 430
    CARD_HEIGHT = 560

    # 十连抽布局
    H_GAP = -50  # 水平间距
    V_GAP = 30  # 垂直间距

    # 卡池详情布局
    DETAIL_WIDTH = 1000
    DETAIL_PADDING = 40
    DETAIL_LINE_HEIGHT = 50
    DETAIL_TITLE_HEIGHT = 100
    DETAIL_HEADER_HEIGHT = 60
    DETAIL_ROW_HEIGHT = 40

    # 颜色配置
    COLOR_BG = (30, 30, 30)
    COLOR_TEXT = (255, 255, 255)
    COLOR_ACCENT = (255, 215, 0)
    COLOR_SUB_TEXT = (200, 200, 200)


class GachaRenderer:
    """抽卡结果渲染器"""

    def __init__(self, ui_resource_manager: UIResourceManager):
        self.ui_resource_manager = ui_resource_manager
        self.font_path = ""
        self.font_source = "unchecked"
        self._font_warning_logged = False
        # 渲染参数 (从配置类加载)
        self.card_width = LayoutConfig.CARD_WIDTH
        self.card_height = LayoutConfig.CARD_HEIGHT
        self.h_gap = LayoutConfig.H_GAP
        self.v_gap = LayoutConfig.V_GAP

        # 性能缓存
        self._font_cache: dict[int, Any] = {}
        self._sprite_cache: dict[str, Image.Image] = {}
        self._halftone_cache: Image.Image | None = None
        self._cached_single_bg: Image.Image | None = None
        self._cached_ten_bg: Image.Image | None = None

    def _get_font(self, size: int):
        """获取系统字体对象（带缓存），优先使用系统中文字体"""
        if size in self._font_cache:
            return self._font_cache[size]
        font = None
        candidates = ([self.font_path] if self.font_path else []) + _SYSTEM_FONT_CANDIDATES
        for name in candidates:
            try:
                font = ImageFont.truetype(name, size)
                self.font_source = name
                break
            except OSError:
                continue
        if font is None:
            font = ImageFont.load_default()
            self.font_source = "fallback"
            if not self._font_warning_logged:
                logger.warning("未找到可用的中文字体，图片文字可能显示不完整；请配置 font_path")
                self._font_warning_logged = True
        self._font_cache[size] = font
        return font

    def render_pool_detail(self, config: "CardPoolConfig") -> Image.Image:
        """渲染卡池详细信息"""
        # 引用布局配置
        LC = LayoutConfig

        # 基础尺寸
        width = LC.DETAIL_WIDTH
        padding = LC.DETAIL_PADDING

        # 字体
        title_font = self._get_font(60)
        header_font = self._get_font(40)
        text_font = self._get_font(30)
        small_font = self._get_font(24)

        # 颜色
        bg_color = LC.COLOR_BG
        text_color = LC.COLOR_TEXT
        accent_color = LC.COLOR_ACCENT
        sub_text_color = LC.COLOR_SUB_TEXT

        # 预计算内容高度
        content_height = 0

        # 标题区域
        content_height += LC.DETAIL_TITLE_HEIGHT

        # 1. 基础信息
        content_height += LC.DETAIL_HEADER_HEIGHT
        content_height += LC.DETAIL_ROW_HEIGHT * 2

        # 2. 概率设置
        content_height += LC.DETAIL_HEADER_HEIGHT
        content_height += LC.DETAIL_ROW_HEIGHT * 6

        # 3. 保底机制
        content_height += LC.DETAIL_HEADER_HEIGHT
        content_height += 40 * 3  # 5star hard, soft, 4star hard

        # 4. UP物品
        up_5 = config.rate_up_item_ids.get("5star", [])
        up_4 = config.rate_up_item_ids.get("4star", [])
        content_height += 60  # Header
        content_height += 40 * (len(up_5) + len(up_4) + 2)  # List + labels

        # 5. 包含物品统计
        included_5 = config.included_item_ids.get("5star", [])
        included_4 = config.included_item_ids.get("4star", [])
        included_3 = config.included_item_ids.get("3star", [])
        content_height += 60  # Header
        content_height += 40 * 3  # Counts

        # 创建画布
        total_height = content_height + padding * 2
        image = Image.new("RGBA", (width, total_height), bg_color)
        draw = ImageDraw.Draw(image)

        y = padding

        # --- 标题 ---
        draw.text(
            (width // 2, y),
            "卡池配置详情",
            font=title_font,
            fill=accent_color,
            anchor="mt",
        )
        y += 80

        # 辅助绘制函数
        def draw_section_header(text, current_y):
            draw.text((padding, current_y), text, font=header_font, fill=accent_color)
            draw.line(
                [(padding, current_y + 45), (width - padding, current_y + 45)],
                fill=accent_color,
                width=2,
            )
            return current_y + 60

        def draw_row(label, value, current_y, font=text_font, indent=0):
            draw.text(
                (padding + indent, current_y), label, font=font, fill=sub_text_color
            )
            draw.text(
                (width - padding, current_y),
                str(value),
                font=font,
                fill=text_color,
                anchor="ra",
            )
            return current_y + 40

        # --- 1. 基础信息 ---
        y = draw_section_header("基础信息", y)
        y = draw_row("卡池名称", config.name, y)
        y = draw_row("卡池ID", config.cp_id, y)
        y += 20

        # --- 2. 概率设置 ---
        y = draw_section_header("概率设置", y)
        probs = config.probability_settings
        y = draw_row("五星基础概率", f"{probs.get('base_5star_rate', 0.008):.2%}", y)
        y = draw_row("四星基础概率", f"{probs.get('base_4star_rate', 0.06):.2%}", y)
        y = draw_row("三星基础概率", f"{probs.get('base_3star_rate', 0.932):.2%}", y)
        y = draw_row("五星UP概率", f"{probs.get('up_5star_rate', 0.5):.2%}", y)
        y = draw_row("四星UP概率", f"{probs.get('up_4star_rate', 0.5):.2%}", y)
        # _4star_role_rate 看起来像是概率阈值，这里直接显示数值
        role_rate = probs.get("_4star_role_rate", 0.06)
        y = draw_row("四星角色判定阈值", f"{role_rate}", y)
        y += 20

        # --- 3. 保底机制 ---
        y = draw_section_header("保底机制", y)
        prog = config.probability_progression

        # 5星硬保底
        p5 = prog.get("5star", {})
        y = draw_row("五星硬保底", f"{p5.get('hard_pity_pull', 80)} 抽", y)

        # 5星软保底
        soft_pity = p5.get("soft_pity", [])
        soft_text = "无"
        if soft_pity:
            # 简单展示第一个区间的起点，或者显示范围
            starts = [str(x["start_pull"]) for x in soft_pity]
            soft_text = f"第 {', '.join(starts)} 抽开始提升"
        y = draw_row("五星软保底", soft_text, y)

        # 4星硬保底
        p4 = prog.get("4star", {})
        y = draw_row("四星硬保底", f"{p4.get('hard_pity_pull', 10)} 抽", y)
        y += 20

        # --- 4. UP物品 ---
        y = draw_section_header("UP物品", y)

        if up_5:
            draw.text((padding, y), "五星 UP:", font=text_font, fill=text_color)
            y += 35
            for item in up_5:
                draw.text(
                    (padding + 40, y), f"• {item}", font=small_font, fill=sub_text_color
                )
                y += 30
        else:
            draw.text((padding, y), "五星 UP: 无", font=text_font, fill=sub_text_color)
            y += 35

        y += 10
        if up_4:
            draw.text((padding, y), "四星 UP:", font=text_font, fill=text_color)
            y += 35
            for item in up_4:
                draw.text(
                    (padding + 40, y), f"• {item}", font=small_font, fill=sub_text_color
                )
                y += 30
        else:
            draw.text((padding, y), "四星 UP: 无", font=text_font, fill=sub_text_color)
            y += 35
        y += 20

        # --- 5. 包含物品统计 ---
        y = draw_section_header("卡池内容统计", y)
        y = draw_row("五星物品总数", f"{len(included_5)} 个", y)

        # 如果物品数量不多，可以列出名称，或者只显示前几个
        if len(included_5) > 0:
            # 显示前5个
            items_str = ", ".join(included_5[:5])
            if len(included_5) > 5:
                items_str += "..."
            draw.text(
                (padding + 20, y), items_str, font=small_font, fill=(150, 150, 150)
            )
            y += 30

        y = draw_row("四星物品总数", f"{len(included_4)} 个", y)
        y = draw_row("三星物品总数", f"{len(included_3)} 个", y)

        return image

    def _create_single_card(self, item: Item) -> Image.Image:
        """
        优化后的抽卡卡片渲染：基于底部基准的布局方案
        """
        # --- 1. 基础参数定义 ---
        W, H = self.card_width, self.card_height
        rarity = item.rarity

        # 定义布局配置 (基于底部的定位，使用距离底部的高度)
        LAYOUT = {
            "portrait": {
                "width_ratio": 0.77,
                "bottom_offset_ratio": 0.08,
            },  # 立绘占宽100%，底部距离为卡片高度的40%
            "info": {
                "width_ratio": 1.0,
                "bottom_offset_ratio": -0.05,
            },  # 信息层占满宽，底部距离为卡片高度的15%
            "border": {"scale": 1.2},  # 边框稍微溢出，使用更大的缩放值
            "icon": {
                "size_ratio": 0.11,
                "left_offset_ratio": 0.10,
                "bottom_offset_ratio": 0.07,
            },  # 图标大小为卡片高度的15%，左侧偏移5%，底部偏移10%
        }

        # 创建透明底板画布，直接使用原始卡片尺寸，不预留额外空间
        card = Image.new("RGBA", (W, H), (0, 0, 0, 0))

        # --- 辅助函数：按比例缩放并返回位置 ---
        def get_scaled_layer(img, target_w, target_h, cover=False):
            img = img.convert("RGBA")
            iw, ih = img.size
            # 计算缩放比例 (fit or cover)
            ratio = (
                max(target_w / iw, target_h / ih)
                if cover
                else min(target_w / iw, target_h / ih)
            )
            new_size = (int(iw * ratio), int(ih * ratio))
            return img.resize(new_size, Image.Resampling.LANCZOS)

        # --- 图层 2: 背景层 (Background，缓存缩放结果) ---
        bg_cache_key = f"bg_{rarity}_{W}x{H}"
        if bg_cache_key in self._sprite_cache:
            bg_layer = self._sprite_cache[bg_cache_key]
        else:
            bg_sprite_name = f"bg_star_{rarity}.png"
            bg_img = self.ui_resource_manager.get_sprite_from_atlas(
                bg_sprite_name, remove_transparent_border=False
            )
            if bg_img:
                bg_layer = get_scaled_layer(bg_img, W, H, cover=True)
                self._sprite_cache[bg_cache_key] = bg_layer
            else:
                bg_layer = None

        if bg_layer:
            card.paste(bg_layer, (0, 0), bg_layer)
        else:
            # 后备方案
            rarity_int = int(rarity.replace("star", "")) if rarity else 3
            bg_source = self.ui_resource_manager.get_background_for_quality(rarity_int)
            if isinstance(bg_source, Image.Image):
                alt_bg = bg_source.convert("RGBA").resize((W, H))
            else:
                with Image.open(bg_source) as background:
                    alt_bg = background.convert("RGBA").resize((W, H))
            card.paste(alt_bg, (0, 0))

        # --- 图层 3: 立绘层 (Portrait) ---
        try:
            # 直接从 UIResourceManager 获取立绘图像
            portrait_raw = self.ui_resource_manager.get_item_portrait(item)
            if portrait_raw:
                portrait_raw = portrait_raw.convert("RGBA")
                iw, ih = portrait_raw.size

                # 裁剪到 404:560 宽高比（以图像中心为焦点）
                crop_ar = 404 / 560
                crop_w = iw
                crop_h = int(iw / crop_ar)
                if crop_h > ih:
                    crop_h = ih
                    crop_w = int(ih * crop_ar)
                # 以图像中心裁剪
                cx = iw // 2
                cy = ih // 2
                crop_box = (
                    cx - crop_w // 2,
                    cy - crop_h // 2,
                    cx + crop_w // 2,
                    cy + crop_h // 2,
                )
                if (crop_w < iw or crop_h < ih) and crop_w > 0 and crop_h > 0:
                    portrait_raw = portrait_raw.crop(crop_box)

                # 按比例计算立绘目标尺寸
                p_target_w = W * LAYOUT["portrait"]["width_ratio"]
                p_target_h = H  # 给立绘预留的最大高度比例
                portrait_layer = get_scaled_layer(portrait_raw, p_target_w, p_target_h)

                # 从底部开始计算位置，直接使用原始卡片尺寸
                px = (W - portrait_layer.width) // 2
                # 计算从画布底部到元素底部的距离
                element_bottom_y = H - int(
                    H * LAYOUT["portrait"]["bottom_offset_ratio"]
                )  # 元素底部的Y坐标
                py = element_bottom_y - portrait_layer.height  # 元素顶部的Y坐标
                card.paste(portrait_layer, (px, py), portrait_layer)
        except Exception as e:
            logger.warning(f"立绘加载失败: {e}")
            if item.portrait_url:
                raise

        # --- 图层 3.5: 半调图案层 (缓存缩放+透明度结果) ---
        try:
            if self._halftone_cache is None:
                bandiao_img = self.ui_resource_manager.get_halftone_pattern()
                if bandiao_img:
                    scaled = bandiao_img.resize((W, H), Image.Resampling.LANCZOS)
                    alpha = scaled.split()[3]
                    alpha = alpha.point(lambda x: int(x * 0.618))
                    scaled.putalpha(alpha)
                    self._halftone_cache = scaled

            if self._halftone_cache:
                offset_y = int(H * 0.049)
                card.paste(self._halftone_cache, (0, offset_y), self._halftone_cache)
            else:
                logger.warning("警告: 半调图案不存在，无法绘制半调图案")
        except Exception as e:
            logger.warning(f"半调图案加载失败: {e}")

        # --- 图层 4: 信息展示层 (Info/Show Layer，缓存缩放结果) ---
        show_sprite_name = f"show_star_{rarity}.png"
        info_w = W * LAYOUT["info"]["width_ratio"] + 4
        target_info_height = 2 * H
        show_cache_key = f"show_{rarity}_{int(info_w)}x{int(target_info_height)}"

        if show_cache_key in self._sprite_cache:
            info_layer = self._sprite_cache[show_cache_key]
        else:
            show_img = self.ui_resource_manager.get_sprite_from_atlas(
                show_sprite_name, remove_transparent_border=False
            )
            if show_img:
                info_layer = get_scaled_layer(show_img, info_w, int(target_info_height))
                self._sprite_cache[show_cache_key] = info_layer
            else:
                info_layer = None

        if info_layer:
            ix = (W - info_layer.width) // 2
            element_bottom_y = H - int(H * LAYOUT["info"]["bottom_offset_ratio"])
            iy = element_bottom_y - info_layer.height
            card.paste(info_layer, (ix, iy), info_layer)

        # --- 图层 5: 图标层 (Icon Layer) --- 信息层上方，位于左侧开头
        at = item.affiliated_type.title()

        try:
            # 尝试加载图标
            icon_path_str = self.ui_resource_manager.get_icon_path(at)
            if icon_path_str:
                icon_img = Image.open(icon_path_str).convert("RGBA")
            else:
                # 图标不存在，跳过绘制
                logger.warning(f"警告: 图标 {at} 不存在，无法绘制图标")
                icon_img = None

            if icon_img:
                # 计算图标大小和位置
                icon_size = int(H * LAYOUT["icon"]["size_ratio"])
                scaled_icon = icon_img.resize(
                    (icon_size, icon_size), Image.Resampling.LANCZOS
                )

                # 调整图标亮度，将亮度调暗

                enhancer = ImageEnhance.Brightness(scaled_icon)
                dimmed_icon = enhancer.enhance(0.9)  # 将亮度调整为原始的90%

                # 计算位置:
                icon_x = int(W * LAYOUT["icon"]["left_offset_ratio"])
                icon_bottom_y = H - int(H * LAYOUT["icon"]["bottom_offset_ratio"])
                icon_y = icon_bottom_y - dimmed_icon.height

                # 绘制图标到卡片上
                card.paste(dimmed_icon, (icon_x, icon_y), dimmed_icon)
        except Exception as e:
            logger.warning(f"图标加载失败: {e}")

        # 移除文字渲染层
        return card

    def _draw_alpha_text(
        self,
        image: Image.Image,
        pos: tuple[int, int],
        text: str,
        font: Any,
        fill: tuple[int, int, int, int],
    ):
        """绘制带透明度的文字（Pillow 的 draw.text 不支持 RGBA fill）"""
        color = fill[:3]
        alpha = fill[3] if len(fill) == 4 else 255

        if alpha >= 255:
            draw = ImageDraw.Draw(image)
            draw.text(pos, text, font=font, fill=color)
            return

        text_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        txt_draw = ImageDraw.Draw(text_layer)
        txt_draw.text(pos, text, font=font, fill=(*color, 255))
        r, g, b, a = text_layer.split()
        a = a.point(lambda x: int(x * alpha / 255))
        text_layer = Image.merge("RGBA", (r, g, b, a))
        image.paste(text_layer, (0, 0), text_layer)

    def render_single_pull(
        self, item: Item, nickname: str = "", user_id: str = ""
    ) -> Image.Image:
        """渲染单次抽卡结果"""
        card = self._create_single_card(item)

        # --- 使用T_LuckdrawBg.png作为单抽背景（缓存裁剪结果） ---
        bg_path_str = self.ui_resource_manager.get_background_path()
        final_image = None
        background_composited = False

        if bg_path_str:
            try:
                if self._cached_single_bg is None:
                    bg_image = Image.open(bg_path_str).convert("RGBA")
                    crop_width, crop_height = 1000, 800
                    crop_left = (bg_image.width - crop_width) // 2
                    crop_top = (bg_image.height - crop_height) // 2
                    self._cached_single_bg = bg_image.crop(
                        (
                            crop_left,
                            crop_top,
                            crop_left + crop_width,
                            crop_top + crop_height,
                        )
                    )

                final_image = self._cached_single_bg.copy()
                if final_image.mode != "RGBA":
                    final_image = final_image.convert("RGBA")
                card_x = (1000 - card.width) // 2
                card_y = (800 - card.height) // 2
                final_image.paste(card, (card_x, card_y), card)
                background_composited = True
            except Exception as e:
                logger.warning(f"背景处理失败: {e}")
                final_image = card
        else:
            final_image = card

        # --- 添加文字信息（使用 _draw_alpha_text 修复透明度） ---
        image_width, image_height = final_image.size

        # 1. 警告文字 (左下角)
        warning_text = "模拟抽卡仅供娱乐，素材版权属于库洛"
        font = self._get_font(12)
        bbox = font.getbbox(warning_text) or (0, 0, 0, 0)
        text_h = bbox[3] - bbox[1]
        self._draw_alpha_text(
            final_image,
            (10, image_height - text_h - 10),
            warning_text,
            font=font,
            fill=(255, 255, 255, 200),
        )

        # 2. 用户昵称和ID (右下角)
        if nickname or user_id:
            right_margin = 15
            bottom_margin = 15
            text_font = self._get_font(24)

            if user_id:
                id_text = f"特征码: {user_id}"
                id_bbox = text_font.getbbox(id_text) or (0, 0, 0, 0)
                id_w = id_bbox[2] - id_bbox[0]
                id_h = id_bbox[3] - id_bbox[1]

                id_x = image_width - id_w - right_margin
                id_y = image_height - id_h - bottom_margin
                self._draw_alpha_text(
                    final_image,
                    (id_x, id_y),
                    id_text,
                    font=text_font,
                    fill=(255, 255, 255, 204),
                )
                bottom_margin += id_h + 5

            if nickname:
                name_bbox = text_font.getbbox(nickname) or (0, 0, 0, 0)
                name_w = name_bbox[2] - name_bbox[0]
                name_h = name_bbox[3] - name_bbox[1]

                name_x = image_width - name_w - right_margin
                name_y = image_height - name_h - bottom_margin
                self._draw_alpha_text(
                    final_image,
                    (name_x, name_y),
                    nickname,
                    font=text_font,
                    fill=(255, 255, 255, 204),
                )

        # This is a complete poster on its own background. Intermediate layer
        # alpha must not blend the poster again with the chat page background.
        return final_image.convert("RGB") if background_composited else final_image

    def render_ten_pulls(
        self, results: list[Item], nickname: str = "", user_id: str = ""
    ) -> Image.Image:
        """渲染十连抽卡结果"""
        cards_per_row = 5
        rows = (len(results) + cards_per_row - 1) // cards_per_row

        total_width = cards_per_row * self.card_width + (cards_per_row + 1) * self.h_gap
        total_height = rows * self.card_height + (rows + 1) * self.v_gap

        bg_path_str = self.ui_resource_manager.get_background_path()
        if bg_path_str:
            # 缓存背景图加载
            if self._cached_ten_bg is None:
                self._cached_ten_bg = Image.open(bg_path_str).convert("RGBA")
            bg_image = self._cached_ten_bg
            bg_width, bg_height = bg_image.size

            cards_total_width = (
                cards_per_row * self.card_width + (cards_per_row - 1) * self.h_gap
            )
            cards_total_height = rows * self.card_height + (rows - 1) * self.v_gap

            start_x = (bg_width - cards_total_width) // 2
            start_y = (bg_height - cards_total_height) // 2

            # 裁剪背景到包含卡片的合适区域，减少输出图片体积
            padding = 80
            crop_x1 = max(0, start_x - padding)
            crop_y1 = max(0, start_y - padding)
            crop_x2 = min(bg_width, start_x + cards_total_width + padding)
            crop_y2 = min(bg_height, start_y + cards_total_height + padding)
            cropped_bg = bg_image.crop((crop_x1, crop_y1, crop_x2, crop_y2))
            full_image = cropped_bg.copy()

            # 调整卡片起始位置到裁剪后的坐标
            start_x -= crop_x1
            start_y -= crop_y1
        else:
            full_image = Image.new(
                "RGBA", (total_width, total_height), (50, 50, 50, 255)
            )
            start_x = self.h_gap
            start_y = self.v_gap

        if full_image.mode != "RGBA":
            full_image = full_image.convert("RGBA")

        # 渲染每张卡片
        for idx, item in enumerate(results):
            card = self._create_single_card(item)
            row = idx // cards_per_row
            col = idx % cards_per_row
            x = start_x + col * (self.card_width + self.h_gap)
            y = start_y + row * (self.card_height + self.v_gap)

            if card.mode == "RGBA":
                full_image.paste(card, (x, y), card)
            else:
                full_image.paste(card, (x, y))

        # --- 添加文字信息（使用 _draw_alpha_text 修复透明度） ---
        image_width, image_height = full_image.size

        warning_text = "模拟抽卡仅供娱乐，素材版权属于库洛"
        font = self._get_font(24)
        bbox = font.getbbox(warning_text) or (0, 0, 0, 0)
        th = bbox[3] - bbox[1]
        self._draw_alpha_text(
            full_image,
            (50, image_height - th - 50),
            warning_text,
            font=font,
            fill=(255, 255, 255, 200),
        )

        if nickname or user_id:
            right_margin = 50
            bottom_margin = 50
            id_font = self._get_font(36)

            if user_id:
                id_text = f"特征码: {user_id}"
                id_bbox = id_font.getbbox(id_text) or (0, 0, 0, 0)
                id_w = id_bbox[2] - id_bbox[0]
                id_h = id_bbox[3] - id_bbox[1]
                self._draw_alpha_text(
                    full_image,
                    (
                        image_width - id_w - right_margin,
                        image_height - id_h - bottom_margin + 5,
                    ),
                    id_text,
                    font=id_font,
                    fill=(255, 255, 255, 255),
                )
                bottom_margin += id_h + 10

            if nickname:
                name_bbox = id_font.getbbox(nickname) or (0, 0, 0, 0)
                name_w = name_bbox[2] - name_bbox[0]
                name_h = name_bbox[3] - name_bbox[1]
                self._draw_alpha_text(
                    full_image,
                    (
                        image_width - name_w - right_margin,
                        image_height - name_h - bottom_margin,
                    ),
                    nickname,
                    font=id_font,
                    fill=(255, 255, 255, 255),
                )

        return full_image.convert("RGB") if bg_path_str else full_image

    def render_history(
        self,
        records: list[dict],
        page: int,
        total_pages: int,
        total_records: int,
        pool_name: str = "全部卡池",
    ) -> Image.Image:
        """
        渲染历史记录图片

        Args:
            records: 记录列表，每个记录包含 item(name), rarity, pull_time, type
            page: 当前页码
            total_pages: 总页数
            total_records: 总记录数
            pool_name: 卡池名称
        """
        # 1. 设置画布参数（根据记录数动态计算高度）
        width = 1200
        base_height = 300
        records_area = len(records) * 45
        height = max(800, base_height + records_area)

        # 颜色定义
        bg_color = (240, 240, 240, 255)  # 浅灰背景
        text_color = (0, 0, 0, 255)
        text_color_white = (255, 255, 255, 255)

        # 创建画布
        image = Image.new("RGBA", (width, height), bg_color)
        draw = ImageDraw.Draw(image)

        # 字体
        font_title = self._get_font(32)
        font_header = self._get_font(24)
        font_content = self._get_font(22)
        font_footer = self._get_font(18)

        # 2. 绘制标题栏 (顶部)
        title_bar_height = 60
        draw.rectangle([0, 0, width, title_bar_height], fill=(30, 30, 30, 255))
        draw.text((30, 15), "唤取记录", font=font_title, fill=text_color_white)
        # 模拟关闭按钮
        draw.text((width - 50, 15), "×", font=font_title, fill=text_color_white)

        # 3. 绘制筛选栏 (唤取类型)
        filter_y = title_bar_height + 20
        draw.text((30, filter_y), "唤取类型", font=font_header, fill=text_color)
        # 模拟下拉框
        draw.rectangle(
            [140, filter_y - 5, 400, filter_y + 35], outline=(150, 150, 150), width=2
        )
        draw.text((150, filter_y), pool_name, font=font_header, fill=text_color)

        # 4. 绘制表格表头
        table_y = filter_y + 60
        table_header_height = 50
        col_names = ["唤取类型", "唤取物品", "唤取数量", "唤取时间"]
        col_x = [50, 250, 650, 850]  # 起始X坐标

        # 表头背景
        draw.rectangle(
            [30, table_y, width - 30, table_y + table_header_height],
            fill=(220, 220, 220, 255),
        )

        for i, name in enumerate(col_names):
            draw.text((col_x[i], table_y + 10), name, font=font_header, fill=text_color)

        # 5. 绘制记录列表
        row_height = 45
        start_y = table_y + table_header_height

        for idx, record in enumerate(records):
            current_y = start_y + idx * row_height

            # 斑马纹背景 (可选)
            if idx % 2 == 1:
                draw.rectangle(
                    [30, current_y, width - 30, current_y + row_height],
                    fill=(245, 245, 245, 255),
                )

            # 分隔线
            draw.line(
                [30, current_y + row_height, width - 30, current_y + row_height],
                fill=(200, 200, 200),
                width=1,
            )

            # 数据
            item_type = record.get("type", "未知")
            # 转换英文类型为中文
            type_map = {"character": "角色", "weapon": "武器"}
            item_type_cn = type_map.get(item_type, item_type)

            item_name = record.get("item", "未知")
            rarity = record.get("rarity", "3star")
            pull_time = record.get("pull_time", "")

            # 颜色处理：5星物品使用金色
            row_color = text_color
            if rarity == "5star":
                row_color = (255, 165, 0, 255)  # 橙色/金色

            draw.text(
                (col_x[0], current_y + 10),
                item_type_cn,
                font=font_content,
                fill=row_color,
            )
            draw.text(
                (col_x[1], current_y + 10), item_name, font=font_content, fill=row_color
            )
            draw.text(
                (col_x[2], current_y + 10), "1", font=font_content, fill=row_color
            )
            draw.text(
                (col_x[3], current_y + 10),
                str(pull_time),
                font=font_content,
                fill=(100, 100, 100, 255),
            )

        # 6. 绘制页脚 (Disclaimer)
        footer_y = height - 80
        disclaimer = "若记录存在延迟，请稍后再次查看。记录只保留最近3个月的数据。\n所有时间均为当前时区时间。"
        draw.text(
            (30, footer_y), disclaimer, font=font_footer, fill=(100, 100, 100, 255)
        )

        # 7. 绘制分页
        page_str = f"<< {page} / {total_pages} >>"
        # 计算文字宽度居中
        bbox = draw.textbbox((0, 0), page_str, font=font_content)
        w = bbox[2] - bbox[0]
        draw.text(
            ((width - w) // 2, footer_y + 40),
            page_str,
            font=font_content,
            fill=text_color,
        )

        return image
