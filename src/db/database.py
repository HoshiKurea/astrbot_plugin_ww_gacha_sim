"""
数据库交互模块
负责所有与数据库相关的操作，包括数据查询、插入、更新和删除
只包含基本的、业务无关的数据库操作方法
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import StarTools

# 定义插件路径 (使用 StarTools 获取更稳健的路径)
# 注意：这里我们不再依赖相对路径计算，而是直接使用 StarTools 获取插件根目录（如果可用），
# 或者保留相对路径但添加注释说明其局限性。
# 由于 StarTools 主要用于获取数据目录，获取插件代码目录通常依赖 __file__。
# 我们可以将其指向 src 的父目录，即插件根目录。
PLUGIN_PATH = Path(__file__).resolve().parent.parent.parent


class CommonDatabase:
    """通用数据库操作类"""

    SCHEMA_VERSION = 2  # 当前数据库架构版本

    def __init__(
        self,
        db_path: Path | None = None,
    ):
        """
        初始化数据库连接

        Args:
            db_path: 数据库文件路径
        """
        if db_path is None:
            self.db_path = (
                Path(StarTools.get_data_dir("astrbot_plugin_ww_gacha_sim"))
                / "ww_gacha_sim_data.db"
            )
        else:
            self.db_path = db_path
        self._ensure_directory_exists()

        # 线程局部存储，用于复用数据库连接
        self._local = threading.local()

        self.init_db()
        self._init_metadata_table()

    def _ensure_directory_exists(self):
        """确保数据库所在目录存在"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def init_db(self):
        """初始化数据库表结构 - 由子类或专门的初始化函数负责"""
        pass

    def _get_thread_local_connection(self):
        """获取线程局部的数据库连接"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            try:
                self._local.conn = sqlite3.connect(self.db_path)
                self._local.conn.execute("PRAGMA busy_timeout = 5000")
                self._local.conn.execute("PRAGMA foreign_keys = ON")  # 启用外键约束
                self._local.conn.execute(
                    "PRAGMA journal_mode = WAL"
                )  # 启用WAL模式，提高并发性能
            except sqlite3.Error as e:
                logger.error(f"数据库连接错误: {e}")
                raise
        return self._local.conn

    def close_thread_local_connection(self):
        """关闭当前线程的数据库连接"""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except sqlite3.Error as e:
                logger.error(f"关闭数据库连接错误: {e}")
            finally:
                self._local.conn = None

    @contextmanager
    def get_connection(self):
        """
        获取数据库连接上下文

        注意：现在复用线程局部连接，不再每次都关闭。
        """
        conn = self._get_thread_local_connection()
        try:
            yield conn
            # 如果是写操作，调用者应该手动 commit，或者我们在 execute_update 中 commit
            # 对于复用的连接，我们不在此处 close
        except BaseException:
            if conn:
                conn.rollback()
            raise
        # finally 块中不再关闭连接

    # 通用数据库操作方法
    def execute_query(self, query: str, params: tuple = ()) -> list[sqlite3.Row]:
        """
        执行查询操作

        Args:
            query: SQL查询语句
            params: 查询参数

        Returns:
            查询结果列表
        """
        try:
            with self.get_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(query, params)
                return cursor.fetchall()
        except sqlite3.Error as e:
            logger.error(f"查询执行错误: {e}, SQL: {query}, Params: {params}")
            raise

    def execute_query_single(self, query: str, params: tuple = ()) -> sqlite3.Row:
        """
        执行查询操作，返回单个结果

        Args:
            query: SQL查询语句
            params: 查询参数

        Returns:
            查询结果，如果没有结果返回None
        """
        results = self.execute_query(query, params)
        return results[0] if results else None

    def execute_update(self, query: str, params: tuple = ()) -> int:
        """
        执行更新操作（INSERT, UPDATE, DELETE）

        Args:
            query: SQL更新语句
            params: 更新参数

        Returns:
            影响的行数
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)
                conn.commit()
                logger.debug(
                    f"更新执行成功，影响行数: {cursor.rowcount}, SQL: {query}, Params: {params}"
                )
                return cursor.rowcount
        except sqlite3.Error as e:
            logger.error(f"更新执行错误: {e}, SQL: {query}, Params: {params}")
            raise

    def execute_many(self, query: str, params_list: list[tuple]) -> int:
        """
        执行批量操作

        Args:
            query: SQL语句
            params_list: 参数列表

        Returns:
            影响的行数
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.executemany(query, params_list)
                conn.commit()
                logger.debug(f"批量执行成功，影响行数: {cursor.rowcount}, SQL: {query}")
                return cursor.rowcount
        except sqlite3.Error as e:
            logger.error(f"批量执行错误: {e}, SQL: {query}")
            raise

    def execute_script(self, script: str) -> None:
        """
        执行SQL脚本

        Args:
            script: SQL脚本内容
        """
        try:
            with self.get_connection() as conn:
                conn.executescript(script)
                conn.commit()
                logger.debug("SQL脚本执行成功")
        except sqlite3.Error as e:
            logger.error(f"SQL脚本执行错误: {e}")
            raise

    # ------------------------------------------------------------------
    # Schema version tracking
    # ------------------------------------------------------------------

    def _init_metadata_table(self):
        """创建元数据表用于跟踪数据库版本"""
        self.execute_update("""
            CREATE TABLE IF NOT EXISTS _metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

    def get_schema_version(self) -> int:
        """获取当前数据库架构版本，不存在则返回 0"""
        row = self.execute_query_single(
            "SELECT value FROM _metadata WHERE key = 'schema_version'"
        )
        return int(row["value"]) if row else 0

    def set_schema_version(self, version: int):
        """设置数据库架构版本"""
        self.execute_update(
            "INSERT OR REPLACE INTO _metadata (key, value) VALUES ('schema_version', ?)",
            (str(version),),
        )

    def close(self):
        """Close the connection owned by the calling thread."""
        self.close_thread_local_connection()
