"""Bounded execution, shared artwork, pool jobs and validated offline transfers."""

import asyncio
import io
import json
import logging
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile, ZIP_STORED

import pytest
from PIL import Image

from test_phase_c import native as native, FakeRequest


@pytest.fixture(autouse=True)
def astrbot_stub(monkeypatch, tmp_path):
    api = types.ModuleType("astrbot.api")
    star = types.ModuleType("astrbot.api.star")
    api.logger = logging.getLogger("resources")
    star.StarTools = SimpleNamespace(get_data_dir=lambda _: tmp_path)
    for name, module in (("astrbot", types.ModuleType("astrbot")),
                         ("astrbot.api", api), ("astrbot.api.star", star)):
        monkeypatch.setitem(sys.modules, name, module)


def picture():
    buffer = io.BytesIO()
    Image.new("RGBA", (200, 300), (30, 150, 120, 200)).save(buffer, "PNG")
    return buffer.getvalue()


class Loader:
    def __init__(self, content=None):
        self.calls = []
        self.content = content or picture()
        self.block = None
        self.started = threading.Event()

    def download_with_retry(self, url, **kwargs):
        self.calls.append(url)
        self.started.set()
        if self.block:
            self.block.wait(3)
        return self.content


def store_at(tmp_path, loader=None):
    from src.render.local_file_cache_manager import LocalFileCacheManager
    from src.render.portrait_store import PortraitStore
    cache = LocalFileCacheManager(tmp_path / "cache")
    cache.stop_scheduled_cleanup()
    return PortraitStore(tmp_path / "portraits", cache, loader or Loader())


SOURCE = "https://raw.githubusercontent.com/TomyJan/WutheringWaves-UIResources/3.3/a.png"


def test_worker_admission_and_cancellation_retain_real_capacity():
    from src.services.work_queue import WorkQueue, WorkBusy

    async def scenario():
        queue = WorkQueue(workers=1, queue_limit=0)
        started, release = threading.Event(), threading.Event()
        def block():
            started.set()
            release.wait(3)
            return "done"
        task = asyncio.create_task(queue.run(block))
        await asyncio.to_thread(started.wait, 1)
        ticks = []
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks.append(True)
        assert len(ticks) == 5 and queue.pending == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(WorkBusy):
            await queue.run(lambda: "must not run")
        assert queue.pending == 1
        release.set()
        await queue.close()
        await asyncio.sleep(0)
        assert queue.pending == 0

    asyncio.run(scenario())


def test_preview_import_and_render_share_one_download(tmp_path):
    from src.web.portrait_catalog import PortraitCatalog
    from src.render.ui_resources_manager import UIResourceManager
    from src.render.proxy_config import ProxyConfig
    loader = Loader()
    store = store_at(tmp_path, loader)
    catalog = PortraitCatalog(store.root, store=store)
    preview = catalog.preview(SOURCE)
    imported = catalog.import_picture("default", SOURCE)
    renderer = UIResourceManager(loader, store.cache, ProxyConfig(None), portrait_store=store)
    item = SimpleNamespace(portrait_url=SOURCE, external_id="hero", name="角色")
    assert renderer.has_item_portrait(item)
    rendered = renderer.get_item_portrait(item)
    assert rendered.size == (200, 300)
    assert Path(store.root / imported["portrait_url"][6:]).is_file()
    assert preview["data_url"].startswith("data:image/webp;base64,")
    assert len(loader.calls) == 1
    # Reloading a catalog/store keeps both full images and thumbnail cache.
    again = store_at(tmp_path, Loader())
    assert again.preview(SOURCE) == preview and not again.loader.calls


def test_shared_download_coalesces_across_worker_pools_and_legacy_urls(tmp_path):
    loader = Loader()
    loader.block = threading.Event()
    store = store_at(tmp_path, loader)
    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(store.ensure, SOURCE)
        assert loader.started.wait(1)
        second = executor.submit(store.ensure, "https://v6.gh-proxy.org/" + SOURCE)
        loader.block.set()
        assert first.result() == second.result()
    assert len(loader.calls) == 1


