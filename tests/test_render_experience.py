"""Cold-cache replies, bounded downloads and compact resource regressions."""

import ast
import asyncio
import io
import logging
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image


@pytest.fixture(autouse=True)
def astrbot_stub(monkeypatch, tmp_path):
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    star = types.ModuleType("astrbot.api.star")
    api.logger = logging.getLogger("render_experience")
    star.StarTools = SimpleNamespace(get_data_dir=lambda _: tmp_path)
    for name, module in (("astrbot", astrbot), ("astrbot.api", api), ("astrbot.api.star", star)):
        monkeypatch.setitem(sys.modules, name, module)


class BlockingResources:
    def __init__(self, expected=1, success=True):
        self.ready = set()
        self.calls = []
        self.expected = expected
        self.success = success
        self.lock = threading.Lock()
        self.started = threading.Event()
        self.release = threading.Event()

    def portrait_cache_key(self, item):
        return item.external_id

    def has_item_portrait(self, item):
        return item.external_id in self.ready

    def download_item_portrait(self, item, stop):
        with self.lock:
            self.calls.append(item.external_id)
            if len(self.calls) >= self.expected:
                self.started.set()
        self.release.wait(3)
        if self.success:
            self.ready.add(item.external_id)
        return self.success


def art(name):
    return SimpleNamespace(external_id=name, portrait_url=f"https://example.org/{name}.png")


def test_parallel_downloads_merge_duplicates_and_concurrent_chats():
    from src.services.portrait_service import PortraitService

    async def scenario():
        resources = BlockingResources(expected=2)
        service = PortraitService(resources, workers=2)
        try:
            first = asyncio.create_task(service.prepare([art("a")] * 10 + [art("b")]))
            assert await asyncio.to_thread(resources.started.wait, 2)
            second = asyncio.create_task(service.prepare([art("a")]))
            await asyncio.sleep(0)
            assert service.pending == 2 and sorted(resources.calls) == ["a", "b"]
            resources.release.set()
            await asyncio.gather(first, second)
            assert service.pending == 0
            await service.prepare([art("a"), art("b")])
            assert len(resources.calls) == 2
        finally:
            resources.release.set()
            await service.close()

    asyncio.run(scenario())


def test_download_timeout_keeps_capacity_and_finishes_shared_cache():
    from src.services.portrait_service import PortraitService, PortraitUnavailable

    async def scenario():
        resources = BlockingResources()
        service = PortraitService(resources, workers=1, queue_limit=0)
        try:
            with pytest.raises(PortraitUnavailable, match="等待超时"):
                await service.prepare([art("a")], timeout_seconds=0.03)
            assert service.pending == 1
            with pytest.raises(PortraitUnavailable, match="队列繁忙"):
                await service.prepare([art("b")])
            resources.release.set()
            await asyncio.wait_for(asyncio.gather(*service.tasks.values()), 2)
            await service.prepare([art("a")])
            assert resources.calls == ["a"] and service.pending == 0
        finally:
            resources.release.set()
            await service.close()

    asyncio.run(scenario())


def test_failed_source_cooldown_prevents_repeated_downloads():
    from src.services.portrait_service import PortraitService, PortraitUnavailable

    async def scenario():
        resources = BlockingResources(success=False)
        resources.release.set()
        service = PortraitService(resources)
        try:
            for _ in range(2):
                with pytest.raises(PortraitUnavailable):
                    await service.prepare([art("a")])
            assert resources.calls == ["a"]
        finally:
            await service.close()

    asyncio.run(scenario())


