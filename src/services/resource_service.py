"""Pool artwork jobs and bounded, validated offline artwork packages."""

import asyncio
import hashlib
import io
import json
import re
import stat
import tempfile
import time
import uuid
from pathlib import Path
from zipfile import BadZipFile, ZIP_STORED, ZipFile

from PIL import Image

from .work_queue import WorkQueue, WorkBusy
from ..item_data.item_manager import ItemManager


MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_RESOURCES = 500


class ResourceService:
    def __init__(self, store, pools, item_ops, database_worker, workers=3):
        self.store, self.pools, self.items, self.database = store, pools, item_ops, database_worker
        self.worker = WorkQueue(workers=workers, queue_limit=8, name="WwResources")
        self.parallel = workers
        self.jobs = {}
        self.tasks = {}
        self.closed = False
        self.root = store.root.parent / "resource_packages"

    def selection(self, pool_id):
        pool = self.pools.get(pool_id)
        if pool is None:
            raise ValueError("卡池不存在，请刷新列表")
        manager = ItemManager(self.items, pool.config_group)
        objects = manager.get_item_objects()
        wanted = set()
        if not isinstance(pool.included_item_ids, dict) or not isinstance(pool.rate_up_item_ids, dict):
            raise ValueError("卡池物品配置格式无效，请先修正卡池")
        for rarity in ("3star", "4star", "5star"):
            ids = pool.included_item_ids.get(rarity, [])
            up_ids = pool.rate_up_item_ids.get(rarity, [])
            if (not isinstance(ids, list) or not isinstance(up_ids, list)
                    or any(not isinstance(key, str) for key in ids + up_ids)):
                raise ValueError("卡池物品列表无效，请先修正卡池")
            wanted.update(ids)
            wanted.update(up_ids)
        missing = wanted - objects.keys()
        if missing:
            raise ValueError("卡池引用了不存在的物品，请先修正卡池")
        selected = {}
        empty = 0
        for key in sorted(wanted):
            item = objects[key]
            if not item.portrait_url:
                empty += 1
                continue
            source = self.store.source(item.portrait_url)
            selected.setdefault(source, {"source": source, "name": item.name})
        if len(selected) > MAX_RESOURCES:
            raise ValueError("单个资源包最多支持 500 张不同立绘，请拆分卡池")
        return pool, list(selected.values()), empty

    def describe(self, pool_id):
        pool, entries, empty = self.selection(pool_id)
        entries = [dict(row, ready=self.store.has(row["source"]),
                        pinned=self.store.is_pinned(row["source"])) for row in entries]
        ready = sum(row["ready"] for row in entries)
        pinned = sum(row["pinned"] for row in entries)
        return {"pool_id": pool.cp_id, "name": pool.name, "total": len(entries),
                "ready": ready, "pinned": pinned, "without_portrait": empty, "items": entries}

    def snapshot(self, job_id):
        job = self.jobs.get(job_id)
        if job is None:
            raise ValueError("资源任务不存在或已过期")
        return dict(job)

    def latest(self, pool_id):
        jobs = [job for job in self.jobs.values() if job["pool_id"] == pool_id]
        return self.snapshot(jobs[-1]["id"]) if jobs else None

    def reserve(self, kind, pool_id="", name="离线资源包"):
        if self.closed or sum(job["state"] == "running" for job in self.jobs.values()) >= 2:
            raise WorkBusy("已有两个资源任务运行，请稍后再试")
        for key in list(self.jobs):
            if len(self.jobs) < 8:
                break
            if key not in self.tasks or self.tasks[key].done():
                self.jobs.pop(key)
                self.tasks.pop(key, None)
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = {"id": job_id, "kind": kind, "pool_id": pool_id,
                             "name": name, "state": "running", "total": 0,
                             "completed": 0, "ready": 0, "failed": [], "cancel": False,
                             "started_at": time.time()}
        return self.jobs[job_id]

    async def start(self, pool_id):
        for job in self.jobs.values():
            if job["pool_id"] == pool_id and job["state"] == "running":
                return self.snapshot(job["id"])
        pool, entries, empty = await self.database.run(self.selection, pool_id)
        for job in self.jobs.values():
            if job["pool_id"] == pool.cp_id and job["state"] == "running":
                return self.snapshot(job["id"])
        job = self.reserve("prepare", pool.cp_id, pool.name)
        job.update(total=len(entries), without_portrait=empty)
        self.tasks[job["id"]] = asyncio.create_task(self._prepare(job, entries))
        return self.snapshot(job["id"])

    async def _prepare(self, job, entries):
        queue = iter(entries)

        async def consume():
            for entry in queue:
                if job["cancel"] or self.closed:
                    return
                try:
                    await self.worker.run(self.store.pin, entry["source"])
                    job["ready"] += 1
                except Exception as exc:
                    job["failed"].append({"name": entry["name"], "message": str(exc)[:240]})
                finally:
                    job["completed"] += 1

        try:
            await asyncio.gather(*(consume() for _ in range(self.parallel)))
            job["state"] = "cancelled" if job["cancel"] or self.closed else "partial" if job["failed"] else "done"
        except Exception as exc:
            job["state"] = "failed"
            job["failed"].append({"name": "资源准备", "message": str(exc)[:240]})

    def cancel(self, job_id):
        self.snapshot(job_id)
        self.jobs[job_id]["cancel"] = True
        return self.snapshot(job_id)

    async def export(self, pool_id):
        pool, entries, empty = await self.database.run(self.selection, pool_id)
        if not entries:
            raise ValueError("当前卡池没有配置立绘，无法导出")
        return await self.worker.run(self._export, pool, entries)

    def _export(self, pool, entries):
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / ("wwg-resources-" + uuid.uuid4().hex + ".zip")
        records = []
        try:
            with ZipFile(target, "x", compression=ZIP_STORED) as package:
                for entry in entries:
                    raw = self.store.cached_bytes(entry["source"])
                    if raw is None:
                        raise ValueError("还有立绘未就绪，请先点击「准备卡池资源」")
                    # Local PNG and old caches are compacted without network access.
                    from ..render.image_encoding import encode_portrait
                    with Image.open(io.BytesIO(raw)) as image:
                        compact = raw if image.format == "WEBP" and max(image.size) <= 1280 else encode_portrait(raw)
                    filename = "assets/" + self.store.digest(entry["source"]) + ".webp"
                    package.writestr(filename, compact)
                    records.append(dict(entry, path=filename, size=len(compact),
                                        sha256=hashlib.sha256(compact).hexdigest()))
                    if target.stat().st_size > MAX_PACKAGE_BYTES - 1024 * 1024:
                        raise ValueError("离线资源包超过 64 MiB，请拆分卡池")
                manifest = {"format": "wwg-artwork", "version": 1,
                            "pool": {"id": pool.cp_id, "name": pool.name}, "resources": records}
                package.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False).encode())
            # Keep only a few recent exports. Never remove files served now: retain
            # every export younger than one hour, and prune only this owned pattern.
            for old in sorted(self.root.glob("wwg-resources-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)[4:]:
                if old.stat().st_mtime < time.time() - 3600 and old.resolve().is_relative_to(self.root.resolve()):
                    old.unlink(missing_ok=True)
            return target
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def start_import(self, raw, job=None):
        if self.closed:
            raise WorkBusy("资源服务正在关闭")
        job = job or self.reserve("import")
        self.tasks[job["id"]] = asyncio.create_task(self._import(job, raw))
        return self.snapshot(job["id"])

    async def _import(self, job, raw):
        try:
            count = await self.worker.run(self._import_package, raw)
            job.update(total=count, completed=count, ready=count, state="done")
        except Exception as exc:
            job.update(state="failed", failed=[{"name": "离线包", "message": str(exc)[:240]}])

    def _import_package(self, raw):
        if len(raw) > MAX_PACKAGE_BYTES:
            raise ValueError("离线资源包超过 64 MiB")
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with ZipFile(io.BytesIO(raw)) as package, tempfile.TemporaryDirectory(dir=self.root) as staging:
                infos = package.infolist()
                names = [info.filename for info in infos]
                if (len(names) != len(set(names)) or len(names) > MAX_RESOURCES + 1
                        or "manifest.json" not in names):
                    raise ValueError("资源包目录无效或重复")
                if sum(info.file_size for info in infos) > MAX_EXPANDED_BYTES:
                    raise ValueError("资源包解压体积超过 256 MiB")
                for info in infos:
                    if (info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16)
                            or info.file_size > (1024 * 1024 if info.filename == "manifest.json" else 10 * 1024 * 1024)
                            or (info.filename != "manifest.json" and not re.fullmatch(r"assets/[0-9a-f]{64}\.webp", info.filename))):
                        raise ValueError("资源包包含无效文件")
                manifest = json.loads(package.read("manifest.json"))
                entries = manifest.get("resources")
                if (manifest.get("format") != "wwg-artwork" or manifest.get("version") != 1
                        or not isinstance(entries, list) or not 1 <= len(entries) <= MAX_RESOURCES):
                    raise ValueError("不支持的离线资源包格式")
                expected = {"manifest.json"}
                validated = []
                for entry in entries:
                    source = self.store.source(entry["source"])
                    filename = "assets/" + self.store.digest(source) + ".webp"
                    if entry.get("path") != filename or filename in expected:
                        raise ValueError("资源包立绘映射无效或重复")
                    expected.add(filename)
                    content = package.read(filename)
                    if len(content) != entry.get("size") or hashlib.sha256(content).hexdigest() != entry.get("sha256"):
                        raise ValueError("资源包校验失败，文件可能损坏")
                    with Image.open(io.BytesIO(content)) as picture:
                        if picture.format != "WEBP" or max(picture.size) > 1280:
                            raise ValueError("资源包图片格式或尺寸无效")
                        picture.load()
                    path = Path(staging) / (self.store.digest(source) + ".webp")
                    path.write_bytes(content)
                    validated.append((source, path))
                if expected != set(names):
                    raise ValueError("资源包包含未登记文件")
                # Verify the entire package before publishing any portrait. No ZIP
                # path is ever used as an extraction destination or network URL.
                for source, path in validated:
                    self.store.publish(source, path.read_bytes())
                return len(validated)
        except (BadZipFile, KeyError, TypeError, AttributeError, UnicodeError) as exc:
            raise ValueError("离线资源包无效，请选择本插件导出的 ZIP") from exc

    async def close(self):
        self.closed = True
        for job in self.jobs.values():
            job["cancel"] = True
        # Finished jobs may belong to a previous, already closed event loop.
        # Only running jobs need draining; Python 3.14 rejects gathering tasks
        # from a different loop even when they have already finished.
        pending = [task for task in self.tasks.values() if not task.done()]
        await asyncio.gather(*pending, return_exceptions=True)
        await self.worker.close()
