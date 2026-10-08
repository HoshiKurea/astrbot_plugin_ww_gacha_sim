"""Migration, identity, receipt and snapshot regression tests."""

import asyncio
import json
import logging
import sqlite3
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def astrbot_stub(monkeypatch, tmp_path):
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    star = types.ModuleType("astrbot.api.star")
    api.logger = logging.getLogger("phase_b")
    star.StarTools = SimpleNamespace(get_data_dir=lambda _: str(tmp_path))
    monkeypatch.setitem(sys.modules, "astrbot", astrbot)
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    monkeypatch.setitem(sys.modules, "astrbot.api.star", star)


def migrated_db(tmp_path):
    from src.db.database import CommonDatabase
    from src.db.gacha_db_operations import GachaDBOperations
    from src.db.migration import run_migrations

    db = CommonDatabase(tmp_path / "data.db")
    ops = GachaDBOperations(db)
    db.set_schema_version(1)
    pool_dir = tmp_path / "pools"
    pool_dir.mkdir()
    (pool_dir / "pool.json").write_text('{"name":"test"}', encoding="utf-8")
    run_migrations(db, None, None, SimpleNamespace(config_dir=pool_dir))
    return db, ops


def make_item():
    from src.item_data.item_manager import Item
    return Item("测试物品", "5star", "character", "character", external_id="item-1")


def draw_once(state):
    state = state.copy()
    state["pity_5star"] += 1
    state["pull_count"] += 1
    return [make_item()], state


def test_migration_backup_claim_and_platform_isolation(tmp_path):
    from src.db.database import CommonDatabase
    from src.db.gacha_db_operations import GachaDBOperations
    from src.db.migration import run_migrations

    db = CommonDatabase(tmp_path / "legacy.db")
    ops = GachaDBOperations(db)
    ops.save_user_state("old-id", dict(pity_5star=9, pity_4star=2,
                                       _5star_guaranteed=False,
                                       _4star_guaranteed=False, pull_count=20))
    ops.save_pull_history("old-id", {"item": "旧角色", "rarity": "5star",
                                     "pool_id": "old", "pull_time": "2024-01-01"})
    db.set_schema_version(1)
    pool_dir = tmp_path / "pools"
    pool_dir.mkdir()
    (pool_dir / "pool.json").write_text('{"name":"test"}', encoding="utf-8")
    run_migrations(db, None, None, SimpleNamespace(config_dir=pool_dir))
    assert db.get_schema_version() == 2
    assert list((tmp_path / "backups").glob("*.db"))
    assert list((tmp_path / "backups").glob("*.zip"))
    with sqlite3.connect(next((tmp_path / "backups").glob("*.db"))) as backup:
        assert backup.execute("SELECT COUNT(*) FROM pull_history").fetchone()[0] == 1
    assert ops.get_pull_history_count_v2("platform-a", "old-id") == 0
    assert ops.claim_legacy("old-id", "platform-a", "user-a") == 1
    assert ops.claim_legacy("old-id", "platform-a", "user-a") == 0
    assert ops.get_pull_history_count_v2("platform-a", "user-a") == 1
    assert ops.get_pull_history_count_v2("platform-b", "user-a") == 0
    assert ops.load_user_state("old-id")["pity_5star"] == 9
    actor = ops.actor_id("platform-a", "user-a")
    state = db.execute_query_single("SELECT pity_5star FROM gacha_states_v2 "
                                    "WHERE actor_id=? AND pity_group_id='legacy_shared'", (actor,))
    assert state["pity_5star"] == 9
    db.close()


def test_failed_migration_keeps_version_and_can_retry(tmp_path):
    from src.db.database import CommonDatabase
    from src.db.gacha_db_operations import GachaDBOperations
    from src.db.migration import run_migrations

    db = CommonDatabase(tmp_path / "retry.db")
    GachaDBOperations(db)
    db.set_schema_version(1)
    pool_dir = tmp_path / "pools"
    pool_dir.mkdir()
    manager = SimpleNamespace(config_dir=pool_dir)
    db._get_thread_local_connection().execute("PRAGMA query_only=ON")
    with pytest.raises(sqlite3.OperationalError):
        run_migrations(db, None, None, manager)
    assert db.get_schema_version() == 1
    db._get_thread_local_connection().execute("PRAGMA query_only=OFF")
    run_migrations(db, None, None, manager)
    assert db.get_schema_version() == 2
    db.close()


