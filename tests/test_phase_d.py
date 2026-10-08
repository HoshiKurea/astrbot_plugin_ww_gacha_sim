"""User-facing pagination, state, bounded rendering and cache regressions."""

import ast
import asyncio
import json
import logging
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def astrbot_stub(monkeypatch, tmp_path):
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    star = types.ModuleType("astrbot.api.star")
    api.logger = logging.getLogger("phase_d")
    star.StarTools = SimpleNamespace(get_data_dir=lambda _: str(tmp_path))
    monkeypatch.setitem(sys.modules, "astrbot", astrbot)
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    monkeypatch.setitem(sys.modules, "astrbot.api.star", star)


def test_history_arguments_and_navigation_preserve_pool():
    from src.services.command_utils import format_history_text, parse_history_args

    assert parse_history_args() == (None, 1)
    assert parse_history_args("2") == (None, 2)
    assert parse_history_args("pool-id", "3") == ("pool-id", 3)
    for args in (("pool-id", "0"), ("pool-id", "x"), ("2", "3")):
        with pytest.raises(ValueError):
            parse_history_args(*args)
    result = format_history_text(
        [{"rarity": "5star", "item": "旧角色", "type": "character",
          "pull_time": "2026-09-27"}], 2, 4, 31, "deleted-pool"
    )
    assert "旧角色" in result
    assert "/wwg 记录 deleted-pool 1" in result
    assert "/wwg 记录 deleted-pool 3" in result


def test_help_describes_public_and_admin_commands():
    handler = _main_handler("wgs_help", {"AstrMessageEvent": object})
    event = SimpleNamespace(plain_result=lambda message: message)

    async def consume():
        return [result async for result in handler.wgs_help(event)]

    result = asyncio.run(consume())[0]
    for command in (
        "/wwg 卡池",
        "/wwg 选择 <编号|卡池ID|名称>",
        "/wwg 单抽 [编号|卡池ID|名称]",
        "/wwg 十连 [编号|卡池ID|名称]",
        "/wwg 记录 [卡池ID|名称] [页码]",
        "/wwg 保底 [卡池ID|名称]",
        "/wwg 诊断",
        "/wwg_help",
        "/认领旧抽卡 <旧发送者ID> <目标发送者ID> [目标平台实例ID]",
    ):
        assert command in result
    assert "只填写一个数字时，该数字按页码处理" in result
    assert "主入口：/wwg" in result
    assert "兼容旧入口 /鸣潮、/ww抽卡" in result


def _main_handler(name, namespace):
    source = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    plugin = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    method = next(node for node in plugin.body if getattr(node, "name", None) == name)
    method.decorator_list = []
    handler_class = ast.ClassDef(
        name="Handler", bases=[], keywords=[], body=[method], decorator_list=[]
    )
    exec(compile(ast.fix_missing_locations(
        ast.Module(body=[handler_class], type_ignores=[])), str(source), "exec"), namespace)
    return namespace["Handler"]()


def test_history_handler_filters_before_fetching_and_uses_snapshot():
    from src.services.command_utils import format_history_text, parse_history_args

    handler = _main_handler("view_pull_history", {
        "AstrMessageEvent": object, "logger": logging.getLogger("phase_d"),
        "parse_history_args": parse_history_args,
        "format_history_text": format_history_text,
    })
    handler.enable_history_recording = True
    handler.enable_rendering = False
    handler._find_pool_config = lambda _: SimpleNamespace(cp_id="pool-id", name="池")
    calls = []

    async def count(*args):
        calls.append(("count", args))
        return 11

    async def history(*args):
        calls.append(("history", args))
        return [{"rarity": "5star", "item": "旧角色", "type": "character",
                 "pull_time": "2026-09-27"}]

    handler.gacha_service = SimpleNamespace(history_count=count, history=history)
    event = SimpleNamespace(
        get_platform_id=lambda: "platform", get_sender_id=lambda: "user",
        plain_result=lambda message: message,
    )

    async def consume():
        return [result async for result in handler.view_pull_history(event, "pool-id", "2")]

    result = asyncio.run(consume())
    assert calls == [
        ("count", ("platform", "user", "pool-id")),
        ("history", ("platform", "user", 10, 10, "pool-id")),
    ]
    assert "角色 · ★★★★★ 旧角色" in result[0]
    assert "/wwg 记录 pool-id 1" in result[0]