def test_offline_package_roundtrip_never_contacts_network(tmp_path):
    from src.services.resource_service import ResourceService
    from src.services.work_queue import WorkQueue
    store = store_at(tmp_path / "online")
    store.ensure(SOURCE)
    pool = SimpleNamespace(cp_id="p", name="测试池")
    online = ResourceService(store, None, None, WorkQueue())
    target = online._export(pool, [{"source": SOURCE, "name": "角色"}])
    offline = store_at(tmp_path / "offline")
    offline.loader.download_with_retry = lambda *args, **kwargs: pytest.fail("offline must not download")
    service = ResourceService(offline, None, None, WorkQueue())
    assert service._import_package(target.read_bytes()) == 1
    assert offline.is_pinned(SOURCE)
    assert offline.preview(SOURCE)["data_url"]
    offline.cache.clear_all_cache()
    assert offline.has(SOURCE) and offline.ensure(SOURCE)
    # A missing legacy local portrait is restored by its source mapping as well.
    local_source = "local:default/old.png"
    local = store.root / "default/old.png"
    local.parent.mkdir(parents=True)
    local.write_bytes(picture())
    local_zip = online._export(pool, [{"source": local_source, "name": "本地角色"}])
    assert service._import_package(local_zip.read_bytes()) == 1
    assert offline.ensure(local_source)
    asyncio.run(online.close())
    asyncio.run(service.close())
    asyncio.run(online.database.close())
    asyncio.run(service.database.close())


@pytest.mark.parametrize("invalid", ["traversal", "checksum", "unlisted", "duplicate", "symlink"])
def test_invalid_packages_publish_nothing(tmp_path, invalid):
    from src.services.resource_service import ResourceService
    from src.services.work_queue import WorkQueue
    store = store_at(tmp_path / "online")
    service = ResourceService(store, None, None, WorkQueue())
    store.ensure(SOURCE)
    original = service._export(SimpleNamespace(cp_id="p", name="P"), [{"source": SOURCE, "name": "N"}])
    with ZipFile(original) as zipped:
        values = {name: zipped.read(name) for name in zipped.namelist()}
    manifest = json.loads(values["manifest.json"])
    if invalid == "checksum":
        manifest["resources"][0]["sha256"] = "0" * 64
    elif invalid == "traversal":
        manifest["resources"][0]["path"] = "../../escape.webp"
        values["../../escape.webp"] = picture()
    elif invalid == "unlisted":
        values["assets/" + "0" * 64 + ".webp"] = picture()
    values["manifest.json"] = json.dumps(manifest).encode()
    corrupt = io.BytesIO()
    with ZipFile(corrupt, "w", compression=ZIP_STORED) as zipped:
        for name, raw in values.items():
            if invalid == "symlink" and name != "manifest.json":
                from zipfile import ZipInfo
                info = ZipInfo(name)
                info.external_attr = 0o120777 << 16
                zipped.writestr(info, raw)
            else:
                zipped.writestr(name, raw)
        if invalid == "duplicate":
            with pytest.warns(UserWarning):
                zipped.writestr("manifest.json", values["manifest.json"])
    destination = store_at(tmp_path / "destination")
    importer = ResourceService(destination, None, None, WorkQueue())
    with pytest.raises(ValueError):
        importer._import_package(corrupt.getvalue())
    assert not list(destination.root.glob("offline/*.webp"))
    asyncio.run(service.close())
    asyncio.run(importer.close())
    asyncio.run(service.database.close())
    asyncio.run(importer.database.close())


def test_admin_io_runs_off_loop_and_busy_returns_429(native, monkeypatch):
    from src.services.work_queue import WorkBusy
    async def scenario():
        owner = threading.get_ident()
        original = native.pools.list_configs
        def checked():
            assert threading.get_ident() != owner
            return original()
        monkeypatch.setattr(native.pools, "list_configs", checked)
        assert (await native.service.list_pools())[0] == 200
        async def busy(*args):
            raise WorkBusy("busy")
        monkeypatch.setattr(native.service.database, "run", busy)
        assert (await native.service.list_pools())[0] == 429
    asyncio.run(scenario())


