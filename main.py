import asyncio
import hashlib
import json
import time
import threading
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.components import Image

from .src.db.database import CommonDatabase
from .src.db.gacha_db_operations import GachaDBOperations
from .src.db.item_db_operations import ItemDBOperations
from .src.db.migration import run_migrations
from .src.gacha.cardpool_manager import CardPoolConfig, CardPoolManager
from .src.item_data.item_manager import ItemManager
from .src.render.gacha_renderer import GachaRenderer
from .src.render.local_file_cache_manager import LocalFileCacheManager
from .src.render.proxy_config import ProxyConfig
from .src.render.resource_loader import ResourceLoader
from .src.render.portrait_store import PortraitStore
from .src.render.image_encoding import encode_result_image
from .src.render.ui_resources_manager import UIResourceManager
from .src.services.gacha_service import GachaService, GachaBusy
from .src.services.pool_service import PoolService
from .src.services.command_utils import finish_command, format_history_text, parse_history_args
from .src.services.render_service import RenderService
from .src.services.portrait_service import PortraitService, PortraitUnavailable
from .src.services.settings import PluginSettings


class WutheringWavesGachaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.admin_api = None
        self._closed = False
        self.cdb = None
        self.gacha_service = None
        self.lf_cache = None
        self.render_service = None
        self.portrait_service = None
        self.rs_loader = None
        self.portrait_store = None
        self._result_cleanup_lock = threading.Lock()
        self._last_result_cleanup = 0.0
        self._last_pool_reload_error = ""

    async def initialize(self):
        try:
            await self._initialize_services()
        except BaseException:
            await self.terminate()
            raise

    async def _initialize_services(self):

        self.cdb = CommonDatabase()
        self.gdb_ops = GachaDBOperations(self.cdb)
        self.idb_ops = ItemDBOperations(self.cdb)
        self.item_manager = ItemManager(self.idb_ops)

        self.settings = PluginSettings.from_config(self.config)
        self.enable_rendering = self.settings.enable_rendering
        self.enable_history_recording = self.settings.enable_history_recording

        proxy_url = self.settings.proxy_url
        if not self.settings.enable_proxy:
            proxy_url = None
        self.proxy_config = ProxyConfig(proxy_url if proxy_url else None)

        # Management and offline resources also work when chat rendering is off.
        self.lf_cache = LocalFileCacheManager(
            cleanup_interval=self.settings.cache_cleanup_interval,
            max_bytes=self.settings.cache_max_mb * 1024 * 1024,
        )
        self.rs_loader = ResourceLoader(reuse_connections=True)
        self.portrait_store = PortraitStore(
            Path(StarTools.get_data_dir("astrbot_plugin_ww_gacha_sim")) / "portraits",
            self.lf_cache, self.rs_loader, self.proxy_config.get_proxy_dict(),
        )

        if self.enable_rendering:
            self.render_service = RenderService(
                self.settings.render_workers,
                self.settings.render_queue_limit,
                self.settings.render_timeout_seconds,
            )
            logger.info("启用渲染结果输出功能")
            self.ui_rs_manager = UIResourceManager(
                resources_loader=self.rs_loader,
                cache_manager=self.lf_cache,
                proxy_config=self.proxy_config,
                portrait_store=self.portrait_store,
            )
            self.renderer = GachaRenderer(self.ui_rs_manager)
            self.renderer.font_path = self.settings.font_path
            self.portrait_service = PortraitService(
                self.ui_rs_manager, workers=self.settings.portrait_download_workers
            )

        self.cp_manager = CardPoolManager()

        self.save_rendered_results = self.settings.save_rendered_results

        # A failed migration must prevent draws; the caller cleans up partial startup.
        run_migrations(self.cdb, self.idb_ops, self.item_manager, self.cp_manager)
        self.pool_service = PoolService(self.cp_manager, self.idb_ops)
        self.gacha_service = GachaService(
            self.gdb_ops, self.idb_ops, self.enable_history_recording,
            queue_limit=self.settings.database_queue_limit, pools=self.pool_service,
        )
        await self.gacha_service.prune_receipts()

        from .src.web.api import NativeAdminAPI

        self.admin_api = NativeAdminAPI(
            self.pool_service, self.idb_ops, self.cdb,
            self.lf_cache, getattr(self, "renderer", None),
            proxy_config=self.proxy_config,
            database_worker=self.gacha_service, portrait_store=self.portrait_store,
            portrait_workers=self.settings.portrait_download_workers,
        )
        self.admin_api.register(self.context)

        logger.info("鸣潮模拟抽卡插件已初始化")

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _user_kv_key(platform_id: str, sender_id: str) -> str:
        identity = f"{platform_id}\0{sender_id}"
        return f"user_default_pool_v2_{hashlib.sha256(identity.encode()).hexdigest()}"

    def _save_rendered_image(self, image, user_id: str, encoded=None, suffix=None):
        if not self.save_rendered_results:
            return
        try:
            output_path = (
                Path(StarTools.get_data_dir("astrbot_plugin_ww_gacha_sim"))
                / "rendered_results"
            )
            output_path.mkdir(parents=True, exist_ok=True)
            user_key = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
            if encoded is None:
                encoded, suffix = encode_result_image(image)
            filename = f"gacha_result_{user_key}_{time.time_ns()}.{suffix}"
            file_path = output_path / filename
            file_path.write_bytes(encoded)
            now = time.time()
            with self._result_cleanup_lock:
                if now - self._last_result_cleanup > 86400:
                    cutoff = now - self.settings.rendered_result_retention_days * 86400
                    for old in output_path.glob("gacha_result_*.*"):
                        if old.suffix.lower() not in {".png", ".jpg"}:
                            continue
                        try:
                            if old.stat().st_mtime < cutoff:
                                old.unlink()
                        except OSError:
                            logger.exception("清理过期结果图片失败")
                    self._last_result_cleanup = now
            logger.info(f"已保存抽卡结果图片: {file_path}")
        except Exception as e:
            logger.error(f"保存抽卡结果图片失败: {e}")

    @staticmethod
    def _image_as_chain(image) -> list:
        encoded, _ = encode_result_image(image)
        return [Image.fromBytes(encoded)]

    @staticmethod
    def _rarity_stars(rarity: str) -> str:
        return "★★★★★" if rarity == "5star" else "★★★★" if rarity == "4star" else "★★★"

    @staticmethod
    def _request_key(event, operation: str) -> str | None:
        message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        return GachaService.request_key(
            str(event.get_platform_id()), str(event.unified_msg_origin),
            str(event.get_sender_id()), str(message_id) if message_id else None,
            operation,
        )

    def _render_and_encode(self, render_method, *args, **kwargs):
        image = render_method(*args, **kwargs)
        encoded, suffix = encode_result_image(image)
        self._save_rendered_image(image, kwargs.get("user_id", ""), encoded, suffix)
        return [Image.fromBytes(encoded)]

    def _render_to_chain(self, render_method, *args, **kwargs):
        return self._image_as_chain(render_method(*args, **kwargs))

    async def _run_render(self, function, *args, **kwargs):
        if self._closed or self.render_service is None:
            raise RuntimeError("渲染服务已关闭")
        return await self.render_service.run(function, *args, **kwargs)

    async def _draw_response(self, event, text_result, items, render_method, render_items):
        if not self.enable_rendering:
            yield event.plain_result(text_result)
            return
        text_sent = False
        try:
            if self.portrait_service.missing(items):
                yield event.plain_result(
                    text_result + "\n部分立绘尚未就绪，正在准备资源，完成后补发完整图片。"
                )
                text_sent = True
            await self.portrait_service.prepare(
                items, timeout_seconds=self.settings.portrait_wait_timeout_seconds
            )
            sender_name = event.get_sender_name() if hasattr(event, "get_sender_name") else "未知用户"
            chain = await self._run_render(
                self._render_and_encode, render_method, render_items,
                nickname=sender_name, user_id=str(event.get_sender_id()),
            )
            yield event.chain_result(chain)
        except PortraitUnavailable as exc:
            logger.warning("抽卡已提交，立绘准备未完成：%s", exc)
            message = str(exc)
            yield event.plain_result(message if text_sent else f"抽卡已完成，{message}\n{text_result}")
        except Exception:
            logger.exception("抽卡已提交，但图片渲染失败")
            message = "图片生成失败，请以已发送的文字结果为准。" if text_sent else f"抽卡已完成，图片生成失败。\n{text_result}"
            yield event.plain_result(message)

    def _find_pool_config(
        self, pool_identifier: str
    ) -> CardPoolConfig | list[CardPoolConfig] | None:
        """查找卡池配置，返回 None 表示未找到，返回 list 表示多个重名结果"""
        configs = self.pool_service.all()
        exact_id = next((config for config in configs
                         if config.cp_id == pool_identifier), None)
        if exact_id is not None:
            return exact_id
        matched = [config for config in configs
                   if config.name == pool_identifier]
        if len(matched) > 1:
            return matched
        if len(matched) == 1:
            return matched[0]
        return self.pool_service.get(pool_identifier)

    def _active_pools(self) -> list[CardPoolConfig]:
        return sorted(
            (pool for pool in self.pool_service.all() if pool.enable),
            key=lambda pool: (pool.name, pool.cp_id),
        )

    @classmethod
    def _pool_list_kv_key(cls, event) -> str:
        return cls._user_kv_key(
            str(event.get_platform_id()), str(event.get_sender_id())
        ) + "_list"

    async def _numbered_pool_id(self, event, identifier: str) -> str | None:
        if not identifier.isdecimal():
            return identifier
        raw = await self.get_kv_data(self._pool_list_kv_key(event), default=None)
        try:
            snapshot = json.loads(raw) if raw else {}
            if snapshot.get("version") != self.pool_service.version:
                return None
            number = int(identifier)
            ids = snapshot["ids"]
            return ids[number - 1] if 1 <= number <= len(ids) else None
        except (ValueError, TypeError, KeyError, IndexError):
            return None

    async def _resolve_pool_config(self, event, pool_identifier: str):
        sender_id = str(event.get_sender_id())
        kv_key = self._user_kv_key(str(event.get_platform_id()), sender_id)
        active = self._active_pools()
        config_ids = [pool.cp_id for pool in active]

        if not config_ids:
            return None, event.plain_result(
                "当前没有启用的卡池，请管理员在插件管理页创建或启用卡池。"
            )

        if pool_identifier == "":
            saved_cp_id = await self.get_kv_data(kv_key, default=None)
            if saved_cp_id and saved_cp_id in config_ids:
                pool_identifier = saved_cp_id
            else:
                preferred = self.settings.default_pool_id
                pool_identifier = preferred if preferred in config_ids else config_ids[0]
                if saved_cp_id and saved_cp_id not in config_ids:
                    await self.delete_kv_data(kv_key)
                    logger.info(
                        f"用户 {sender_id} 的默认卡池 {saved_cp_id} 不存在，已清理"
                    )

        if pool_identifier.isdecimal():
            pool_identifier = await self._numbered_pool_id(event, pool_identifier)
            if pool_identifier is None:
                return None, event.plain_result("卡池编号无效或列表已变化，请先发送 /wwg 卡池 刷新列表。")

        target_config = self._find_pool_config(pool_identifier)

        if isinstance(target_config, list):
            choices = "、".join(pool.cp_id for pool in target_config)
            return None, event.plain_result(
                f"存在多个名为「{pool_identifier}」的卡池，请使用 ID：{choices}"
            )

        if target_config is None:
            pool_list = "找不到指定的卡池。可用的卡池有：\n"
            for i, config in enumerate(active, 1):
                pool_list += f"{i}. {config.name} - ID: {config.cp_id}\n"
            return None, event.plain_result(pool_list)

        if not target_config.enable:
            return None, event.plain_result(
                f"卡池「{target_config.name}」已被禁用，无法进行抽卡。"
            )

        return target_config, None

    # ------------------------------------------------------------------
    # 命令处理器
    # ------------------------------------------------------------------

    @filter.command_group("wwg", alias={"鸣潮", "ww抽卡"}, priority=1)
    def wuwa_group():
        """鸣潮抽卡的统一指令入口。"""
        pass

    @wuwa_group.command("帮助", priority=1)
    @finish_command
    async def group_help(self, event: AstrMessageEvent):
        async for result in self.wgs_help(event):
            yield result

    @wuwa_group.command("卡池", priority=1)
    @finish_command
    async def group_pools(self, event: AstrMessageEvent):
        async for result in self.list_card_pools(event):
            yield result

    @wuwa_group.command("选择", priority=1)
    @finish_command
    async def group_select(self, event: AstrMessageEvent, pool_identifier: str = ""):
        async for result in self.set_default_pool(event, pool_identifier):
            yield result

    @wuwa_group.command("单抽", priority=1)
    @finish_command
    async def group_single(self, event: AstrMessageEvent, pool_identifier: str = ""):
        async for result in self.single_pull(event, pool_identifier):
            yield result

    @wuwa_group.command("十连", priority=1)
    @finish_command
    async def group_ten(self, event: AstrMessageEvent, pool_identifier: str = ""):
        async for result in self.ten_pulls(event, pool_identifier):
            yield result

    @wuwa_group.command("记录", priority=1)
    @finish_command
    async def group_history(self, event: AstrMessageEvent,
                            pool_or_page: str = "", page_text: str = ""):
        async for result in self.view_pull_history(event, pool_or_page, page_text):
            yield result

    @wuwa_group.command("保底", priority=1)
    @finish_command
    async def group_pity(self, event: AstrMessageEvent, pool_identifier: str = ""):
        async for result in self.pity_status(event, pool_identifier):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @wuwa_group.command("诊断", priority=1)
    @finish_command
    async def group_diagnose(self, event: AstrMessageEvent):
        async for result in self.diagnose(event):
            yield result

    @filter.command("单抽", alias={"单次抽卡", "抽卡", "单次唤取"}, priority=1)
    @finish_command
    async def single_pull(self, event: AstrMessageEvent, pool_identifier: str = ""):
        try:
            target_config, error_msg = await self._resolve_pool_config(
                event, pool_identifier
            )
            if error_msg:
                yield error_msg
                return

            sender_id = str(event.get_sender_id())
            snapshot, version = self.pool_service.get_with_version(target_config.cp_id)
            if snapshot is None or not snapshot.enable:
                raise ValueError("卡池刚刚发生变更，请重新抽卡")
            pull_result = await self.gacha_service.draw(
                str(event.get_platform_id()), sender_id, snapshot,
                version, 1, self._request_key(event, "single"),
            )
            item_obj = pull_result.get("item_obj")

            if not item_obj:
                yield event.plain_result("抽卡过程中出现错误，未能获得有效物品。")
                return

            text_result = (
                f"单次抽卡结果：\n{self._rarity_stars(item_obj.rarity)} {item_obj.name}"
            )
            async for result in self._draw_response(
                event, text_result, [item_obj], self.renderer.render_single_pull
                if self.enable_rendering else None, item_obj,
            ):
                yield result

        except GachaBusy:
            yield event.plain_result("抽卡队列繁忙，本次尚未抽卡，请稍后再试。")
        except ValueError as e:
            logger.warning(f"单抽卡池配置无效: {e}")
            yield event.plain_result(f"卡池配置无效：{e}。请联系管理员修正后重试。")
        except Exception as e:
            logger.error(f"单次抽卡失败: {e}")
            yield event.plain_result("单次抽卡时发生错误，请检查插件配置或联系管理员。")

    @filter.command("十抽", alias={"十连", "10抽", "10连"}, priority=1)
    @finish_command
    async def ten_pulls(self, event: AstrMessageEvent, pool_identifier: str = ""):
        try:
            target_config, error_msg = await self._resolve_pool_config(
                event, pool_identifier
            )
            if error_msg:
                yield error_msg
                return

            sender_id = str(event.get_sender_id())
            snapshot, version = self.pool_service.get_with_version(target_config.cp_id)
            if snapshot is None or not snapshot.enable:
                raise ValueError("卡池刚刚发生变更，请重新抽卡")
            item_objs = await self.gacha_service.draw(
                str(event.get_platform_id()), sender_id, snapshot,
                version, 10, self._request_key(event, "ten"),
            )

            if not item_objs:
                yield event.plain_result("抽卡过程中出现错误，未能获得有效物品。")
                return

            lines = ["十连抽卡结果："]
            for idx, obj in enumerate(item_objs, 1):
                lines.append(f"{idx}. {self._rarity_stars(obj.rarity)} {obj.name}")
            text_result = "\n".join(lines)
            async for result in self._draw_response(
                event, text_result, item_objs, self.renderer.render_ten_pulls
                if self.enable_rendering else None, item_objs,
            ):
                yield result

        except GachaBusy:
            yield event.plain_result("抽卡队列繁忙，本次尚未抽卡，请稍后再试。")
        except ValueError as e:
            logger.warning(f"十连卡池配置无效: {e}")
            yield event.plain_result(f"卡池配置无效：{e}。请联系管理员修正后重试。")
        except Exception as e:
            logger.error(f"十连抽卡失败: {e}")
            yield event.plain_result("十连抽卡时发生错误，请检查插件配置或联系管理员。")

    @filter.command("卡池", alias={"卡池列表", "查看卡池"}, priority=1)
    @finish_command
    async def list_card_pools(self, event: AstrMessageEvent):
        try:
            active = self._active_pools()
            if not active:
                yield event.plain_result(
                    "当前没有启用的卡池，请管理员在插件管理页创建或启用卡池。"
                )
                return

            lines = ["当前可用的卡池："]
            for i, config in enumerate(active, 1):
                lines.append(f"{i}. {config.name} - ID: {config.cp_id}")
            await self.put_kv_data(
                self._pool_list_kv_key(event),
                json.dumps({"version": self.pool_service.version,
                            "ids": [pool.cp_id for pool in active]}),
            )
            lines.append("使用 `/wwg 选择 <编号/卡池ID/名称>` 选择，或 `/wwg 单抽 <卡池ID/名称>` 抽卡。")
            yield event.plain_result("\n".join(lines))

        except Exception as e:
            logger.error(f"获取卡池列表失败: {e}")
            yield event.plain_result(
                "获取卡池列表时发生错误，请检查插件配置或联系管理员。"
            )

    @filter.command("唤取", alias={"选抽", "设置卡池", "选择卡池"}, priority=1)
    @finish_command
    async def set_default_pool(
        self, event: AstrMessageEvent, pool_identifier: str = ""
    ):
        try:
            if not pool_identifier:
                yield event.plain_result(
                    "请先发送 /wwg 卡池 查看编号，再用 /wwg 选择 <编号/卡池ID/名称> 选择。"
                )
                return

            active = self._active_pools()
            if not active:
                yield event.plain_result(
                    "当前没有启用的卡池，请管理员在插件管理页创建或启用卡池。"
                )
                return

            if pool_identifier.isdecimal():
                pool_identifier = await self._numbered_pool_id(event, pool_identifier)
                if pool_identifier is None:
                    yield event.plain_result("卡池编号无效或列表已变化，请先发送 /wwg 卡池 刷新列表。")
                    return
            found = self._find_pool_config(pool_identifier)
            if isinstance(found, list):
                lines = [
                    f"找到 {len(found)} 个名为「{pool_identifier}」的卡池，请选择："
                ]
                for i, c in enumerate(found, 1):
                    lines.append(f"{i}. {c.name} (ID: {c.cp_id})")
                lines.append("请使用 `/wwg 选择 <卡池ID>` 来指定具体卡池。")
                yield event.plain_result("\n".join(lines))
                return

            if found is None:
                lines = ["找不到指定的卡池。可用的卡池有："]
                for i, config in enumerate(active, 1):
                    lines.append(f"{i}. {config.name} - ID: {config.cp_id}")
                yield event.plain_result("\n".join(lines))
                return

            if not found.enable:
                yield event.plain_result(
                    f"卡池「{found.name}」已被禁用，无法设置为默认卡池。"
                )
                return

            sender_id = str(event.get_sender_id())
            kv_key = self._user_kv_key(str(event.get_platform_id()), sender_id)
            await self.put_kv_data(kv_key, found.cp_id)

            yield event.plain_result(
                f"已设置您的默认卡池为：{found.name} (ID: {found.cp_id})\n"
                "现在您可以使用 `/wwg 单抽` 命令进行抽卡，将默认使用此卡池。"
            )

        except Exception as e:
            logger.error(f"设置默认卡池失败: {e}")
            yield event.plain_result(
                "设置默认卡池时发生错误，请检查插件配置或联系管理员。"
            )

    @filter.command("唤取记录", alias={"抽卡记录", "查看抽卡", "抽卡历史"}, priority=1)
    @finish_command
    async def view_pull_history(self, event: AstrMessageEvent,
                                pool_or_page: str = "1", page_text: str = ""):
        if not self.enable_history_recording:
            yield event.plain_result("抽卡历史记录已在插件配置中关闭。")
            return
        try:
            pool_identifier, page = parse_history_args(pool_or_page, page_text)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        try:
            platform_id = str(event.get_platform_id())
            sender_id = str(event.get_sender_id())
            pool_id = None
            pool_name = "全部卡池"
            if pool_identifier:
                found = self._find_pool_config(pool_identifier)
                if isinstance(found, list):
                    ids = "、".join(pool.cp_id for pool in found)
                    yield event.plain_result(
                        f"卡池名称重复，请使用 ID 查询：{ids}"
                    )
                    return
                if found:
                    pool_id, pool_name = found.cp_id, found.name
                elif await self.gacha_service.history_count(
                    platform_id, sender_id, pool_identifier
                ):
                    # A removed pool remains queryable by its historical ID.
                    pool_id = pool_identifier
                    pool_name = pool_identifier
                else:
                    yield event.plain_result(
                        f"找不到卡池「{pool_identifier}」或该 ID 没有历史记录。"
                    )
                    return
            total = await self.gacha_service.history_count(
                platform_id, sender_id, pool_id
            )
            if total == 0:
                yield event.plain_result("该卡池暂无抽卡记录。" if pool_id else
                                         "您还没有任何抽卡记录。")
                return
            page_size = 10
            total_pages = (total + page_size - 1) // page_size
            if page > total_pages:
                yield event.plain_result(
                    f"页码超出范围，当前共有 {total_pages} 页、{total} 条记录。"
                )
                return
            records = await self.gacha_service.history(
                platform_id, sender_id, page_size, (page - 1) * page_size, pool_id
            )
            text_result = format_history_text(
                records, page, total_pages, total, pool_id
            )
            if self.enable_rendering:
                try:
                    chain = await self._run_render(
                        self._render_to_chain, self.renderer.render_history,
                        records, page, total_pages, total,
                        pool_name=pool_name,
                    )
                    yield event.chain_result(chain)
                    return
                except Exception:
                    logger.exception("历史图片生成失败，已改用文字输出")
                    yield event.plain_result("图片生成失败，以下为文字记录：\n" + text_result)
                    return
            yield event.plain_result(text_result)
        except Exception:
            logger.exception("查看抽卡历史记录失败")
            yield event.plain_result("查看抽卡历史记录失败，请稍后重试或联系管理员。")

    @filter.command("卡池详细", priority=1)
    @finish_command
    async def pool_detail(self, event: AstrMessageEvent, pool_identifier: str):
        try:
            found = self._find_pool_config(pool_identifier)
            if isinstance(found, list):
                names = [c.cp_id for c in found]
                yield event.plain_result(
                    f"卡池名称重复，请使用具体 ID：{', '.join(names)}。"
                )
                return
            if found is None:
                yield event.plain_result(f"未找到匹配的卡池: {pool_identifier}")
                return

            rates = found.probability_settings
            included = found.included_item_ids
            lines = [
                f"卡池：{found.name}（ID: {found.cp_id}）",
                f"状态：{'启用' if found.enable else '停用'} · 配置组：{found.config_group}",
                f"保底组：{found.pity_group_id or found.cp_id}",
                f"五星基础概率：{rates.get('base_5star_rate', 0.008):.2%}",
                f"四星基础概率：{rates.get('base_4star_rate', 0.06):.2%}",
                "候选物品：" + "、".join(
                    f"{rarity} {len(included.get(rarity, []))} 件"
                    for rarity in ("3star", "4star", "5star")
                ),
            ]
            text_result = "\n".join(lines)
            if self.enable_rendering:
                try:
                    chain = await self._run_render(
                        self._render_to_chain, self.renderer.render_pool_detail, found
                    )
                    yield event.chain_result(chain)
                    return
                except Exception:
                    logger.exception("卡池详情图片生成失败，已改用文字输出")
            yield event.plain_result(text_result)

        except Exception as e:
            logger.error(f"查询卡池详情失败: {e}")
            yield event.plain_result(f"查询失败: {e}")

    @filter.command("抽卡保底", alias={"保底状态"}, priority=1)
    @finish_command
    async def pity_status(self, event: AstrMessageEvent,
                          pool_identifier: str = ""):
        try:
            pool, error = await self._resolve_pool_config(event, pool_identifier)
            if error:
                yield error
                return
            group = pool.pity_group_id or pool.cp_id
            state = await self.gacha_service.state(
                str(event.get_platform_id()), str(event.get_sender_id()), group
            )
            five_hard = pool.probability_progression.get("5star", {}).get(
                "hard_pity_pull", 80
            )
            four_hard = pool.probability_progression.get("4star", {}).get(
                "hard_pity_pull", 10
            )
            yield event.plain_result(
                f"「{pool.name}」保底状态（组 {group}）\n"
                f"五星：{state['pity_5star']}/{five_hard}，"
                f"UP 保证：{'是' if state['_5star_guaranteed'] else '否'}\n"
                f"四星：{state['pity_4star']}/{four_hard}，"
                f"UP 保证：{'是' if state['_4star_guaranteed'] else '否'}\n"
                f"本组累计抽数：{state['pull_count']}"
            )
        except Exception:
            logger.exception("查询保底状态失败")
            yield event.plain_result("查询保底状态失败，请稍后重试。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("鸣潮诊断", priority=1)
    @finish_command
    async def diagnose(self, event: AstrMessageEvent):
        try:
            schema_version = await self.gacha_service.database_status()
            cache_size = await asyncio.to_thread(self.lf_cache.get_cache_size) if self.lf_cache else 0
            font = "关闭图片" if not self.enable_rendering else self.renderer.font_source
            if self.enable_rendering and font == "unchecked":
                self.renderer._get_font(12)
                font = self.renderer.font_source
            pending = self.render_service.pending if self.render_service else 0
            downloads = self.portrait_service.pending if self.portrait_service else 0
            try:
                host_version = version("AstrBot")
            except PackageNotFoundError:
                host_version = "未知"
            yield event.plain_result(
                f"鸣潮抽卡诊断 · 插件 {self._plugin_version()} · AstrBot {host_version}\n"
                f"数据库：可写 · schema v{schema_version}\n"
                f"数据库任务：{self.gacha_service.pending} / {self.settings.database_queue_limit + 1}\n"
                f"卡池：{len(self._active_pools())} 个启用，revision {self.pool_service.revision}\n"
                f"最近卡池重载错误：{self._last_pool_reload_error or '无'}\n"
                f"字体：{font}\n缓存：{cache_size / 1024 / 1024:.1f} MiB / "
                f"{self.settings.cache_max_mb} MiB\n"
                f"在途渲染：{pending} / "
                f"{self.settings.render_workers + self.settings.render_queue_limit}\n"
                f"立绘准备：{downloads} 项在途，最多 {self.settings.portrait_download_workers} 路下载"
            )
        except Exception:
            logger.exception("鸣潮诊断失败")
            yield event.plain_result("诊断失败，请检查插件日志。")

    @staticmethod
    def _plugin_version() -> str:
        metadata = Path(__file__).with_name("metadata.yaml")
        for line in metadata.read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].split("#", 1)[0].strip()
        return "未知"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重载卡池", alias={"刷新卡池", "reload_pools"}, priority=1)
    @finish_command
    async def reload_pools(self, event: AstrMessageEvent):
        try:
            revision = await self.gacha_service.run(self.pool_service.reload)
            self._last_pool_reload_error = ""
            yield event.plain_result(
                f"已重新加载 {len(self.pool_service.all())} 个卡池配置（版本 {revision}）。"
            )
        except Exception as e:
            self._last_pool_reload_error = str(e)[:200]
            logger.error(f"重载卡池配置失败: {e}")
            yield event.plain_result("重载卡池配置时发生错误。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("认领旧抽卡", priority=1)
    @finish_command
    async def claim_legacy_draws(self, event: AstrMessageEvent,
                                 legacy_user_id: str, target_sender_id: str,
                                 target_platform_id: str = ""):
        """Bind an old sender ID to one explicitly chosen platform user."""
        try:
            platform_id = target_platform_id or str(event.get_platform_id())
            copied = await self.gacha_service.claim_legacy(
                legacy_user_id, platform_id, target_sender_id
            )
            old_key = f"user_default_pool_{hashlib.md5(legacy_user_id.encode()).hexdigest()[:8]}"
            old_pool = await self.get_kv_data(old_key, default=None)
            if old_pool and self.pool_service.get(old_pool):
                await self.put_kv_data(
                    self._user_kv_key(platform_id, target_sender_id), old_pool
                )
            yield event.plain_result(
                f"旧身份 {legacy_user_id} 已归属 {platform_id}/{target_sender_id}；"
                f"迁入 {copied} 条历史。旧保底保存在 legacy_shared 组。"
            )
        except ValueError as exc:
            yield event.plain_result(f"认领失败：{exc}")
        except Exception:
            logger.exception("旧抽卡数据认领失败")
            yield event.plain_result("认领失败，数据未更改，请检查日志。")

    @filter.command("wgs_help", alias={"wwg_help", "抽卡帮助", "鸣潮帮助", "ww抽卡帮助"}, priority=1)
    @finish_command
    async def wgs_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "主入口：/wwg（兼容旧入口 /鸣潮、/ww抽卡）\n"
            "鸣潮模拟抽卡帮助\n"
            "\n"
            "【快速开始】\n"
            "1. /wwg 卡池\n"
            "   查看当前启用的卡池、临时编号和卡池 ID。\n"
            "2. /wwg 选择 <编号|卡池ID|名称>\n"
            "   设置自己的默认卡池；编号来自最近一次 /wwg 卡池。\n"
            "3. /wwg 单抽 [编号|卡池ID|名称]\n"
            "   进行一次抽卡；省略参数时使用个人默认卡池。\n"
            "4. /wwg 十连 [编号|卡池ID|名称]\n"
            "   进行十次抽卡；省略参数时使用个人默认卡池。\n"
            "\n"
            "【抽卡与卡池】\n"
            "/卡池详细 <卡池ID|名称>\n"
            "   查看卡池状态、配置组、保底组、基础概率和各星级候选数量。\n"
            "/wwg 保底 [卡池ID|名称]\n"
            "   查看五星/四星保底进度、UP 保证状态和当前保底组累计抽数。\n"
            "\n"
            "【记录】\n"
            "/wwg 记录 [卡池ID|名称] [页码]\n"
            "   查看自己的抽卡历史，每页 10 条；省略卡池时查看全部卡池。\n"
            "   只填写一个数字时，该数字按页码处理，例如：/wwg 记录 2。\n"
            "   翻页提示会保留卡池筛选，例如：/wwg 记录 limited 2。\n"
            "\n"
            "【管理员指令】\n"
            "/wwg 诊断\n"
            "   查看数据库、卡池、字体、缓存和渲染队列状态。\n"
            "/重载卡池\n"
            "   重新读取磁盘中的卡池配置；别名：/刷新卡池、/reload_pools。\n"
            "/认领旧抽卡 <旧发送者ID> <目标发送者ID> [目标平台实例ID]\n"
            "   将升级前未归属的历史记录和保底绑定到指定用户。\n"
            "\n"
            "【兼容指令】\n"
            "/卡池（/卡池列表、/查看卡池）\n"
            "/唤取 <编号|卡池ID|名称>（/选抽、/设置卡池、/选择卡池）\n"
            "/单抽 [卡池ID|名称]（/单次抽卡、/抽卡、/单次唤取）\n"
            "/十抽 [卡池ID|名称]（/十连、/10抽、/10连）\n"
            "/抽卡记录 [卡池ID|名称] [页码]（/唤取记录、/查看抽卡、/抽卡历史）\n"
            "/抽卡保底 [卡池ID|名称]（/保底状态）\n"
            "/wwg_help（/wgs_help、/抽卡帮助、/ww抽卡帮助、/鸣潮帮助）\n"
            "\n"
            "【使用规则】\n"
            "卡池名称重复时请改用卡池 ID；卡池被停用后不能继续抽取。\n"
            "立绘首次使用时先发文字结果，下载就绪后补发完整图片；缓存齐全时直接发图。\n"
            "下载或渲染失败时保留文字结果，不会重抽；是否保存历史和图片由插件配置决定。\n"
            "管理员可在「资源与离线包」页面提前准备卡池立绘，或导入离线 ZIP。\n"
            "控制台可以禁用或重命名指令，实际名称以 AstrBot 指令管理页为准。\n"
            "示例：/wwg 卡池 → /wwg 选择 1 → /wwg 十连 → /wwg 保底 → /wwg 记录。"
        )

    async def terminate(self):
        if self._closed:
            return
        self._closed = True
        if self.portrait_store is not None:
            self.portrait_store.close()
        if self.admin_api is not None:
            await self.admin_api.shutdown()
        if self.gacha_service is not None:
            await self.gacha_service.close()
            self.gacha_service = None
        if self.portrait_service is not None:
            await self.portrait_service.close()
            self.portrait_service = None
        if self.render_service is not None:
            await self.render_service.close()
            self.render_service = None
        if self.rs_loader is not None:
            self.rs_loader.close()
            self.rs_loader = None
        if self.lf_cache is not None:
            self.lf_cache.stop_scheduled_cleanup()
        if self.cdb is not None:
            self.cdb.close()
        logger.info("鸣潮模拟抽卡插件已卸载")