def test_pity_handler_reads_selected_group():
    handler = _main_handler("pity_status", {
        "AstrMessageEvent": object, "logger": logging.getLogger("phase_d"),
    })
    pool = SimpleNamespace(
        name="测试池", cp_id="pool-id", pity_group_id="shared",
        probability_progression={"5star": {"hard_pity_pull": 80},
                                 "4star": {"hard_pity_pull": 10}},
    )

    async def resolve(*args):
        return pool, None

    calls = []

    async def state(*args):
        calls.append(args)
        return {"pity_5star": 9, "pity_4star": 2,
                "_5star_guaranteed": True, "_4star_guaranteed": False,
                "pull_count": 32}

    handler._resolve_pool_config = resolve
    handler.gacha_service = SimpleNamespace(state=state)
    event = SimpleNamespace(
        get_platform_id=lambda: "platform", get_sender_id=lambda: "user",
        plain_result=lambda message: message,
    )

    async def consume():
        return [result async for result in handler.pity_status(event, "pool-id")]

    result = asyncio.run(consume())
    assert calls == [("platform", "user", "shared")]
    assert "五星：9/80，UP 保证：是" in result[0]
    assert "本组累计抽数：32" in result[0]


def test_settings_match_schema_and_reject_bad_values(tmp_path):
    from src.services.settings import PluginSettings

    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    defaults = {key: value["default"] for key, value in schema.items()}
    settings = PluginSettings.from_config(defaults)
    for key, value in defaults.items():
        assert getattr(settings, key) == value
    with pytest.raises(ValueError, match="render_workers"):
        PluginSettings.from_config({**defaults, "render_workers": 0})
    with pytest.raises(ValueError, match="font_path"):
        PluginSettings.from_config({**defaults, "font_path": str(tmp_path / "missing.ttf")})


def test_render_timeout_keeps_capacity_until_worker_finishes():
    from src.services.render_service import RenderBusy, RenderService, RenderTimedOut

    async def scenario():
        started = threading.Event()
        release = threading.Event()
        service = RenderService(workers=1, queue_limit=0, timeout_seconds=0.05)

        def slow():
            started.set()
            release.wait(2)
            return "done"

        try:
            first = asyncio.create_task(service.run(slow))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            assert started.is_set()
            with pytest.raises(RenderBusy):
                await service.run(lambda: "must not run")
            with pytest.raises(RenderTimedOut):
                await first
            assert service.pending == 1
            with pytest.raises(RenderBusy):
                await service.run(lambda: "must not run")
            release.set()
            for _ in range(100):
                if service.pending == 0:
                    break
                await asyncio.sleep(0.005)
            assert service.pending == 0
            assert await service.run(lambda: "ready") == "ready"
        finally:
            release.set()
            await service.close()

    asyncio.run(scenario())


def test_cache_capacity_restart_and_invalid_metadata(tmp_path):
    from src.render.local_file_cache_manager import LocalFileCacheManager

    cache_dir = tmp_path / "cache"
    cache = LocalFileCacheManager(cache_dir, max_bytes=6)
    try:
        cache.cache_file(b"aaaa", "first")
        cache.cache_file(b"bbbb", "second")
        assert cache.get_cached_file_path("first") is None
        assert cache.get_cached_file_path("second").read_bytes() == b"bbbb"
        assert cache.get_cache_size() <= 6
        with pytest.raises(ValueError):
            cache.cache_file(b"bad", "../escape")
    finally:
        cache.stop_scheduled_cleanup()

    (cache_dir / "cache_meta.json").write_text(
        json.dumps({"../escape": {"created_at": 0, "expires_at": 9999999999},
                    "bad": "invalid", "second": cache.cache_meta["second"]}),
        encoding="utf-8",
    )
    reopened = LocalFileCacheManager(cache_dir, max_bytes=6)
    try:
        assert list(reopened.cache_meta) == ["second"]
        assert reopened.get_cached_file_path("second").read_bytes() == b"bbbb"
    finally:
        reopened.stop_scheduled_cleanup()


