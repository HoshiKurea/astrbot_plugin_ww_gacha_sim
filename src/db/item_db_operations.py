"""
物品数据数据库操作模块
负责从数据库加载物品数据并提供物品数据管理功能，包括角色和武器
"""

from typing import Any

from astrbot.api import logger

from .database import CommonDatabase


class ItemDBOperations:
    """
    物品数据数据库操作类
    提供数据库驱动的物品数据管理功能
    使用 CommonDatabase 作为底层数据库交互组件
    """

    def __init__(self, db: CommonDatabase):
        """
        初始化数据库操作管理器

        Args:
            db: 数据库实例
        """
        self.db = db

    @staticmethod
    def _normalize_portrait_url(value: Any) -> Any:
        """Store the repository origin instead of a nested proxy URL."""
        if not isinstance(value, str):
            return value
        marker = "https://raw.githubusercontent.com/"
        proxy_prefix = "https://v6.gh-proxy.org/" + marker
        while value.startswith(proxy_prefix):
            value = marker + value[len(proxy_prefix):]
        return value

    def _init_tables(self, table_name="items"):
        """初始化物品相关的数据库表结构

        Args:
            table_name: 物品表名称，默认为'items'
        """
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()

                # 创建物品表
                cursor.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table_name} (
                        unique_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        external_id TEXT UNIQUE,
                        name TEXT NOT NULL,
                        rarity TEXT NOT NULL,
                        type TEXT NOT NULL,
                        affiliated_type TEXT,
                        portrait_path TEXT,
                        portrait_url TEXT,
                        apply_gradient INTEGER DEFAULT 0
                    )
                """)
                logger.debug(f"创建或验证{table_name}表")

                # 迁移：为旧表添加 apply_gradient 列（先检查列是否存在）
                cursor.execute(f"PRAGMA table_info({table_name})")
                existing_cols = {row[1] for row in cursor.fetchall()}
                if "apply_gradient" not in existing_cols:
                    cursor.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN apply_gradient INTEGER DEFAULT 0"
                    )
                    conn.commit()
                    logger.debug(f"为{table_name}表添加 apply_gradient 列")

                # 创建物品表索引
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_name ON {table_name}(name)"
                )
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_rarity ON {table_name}(rarity)"
                )
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_external_id ON {table_name}(external_id)"
                )
                logger.debug(f"创建{table_name}表索引")

                conn.commit()

            # 检查是否需要添加默认物品数据
            self._add_default_items(table_name)
        except Exception as e:
            logger.error(f"初始化物品表失败: {e}")
            raise

    def _add_default_items(self, table_name="items"):
        """添加默认物品数据到数据库

        Args:
            table_name: 物品表名称，默认为'items'
        """
        try:
            # 检查表中是否已有数据
            item_count = self.get_items_count(table_name)
            if item_count > 0:
                logger.debug(
                    f"{table_name}表中已有{item_count}个物品，跳过添加默认物品"
                )
                return

            logger.info(f"{table_name}表为空，开始添加默认物品数据")

            # 从CSV文件读取默认物品数据
            import csv
            from pathlib import Path

            # CSV文件路径
            csv_path = Path(__file__).parent.parent / "assets" / "data" / "default.csv"

            if not csv_path.exists():
                logger.error(f"默认物品CSV文件不存在: {csv_path}")
                return

            logger.info(f"从CSV文件读取默认物品数据: {csv_path}")

            # 读取CSV文件
            default_items = []
            with open(csv_path, encoding="utf-8-sig") as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    # 处理稀有度格式，确保是{number}star格式
                    rarity = row["rarity"]
                    if rarity == "2":
                        formatted_rarity = "2star"
                    elif rarity == "3":
                        formatted_rarity = "3star"
                    elif rarity == "4":
                        formatted_rarity = "4star"
                    elif rarity == "5":
                        formatted_rarity = "5star"
                    else:
                        formatted_rarity = rarity

                    # 构建物品数据
                    item_data = {
                        "name": row["name"],
                        "rarity": formatted_rarity,
                        "type": row["type"],
                        "affiliated_type": row["affiliated_type"],
                        "portrait_path": "",
                        "portrait_url": row["portrait_url"],
                    }
                    default_items.append(item_data)

            if not default_items:
                logger.error(f"CSV文件中没有有效物品数据: {csv_path}")
                return

            # 批量添加默认物品
            result = self.add_items_batch(default_items, table_name)
            if result:
                logger.info(f"成功添加{len(default_items)}个默认物品到{table_name}表")
            else:
                logger.error("添加默认物品失败")
        except Exception as e:
            logger.error(f"添加默认物品失败: {e}")
            raise

    def _map_row_to_item(self, row) -> dict[str, Any]:
        """
        将数据库行映射为物品字典

        Args:
            row: 数据库查询结果行

        Returns:
            物品字典
        """
        # 处理portrait_url字段，兼容旧数据库
        portrait_url = ""
        if "portrait_url" in row.keys():
            portrait_url = row["portrait_url"]

        apply_gradient = False
        if "apply_gradient" in row.keys() and row["apply_gradient"]:
            apply_gradient = bool(row["apply_gradient"])

        return {
            "external_id": row["external_id"],
            "name": row["name"],
            "rarity": row["rarity"],
            "type": row["type"],
            "affiliated_type": row["affiliated_type"],
            "portrait_path": row["portrait_path"],
            "portrait_url": portrait_url,
            "apply_gradient": apply_gradient,
        }

    def load_all_items(self, table_name="items") -> dict[str, dict[str, Any]]:
        """
        从数据库加载所有物品信息

        Args:
            table_name: 物品表名称，默认为'items'

        Returns:
            包含所有物品信息的字典，键为物品ID
        """
        try:
            logger.debug(f"加载{table_name}表的所有物品")
            # 在查询前先初始化表，确保表存在
            self._init_tables(table_name)
            rows = self.db.execute_query(
                f"SELECT * FROM {table_name} ORDER BY unique_id"
            )

            items = {}
            for row in rows:
                item_id = row["external_id"]
                items[item_id] = self._map_row_to_item(row)

            logger.debug(f"成功加载 {len(items)} 个物品")
            return items
        except Exception as e:
            logger.error(f"加载所有物品失败: {e}")
            raise

    def get_item_by_id(self, item_id: str, table_name="items") -> dict[str, Any] | None:
        """
        根据物品external_id获取物品详细信息

        Args:
            item_id: 物品external_id
            table_name: 物品表名称，默认为'items'

        Returns:
            物品详细信息字典，如果不存在则返回None
        """
        try:
            logger.debug(f"根据ID获取物品: {item_id}, 表: {table_name}")
            # 在查询前先初始化表，确保表存在
            self._init_tables(table_name)
            row = self.db.execute_query_single(
                f"SELECT * FROM {table_name} WHERE external_id = ?", (item_id,)
            )
            return self._map_row_to_item(row) if row else None
        except Exception as e:
            logger.error(f"获取物品失败: {item_id}, 表: {table_name}, 错误: {e}")
            raise

    def item_exists(self, item_id: str, table_name="items") -> bool:
        """
        检查物品是否存在于数据库中

        Args:
            item_id: 物品external_id
            table_name: 物品表名称，默认为'items'

        Returns:
            如果物品存在返回True，否则返回False
        """
        try:
            logger.debug(f"检查物品是否存在: {item_id}, 表: {table_name}")
            # 在查询前先初始化表，确保表存在
            self._init_tables(table_name)
            row = self.db.execute_query_single(
                f"SELECT COUNT(*) as count FROM {table_name} WHERE external_id = ?",
                (item_id,),
            )
            return row["count"] > 0 if row else False
        except Exception as e:
            logger.error(f"检查物品存在性失败: {item_id}, 表: {table_name}, 错误: {e}")
            raise

    def _generate_default_external_id(self, item_data: dict[str, Any]) -> str:
        """
        自动生成默认的 external_id

        Args:
            item_data: 物品数据字典

        Returns:
            生成的 external_id
        """
        import hashlib

        # 使用名称、稀有度、类型和所属类型生成唯一标识符，与配置文件保持一致
        affiliated_type = item_data.get("affiliated_type", "")
        base_str = f"{item_data['name']}_{item_data['rarity']}_{item_data['type']}_{affiliated_type}"
        # 使用MD5哈希算法，确保跨进程一致性
        hash_value = hashlib.md5(base_str.encode("utf-8")).hexdigest()
        # 取前4位作为哈希后缀
        hash_suffix = hash_value[:4]
        # 格式化为 {type}_{name}_{hash}
        type_prefix = item_data["type"][:3].lower()  # 类型前缀
        name_part = (
            item_data["name"].replace(" ", "_").replace(".", "_")[:10]
        )  # 名称部分，最多10个字符，替换特殊字符
        return f"{type_prefix}_{name_part}_{hash_suffix}"  # 格式：chr_莫宁_a1b2c3d4

    def add_item(self, item_data: dict[str, Any], table_name="items") -> bool:
        """
        添加物品到数据库

        Args:
            item_data: 包含物品信息的字典，应包含name, rarity, type
            table_name: 物品表名称，默认为'items'

        Returns:
            成功返回True，失败返回False
        """

        required_fields = {"name", "rarity", "type"}
        missing_fields = required_fields - set(item_data.keys())
        if missing_fields:
            raise ValueError(f"缺少必需字段: {missing_fields}")

        try:
            # 自动生成 external_id（如果未提供）
            if "external_id" not in item_data or not item_data["external_id"]:
                item_data["external_id"] = self._generate_default_external_id(item_data)

            logger.debug(
                f"添加物品: {item_data['name']}, 表: {table_name}, external_id: {item_data['external_id']}"
            )

            # 统一稀有度格式为{number}star
            rarity = item_data["rarity"]
            if isinstance(rarity, int):
                formatted_rarity = f"{rarity}star"
            elif rarity in ("3", "4", "5"):
                formatted_rarity = f"{rarity}star"
            else:
                formatted_rarity = str(rarity)

            result = self.db.execute_update(
                f"""
                INSERT INTO {table_name}
                (external_id, name, rarity, type, affiliated_type, portrait_url, apply_gradient)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    item_data["external_id"],
                    item_data["name"],
                    formatted_rarity,
                    item_data["type"],
                    item_data.get("affiliated_type", ""),
                    self._normalize_portrait_url(item_data.get("portrait_url", "")),
                    1 if item_data.get("apply_gradient") else 0,
                ),
            )
            return result >= 0
        except Exception as e:
            logger.error(
                f"添加物品失败: {item_data.get('name', '未知')}, 表: {table_name}, 错误: {e}"
            )
            return False

    def add_items_batch(
        self, items_data: list[dict[str, Any]], table_name="items"
    ) -> bool:
        """
        批量添加物品到数据库

        Args:
            items_data: 包含多个物品信息字典的列表
            table_name: 物品表名称，默认为'items'

        Returns:
            成功返回True，失败返回False
        """
        if not items_data:
            logger.debug("没有物品需要添加")
            return True

        required_fields = {"name", "rarity", "type"}
        for item_data in items_data:
            missing_fields = required_fields - set(item_data.keys())
            if missing_fields:
                raise ValueError(
                    f"缺少必需字段: {missing_fields} 物品: {item_data.get('name', '未知')}"
                )

        try:
            logger.debug(f"批量添加 {len(items_data)} 个物品到表: {table_name}")
            params_list = []

            for item in items_data:
                # 自动生成 external_id（如果未提供）
                if "external_id" not in item or not item["external_id"]:
                    item["external_id"] = self._generate_default_external_id(item)

                # 统一稀有度格式为{number}star
                rarity = item["rarity"]
                if isinstance(rarity, int):
                    formatted_rarity = f"{rarity}star"
                elif rarity in ("3", "4", "5"):
                    formatted_rarity = f"{rarity}star"
                else:
                    formatted_rarity = str(rarity)

                params_list.append(
                    (
                        item["external_id"],
                        item["name"],
                        formatted_rarity,
                        item["type"],
                        item.get("affiliated_type", ""),
                        self._normalize_portrait_url(item.get("portrait_url", "")),
                        1 if item.get("apply_gradient") else 0,
                    )
                )

            result = self.db.execute_many(
                f"""
                INSERT INTO {table_name}
                (external_id, name, rarity, type, affiliated_type, portrait_url, apply_gradient)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                params_list,
            )
            logger.debug(f"成功添加 {result} 个物品")
            return result >= 0
        except Exception as e:
            logger.error(f"批量添加物品失败: {table_name}, 错误: {e}")
            return False

    def update_item(
        self,
        item_id: str,
        update_data: dict[str, Any],
        table_name="items",
        update_configs: bool = False,
        config_manager=None,
    ) -> bool:
        """
        更新物品信息

        Args:
            item_id: 要更新的物品ID
            update_data: 包含要更新字段的字典
            table_name: 物品表名称，默认为'items'
            update_configs: 是否更新相关配置文件
            config_manager: 卡池配置管理器实例

        Returns:
            成功返回True，失败返回False
        """
        if not update_data:
            logger.debug(f"没有更新数据，跳过更新: {item_id}")
            return True

        try:
            # 获取物品当前信息
            current_item = self.get_item_by_id(item_id, table_name)
            if not current_item:
                logger.error(f"物品不存在: {item_id}")
                return False

            # 检查关键属性是否发生变化
            key_fields = ["type", "name", "rarity"]
            key_changed = any(
                update_data.get(field) is not None
                and str(update_data[field]) != str(current_item[field])
                for field in key_fields
            )

            # 构建动态更新语句
            valid_fields = [
                "name",
                "rarity",
                "type",
                "affiliated_type",
                "portrait_url",
                "apply_gradient",
            ]
            fields = []
            values = []

            for field, value in update_data.items():
                if field in valid_fields:
                    # 统一稀有度格式为{number}star
                    if field == "rarity":
                        rarity = value
                        if isinstance(rarity, int):
                            formatted_rarity = f"{rarity}star"
                        elif rarity in ("3", "4", "5"):
                            formatted_rarity = f"{rarity}star"
                        else:
                            formatted_rarity = str(rarity)
                        values.append(formatted_rarity)
                    elif field == "apply_gradient":
                        values.append(1 if value else 0)
                    elif field == "portrait_url":
                        values.append(self._normalize_portrait_url(value))
                    else:
                        values.append(value)
                    fields.append(f"{field} = ?")

            # 如果关键属性发生变化，需要重新生成external_id
            if key_changed:
                # 合并当前数据和更新数据，用于生成新的external_id
                updated_data = current_item.copy()
                updated_data.update(update_data)
                # 统一稀有度格式
                if "rarity" in updated_data:
                    rarity = updated_data["rarity"]
                    if isinstance(rarity, int):
                        updated_data["rarity"] = f"{rarity}star"
                    elif rarity in ("3", "4", "5"):
                        updated_data["rarity"] = f"{rarity}star"
                    else:
                        updated_data["rarity"] = str(rarity)
                # 生成新的external_id
                new_external_id = self._generate_default_external_id(updated_data)
                fields.append("external_id = ?")
                values.append(new_external_id)

            if not fields:
                logger.debug(f"没有有效字段需要更新: {item_id}")
                return False

            values.append(item_id)
            set_clause = ", ".join(fields)

            result = self.db.execute_update(
                f"UPDATE {table_name} SET {set_clause} WHERE external_id = ?",
                tuple(values),
            )
            logger.debug(f"成功更新 {result} 个物品")

            # 如果需要更新配置文件且物品更新成功
            if result > 0 and update_configs and config_manager:
                # 获取物品的旧external_id和新external_id
                old_external_id = current_item["external_id"]

                # 获取更新后的物品信息
                updated_item = self.get_item_by_id(item_id, table_name)
                new_external_id = updated_item["external_id"] if updated_item else None

                if new_external_id and old_external_id != new_external_id:
                    # 更新配置文件
                    self._update_configs_for_item_change(
                        old_unique_id="",  # 不再使用unique_id
                        old_external_id=old_external_id,
                        new_external_id=new_external_id,
                        config_manager=config_manager,
                    )

            return result > 0
        except Exception as e:
            logger.error(f"更新物品失败: {item_id}, 表: {table_name}, 错误: {e}")
            return False

    def _update_configs_for_item_change(
        self,
        old_unique_id: str,
        old_external_id: str,
        new_external_id: str,
        config_manager,
    ) -> None:
        """
        更新所有配置文件中对该物品的引用

        Args:
            old_unique_id: 不再使用，保留兼容
            old_external_id: 物品的旧external_id
            new_external_id: 物品的新external_id
            config_manager: 卡池配置管理器实例
        """
        try:
            logger.debug(
                f"开始更新配置文件中的物品引用: {old_external_id} -> {new_external_id}"
            )

            # 获取所有配置
            all_configs = config_manager._configs.values()

            for config in all_configs:
                # 检查是否需要更新此配置
                config_updated = False

                # 更新included_item_ids
                for rarity, item_ids in config.included_item_ids.items():
                    updated_ids = []
                    for item_id in item_ids:
                        if item_id == old_external_id:
                            updated_ids.append(new_external_id)
                            config_updated = True
                        else:
                            updated_ids.append(item_id)
                    config.included_item_ids[rarity] = updated_ids

                # 更新rate_up_item_ids
                for rarity, item_ids in config.rate_up_item_ids.items():
                    updated_ids = []
                    for item_id in item_ids:
                        if item_id == old_external_id:
                            updated_ids.append(new_external_id)
                            config_updated = True
                        else:
                            updated_ids.append(item_id)
                    config.rate_up_item_ids[rarity] = updated_ids

                # 如果配置被更新，保存到文件
                if config_updated:
                    # 找到配置对应的文件路径
                    file_path = None
                    for path, cp_id in config_manager._file_path_to_cp_id.items():
                        if cp_id == config.cp_id:
                            file_path = path
                            break

                    if file_path:
                        config_manager._save_config(file_path, config)
                        logger.debug(f"更新配置文件: {file_path}")

            logger.debug("完成更新配置文件中的物品引用")
        except Exception as e:
            logger.error(f"更新配置文件中的物品引用失败: {e}")

    def _remove_item_from_configs(
        self, item_id: str, external_id: str, config_manager
    ) -> None:
        """
        从所有配置文件中移除该物品的引用

        Args:
            item_id: 不再使用，保留兼容
            external_id: 物品的external_id
            config_manager: 卡池配置管理器实例
        """
        try:
            logger.debug(f"开始从配置文件中移除物品引用: {external_id}")

            # 获取所有配置
            all_configs = config_manager._configs.values()

            for config in all_configs:
                # 检查是否需要更新此配置
                config_updated = False

                # 从included_item_ids中移除
                for rarity, item_ids in config.included_item_ids.items():
                    updated_ids = [id for id in item_ids if id != external_id]
                    if len(updated_ids) != len(item_ids):
                        config.included_item_ids[rarity] = updated_ids
                        config_updated = True

                # 从rate_up_item_ids中移除
                for rarity, item_ids in config.rate_up_item_ids.items():
                    updated_ids = [id for id in item_ids if id != external_id]
                    if len(updated_ids) != len(item_ids):
                        config.rate_up_item_ids[rarity] = updated_ids
                        config_updated = True

                # 如果配置被更新，保存到文件
                if config_updated:
                    # 找到配置对应的文件路径
                    file_path = None
                    for path, cp_id in config_manager._file_path_to_cp_id.items():
                        if cp_id == config.cp_id:
                            file_path = path
                            break

                    if file_path:
                        config_manager._save_config(file_path, config)
                        logger.debug(f"更新配置文件: {file_path}")

            logger.debug("完成从配置文件中移除物品引用")
        except Exception as e:
            logger.error(f"从配置文件中移除物品引用失败: {e}")

    def delete_item(
        self,
        item_id: str,
        table_name="items",
        update_configs: bool = False,
        config_manager=None,
    ) -> bool:
        """
        从数据库中删除物品

        Args:
            item_id: 要删除的物品ID
            table_name: 物品表名称，默认为'items'
            update_configs: 是否更新相关配置文件
            config_manager: 卡池配置管理器实例

        Returns:
            成功返回True，失败返回False
        """
        try:
            logger.debug(f"删除物品: {item_id}, 表: {table_name}")

            # 如果需要更新配置文件，先获取物品的external_id
            external_id = None
            if update_configs and config_manager:
                item = self.get_item_by_id(item_id, table_name)
                external_id = item["external_id"] if item else None

            # 删除物品
            result = self.db.execute_update(
                f"DELETE FROM {table_name} WHERE external_id = ?", (item_id,)
            )
            logger.debug(f"成功删除 {result} 个物品")

            # 如果需要更新配置文件且物品删除成功
            if result > 0 and update_configs and config_manager and external_id:
                # 从配置文件中移除物品引用
                self._remove_item_from_configs(item_id, external_id, config_manager)

            return result > 0
        except Exception as e:
            logger.error(f"删除物品失败: {item_id}, 表: {table_name}, 错误: {e}")
            return False

    def delete_items_batch(self, item_ids: list[str], table_name="items") -> bool:
        """
        批量删除物品

        Args:
            item_ids: 要删除的物品external_id列表
            table_name: 物品表名称，默认为'items'

        Returns:
            成功返回True，失败返回False
        """
        if not item_ids:
            logger.debug("没有物品需要删除")
            return True

        try:
            logger.debug(f"批量删除 {len(item_ids)} 个物品，表: {table_name}")
            # 构建参数列表
            params_list = [(item_id,) for item_id in item_ids]
            result = self.db.execute_many(
                f"DELETE FROM {table_name} WHERE external_id = ?", params_list
            )
            logger.debug(f"成功删除 {result} 个物品")
            return result >= 0
        except Exception as e:
            logger.error(f"批量删除物品失败: {table_name}, 错误: {e}")
            return False

    def get_items_by_rarity(
        self, rarity: str, table_name="items"
    ) -> list[dict[str, Any]]:
        """
        根据稀有度获取物品列表

        Args:
            rarity: 稀有度（'5star', '4star', '3star'等）
            table_name: 物品表名称，默认为'items'

        Returns:
            符合条件的物品列表
        """
        try:
            logger.debug(f"根据稀有度获取物品: {rarity}, 表: {table_name}")
            rows = self.db.execute_query(
                f"SELECT * FROM {table_name} WHERE rarity = ? ORDER BY unique_id",
                (rarity,),
            )

            items = [self._map_row_to_item(row) for row in rows]
            logger.debug(f"找到 {len(items)} 个稀有度为 {rarity} 的物品")
            return items
        except Exception as e:
            logger.error(
                f"根据稀有度获取物品失败: {rarity}, 表: {table_name}, 错误: {e}"
            )
            raise

    def get_items_by_type(
        self, item_type: str, table_name="items"
    ) -> list[dict[str, Any]]:
        """
        根据物品类型获取物品列表

        Args:
            item_type: 物品类型（character, weapon等）
            table_name: 物品表名称，默认为'items'

        Returns:
            符合条件的物品列表
        """
        try:
            logger.debug(f"根据类型获取物品: {item_type}, 表: {table_name}")
            rows = self.db.execute_query(
                f"SELECT * FROM {table_name} WHERE type = ? ORDER BY unique_id",
                (item_type,),
            )

            items = [self._map_row_to_item(row) for row in rows]
            logger.debug(f"找到 {len(items)} 个类型为 {item_type} 的物品")
            return items
        except Exception as e:
            logger.error(
                f"根据类型获取物品失败: {item_type}, 表: {table_name}, 错误: {e}"
            )
            raise

    def search_items_by_name(
        self, name: str, table_name="items", limit: int = 100
    ) -> list[dict[str, Any]]:
        """
        根据名称搜索物品

        Args:
            name: 要搜索的物品名称
            table_name: 物品表名称，默认为'items'
            limit: 返回结果的最大数量

        Returns:
            匹配的物品列表
        """
        try:
            logger.debug(f"搜索物品: {name}, 表: {table_name}")
            rows = self.db.execute_query(
                f"SELECT * FROM {table_name} WHERE name LIKE ? ORDER BY unique_id LIMIT ?",
                (f"%{name}%", limit),
            )

            items = [self._map_row_to_item(row) for row in rows]
            logger.debug(f"找到 {len(items)} 个匹配的物品")
            return items
        except Exception as e:
            logger.error(f"搜索物品失败: {name}, 表: {table_name}, 错误: {e}")
            raise

    def get_items_by_filters(
        self, filters: dict[str, Any], table_name="items"
    ) -> list[dict[str, Any]]:
        """
        根据多个条件筛选物品

        Args:
            filters: 筛选条件字典，支持rarity, type
            table_name: 物品表名称，默认为'items'

        Returns:
            符合条件的物品列表
        """
        try:
            logger.debug(f"根据条件筛选物品: {filters}, 表: {table_name}")

            # 构建查询条件
            conditions = []
            params = []

            if "rarity" in filters:
                conditions.append("rarity = ?")
                params.append(filters["rarity"])

            if "type" in filters:
                conditions.append("type = ?")
                params.append(filters["type"])

            query = f"SELECT * FROM {table_name}"
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY unique_id"

            rows = self.db.execute_query(query, tuple(params))

            items = [self._map_row_to_item(row) for row in rows]
            logger.debug(f"找到 {len(items)} 个符合条件的物品")
            return items
        except Exception as e:
            logger.error(f"根据条件筛选物品失败: {table_name}, 错误: {e}")
            raise

    def get_items_count(self, table_name="items") -> int:
        """
        获取物品总数

        Args:
            table_name: 物品表名称，默认为'items'

        Returns:
            物品总数
        """
        try:
            logger.debug(f"获取{table_name}表的物品总数")
            row = self.db.execute_query_single(
                f"SELECT COUNT(*) as count FROM {table_name}"
            )
            count = row["count"] if row else 0
            logger.debug(f"物品总数: {count}")
            return count
        except Exception as e:
            logger.error(f"获取物品总数失败: {table_name}, 错误: {e}")
            raise

    def sync_new_items_from_csv(self, table_name="items") -> int:
        """从 CSV 同步新增的物品到数据库，仅插入不存在的物品，不覆盖已有数据。

        用于插件升级时自动补充新增的角色/武器数据。

        Args:
            table_name: 物品表名称

        Returns:
            int: 本次同步新增的物品数量
        """
        import csv
        from pathlib import Path

        csv_path = Path(__file__).parent.parent / "assets" / "data" / "default.csv"
        if not csv_path.exists():
            logger.warning(f"CSV 文件不存在，跳过物品同步: {csv_path}")
            return 0

        # 确保表已初始化
        self._init_tables(table_name)

        # 加载数据库中已有的物品（按 external_id 索引）
        existing = self.load_all_items(table_name)

        # 读取 CSV 并对比
        added = 0
        with open(csv_path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                item_dict = {
                    "name": row["name"],
                    "rarity": row["rarity"],
                    "type": row["type"],
                    "affiliated_type": row.get("affiliated_type", ""),
                    "portrait_url": row.get("portrait_url", ""),
                }
                # 生成 external_id 并与数据库已有数据比对
                external_id = self._generate_default_external_id(item_dict)
                if external_id not in existing:
                    item_dict["external_id"] = external_id
                    if self.add_item(item_dict, table_name):
                        added += 1

        if added > 0:
            logger.info(f"同步 CSV → {table_name} 完成，新增 {added} 个物品")
        return added

    def clear_table(self, table_name="items") -> bool:
        """
        清空物品表中的所有数据

        Args:
            table_name: 物品表名称，默认为'items'

        Returns:
            成功返回True，失败返回False
        """
        try:
            logger.debug(f"清空{table_name}表中的所有数据")
            # SQLite不支持TRUNCATE，直接使用DELETE FROM并重置自增计数器
            with self.db.get_connection() as conn:
                cursor = conn.cursor()

                # 删除所有数据
                cursor.execute(f"DELETE FROM {table_name}")
                # 重置SQLite自增计数器
                cursor.execute(
                    "DELETE FROM sqlite_sequence WHERE name = ?", (table_name,)
                )
                conn.commit()
            logger.debug(f"使用DELETE方式清空{table_name}表并重置自增计数器")
            return True
        except Exception as e:
            logger.error(f"清空表失败: {table_name}, 错误: {e}")
            return False

    def clear_table_with_transaction(self, table_name="items") -> bool:  # type: ignore[attr-defined]
        """
        使用事务清空物品表中的所有数据，确保原子性操作

        Args:
            table_name: 物品表名称，默认为'items'

        Returns:
            成功返回True，失败返回False
        """
        try:
            logger.debug(f"使用事务清空{table_name}表")
            with self.db.get_connection() as conn:
                cursor = conn.cursor()

                # 开始事务
                cursor.execute("BEGIN TRANSACTION")

                # 删除所有数据
                cursor.execute(f"DELETE FROM {table_name}")

                # 重置SQLite自增计数器
                cursor.execute(
                    "DELETE FROM sqlite_sequence WHERE name = ?", (table_name,)
                )

                # 提交事务
                conn.commit()

                logger.debug(
                    f"成功清空{table_name}表，删除了所有数据并重置了自增计数器"
                )
                return True
        except Exception as e:
            logger.error(f"清空表失败: {table_name}, 错误: {e}")
            return False

    def get_items_list(self, table_name="items") -> list[dict[str, Any]]:
        """
        获取物品列表

        Args:
            table_name: 物品表名称，默认为'items'

        Returns:
            物品列表
        """
        try:
            logger.debug(f"获取{table_name}表的物品列表")

            # 初始化表（如果不存在则创建）
            self._init_tables(table_name)

            rows = self.db.execute_query(
                f"SELECT * FROM {table_name} ORDER BY unique_id"
            )
            return [self._map_row_to_item(row) for row in rows]
        except Exception as e:
            logger.error(f"获取物品列表失败: {table_name}, 错误: {e}")
            # 如果是表不存在的错误，返回空列表
            if "no such table" in str(e):
                return []
            raise