def test_render_reads_cache_only_and_download_cache_is_compact_and_persistent(tmp_path):
    from src.render.local_file_cache_manager import LocalFileCacheManager
    from src.render.ui_resources_manager import UIResourceManager

    image = Image.effect_noise((1800, 1400), 60).convert("RGBA")
    image.putalpha(200)
    output = io.BytesIO()
    image.save(output, "PNG")
    raw = output.getvalue()
    calls = []
    downloader = SimpleNamespace(download_with_retry=lambda url, **kw: calls.append(url) or raw)
    cache = LocalFileCacheManager(tmp_path / "cache")
    manager = UIResourceManager(downloader, cache, SimpleNamespace(get_proxy_dict=lambda: None))
    item = art("hero")
    item.name = "角色"
    try:
        with pytest.raises(ValueError, match="尚未准备"):
            manager.get_item_portrait(item)
        assert calls == []
        assert manager.download_item_portrait(item)
        path = cache.get_cached_file_path(manager.portrait_cache_key(item))
        assert path.stat().st_size < len(raw)
        with Image.open(path) as compact:
            assert compact.format == "WEBP" and max(compact.size) <= 1280
            assert compact.getchannel("A").getextrema() == (200, 200)
        metadata = cache.cache_meta[manager.portrait_cache_key(item)]
        assert metadata["expires_at"] - metadata["created_at"] == 30 * 86400
        assert manager.get_item_portrait(item).size == (1280, 996)
        assert len(calls) == 1
    finally:
        cache.stop_scheduled_cleanup()
    reopened = LocalFileCacheManager(tmp_path / "cache")
    try:
        assert reopened.get_cached_image(manager.portrait_cache_key(item)) is not None
    finally:
        reopened.stop_scheduled_cleanup()


def test_result_encoder_preserves_transparency_and_uses_jpeg_for_opaque_output():
    from src.render.image_encoding import encode_result_image

    raw, suffix = encode_result_image(Image.new("RGBA", (32, 32), (20, 30, 40, 255)))
    assert suffix == "jpg" and Image.open(io.BytesIO(raw)).format == "JPEG"
    raw, suffix = encode_result_image(Image.new("RGBA", (32, 32), (20, 30, 40, 128)))
    decoded = Image.open(io.BytesIO(raw))
    assert suffix == "png" and decoded.getchannel("A").getextrema() == (128, 128)


def test_legacy_png_cache_is_reused_and_shared_urls_have_one_new_key(tmp_path):
    import hashlib
    from src.render.local_file_cache_manager import LocalFileCacheManager
    from src.render.ui_resources_manager import UIResourceManager

    cache = LocalFileCacheManager(tmp_path / "cache")
    manager = UIResourceManager(
        SimpleNamespace(download_with_retry=lambda *args, **kwargs: pytest.fail("cached art must not download")),
        cache, SimpleNamespace(get_proxy_dict=lambda: None),
    )
    first, second = art("first"), art("second")
    second.portrait_url = first.portrait_url
    assert manager.portrait_cache_key(first) == manager.portrait_cache_key(second)
    legacy = hashlib.md5(f"{first.external_id}\0{first.portrait_url}".encode()).hexdigest()
    first.name = "旧缓存"
    try:
        cache.cache_image(Image.new("RGBA", (20, 20), "teal"), legacy)
        assert manager.has_item_portrait(first)
        assert manager.get_item_portrait(first).size == (20, 20)
    finally:
        cache.stop_scheduled_cleanup()


def test_download_pool_reuses_connections_and_prefers_successful_mirror(monkeypatch):
    from src.render.resource_loader import ResourceLoader

    clients, calls = [], []

    class Response:
        def __init__(self, code):
            self.status_code = code

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_bytes(self):
            yield b"image"

    class Client:
        def __init__(self, **kwargs):
            clients.append(self)
            self.closed = False
            assert kwargs["verify"] and not kwargs["follow_redirects"]

        def stream(self, method, url, **kwargs):
            calls.append(url)
            return Response(503 if "raw.githubusercontent.com" in url else 200)

        def close(self):
            self.closed = True

    monkeypatch.setattr("src.render.resource_loader.httpx.Client", Client)
    monkeypatch.setattr(ResourceLoader, "is_valid_resource_url", lambda *_: True)
    loader = ResourceLoader(reuse_connections=True)
    source = "https://raw.githubusercontent.com/TomyJan/WutheringWaves-UIResources/3.3/"
    try:
        assert loader.download_with_retry(source + "one.png", max_retries=1) == b"image"
        assert loader.download_with_retry(source + "two.png", max_retries=1) == b"image"
        assert len(clients) == 1
        assert "cdn.jsdelivr.net" in calls[-1] and len(calls) == 3
    finally:
        loader.close()
    assert clients[0].closed