def test_concurrent_cache_writes_keep_metadata_readable(tmp_path):
    from src.render.local_file_cache_manager import LocalFileCacheManager

    cache = LocalFileCacheManager(tmp_path / "cache", max_bytes=1024 * 1024)
    try:
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(
                lambda index: cache.cache_file(f"image-{index}".encode(), f"key-{index}"),
                range(32),
            ))
        metadata = json.loads(cache.meta_file.read_text(encoding="utf-8"))
        assert len(metadata) == 32
        assert cache.get_cache_size() == sum(
            len(f"image-{index}".encode()) for index in range(32)
        )
    finally:
        cache.stop_scheduled_cleanup()


def test_remote_portrait_download_stops_at_size_limit(monkeypatch):
    from src.render.resource_loader import ResourceLoader

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_bytes(self):
            yield b"abc"
            yield b"def"

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def stream(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("src.render.resource_loader.httpx.Client", Client)
    monkeypatch.setattr(ResourceLoader, "is_valid_resource_url", lambda *_: True)
    loader = ResourceLoader()
    assert loader.download_with_retry(
        "https://example.com/image.png", max_retries=1, max_bytes=5
    ) is None
    assert loader.download_with_retry(
        "https://example.com/image.png", max_retries=1, max_bytes=6
    ) == b"abcdef"


def test_remote_portrait_uses_public_cdn_fallback(monkeypatch):
    from src.render.resource_loader import ResourceLoader

    calls = []

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_bytes(self):
            yield b"png-data"

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def stream(self, method, url, **kwargs):
            calls.append(url)
            return Response(503 if len(calls) == 1 else 200)

    monkeypatch.setattr("src.render.resource_loader.httpx.Client", Client)
    monkeypatch.setattr(ResourceLoader, "is_valid_resource_url", lambda *_: True)
    monkeypatch.setattr("src.render.resource_loader.time.sleep", lambda _: None)
    loader = ResourceLoader()
    source = (
        "https://raw.githubusercontent.com/TomyJan/WutheringWaves-UIResources/3.3/"
        "UIResources/Common/Image/Luckdraw/T_Luckdraw_lingyang_UI.png"
    )

    assert loader.download_with_retry(source, max_retries=1) == b"png-data"
    assert calls == [
        source,
        "https://cdn.jsdelivr.net/gh/TomyJan/WutheringWaves-UIResources@3.3/"
        "UIResources/Common/Image/Luckdraw/T_Luckdraw_lingyang_UI.png",
    ]
    assert ResourceLoader._mirror_urls("https://raw.githubusercontent.com/other/repo/main/a.png") == ()


def test_remote_portrait_tries_fastly_after_jsdelivr(monkeypatch):
    from src.render.resource_loader import ResourceLoader

    calls = []

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_bytes(self):
            yield b"png-data"

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def stream(self, method, url, **kwargs):
            calls.append(url)
            return Response(503 if len(calls) < 3 else 200)

    monkeypatch.setattr("src.render.resource_loader.httpx.Client", Client)
    monkeypatch.setattr(ResourceLoader, "is_valid_resource_url", lambda *_: True)
    monkeypatch.setattr("src.render.resource_loader.time.sleep", lambda _: None)
    loader = ResourceLoader()
    source = (
        "https://raw.githubusercontent.com/TomyJan/WutheringWaves-UIResources/3.3/"
        "UIResources/Common/Image/Luckdraw/T_Luckdraw_lingyang_UI.png"
    )

    assert loader.download_with_retry(source, max_retries=1) == b"png-data"
    assert calls == [
        source,
        "https://cdn.jsdelivr.net/gh/TomyJan/WutheringWaves-UIResources@3.3/"
        "UIResources/Common/Image/Luckdraw/T_Luckdraw_lingyang_UI.png",
        "https://fastly.jsdelivr.net/gh/TomyJan/WutheringWaves-UIResources@3.3/"
        "UIResources/Common/Image/Luckdraw/T_Luckdraw_lingyang_UI.png",
    ]


def test_legacy_proxy_portrait_is_unwrapped_before_download(monkeypatch):
    from src.render.resource_loader import ResourceLoader

    calls = []

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_bytes(self):
            yield b"png-data"

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def stream(self, method, url, **kwargs):
            calls.append(url)
            return Response()

    monkeypatch.setattr("src.render.resource_loader.httpx.Client", Client)
    loader = ResourceLoader()
    legacy = (
        "https://v6.gh-proxy.org/https://raw.githubusercontent.com/"
        "TomyJan/WutheringWaves-UIResources/3.3/"
        "UIResources/Common/Image/Luckdraw/T_Luckdraw_lingyang_UI.png"
    )

    assert loader.download_with_retry(legacy, max_retries=1) == b"png-data"
    assert calls == [
        "https://raw.githubusercontent.com/TomyJan/WutheringWaves-UIResources/3.3/"
        "UIResources/Common/Image/Luckdraw/T_Luckdraw_lingyang_UI.png",
    ]


def test_pity_state_is_read_only_and_scoped(tmp_path):
    from src.db.database import CommonDatabase
    from src.db.gacha_db_operations import GachaDBOperations
    from src.db.migration import run_migrations

    db = CommonDatabase(tmp_path / "state.db")
    ops = GachaDBOperations(db)
    db.set_schema_version(1)
    pool_dir = tmp_path / "pools"
    pool_dir.mkdir()
    run_migrations(db, None, None, SimpleNamespace(config_dir=pool_dir))
    try:
        empty = ops.load_state_v2("platform-a", "user", "pool")
        assert empty["pull_count"] == 0
        assert db.execute_query_single("SELECT COUNT(*) AS n FROM gacha_states_v2")["n"] == 0
        actor = ops.actor_id("platform-a", "user")
        db.execute_update(
            "INSERT INTO actors(actor_id,platform_id,sender_id) VALUES (?,?,?)",
            (actor, "platform-a", "user"),
        )
        db.execute_update(
            "INSERT INTO gacha_states_v2 (actor_id,pity_group_id,pity_5star,"
            "pity_4star,_5star_guaranteed,_4star_guaranteed,pull_count,revision) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (actor, "pool", 7, 2, 1, 0, 9, 1),
        )
        assert ops.load_state_v2("platform-a", "user", "pool")["pity_5star"] == 7
        assert ops.load_state_v2("platform-b", "user", "pool")["pull_count"] == 0
        assert ops.load_state_v2("platform-a", "user", "other")["pull_count"] == 0
    finally:
        db.close()


def test_100_users_mixed_draws_keep_independent_counts(tmp_path):
    from src.db.database import CommonDatabase
    from src.db.gacha_db_operations import GachaDBOperations
    from src.db.migration import run_migrations
    from src.item_data.item_manager import Item

    db = CommonDatabase(tmp_path / "load.db")
    ops = GachaDBOperations(db)
    db.set_schema_version(1)
    pool_dir = tmp_path / "pools"
    pool_dir.mkdir()
    run_migrations(db, None, None, SimpleNamespace(config_dir=pool_dir))
    item = Item("测试", "3star", "weapon", "weapon", external_id="item-1")

    def work(index):
        count = 10 if index % 10 == 0 else 1

        def draw(state):
            updated = state.copy()
            updated["pull_count"] += count
            updated["pity_5star"] += count
            return [item] * count, updated

        try:
            return ops.commit_draws_v2(
                "platform", f"user-{index}", "pool", "pool", "default", "v1",
                "ten" if count == 10 else "single", f"request-{index}", count,
                draw, True,
            )
        finally:
            db.close_thread_local_connection()

    try:
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(work, range(100)))
        assert sum(map(len, results)) == 190
        assert ops.get_pull_history_count_v2("platform", "user-0") == 10
        assert ops.get_pull_history_count_v2("platform", "user-1") == 1
        assert db.execute_query_single(
            "SELECT COUNT(*) AS n FROM gacha_states_v2"
        )["n"] == 100
        assert db.execute_query_single(
            "SELECT SUM(pull_count) AS n FROM gacha_states_v2"
        )["n"] == 190
    finally:
        db.close()
