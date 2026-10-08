"""AstrBot Plugin Pages adapter. The host owns authentication and HTTP serving."""

from __future__ import annotations

import asyncio
import io
import os
import uuid
from pathlib import Path

from PIL import Image, ImageFont, UnidentifiedImageError

from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.api.web import PluginUploadFile, error_response, json_response, request

from ..db.database import CommonDatabase
from ..db.item_db_operations import ItemDBOperations
from ..gacha.cardpool_manager import CardPoolConfig
from ..security import validate_group
from ..services.pool_service import PoolService, RevisionConflict
from .path_security import managed_path
from ..render.image_encoding import encode_portrait
from .portrait_catalog import PortraitCatalog
from .resources_api import ResourceAPI
from ..services.resource_service import ResourceService
from ..services.work_queue import WorkQueue, WorkBusy


PLUGIN_NAME = "astrbot_plugin_ww_gacha_sim"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000


class NativeAdminAPI(ResourceAPI):
    def __init__(self, pool_service: PoolService, item_ops: ItemDBOperations,
                 db: CommonDatabase, cache_manager=None, renderer=None, proxy_config=None,
                 database_worker=None, portrait_store=None, portrait_workers=3):
        self.pools = pool_service
        self.items = item_ops
        self.db = db
        self.cache_manager = cache_manager
        self.renderer = renderer
        self.closed = False
        self.uploads = 0
        self.owns_database = database_worker is None
        self.database = database_worker or WorkQueue(workers=1, queue_limit=32, name="WwAdminDB",
                                                    cleanup=db.close_thread_local_connection)
        self.resource_worker = WorkQueue(workers=4, queue_limit=8, name="WwPreview",
                                         cleanup=db.close_thread_local_connection)
        manager = getattr(renderer, "ui_resource_manager", None)
        proxy = proxy_config or getattr(manager, "proxy_config", None)
        self.portraits = PortraitCatalog(Path(StarTools.get_data_dir(PLUGIN_NAME)) / "portraits",
                                         proxy.get_proxy_dict() if proxy else None, store=portrait_store)
        self.resources = (ResourceService(portrait_store, self.pools, self.items, self.database,
                                         workers=portrait_workers) if portrait_store else None)
        self.catalog_tasks = {}

    def register(self, context):
        routes = (
            ("pools/list", self.list_pools, "GET", "查看卡池"),
            ("pools/validate", self.validate_pool, "POST", "校验卡池"),
            ("pools/save", self.save_pool, "POST", "保存卡池"),
            ("pools/set-enabled", self.set_pool_enabled, "POST", "启用或停用卡池"),
            ("pools/delete", self.delete_pool, "POST", "删除卡池"),
            ("items/list", self.list_items, "GET", "查看物品"),
            ("items/save", self.save_item, "POST", "注册或更新物品"),
            ("items/delete", self.delete_item, "POST", "删除物品"),
            ("portraits/upload/<group>", self.upload_portrait, "POST", "上传立绘"),
            ("health", self.health, "GET", "插件状态"),
            ("resources/versions", self.resource_versions, "GET", "素材版本"),
            ("resources/portraits", self.resource_portraits, "GET", "浏览版本立绘"),
            ("portraits/preview", self.preview_portrait, "POST", "立绘缩略图"),
            ("portraits/import", self.import_portrait, "POST", "保存远程立绘到本地"),
            ("resources/status", self.resource_status, "GET", "卡池资源与任务状态"),
            ("resources/prepare", self.prepare_resources, "POST", "准备卡池资源"),
            ("resources/cancel", self.cancel_resources, "POST", "停止资源准备"),
            ("resources/export", self.export_resources, "GET", "导出离线立绘包"),
            ("resources/import", self.import_resources, "POST", "导入离线立绘包"),
        )
        for route, handler, method, description in routes:
            context.register_web_api(
                f"/{PLUGIN_NAME}/{route}", handler, [method], description
            )

    def close(self):
        self.closed = True
        self.portraits.close()
        for task in self.catalog_tasks.values():
            task.cancel()

    async def shutdown(self):
        self.close()
        if self.resources is not None:
            await self.resources.close()
        pending = []
        for task in self.catalog_tasks.values():
            if not task.done():
                pending.append(task)
            elif not task.cancelled():
                # Consume finished failures without re-awaiting an old loop.
                task.exception()
        await asyncio.gather(*pending, return_exceptions=True)
        await self.resource_worker.close()
        if self.owns_database:
            await self.database.close()
        self.portraits.loader.close()

    @staticmethod
    def _request():
        return request

    @staticmethod
    def _is_upload(upload):
        return isinstance(upload, PluginUploadFile)

    async def _resource_response(self, callback, *args):
        try:
            return json_response(await self.resource_worker.run(callback, *args))
        except WorkBusy as exc:
            return error_response(str(exc), status_code=429)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return error_response(str(exc) if isinstance(exc, ValueError) else "素材暂时不可用，请稍后重试", status_code=400)

    async def resource_versions(self):
        denied = self._guard()
        if denied is not None:
            return denied
        return await self._resource_response(self.portraits.versions)

    async def resource_portraits(self):
        denied = self._guard()
        if denied is not None:
            return denied
        ref = request.query.get("ref", "")
        task = self.catalog_tasks.get(ref)
        if task is None:
            # Directory walks may exceed the host HTTP response timeout. Keep
            # one bounded background job per version; the page polls its result.
            for key, existing in list(self.catalog_tasks.items()):
                if existing.done():
                    self.catalog_tasks.pop(key)
            if len(self.catalog_tasks) >= 4:
                return error_response("正在读取其他版本，请稍后重试", status_code=429)
            task = asyncio.create_task(self._resource_response(self.portraits.catalog, ref))
            self.catalog_tasks[ref] = task
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
        except asyncio.TimeoutError:
            return json_response({"pending": True, "ref": ref})
        self.catalog_tasks.pop(ref, None)
        return result

    async def preview_portrait(self):
        denied = self._guard()
        if denied is not None:
            return denied
        try:
            payload = await self._payload()
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        return await self._resource_response(self.portraits.preview, payload.get("source"))

    async def import_portrait(self):
        denied = self._guard()
        if denied is not None:
            return denied
        try:
            payload = await self._payload()
            group = validate_group(payload.get("group"))
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        return await self._resource_response(self.portraits.import_picture, group, payload.get("source"))

    def _guard(self):
        if self.closed:
            return error_response("插件正在关闭", status_code=503)
        # Extension routes already require AstrBot's plugin scope. Management is
        # additionally restricted to an interactive Dashboard identity.
        username = request.username
        if not username:
            return error_response("请先登录 AstrBot 控制台", status_code=401)
        if username.startswith("api_key:"):
            return error_response("此管理页面只允许控制台登录用户", status_code=403)
        return None

    @staticmethod
    def _field_error(exc: Exception) -> dict[str, str]:
        message = str(exc)
        if "概率" in message or "rate" in message:
            field = "probability_settings"
        elif "保底" in message or "pity" in message:
            field = "probability_progression"
        elif "UP" in message:
            field = "rate_up_item_ids"
        elif "物品" in message or "候选" in message:
            field = "included_item_ids"
        elif "配置组" in message:
            field = "config_group"
        elif "名称" in message:
            field = "name"
        else:
            field = "config"
        return {field: message}

    @staticmethod
    async def _payload() -> dict:
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            raise ValueError("请求必须是 JSON 对象")
        return payload

    @staticmethod
    def _revision(payload: dict) -> int:
        value = payload.get("expected_revision")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("缺少有效的 expected_revision")
        return value

    def _candidate(self, content: dict, filename: str = "") -> CardPoolConfig:
        if not isinstance(content, dict):
            raise ValueError("卡池内容必须是对象")
        data = dict(content)
        if not data.get("cp_id"):
            if not filename or not data.get("name"):
                raise ValueError("新卡池需要名称和文件标识")
            path = managed_path(self.pools.config_dir, filename, suffix=".json")
            relative = path.relative_to(self.pools.config_dir).with_suffix("").as_posix()
            data["cp_id"] = self.pools.manager._generate_cp_id(relative, data["name"])
        return CardPoolConfig.from_dict(data)

    async def list_pools(self):
        if denied := self._guard():
            return denied
        def work():
            pools = [dict(entry, filename=f'{entry["filename"]}.json')
                     for entry in self.pools.list_configs()]
            return json_response({"revision": self.pools.revision,
                                  "version": self.pools.version,
                                  "pools": pools})
        try:
            return await self.database.run(work)
        except WorkBusy as exc:
            return error_response(str(exc), status_code=429)

    async def validate_pool(self):
        if denied := self._guard():
            return denied
        try:
            payload = await self._payload()
            def work():
                candidate = self._candidate(payload.get("content"), payload.get("filename", ""))
                self.pools.validate(candidate)
                return json_response({"valid": True, "errors": {}})
            return await self.database.run(work)
        except WorkBusy as exc:
            return error_response(str(exc), status_code=429)
        except (ValueError, TypeError, KeyError) as exc:
            return json_response({"valid": False, "errors": self._field_error(exc)})

    async def save_pool(self):
        if denied := self._guard():
            return denied
        try:
            payload = await self._payload()
            def work():
                filename = payload.get("filename")
                content = payload.get("content")
                revision = self._revision(payload)
                if not isinstance(filename, str) or not isinstance(content, dict):
                    raise ValueError("缺少文件标识或卡池内容")
                candidate = self._candidate(content, filename)
                if candidate.enable:
                    self.pools.validate(candidate)
                revision = self.pools.save(filename, content, revision)
                return json_response({"revision": revision, "version": self.pools.version,
                                      "pool": self.pools.get(candidate.cp_id).to_dict()})
            return await self.database.run(work)
        except WorkBusy as exc:
            return error_response(str(exc), status_code=429)
        except RevisionConflict as exc:
            return error_response(str(exc), status_code=409)
        except (ValueError, TypeError, KeyError) as exc:
            return error_response(str(exc), status_code=400,
                                  data={"errors": self._field_error(exc)})
        except Exception:
            logger.exception("原生页面保存卡池失败")
            return error_response("保存失败，请检查插件日志", status_code=500)

    async def set_pool_enabled(self):
        if denied := self._guard():
            return denied
        try:
            payload = await self._payload()
            def work():
                filename = payload.get("filename")
                enabled = payload.get("enabled")
                if not isinstance(filename, str) or not isinstance(enabled, bool):
                    raise ValueError("filename 与 enabled 无效")
                revision = self.pools.set_enabled(filename, enabled,
                                                  self._revision(payload))
                return json_response({"revision": revision, "version": self.pools.version})
            return await self.database.run(work)
        except WorkBusy as exc:
            return error_response(str(exc), status_code=429)
        except RevisionConflict as exc:
            return error_response(str(exc), status_code=409)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return error_response(str(exc), status_code=400)

    async def delete_pool(self):
        if denied := self._guard():
            return denied
        try:
            payload = await self._payload()
            def work():
                filename = payload.get("filename")
                if not isinstance(filename, str):
                    raise ValueError("filename 无效")
                revision = self.pools.delete(filename, self._revision(payload))
                return json_response({"revision": revision, "version": self.pools.version})
            return await self.database.run(work)
        except WorkBusy as exc:
            return error_response(str(exc), status_code=429)
        except RevisionConflict as exc:
            return error_response(str(exc), status_code=409)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return error_response(str(exc), status_code=400)

    def _table(self, group: str) -> str:
        group = validate_group(group)
        if group not in {pool.config_group for pool in self.pools.all()}:
            raise ValueError("未知的物品配置组，请先创建对应卡池")
        return f"{group}_items"

    def _check_revision(self, payload: dict) -> None:
        if self._revision(payload) != self.pools.revision:
            raise RevisionConflict("卡池或物品版本已变化，请刷新后重试")

    def _references(self, group: str, item_id: str) -> list[str]:
        return [pool.name for pool in self.pools.all()
                if pool.config_group == group and (
                    any(item_id in ids for ids in pool.included_item_ids.values())
                    or any(item_id in ids for ids in pool.rate_up_item_ids.values())
                )]

    def _restore_item(self, table: str, item: dict) -> None:
        self.db.execute_update(
            f"INSERT OR REPLACE INTO {table} "
            "(external_id,name,rarity,type,affiliated_type,portrait_path,"
            "portrait_url,apply_gradient) VALUES (?,?,?,?,?,?,?,?)",
            (item["external_id"], item["name"], item["rarity"], item["type"],
             item.get("affiliated_type", ""), item.get("portrait_path", ""),
             item.get("portrait_url", ""), int(item.get("apply_gradient", False))),
        )

    async def list_items(self):
        if denied := self._guard():
            return denied
        try:
            group = request.query.get("group", "default")
            page = request.query.get("page", 1, type=int)
            page_size = request.query.get("page_size", 30, type=int)
            def work():
                if not isinstance(page, int) or page < 1 or not isinstance(page_size, int) or not 1 <= page_size <= 100:
                    raise ValueError("分页参数无效")
                rows = self.items.get_items_list(self._table(group))
                start = (page - 1) * page_size
                return json_response({"items": rows[start:start + page_size],
                                      "total": len(rows), "page": page,
                                      "page_size": page_size, "group": group})
            return await self.database.run(work)
        except WorkBusy as exc:
            return error_response(str(exc), status_code=429)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)

    @staticmethod
    def _item_data(payload: dict) -> dict:
        allowed = ("external_id", "name", "rarity", "type", "affiliated_type",
                   "portrait_url")
        data = {key: payload[key] for key in allowed if key in payload}
        for key in ("name", "rarity", "type"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ValueError(f"{key} 不能为空")
        if data["rarity"] not in ("3star", "4star", "5star"):
            raise ValueError("rarity 必须是 3star、4star 或 5star")
        if data["type"] not in ("character", "weapon"):
            raise ValueError("type 必须是 character 或 weapon")
        for key, value in data.items():
            if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value):
                raise ValueError(f"{key} 无效")
        portrait = data.get("portrait_url")
        if portrait:
            if portrait.startswith("local:"):
                if not portrait.endswith((".png", ".webp")):
                    raise ValueError("受管立绘必须是 PNG 或 WebP 文件")
                managed_path(Path(StarTools.get_data_dir(PLUGIN_NAME)) / "portraits",
                             portrait.removeprefix("local:"))
            elif not portrait.startswith(("https://", "http://")):
                raise ValueError("立绘地址必须是受管资源或 HTTP(S) 地址")
        return data

    async def save_item(self):
        if denied := self._guard():
            return denied
        try:
            payload = await self._payload()
            def work():
                group = validate_group(payload.get("group"))
                table = self._table(group)
                data = self._item_data(payload.get("item", {}))
                portrait = data.get("portrait_url", "")
                if portrait.startswith("local:"):
                    relative = portrait.removeprefix("local:")
                    if not relative.startswith(group + "/") or not managed_path(
                        Path(StarTools.get_data_dir(PLUGIN_NAME)) / "portraits", relative
                    ).is_file():
                        raise ValueError("受管立绘不存在或不属于当前配置组")
                item_id = data.get("external_id")
                with self.pools._lock:
                    self._check_revision(payload)
                    current = self.items.get_item_by_id(item_id, table) if item_id else None
                    if current:
                        if (data["rarity"] != current["rarity"] or data["type"] != current["type"]) and self._references(group, item_id):
                            raise ValueError("物品已被卡池引用，不能改变稀有度或类型")
                        self.db.execute_update(
                            f"UPDATE {table} SET name=?,rarity=?,type=?,affiliated_type=?,portrait_url=? WHERE external_id=?",
                            (data["name"], data["rarity"], data["type"],
                             data.get("affiliated_type", ""), data.get("portrait_url", ""), item_id),
                        )
                    else:
                        if not self.items.add_item(data, table):
                            raise ValueError("物品注册失败，外部 ID 可能重复")
                    try:
                        self.pools.reload()
                    except BaseException:
                        if current:
                            self._restore_item(table, current)
                        else:
                            self.db.execute_update(
                                f"DELETE FROM {table} WHERE external_id=?",
                                (data["external_id"],),
                            )
                        raise
                return json_response({"saved": True, "external_id": data["external_id"],
                                      "revision": self.pools.revision})
            return await self.database.run(work)
        except WorkBusy as exc:
            return error_response(str(exc), status_code=429)
        except RevisionConflict as exc:
            return error_response(str(exc), status_code=409)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            logger.exception("原生页面保存物品失败")
            return error_response("物品保存失败，请检查插件日志", status_code=500)

    async def delete_item(self):
        if denied := self._guard():
            return denied
        try:
            payload = await self._payload()
            def work():
                group = validate_group(payload.get("group"))
                item_id = payload.get("external_id")
                if not isinstance(item_id, str) or not item_id:
                    raise ValueError("external_id 无效")
                table = self._table(group)
                with self.pools._lock:
                    self._check_revision(payload)
                    referenced = self._references(group, item_id)
                    if referenced:
                        raise ValueError(f"物品仍被卡池引用：{', '.join(referenced[:3])}")
                    current = self.items.get_item_by_id(item_id, table)
                    if not current:
                        raise ValueError("物品不存在")
                    if not self.items.delete_item(item_id, table):
                        raise ValueError("物品不存在或删除失败")
                    try:
                        self.pools.reload()
                    except BaseException:
                        self._restore_item(table, current)
                        raise
                return json_response({"deleted": True, "revision": self.pools.revision})
            return await self.database.run(work)
        except WorkBusy as exc:
            return error_response(str(exc), status_code=429)
        except RevisionConflict as exc:
            return error_response(str(exc), status_code=409)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            logger.exception("原生页面删除物品失败")
            return error_response("物品删除失败，请检查插件日志", status_code=500)

    async def upload_portrait(self, group: str):
        if denied := self._guard():
            return denied
        if self.uploads >= 2:
            return error_response("正在处理其他图片上传，请稍后重试", status_code=429)
        self.uploads += 1
        try:
            group = validate_group(group)
            self._table(group)
            upload = (await request.files()).get("file")
            if not isinstance(upload, PluginUploadFile):
                raise ValueError("缺少图片文件")
            if upload.content_length and upload.content_length > MAX_UPLOAD_BYTES:
                raise ValueError("图片超过 10 MiB")
            raw = await upload.read(MAX_UPLOAD_BYTES + 1)
            if len(raw) > MAX_UPLOAD_BYTES:
                raise ValueError("图片超过 10 MiB")
            def work():
                with Image.open(io.BytesIO(raw)) as image:
                    if image.format not in ("PNG", "JPEG", "WEBP"):
                        raise ValueError("仅支持 PNG、JPEG 和 WebP")
                    if image.width * image.height > MAX_IMAGE_PIXELS:
                        raise ValueError("图片像素超过 2000 万")
                compact = encode_portrait(raw)
                root = Path(StarTools.get_data_dir(PLUGIN_NAME)) / "portraits"
                token = uuid.uuid4().hex
                target = managed_path(root, f"{group}/{token}.webp")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(".tmp")
                try:
                    temporary.write_bytes(compact)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                return json_response({"portrait_url": f"local:{group}/{token}.webp"})
            return await self.resource_worker.run(work)
        except WorkBusy as exc:
            return error_response(str(exc), status_code=429)
        except (ValueError, UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            return error_response(str(exc), status_code=400)
        finally:
            self.uploads -= 1

    async def health(self):
        if denied := self._guard():
            return denied
        try:
            def work():
                self.db.execute_query_single("SELECT 1 AS ok")
                cache = ("disabled" if self.cache_manager is None else
                         "ok" if self.cache_manager.cache_dir.is_dir() else "missing")
                font = ("disabled" if self.renderer is None else
                        "system" if isinstance(self.renderer._get_font(12),
                                               ImageFont.FreeTypeFont) else "fallback")
                return json_response({"database": "ok",
                                      "schema_version": self.db.get_schema_version(),
                                      "pool_count": len(self.pools.all()),
                                      "revision": self.pools.revision,
                                      "version": self.pools.version,
                                      "cache": cache, "font": font,
                                      "database_pending": self.database.pending,
                                      "database_capacity": getattr(self.database, "worker", self.database).capacity})
            return await self.database.run(work)
        except WorkBusy as exc:
            return error_response(str(exc), status_code=429)
        except Exception:
            logger.exception("原生页面健康检查失败")
            return error_response("数据库检查失败", status_code=503)