def test_pool_snapshot_read_does_not_wait_for_writer(native):
    held, release = threading.Event(), threading.Event()
    def writer():
        with native.pools._lock:
            held.set()
            release.wait(2)
    worker = threading.Thread(target=writer)
    worker.start()
    assert held.wait(1)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(native.pools.all)
        try:
            assert result.result(timeout=0.2)[0].cp_id == "sample"
        finally:
            release.set()
    worker.join()


def test_pool_job_progress_failure_retry_and_offline_api(native, monkeypatch):
    from src.services.resource_service import ResourceService
    from src.services.work_queue import WorkBusy
    store = store_at(native.tmp_path)
    resources = ResourceService(store, native.pools, native.items, native.service.database, workers=2)
    native.service.resources = resources
    native.service.portraits.store = store
    second = SOURCE.replace("a.png", "b.png")
    failure = {second}
    original = store.loader.download_with_retry
    def download(source, **kwargs):
        if source in failure:
            return None
        return original(source, **kwargs)
    monkeypatch.setattr(store.loader, "download_with_retry", download)
    # Select exactly two portraits and exclude every other default item.
    one = {"external_id": "one", "name": "角色一", "rarity": "5star", "type": "character", "portrait_url": SOURCE}
    two = dict(one, external_id="two", name="角色二", portrait_url=second)
    native.items._init_tables("default_items")
    native.items.add_item(one, "default_items")
    native.items.add_item(two, "default_items")
    config = dict(native.config, included_item_ids={"5star": ["one", "two"]})
    native.pools.save("sample.json", config, native.pools.revision)

    async def scenario():
        native.module.request = FakeRequest(payload={"pool_id": "sample"})
        status, result = await native.service.prepare_resources()
        assert status == 200
        job = result["job"]
        await resources.tasks[job["id"]]
        done = resources.snapshot(job["id"])
        assert done["total"] == 2 and done["completed"] == 2 and done["state"] == "partial"
        assert done["ready"] == 1 and len(done["failed"]) == 1
        failure.clear()
        retry = await resources.start("sample")
        await resources.tasks[retry["id"]]
        assert resources.snapshot(retry["id"])["state"] == "done"
        assert len(store.loader.calls) == 2
        coverage = await native.service.database.run(resources.describe, "sample")
        assert coverage["pinned"] == coverage["ready"] == coverage["total"] == 2
        package = await resources.export("sample")
        native.module.request = FakeRequest(files={"file": native.Upload(package.read_bytes())})
        status, result = await native.service.import_resources()
        assert status == 200
        await resources.tasks[result["job"]["id"]]
        assert resources.snapshot(result["job"]["id"])["state"] == "done"
        # Limit active jobs before allocating uploaded bytes.
        resources.reserve("import")
        resources.reserve("import")
        with pytest.raises(WorkBusy):
            resources.reserve("import")
        native.module.request = FakeRequest(files={"file": native.Upload(package.read_bytes())})
        assert (await native.service.import_resources())[0] == 429
    asyncio.run(scenario())


def test_queued_draw_rejects_pool_disabled_before_transaction(native):
    from src.services.gacha_service import GachaService
    from src.db.gacha_db_operations import GachaDBOperations
    from src.db.migration import run_migrations
    from src.item_data.item_manager import ItemManager
    from src.gacha.cardpool_manager import CardPoolManager
    from src.services.pool_service import PoolService
    manager = CardPoolManager(native.tmp_path / "active-pools")
    gacha = GachaDBOperations(native.db)
    run_migrations(native.db, native.items, ItemManager(native.items), manager)
    pools = PoolService(manager, native.items)
    active = next(pool for pool in pools.all() if pool.enable)
    service = GachaService(gacha, native.items, True, pools=pools)
    version = pools.version
    started, release = threading.Event(), threading.Event()

    async def scenario():
        def block():
            started.set()
            release.wait(3)
        first = asyncio.create_task(service.run(block))
        await asyncio.to_thread(started.wait, 1)
        draw = asyncio.create_task(service.draw("platform", "user", active, version, 10, "stale-request"))
        await asyncio.sleep(0.01)
        # Simulate another administrator changing the published pool while this
        # message is queued. The draw must recheck before touching pity/history.
        filename = next(entry["filename"] for entry in pools.list_configs() if entry["content"]["cp_id"] == active.cp_id)
        pools.set_enabled(filename, False, pools.revision)
        release.set()
        await first
        with pytest.raises(ValueError, match="排队期间"):
            await draw
        assert await service.history_count("platform", "user", None) == 0
        await service.close()
    asyncio.run(scenario())


