"""Core mechanics and transaction tests, independent of an installed AstrBot."""

import ast
import asyncio
import logging
import csv
import hashlib
import json
import sqlite3
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module", autouse=True)
def astrbot_stub():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    star = types.ModuleType("astrbot.api.star")
    api.logger = logging.getLogger("test")
    star.StarTools = SimpleNamespace(get_data_dir=lambda _: str(Path.cwd()))
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api, "astrbot.api.star": star})
    item_manager = types.ModuleType("src.item_data.item_manager")
    item_manager.Item = object
    item_manager.ItemManager = object
    sys.modules["src.item_data.item_manager"] = item_manager
    yield
    for name in ("src.item_data.item_manager", "astrbot.api.star", "astrbot.api", "astrbot"):
        sys.modules.pop(name, None)


def fixtures(tmp_path):
    from src.db.database import CommonDatabase
    from src.db.gacha_db_operations import GachaDBOperations
    from src.gacha.gacha_flow import GachaFlow

    db = CommonDatabase(tmp_path / "gacha.db")
    ops = GachaDBOperations(db)
    items = {}
    for item_id, rarity in [
        ("normal5", "5star"), ("up5", "5star"),
        ("normal4", "4star"), ("up4", "4star"),
        ("normal3", "3star"),
    ]:
        items[item_id] = SimpleNamespace(
            external_id=item_id, name=item_id, rarity=rarity, type="character"
        )
    manager = SimpleNamespace(get_item_objects=lambda: items)
    pool = SimpleNamespace(
        cp_id="pool",
        probability_settings={
            "base_5star_rate": 0.008, "base_4star_rate": 0.06,
            "up_5star_rate": 0.5, "up_4star_rate": 0.5,
            "_4star_weapon_rate": 0,
        },
        probability_progression={
            "5star": {"hard_pity_pull": 80, "hard_pity_rate": 1, "soft_pity": []},
            "4star": {"hard_pity_pull": 10, "hard_pity_rate": 1},
        },
        included_item_ids={
            "5star": ["normal5", "up5"],
            "4star": ["normal4", "up4"],
            "3star": ["normal3"],
        },
        rate_up_item_ids={"5star": ["up5"], "4star": ["up4"]},
    )
    return db, ops, manager, pool, GachaFlow


def test_ten_pulls_commit_state_and_history_together(tmp_path, astrbot_stub):
    db, ops, manager, pool, flow_type = fixtures(tmp_path)
    items = flow_type("user", ops, manager).ten_consecutive_pulls(pool)
    assert len(items) == 10
    assert ops.load_user_state("user")["pull_count"] == 10
    assert ops.get_pull_history_count("user") == 10
    db.close_thread_local_connection()


def test_history_failure_rolls_back_pity(tmp_path, astrbot_stub):
    db, ops, manager, pool, flow_type = fixtures(tmp_path)
    flow_type("user", ops, manager).single_pull(pool)
    before = ops.load_user_state("user")
    db.execute_update("CREATE TRIGGER reject_history BEFORE INSERT ON pull_history "
                      "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        flow_type("user", ops, manager).ten_consecutive_pulls(pool)
    assert ops.load_user_state("user") == before
    assert ops.get_pull_history_count("user") == 1
    db.close_thread_local_connection()


def test_history_switch_preserves_pity(tmp_path, astrbot_stub):
    db, ops, manager, pool, flow_type = fixtures(tmp_path)
    flow_type("user", ops, manager, record_history=False).single_pull(pool)
    assert ops.load_user_state("user")["pull_count"] == 1
    assert ops.get_pull_history_count("user") == 0
    db.close_thread_local_connection()


def test_concurrent_batches_do_not_lose_state(tmp_path, astrbot_stub):
    db, ops, manager, pool, flow_type = fixtures(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(
            lambda _: flow_type("user", ops, manager).ten_consecutive_pulls(pool),
            range(2),
        ))
    assert all(len(batch) == 10 for batch in results)
    assert ops.load_user_state("user")["pull_count"] == 20
    assert ops.get_pull_history_count("user") == 20
    db.close_thread_local_connection()


def test_invalid_pool_does_not_write(tmp_path, astrbot_stub):
    db, ops, manager, pool, flow_type = fixtures(tmp_path)
    pool.included_item_ids["5star"] = []
    pool.rate_up_item_ids["5star"] = []
    with pytest.raises(ValueError, match="5star"):
        flow_type("user", ops, manager).single_pull(pool)
    assert ops.load_user_state("user") is None
    assert ops.get_pull_history_count("user") == 0
    db.close_thread_local_connection()


def test_managed_paths_reject_escape_and_bad_group(tmp_path):
    from src.web.path_security import managed_path, validate_group

    root = tmp_path / "managed"
    root.mkdir()
    assert managed_path(root, "default/pool", suffix=".json") == root / "default/pool.json"
    for value in ("../outside", "C:/outside", "/outside", "default/../../outside", "default\\outside"):
        with pytest.raises(ValueError):
            managed_path(root, value, suffix=".json")
    for value in ("../bad", "x/y", "a;DROP TABLE users", ""):
        with pytest.raises(ValueError):
            validate_group(value)


def test_resource_loader_rejects_local_address(astrbot_stub, monkeypatch):
    import socket

    from src.render.resource_loader import ResourceLoader

    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))],
    )
    assert not ResourceLoader().is_valid_resource_url("https://private.example/image.png")
    assert not ResourceLoader().is_valid_resource_url("file:///etc/passwd")