def test_duplicate_and_concurrent_draws_commit_once(tmp_path):
    db, ops = migrated_db(tmp_path)
    args = ("platform", "sender", "pool", "pool", "default", 3,
            "single", "message-key", 1, draw_once, True)

    def invoke():
        try:
            return ops.commit_draws_v2(*args)
        finally:
            db.close_thread_local_connection()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: invoke(), range(2)))
    assert results[0][0].name == results[1][0].name
    assert ops.get_pull_history_count_v2("platform", "sender") == 1
    assert db.execute_query_single("SELECT pull_count FROM gacha_states_v2")[0] == 1
    ops.commit_draws_v2("platform", "sender", "other-pool", "other-pool", "default",
                        3, "single", "another-key", 1, draw_once, True)
    assert db.execute_query_single("SELECT COUNT(*) FROM gacha_states_v2")[0] == 2
    db.execute_update("UPDATE pull_batches SET created_at='2000-01-01'")
    assert ops.prune_receipts() == 2
    assert ops.get_pull_history_count_v2("platform", "sender") == 2
    db.close()


def test_bad_reload_preserves_pool_snapshot(tmp_path):
    from src.gacha.cardpool_manager import CardPoolManager
    from src.services.pool_service import PoolService, RevisionConflict

    pool_dir = tmp_path / "pools"
    pool_dir.mkdir()
    path = pool_dir / "pool.json"
    config = dict(
        cp_id="pool", name="测试池", config_group="default", enable=True,
        probability_settings={"base_5star_rate": .008, "base_4star_rate": .06,
                              "up_5star_rate": .5, "up_4star_rate": .5,
                              "_4star_weapon_rate": 0},
        probability_progression={"5star": {"hard_pity_pull": 80,
                                            "hard_pity_rate": 1, "soft_pity": []},
                                 "4star": {"hard_pity_pull": 10,
                                            "hard_pity_rate": 1}},
        included_item_ids={"5star": ["five"], "4star": ["four"],
                           "3star": ["three"]},
        rate_up_item_ids={"5star": [], "4star": []},
    )
    path.write_text(json.dumps(config), encoding="utf-8")
    manager = CardPoolManager(pool_dir)
    items = {key: dict(external_id=key, name=key, rarity=rarity,
                       type="character", affiliated_type="character")
             for key, rarity in (("five", "5star"), ("four", "4star"),
                                 ("three", "3star"))}
    service = PoolService(manager, SimpleNamespace(load_all_items=lambda _: items))
    before = service.revision
    path.write_text("{bad json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        service.reload()
    assert service.revision == before
    assert service.get("pool").name == "测试池"
    with pytest.raises(RevisionConflict):
        service.save("pool", config, expected_revision=before - 1)
    path.write_text(json.dumps(config), encoding="utf-8")
    broken = dict(config, included_item_ids={"5star": ["missing"],
                                                   "4star": ["four"],
                                                   "3star": ["three"]})
    with pytest.raises(ValueError):
        service.save("pool", broken)
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "测试池"
    assert service.revision == before
    with pytest.raises(RuntimeError):
        service.save("duplicate", config)
    assert not (pool_dir / "duplicate.json").exists()
    assert service.get("pool").name == "测试池"


def test_fresh_service_start_draw_and_shutdown(tmp_path):
    from src.db.database import CommonDatabase
    from src.db.gacha_db_operations import GachaDBOperations
    from src.db.item_db_operations import ItemDBOperations
    from src.db.migration import run_migrations
    from src.gacha.cardpool_manager import CardPoolManager
    from src.item_data.item_manager import ItemManager
    from src.services.gacha_service import GachaService
    from src.services.pool_service import PoolService

    db = CommonDatabase(tmp_path / "fresh.db")
    gacha_ops = GachaDBOperations(db)
    item_ops = ItemDBOperations(db)
    item_manager = ItemManager(item_ops)
    manager = CardPoolManager(tmp_path / "pools")
    run_migrations(db, item_ops, item_manager, manager)
    pools = PoolService(manager, item_ops)
    version = pools.version
    for _ in range(20):
        pools.reload()
    assert pools.version == version
    active = next(pool for pool in pools.all() if pool.enable)
    service = GachaService(gacha_ops, item_ops, True)

    async def exercise():
        key = service.request_key("platform", "umo", "sender", "message", "single")
        first = await service.draw("platform", "sender", active, pools.version, 1, key)
        replay = await service.draw("platform", "sender", active, pools.version, 1, key)
        assert first["item_obj"].external_id == replay["item_obj"].external_id
        assert await service.history_count("platform", "sender", None) == 1
        await service.close()

    asyncio.run(exercise())
    assert not any(thread.name.startswith("WwGachaDB") for thread in threading.enumerate())
    db.close()
