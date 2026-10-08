"""
抽卡数据库操作模块
处理抽卡相关的数据库异步操作，避免死锁
包含业务逻辑相关的数据库操作方法
"""

import hashlib
import json
from typing import Any

from astrbot.api import logger

from .database import CommonDatabase


class GachaDBOperations:
    """
    抽卡数据库操作类
    处理抽卡相关的数据库异步操作，避免死锁
    包含业务逻辑相关的数据库操作方法
    """

    def __init__(self, db: CommonDatabase):
        """
        初始化抽卡数据库操作管理器

        Args:
            db: 数据库实例
        """
        self.db = db
        # 初始化业务相关的数据库表结构
        self._init_business_tables()

    @staticmethod
    def actor_id(platform_id: str, sender_id: str) -> str:
        if not platform_id or not sender_id:
            raise ValueError("平台实例和用户 ID 均不能为空")
        return hashlib.sha256(f"{platform_id}\0{sender_id}".encode()).hexdigest()

    def commit_draws_v2(self, platform_id: str, sender_id: str,
                        pity_group_id: str, pool_id: str, config_group: str,
                        config_version: str, operation: str, request_key: str | None,
                        count: int, draw, record_history: bool):
        """Serialize state, receipt and optional history in one write transaction."""
        from ..item_data.item_manager import Item

        if count not in (1, 10) or not pity_group_id:
            raise ValueError("无效的抽卡数量或保底组")
        actor = self.actor_id(platform_id, sender_id)
        with self.db.get_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if request_key:
                    existing = conn.execute(
                        "SELECT actor_id, operation, results_json FROM pull_batches WHERE request_key=?",
                        (request_key,),
                    ).fetchone()
                    if existing:
                        if existing[0] != actor or existing[1] != operation:
                            raise ValueError("请求凭据与原抽卡不匹配")
                        results = [Item.from_dict(row) for row in json.loads(existing[2])]
                        conn.commit()
                        return results
                row = conn.execute(
                    "SELECT pity_5star, pity_4star, _5star_guaranteed, "
                    "_4star_guaranteed, pull_count, revision FROM gacha_states_v2 "
                    "WHERE actor_id=? AND pity_group_id=?", (actor, pity_group_id),
                ).fetchone()
                state = dict(
                    pity_5star=row[0] if row else 0,
                    pity_4star=row[1] if row else 0,
                    _5star_guaranteed=bool(row[2]) if row else False,
                    _4star_guaranteed=bool(row[3]) if row else False,
                    pull_count=row[4] if row else 0,
                )
                items, updated = draw(state)
                if len(items) != count or any(item is None for item in items):
                    raise ValueError("抽卡结果不完整，未保存任何结果")
                snapshots = [item.to_dict() for item in items]
                conn.execute("INSERT OR IGNORE INTO actors(actor_id, platform_id, sender_id) VALUES (?,?,?)",
                             (actor, platform_id, sender_id))
                conn.execute("""INSERT INTO gacha_states_v2
                    (actor_id,pity_group_id,pity_5star,pity_4star,_5star_guaranteed,
                    _4star_guaranteed,pull_count,revision) VALUES (?,?,?,?,?,?,?,1)
                    ON CONFLICT(actor_id,pity_group_id) DO UPDATE SET
                    pity_5star=excluded.pity_5star,pity_4star=excluded.pity_4star,
                    _5star_guaranteed=excluded._5star_guaranteed,
                    _4star_guaranteed=excluded._4star_guaranteed,
                    pull_count=excluded.pull_count,revision=revision+1""",
                    (actor, pity_group_id, updated["pity_5star"], updated["pity_4star"],
                     int(updated["_5star_guaranteed"]), int(updated["_4star_guaranteed"]),
                     updated["pull_count"]),
                )
                batch = conn.execute("""INSERT INTO pull_batches
                    (request_key,actor_id,pity_group_id,pool_id,config_version,operation,results_json)
                    VALUES (?,?,?,?,?,?,?)""",
                    (request_key, actor, pity_group_id, pool_id, config_version, operation,
                     json.dumps(snapshots, ensure_ascii=False)),
                ).lastrowid
                if record_history:
                    conn.executemany("""INSERT INTO pull_history_v2
                        (batch_id,draw_index,actor_id,pool_id,config_group,external_id,item,type,rarity)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                        [(batch, index, actor, pool_id, config_group, item.external_id,
                          item.name, item.type, item.rarity)
                         for index, item in enumerate(items, 1)],
                    )
                conn.commit()
                return items
            except BaseException:
                conn.rollback()
                raise

    def load_pull_history_v2(self, platform_id: str, sender_id: str,
                             limit: int, offset: int, pool_id: str | None = None):
        actor = self.actor_id(platform_id, sender_id)
        where = "WHERE actor_id=?" + (" AND pool_id=?" if pool_id else "")
        args = (actor, pool_id) if pool_id else (actor,)
        rows = self.db.execute_query(
            "SELECT id,item,rarity,pool_id,pull_time,type,external_id,draw_index "
            f"FROM pull_history_v2 {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        )
        return [dict(row) for row in rows]

    def get_pull_history_count_v2(self, platform_id: str, sender_id: str,
                                  pool_id: str | None = None) -> int:
        actor = self.actor_id(platform_id, sender_id)
        where = "WHERE actor_id=?" + (" AND pool_id=?" if pool_id else "")
        args = (actor, pool_id) if pool_id else (actor,)
        return self.db.execute_query_single(
            f"SELECT COUNT(*) AS total FROM pull_history_v2 {where}", args,
        )["total"]

    def load_state_v2(self, platform_id: str, sender_id: str,
                      pity_group_id: str) -> dict[str, Any]:
        actor = self.actor_id(platform_id, sender_id)
        row = self.db.execute_query_single(
            "SELECT pity_5star,pity_4star,_5star_guaranteed,"
            "_4star_guaranteed,pull_count FROM gacha_states_v2 "
            "WHERE actor_id=? AND pity_group_id=?",
            (actor, pity_group_id),
        )
        return {
            "pity_5star": row["pity_5star"] if row else 0,
            "pity_4star": row["pity_4star"] if row else 0,
            "_5star_guaranteed": bool(row["_5star_guaranteed"]) if row else False,
            "_4star_guaranteed": bool(row["_4star_guaranteed"]) if row else False,
            "pull_count": row["pull_count"] if row else 0,
        }

    def claim_legacy(self, legacy_user_id: str, platform_id: str,
                     sender_id: str) -> int:
        """Explicit administrator mapping; preserve legacy rows and their original IDs."""
        actor = self.actor_id(platform_id, sender_id)
        with self.db.get_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                claimed = conn.execute("SELECT actor_id FROM legacy_claims WHERE legacy_user_id=?",
                                       (legacy_user_id,)).fetchone()
                if claimed:
                    if claimed[0] != actor:
                        raise ValueError("旧身份已归属其他用户")
                    conn.commit()
                    return 0
                state = conn.execute("SELECT pity_5star,pity_4star,_5star_guaranteed,"
                                     "_4star_guaranteed,pull_count FROM gacha_states WHERE user_id=?",
                                     (legacy_user_id,)).fetchone()
                history = conn.execute("SELECT id,item,rarity,pool_id,pull_time "
                                       "FROM pull_history WHERE user_id=? ORDER BY id",
                                       (legacy_user_id,)).fetchall()
                if state is None and not history:
                    raise ValueError("旧身份没有可迁移的数据")
                if conn.execute("SELECT 1 FROM gacha_states_v2 WHERE actor_id=? AND pity_group_id='legacy_shared'",
                                (actor,)).fetchone():
                    raise ValueError("目标已有 legacy_shared 保底，不能覆盖")
                conn.execute("INSERT OR IGNORE INTO actors VALUES (?,?,?)",
                             (actor, platform_id, sender_id))
                if state:
                    conn.execute("INSERT INTO gacha_states_v2 VALUES (?,?,?,?,?,?,?,1)",
                                 (actor, "legacy_shared", *state))
                conn.executemany("""INSERT INTO pull_history_v2
                    (actor_id,draw_index,pool_id,item,rarity,pull_time,legacy_id)
                    VALUES (?,0,?,?,?,?,?)""",
                    [(actor, row[3], row[1], row[2], row[4], row[0]) for row in history])
                conn.execute("INSERT INTO legacy_claims(legacy_user_id,actor_id) VALUES (?,?)",
                             (legacy_user_id, actor))
                conn.commit()
                return len(history)
            except BaseException:
                conn.rollback()
                raise

    def prune_receipts(self, days: int = 7) -> int:
        """Bound short-term dedup receipts without deleting long-term history."""
        if days < 1:
            raise ValueError("保留天数至少为 1")
        with self.db.get_connection() as conn:
            try:
                cursor = conn.execute(
                    "DELETE FROM pull_batches WHERE created_at < datetime('now', ?)",
                    (f"-{days} days",),
                )
                conn.commit()
                return cursor.rowcount
            except BaseException:
                conn.rollback()
                raise

    def _init_business_tables(self):
        """初始化业务相关的数据库表结构"""
        try:
            # 使用CommonDatabase的上下文管理器确保连接正确关闭
            with self.db.get_connection() as conn:
                cursor = conn.cursor()

                # 创建用户表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id TEXT PRIMARY KEY,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                logger.debug("创建或验证users表")

                # 创建抽卡状态表（存储用户当前的抽卡状态）
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS gacha_states (
                        user_id TEXT PRIMARY KEY,
                        pity_5star INTEGER DEFAULT 0,
                        pity_4star INTEGER DEFAULT 0,
                        _5star_guaranteed BOOLEAN DEFAULT 0,
                        _4star_guaranteed BOOLEAN DEFAULT 0,
                        pull_count INTEGER DEFAULT 0,
                        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                    )
                """)
                logger.debug("创建或验证gacha_states表")

                # 创建抽卡历史记录表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS pull_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT,
                        item TEXT,
                        rarity TEXT,
                        pool_id TEXT,
                        pull_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                    )
                """)
                logger.debug("创建或验证pull_history表")

                # 检查并添加 pool_id 字段（如果不存在）
                cursor.execute("PRAGMA table_info(pull_history)")
                columns = [column[1] for column in cursor.fetchall()]
                if "pool_id" not in columns:
                    cursor.execute("ALTER TABLE pull_history ADD COLUMN pool_id TEXT")
                    logger.debug("为pull_history表添加pool_id字段")

                # 创建索引，提高查询性能
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pull_history_user ON pull_history(user_id)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pull_history_time ON pull_history(pull_time)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pull_history_pool ON pull_history(pool_id)"
                )
                logger.debug("创建pull_history表索引")

                conn.commit()
        except Exception as e:
            logger.error(f"初始化业务表失败: {e}")
            raise

    # 用户相关操作
    def create_user(self, user_id: str):
        """创建新用户"""
        logger.debug(f"创建用户: {user_id}")
        self.db.execute_update(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )

    def commit_draws(self, user_id: str, pool_id: str, count: int, draw, record_history: bool):
        """Read pity, generate a complete batch, and persist it as one transaction.

        BEGIN IMMEDIATE serializes competing draws before either reads the state.
        The draw callback runs against that state and must not perform I/O.
        """
        if count not in (1, 10):
            raise ValueError("只支持单抽或十连")
        with self.db.get_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT pity_5star, pity_4star, _5star_guaranteed, "
                    "_4star_guaranteed, pull_count FROM gacha_states WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                state = {
                    "pity_5star": row[0] if row else 0,
                    "pity_4star": row[1] if row else 0,
                    "_5star_guaranteed": bool(row[2]) if row else False,
                    "_4star_guaranteed": bool(row[3]) if row else False,
                    "pull_count": row[4] if row else 0,
                }
                items, updated = draw(state)
                if len(items) != count or any(item is None for item in items):
                    raise ValueError("抽卡结果不完整，未保存任何结果")
                conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
                conn.execute(
                    "INSERT INTO gacha_states "
                    "(user_id, pity_5star, pity_4star, _5star_guaranteed, "
                    "_4star_guaranteed, pull_count) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET "
                    "pity_5star=excluded.pity_5star, "
                    "pity_4star=excluded.pity_4star, "
                    "_5star_guaranteed=excluded._5star_guaranteed, "
                    "_4star_guaranteed=excluded._4star_guaranteed, "
                    "pull_count=excluded.pull_count",
                    (
                        user_id,
                        updated["pity_5star"],
                        updated["pity_4star"],
                        int(updated["_5star_guaranteed"]),
                        int(updated["_4star_guaranteed"]),
                        updated["pull_count"],
                    ),
                )
                if record_history:
                    from datetime import datetime

                    pull_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    conn.executemany(
                        "INSERT INTO pull_history "
                        "(user_id, item, rarity, pool_id, pull_time) VALUES (?, ?, ?, ?, ?)",
                        [(user_id, item.name, item.rarity, pool_id, pull_time) for item in items],
                    )
                conn.commit()
                return items
            except BaseException:
                conn.rollback()
                raise

    def save_user_state(self, user_id: str, state_data: dict[str, Any]):
        """
        保存用户抽卡状态

        Args:
            user_id: 用户ID
            state_data: 包含用户状态信息的字典
        """
        try:
            logger.debug(f"保存用户状态: {user_id}, 状态: {state_data}")

            # 使用INSERT OR IGNORE确保用户存在，不会删除旧记录
            self.db.execute_update(
                """
                INSERT OR IGNORE INTO users (user_id) VALUES (?)
            """,
                (user_id,),
            )

            self.db.execute_update(
                """
                INSERT OR REPLACE INTO gacha_states
                (user_id, pity_5star, pity_4star, _5star_guaranteed, _4star_guaranteed, pull_count)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    user_id,
                    state_data["pity_5star"],
                    state_data["pity_4star"],
                    int(state_data["_5star_guaranteed"]),
                    int(state_data["_4star_guaranteed"]),
                    state_data["pull_count"],
                ),
            )
        except Exception as e:
            logger.error(f"保存用户状态失败: {user_id}, 错误: {e}")
            raise

    def load_user_state(self, user_id: str) -> dict[str, Any] | None:
        """
        加载用户抽卡状态

        Args:
            user_id: 用户ID

        Returns:
            用户状态字典，如果不存在则返回None
        """
        try:
            logger.debug(f"加载用户状态: {user_id}")
            row = self.db.execute_query_single(
                """
                SELECT pity_5star, pity_4star, _5star_guaranteed, _4star_guaranteed, pull_count
                FROM gacha_states
                WHERE user_id = ?
            """,
                (user_id,),
            )

            if not row:
                return None

            return {
                "pity_5star": row["pity_5star"],
                "pity_4star": row["pity_4star"],
                "_5star_guaranteed": bool(row["_5star_guaranteed"]),
                "_4star_guaranteed": bool(row["_4star_guaranteed"]),
                "pull_count": row["pull_count"],
            }
        except Exception as e:
            logger.error(f"加载用户状态失败: {user_id}, 错误: {e}")
            raise

    def save_pull_history(self, user_id: str, pull_data: dict[str, Any]):
        """
        保存单次抽卡记录

        Args:
            user_id: 用户ID
            pull_data: 抽卡记录数据
        """
        try:
            logger.debug(f"保存抽卡记录: {user_id}, 物品: {pull_data['item']}")
            self.db.execute_update(
                """
                INSERT INTO pull_history
                (user_id, item, rarity, pool_id, pull_time)
                VALUES (?, ?, ?, ?, ?)
            """,
                (
                    user_id,
                    pull_data["item"],
                    pull_data.get("rarity", ""),  # 从数据中获取稀有度，默认未知
                    pull_data.get("pool_id", ""),  # 从数据中获取卡池ID，默认为空字符串
                    pull_data["pull_time"],
                ),
            )
        except Exception as e:
            logger.error(f"保存抽卡记录失败: {user_id}, 错误: {e}")
            raise

    def save_pull_history_batch(
        self, user_id: str, pull_history_list: list[dict[str, Any]]
    ):
        """
        批量保存抽卡记录

        Args:
            user_id: 用户ID
            pull_history_list: 抽卡记录列表
        """
        try:
            logger.debug(f"批量保存抽卡记录: {user_id}, 数量: {len(pull_history_list)}")

            # 确保用户存在
            self.db.execute_update(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
            )

            params_list = [
                (
                    user_id,
                    record["item"],
                    record.get("rarity", ""),  # 从数据中获取稀有度，默认未知
                    record.get("pool_id", ""),  # 从数据中获取卡池ID，默认为空字符串
                    record["pull_time"],
                )
                for record in pull_history_list
            ]

            self.db.execute_many(
                """
                INSERT INTO pull_history
                (user_id, item, rarity, pool_id, pull_time)
                VALUES (?, ?, ?, ?, ?)
            """,
                params_list,
            )
        except Exception as e:
            logger.error(f"批量保存抽卡记录失败: {user_id}, 错误: {e}")
            raise

    def load_pull_history(
        self,
        user_id: str,
        limit: int | None = None,
        offset: int | None = None,
        order: str = "desc",
        pool_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        加载用户抽卡历史

        Args:
            user_id: 用户ID
            limit: 返回记录的最大数量
            offset: 记录偏移量（用于分页）
            order: 排序方式，'asc'或'desc'
            pool_id: 卡池ID，用于过滤特定卡池的抽卡记录

        Returns:
            抽卡历史记录列表
        """
        try:
            logger.debug(
                f"加载抽卡历史: {user_id}, 限制: {limit}, 偏移: {offset}, 排序: {order}, 卡池ID: {pool_id}"
            )

            query = "SELECT id, item, rarity, pool_id, pull_time FROM pull_history WHERE user_id = ?"
            params = [user_id]

            # 添加卡池过滤条件
            if pool_id:
                query += " AND pool_id = ?"
                params.append(pool_id)

            # 添加排序
            query += " ORDER BY id"
            if order.lower() == "desc":
                query += " DESC"
            else:
                query += " ASC"

            # 添加分页参数
            if limit:
                query += " LIMIT ?"
                params.append(str(limit))

            if offset:
                query += " OFFSET ?"
                params.append(str(offset))

            rows = self.db.execute_query(query, tuple(params))

            return [
                {
                    "id": row["id"],
                    "item": row["item"],
                    "rarity": row["rarity"],
                    "pool_id": row["pool_id"],
                    "pull_time": row["pull_time"],
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"加载抽卡历史失败: {user_id}, 错误: {e}")
            raise

    def get_pull_history_count(self, user_id: str, pool_id: str | None = None) -> int:
        """
        获取用户抽卡历史记录总数

        Args:
            user_id: 用户ID
            pool_id: 卡池ID，用于过滤特定卡池的抽卡记录

        Returns:
            抽卡历史记录总数
        """
        try:
            logger.debug(f"获取抽卡历史总数: {user_id}, 卡池ID: {pool_id}")

            query = "SELECT COUNT(*) as total FROM pull_history WHERE user_id = ?"
            params = [user_id]

            # 添加卡池过滤条件
            if pool_id:
                query += " AND pool_id = ?"
                params.append(pool_id)

            row = self.db.execute_query_single(query, tuple(params))

            return row["total"] if row else 0
        except Exception as e:
            logger.error(f"获取抽卡历史总数失败: {user_id}, 错误: {e}")
            raise

    def get_user_statistics(self, user_id: str) -> dict[str, Any]:
        """
        获取用户统计数据

        Args:
            user_id: 用户ID

        Returns:
            包含统计信息的字典
        """
        try:
            logger.debug(f"获取用户统计: {user_id}")

            # 获取总抽卡次数
            total_pulls_row = self.db.execute_query_single(
                """
                SELECT COUNT(*) as total_pulls
                FROM pull_history
                WHERE user_id = ?
            """,
                (user_id,),
            )

            # 获取5星和4星抽卡次数
            rarity_stats_row = self.db.execute_query_single(
                """
                SELECT
                    COUNT(CASE WHEN rarity = '5star' THEN 1 END) as five_star_pulls,
                    COUNT(CASE WHEN rarity = '4star' THEN 1 END) as four_star_pulls
                FROM pull_history
                WHERE user_id = ?
            """,
                (user_id,),
            )

            return {
                "total_pulls": total_pulls_row["total_pulls"] if total_pulls_row else 0,
                "five_star_pulls": rarity_stats_row["five_star_pulls"]
                if rarity_stats_row
                else 0,
                "four_star_pulls": rarity_stats_row["four_star_pulls"]
                if rarity_stats_row
                else 0,
            }
        except Exception as e:
            logger.error(f"获取用户统计失败: {user_id}, 错误: {e}")
            raise

    def clear_user_data(self, user_id: str):
        """
        清除用户所有数据

        Args:
            user_id: 用户ID
        """
        try:
            logger.debug(f"清除用户数据: {user_id}")
            # 由于外键约束设置了ON DELETE CASCADE，删除用户会自动删除相关的抽卡状态和历史记录
            self.db.execute_update("DELETE FROM users WHERE user_id = ?", (user_id,))
        except Exception as e:
            logger.error(f"清除用户数据失败: {user_id}, 错误: {e}")
            raise

    def close(self):
        self.db.close()