def test_all_shipped_pools_reference_available_items(astrbot_stub):
    from src.gacha.cardpool_manager import CardPoolConfig
    from src.gacha.gacha_mechanics import GachaMechanics

    root = Path(__file__).resolve().parents[1] / "src/assets"
    items = {}
    with (root / "data/default.csv").open(encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            key = f"{row['name']}_{row['rarity']}_{row['type']}_{row['affiliated_type']}"
            external_id = (
                f"{row['type'][:3].lower()}_"
                f"{row['name'].replace(' ', '_').replace('.', '_')[:10]}_"
                f"{hashlib.md5(key.encode()).hexdigest()[:4]}"
            )
            items[external_id] = SimpleNamespace(external_id=external_id, rarity=row["rarity"])
    mechanics = GachaMechanics(SimpleNamespace(get_item_objects=lambda: items))
    for preset in (root / "presets").glob("*.json"):
        config = CardPoolConfig.from_dict(json.loads(preset.read_text(encoding="utf-8")))
        assert mechanics.validate_pool(config), preset.name


@pytest.mark.parametrize("command", ["single_pull", "ten_pulls"])
def test_render_failure_returns_committed_results(command):
    """Exercise the real handler body without importing AstrBot itself."""
    source = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    plugin = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    methods = []
    for node in plugin.body:
        if getattr(node, "name", None) in (command, "_rarity_stars", "_draw_response"):
            if node.name == command:
                node.decorator_list = []
            methods.append(node)
    handler_class = ast.ClassDef(
        name="Handlers", bases=[], keywords=[], body=methods, decorator_list=[]
    )
    from src.services.portrait_service import PortraitUnavailable
    from src.services.work_queue import WorkBusy
    namespace = {"asyncio": asyncio, "AstrMessageEvent": object,
                 "logger": logging.getLogger("test"), "PortraitUnavailable": PortraitUnavailable,
                 "GachaBusy": WorkBusy}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[handler_class], type_ignores=[])), str(source), "exec"), namespace)
    handler = namespace["Handlers"]()
    item = SimpleNamespace(name="测试物品", rarity="5star", type="character")
    attempts = []
    handler.enable_rendering = True
    async def prepared(*args, **kwargs):
        pass
    handler.portrait_service = SimpleNamespace(missing=lambda _: [], prepare=prepared)
    handler.settings = SimpleNamespace(portrait_wait_timeout_seconds=45)
    handler.renderer = SimpleNamespace(render_single_pull=lambda *_: None, render_ten_pulls=lambda *_: None)
    handler._render_and_encode = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("render failure"))
    async def fail_render(*_args, **_kwargs):
        raise RuntimeError("render failure")
    handler._run_render = fail_render

    async def resolve(*_args):
        return SimpleNamespace(cp_id="pool"), None

    handler._resolve_pool_config = resolve

    async def draw(*args):
        attempts.append(args)
        return {"item_obj": item} if command == "single_pull" else [item] * 10

    handler.gacha_service = SimpleNamespace(draw=draw)
    handler.pool_service = SimpleNamespace(
        get_with_version=lambda _: (SimpleNamespace(cp_id="pool", enable=True), "v1")
    )
    handler._request_key = lambda *_: "request-key"
    event = SimpleNamespace(
        get_sender_id=lambda: "user",
        get_platform_id=lambda: "platform",
        get_sender_name=lambda: "name",
        plain_result=lambda message: message,
    )

    async def consume():
        return [message async for message in getattr(handler, command)(event)]

    result = asyncio.run(consume())
    assert len(attempts) == 1
    assert len(result) == 1
    assert "抽卡已完成" in result[0]
    assert "测试物品" in result[0]