def test_shutdown_after_finished_background_jobs_is_repeatable(native):
    from src.services.resource_service import ResourceService

    store = store_at(native.tmp_path)
    resources = ResourceService(store, native.pools, native.items, native.service.database)
    native.service.resources = resources

    async def finish_jobs():
        imported = resources.start_import(b"invalid package")
        await resources.tasks[imported["id"]]
        assert resources.snapshot(imported["id"])["state"] == "failed"
        finished = asyncio.create_task(asyncio.sleep(0, result={"items": []}))
        cancelled = asyncio.create_task(asyncio.sleep(60))
        cancelled.cancel()
        await asyncio.gather(finished, cancelled, return_exceptions=True)
        native.service.catalog_tasks.update(finished=finished, cancelled=cancelled)

    # The fixture and host reload may close a service after its original loop
    # has stopped. Completed resource/catalog tasks must not be re-awaited.
    asyncio.run(finish_jobs())
    asyncio.run(native.service.shutdown())
    asyncio.run(native.service.shutdown())
    assert resources.closed and resources.worker.closed
    assert native.service.database.closed and native.service.resource_worker.closed


def test_shutdown_waits_for_running_download_and_stops_remaining_entries(tmp_path, monkeypatch):
    from src.services.resource_service import ResourceService
    from src.services.work_queue import WorkQueue

    loader = Loader()
    loader.block = threading.Event()
    store = store_at(tmp_path, loader)
    database = WorkQueue()
    service = ResourceService(store, None, None, database, workers=1)
    entries = [{"source": SOURCE.replace("a.png", f"{index}.png"), "name": str(index)}
               for index in range(2)]
    monkeypatch.setattr(service, "selection", lambda _: (SimpleNamespace(cp_id="p", name="P"), entries, 0))

    async def scenario():
        try:
            job = await service.start("p")
            assert await asyncio.to_thread(loader.started.wait, 1)
            shutdown = asyncio.create_task(service.close())
            await asyncio.sleep(0)
            assert service.closed and not shutdown.done()
            loader.block.set()
            await shutdown
            final = service.snapshot(job["id"])
            assert final["state"] == "cancelled" and final["completed"] == final["ready"] == 1
            assert store.is_pinned(entries[0]["source"]) and len(loader.calls) == 1
        finally:
            loader.block.set()
            await service.close()
            await database.close()

    asyncio.run(scenario())


def test_cancel_resource_job_then_retry_keeps_completed_artwork(tmp_path, monkeypatch):
    from src.services.resource_service import ResourceService
    from src.services.work_queue import WorkQueue
    loader = Loader()
    loader.block = threading.Event()
    store = store_at(tmp_path, loader)
    database = WorkQueue()
    service = ResourceService(store, None, None, database, workers=1)
    entries = [{"source": SOURCE.replace("a.png", f"{index}.png"), "name": str(index)}
               for index in range(4)]
    monkeypatch.setattr(service, "selection", lambda _: (SimpleNamespace(cp_id="p", name="P"), entries, 0))

    async def scenario():
        job = await service.start("p")
        assert await asyncio.to_thread(loader.started.wait, 1)
        cancelling = service.cancel(job["id"])
        assert cancelling["cancel"] and cancelling["state"] == "running"
        loader.block.set()
        await service.tasks[job["id"]]
        final = service.snapshot(job["id"])
        assert final["state"] == "cancelled" and final["completed"] == final["ready"] == 1
        retry = await service.start("p")
        await service.tasks[retry["id"]]
        assert service.snapshot(retry["id"])["ready"] == 4
        assert len(loader.calls) == 4
        await service.close()
        await database.close()
    asyncio.run(scenario())