def _handler(command):
    from src.services.portrait_service import PortraitUnavailable
    from src.services.work_queue import WorkBusy

    source = Path(__file__).resolve().parents[1] / "main.py"
    plugin = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                  if isinstance(node, ast.ClassDef))
    methods = [node for node in plugin.body if getattr(node, "name", None)
               in {command, "_rarity_stars", "_draw_response"}]
    for method in methods:
        if method.name != "_rarity_stars":
            method.decorator_list = []
    tree = ast.Module(body=[ast.ClassDef(name="Handler", bases=[], keywords=[],
                      body=methods, decorator_list=[])], type_ignores=[])
    namespace = {"AstrMessageEvent": object, "PortraitUnavailable": PortraitUnavailable,
                 "logger": logging.getLogger("draw_response"), "GachaBusy": WorkBusy}
    exec(compile(ast.fix_missing_locations(tree), str(source), "exec"), namespace)
    handler = namespace["Handler"]()
    item = SimpleNamespace(name="测试物品", rarity="5star")
    handler.enable_rendering = True
    handler.settings = SimpleNamespace(portrait_wait_timeout_seconds=1)
    handler.renderer = SimpleNamespace(render_single_pull=None, render_ten_pulls=None)
    handler._render_and_encode = lambda *args, **kwargs: ["IMAGE"]
    handler._resolve_pool_config = AsyncMock(return_value=(SimpleNamespace(cp_id="pool"), None))
    handler.pool_service = SimpleNamespace(get_with_version=lambda _: (
        SimpleNamespace(cp_id="pool", enable=True), "revision"))
    handler.gacha_service = SimpleNamespace(draw=AsyncMock(
        return_value={"item_obj": item} if command == "single_pull" else [item] * 10))
    handler._request_key = lambda *args: "draw-receipt"
    event = SimpleNamespace(get_sender_id=lambda: "user", get_platform_id=lambda: "platform",
                            get_sender_name=lambda: "user", plain_result=lambda text: text,
                            chain_result=lambda chain: chain)
    return handler, event


@pytest.mark.parametrize("command", ["single_pull", "ten_pulls"])
def test_cold_draw_sends_text_before_download_and_does_not_spend_render_budget(command):
    from src.services.render_service import RenderService

    async def scenario():
        handler, event = _handler(command)
        started, release = asyncio.Event(), asyncio.Event()

        async def prepare(*args, **kwargs):
            started.set()
            await release.wait()

        handler.portrait_service = SimpleNamespace(missing=lambda _: ["missing"], prepare=prepare)
        render = RenderService(workers=1, timeout_seconds=0.15)
        handler._run_render = render.run
        generator = getattr(handler, command)(event)
        try:
            first = await anext(generator)
            assert "测试物品" in first and "补发完整图片" in first
            assert not started.is_set() and handler.gacha_service.draw.await_count == 1
            second = asyncio.create_task(anext(generator))
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.sleep(0.2)
            assert render.pending == 0 and not second.done()
            release.set()
            assert await second == ["IMAGE"]
            assert handler.gacha_service.draw.await_count == 1
            with pytest.raises(StopAsyncIteration):
                await anext(generator)
        finally:
            release.set()
            await generator.aclose()
            await render.close()

    asyncio.run(scenario())


def test_preparation_failure_keeps_the_committed_text_and_never_renders_partial_picture():
    from src.services.portrait_service import PortraitUnavailable

    async def scenario():
        handler, event = _handler("ten_pulls")
        handler.portrait_service = SimpleNamespace(
            missing=lambda _: ["missing"], prepare=AsyncMock(side_effect=PortraitUnavailable("下载失败")))
        handler._run_render = AsyncMock()
        replies = [reply async for reply in handler.ten_pulls(event)]
        assert len(replies) == 2 and "十连抽卡结果" in replies[0] and replies[1] == "下载失败"
        assert handler.gacha_service.draw.await_count == 1
        handler._run_render.assert_not_awaited()

    asyncio.run(scenario())
