"""
数据库迁移模块

负责在插件升级时执行数据迁移，确保旧数据库与新代码兼容。
迁移策略：每次升级检查 schema_version，按顺序执行未完成的迁移。
"""

import json
import sqlite3
import zipfile
from datetime import datetime, timezone

from astrbot.api import logger

from ..db.item_db_operations import ItemDBOperations
from ..gacha.cardpool_manager import CardPoolManager
from ..item_data.item_manager import ItemManager
from .database import CommonDatabase


def run_migrations(
    db: CommonDatabase,
    idb_ops: ItemDBOperations,
    item_manager: ItemManager,
    cp_manager: CardPoolManager,
):
    """检查当前数据库版本并执行所有待处理的迁移。"""
    current_version = db.get_schema_version()
    target_version = CommonDatabase.SCHEMA_VERSION

    if current_version >= target_version:
        logger.info(f"数据库已是当前版本 (v{target_version})，无需迁移")
        return

    logger.info(
        f"检测到数据库版本 v{current_version}，开始升级到 v{target_version} ..."
    )

    for version in range(current_version + 1, target_version + 1):
        _run_single_migration(version, db, idb_ops, item_manager, cp_manager)
        if version != 2:  # v2 writes its version in the schema transaction.
            db.set_schema_version(version)
        logger.info(f"数据库已升级到 v{version}")

    logger.info(f"数据库迁移完成（v{current_version} → v{target_version}）")


def _run_single_migration(
    version: int,
    db: CommonDatabase,
    idb_ops: ItemDBOperations,
    item_manager: ItemManager,
    cp_manager: CardPoolManager,
):
    """执行单个版本的迁移逻辑。"""
    if version == 1:
        _migrate_v1(idb_ops, item_manager, cp_manager)
        return
    if version == 2:
        _migrate_v2(db, cp_manager)
        return
    raise ValueError(f"未知的迁移版本 v{version}")


def _migrate_v2(db: CommonDatabase, cp_manager: CardPoolManager):
    """Keep legacy rows unclaimed; their sender IDs have no trustworthy platform."""
    backup_dir = db.db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"pre-v2-{stamp}.db"
    suffix = 1
    while backup_path.exists():
        backup_path = backup_dir / f"pre-v2-{stamp}-{suffix}.db"
        suffix += 1
    with db.get_connection() as source, sqlite3.connect(backup_path) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("数据库备份完整性检查失败")
    archive_path = backup_path.with_suffix(".zip")
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in cp_manager.config_dir.rglob("*.json"):
            if path.is_file() and path.resolve().is_relative_to(cp_manager.config_dir.resolve()):
                archive.write(path, path.relative_to(cp_manager.config_dir))
        archive.writestr("manifest.json", json.dumps({
            "schema_version": 1, "database_backup": backup_path.name,
            "created_at": stamp, "legacy_identity": "unclaimed",
        }))

    with db.get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("原数据库完整性检查失败")
            legacy_states = conn.execute("SELECT COUNT(*) FROM gacha_states").fetchone()[0]
            legacy_history = conn.execute("SELECT COUNT(*) FROM pull_history").fetchone()[0]
            conn.execute("""CREATE TABLE IF NOT EXISTS actors (
                actor_id TEXT PRIMARY KEY, platform_id TEXT NOT NULL,
                sender_id TEXT NOT NULL, UNIQUE(platform_id, sender_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS gacha_states_v2 (
                actor_id TEXT NOT NULL, pity_group_id TEXT NOT NULL,
                pity_5star INTEGER NOT NULL DEFAULT 0,
                pity_4star INTEGER NOT NULL DEFAULT 0,
                _5star_guaranteed INTEGER NOT NULL DEFAULT 0,
                _4star_guaranteed INTEGER NOT NULL DEFAULT 0,
                pull_count INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(actor_id, pity_group_id),
                FOREIGN KEY(actor_id) REFERENCES actors(actor_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS pull_batches (
                batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_key TEXT UNIQUE, actor_id TEXT NOT NULL,
                pity_group_id TEXT NOT NULL, pool_id TEXT NOT NULL,
                config_version TEXT NOT NULL, operation TEXT NOT NULL,
                results_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(actor_id) REFERENCES actors(actor_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS pull_history_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER, draw_index INTEGER NOT NULL,
                actor_id TEXT NOT NULL, pool_id TEXT,
                config_group TEXT, external_id TEXT,
                item TEXT NOT NULL, type TEXT, rarity TEXT,
                pull_time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                legacy_id INTEGER UNIQUE,
                FOREIGN KEY(actor_id) REFERENCES actors(actor_id),
                FOREIGN KEY(batch_id) REFERENCES pull_batches(batch_id) ON DELETE SET NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_v2_actor ON pull_history_v2(actor_id, id)")
            conn.execute("""CREATE TABLE IF NOT EXISTS legacy_claims (
                legacy_user_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL,
                claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(actor_id) REFERENCES actors(actor_id))""")
            if conn.execute("SELECT COUNT(*) FROM gacha_states").fetchone()[0] != legacy_states or \
               conn.execute("SELECT COUNT(*) FROM pull_history").fetchone()[0] != legacy_history:
                raise RuntimeError("迁移期间旧数据数量改变")
            conn.execute("INSERT OR REPLACE INTO _metadata(key, value) VALUES ('schema_version', '2')")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    logger.info(f"v2 迁移完成；备份: {backup_path}；旧数据保留待明确归属")


def _migrate_v1(
    idb_ops: ItemDBOperations,
    item_manager: ItemManager,
    cp_manager: CardPoolManager,
):
    """v1 迁移：同步新增物品和预置卡池配置。"""
    logger.info("正在执行 v1 迁移：同步物品数据和预置配置...")

    # 1. 同步物品：将 CSV 中有而数据库表中没有的新物品插入
    table_name = item_manager.table_name
    added = idb_ops.sync_new_items_from_csv(table_name)
    if added > 0:
        # 刷新 ItemManager 内存缓存
        item_manager._item_details = idb_ops.load_all_items(table_name)

    # 2. 同步预置卡池配置：将 presets 中新增的 .json 复制到配置目录
    cp_manager.sync_new_presets()
