"""
物品数据管理模块
负责从数据库加载物品数据并提供物品数据管理功能，包括角色和武器
"""

from functools import lru_cache
from typing import Any

from ..db.item_db_operations import ItemDBOperations
from ..security import validate_group


class Item:
    """统一物品类，包含所有物品共有的属性"""

    def __init__(
        self,
        name: str,
        rarity: str,
        item_type: str,
        affiliated_type: str,
        portrait_url: str = "",
        external_id: str | None = None,
    ):
        """
        初始化物品

        Args:
            name: 物品名称
            rarity: 物品稀有度 ('5star', '4star', '3star')
            item_type: 物品类型 (character, weapon)
            affiliated_type: 关联类型 (character, weapon)
            portrait_url: 物品立绘URL
            external_id: 物品外部ID，用于稳定标识物品
        """
        if external_id is None:
            raise ValueError("external_id must be provided")
        self.external_id = external_id
        self.name = name
        self.rarity = rarity
        self.type = item_type
        self.affiliated_type = affiliated_type
        self.portrait_url = portrait_url

    @staticmethod
    @lru_cache(maxsize=128)
    def create_item(
        name: str,
        rarity: str,
        item_type: str,
        affiliated_type: str,
        portrait_url: str = "",
        external_id: str | None = None,
    ):
        """
        创建物品实例，使用LRU缓存确保相同参数的物品返回同一实例

        Args:
            name: 物品名称
            rarity: 物品稀有度 ('5star', '4star', '3star')
            item_type: 物品类型 (character, weapon)
            affiliated_type: 关联类型 (character, weapon)
            portrait_url: 物品立绘URL
            external_id: 物品外部ID，用于稳定标识物品
        """
        return Item(
            name,
            rarity,
            item_type,
            affiliated_type,
            portrait_url,
            external_id,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.external_id,
            "external_id": self.external_id,
            "name": self.name,
            "rarity": self.rarity,
            "type": self.type,
            "affiliated_type": self.affiliated_type,
            "portrait_url": self.portrait_url,
        }

    def __eq__(self, other):
        """比较两个物品是否相同，基于external_id"""
        if not isinstance(other, Item):
            return False
        return self.external_id == other.external_id

    def __hash__(self):
        """使物品对象可哈希，基于external_id"""
        return hash(self.external_id)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Item":
        """根据字典创建实例"""
        # 检查必要字段是否存在
        if not data:
            raise ValueError("Item data dictionary is empty")
        # 验证必要字段
        required_fields = {
            "external_id",
            "name",
            "rarity",
            "type",
            "affiliated_type",
        }
        if not required_fields.issubset(data.keys()):
            raise ValueError(
                f"Missing required fields: {required_fields - set(data.keys())}"
            )

        # 统一稀有度格式为{number}star
        rarity_value = data["rarity"]
        if isinstance(rarity_value, int):
            rarity_value = f"{rarity_value}star"
        elif rarity_value in ("3", "4", "5"):
            rarity_value = f"{rarity_value}star"
        else:
            rarity_value = str(rarity_value)

        return cls.create_item(
            name=data["name"],
            rarity=rarity_value,
            item_type=data["type"],
            affiliated_type=data["affiliated_type"],
            portrait_url=data.get("portrait_url", ""),
            external_id=data["external_id"],
        )


class ItemManager:
    """物品数据管理类，统一管理物品数据"""

    def __init__(
        self,
        db_ops: ItemDBOperations,
        config_group: str = "default",
    ):
        """初始化物品数据管理器

        Args:
            db_ops: 数据库操作实例
            config_group: 配置组名称，用于确定使用哪个表
        """
        self.db_ops = db_ops
        self.config_group = validate_group(config_group)
        # 根据配置组确定表名
        self.table_name = f"{self.config_group}_items"
        # 加载所有物品数据到内存缓存
        self._item_details = self.db_ops.load_all_items(self.table_name)

    def set_config_group(self, config_group: str):
        """切换配置组

        Args:
            config_group: 新的配置组名称
        """
        self.config_group = validate_group(config_group)
        self.table_name = f"{self.config_group}_items"
        # 重新加载物品数据
        self._item_details = self.db_ops.load_all_items(self.table_name)

    def is_item_exists(self, item_id: str) -> bool:
        """检测物品是否存在于数据库中"""
        return self.db_ops.item_exists(item_id, self.table_name)

    def get_item_details_dict(self, item_id: str) -> dict[str, Any]:
        """获取物品的详细信息"""
        item_details = self.db_ops.get_item_by_id(item_id, self.table_name)
        if item_details:
            return item_details
        else:
            raise ValueError(f"物品 {item_id} 在数据库中不存在")

    def get_item(self, item_id: str) -> Item | None:
        """
        根据物品external_id返回对应的 Item 实例

        Args:
            item_id: 物品external_id

        Returns:
            Optional[Item]: Item 实例；若物品不存在则返回 None
        """
        try:
            details = self.get_item_details_dict(item_id)

            if details:
                return Item.create_item(
                    name=details["name"],
                    rarity=details["rarity"],
                    item_type=details["type"],
                    affiliated_type=details["affiliated_type"],
                    portrait_url=details.get("portrait_url", ""),
                    external_id=details["external_id"],
                )
            return None
        except ValueError:
            return None

    def get_all_items(self) -> dict[str, dict[str, Any]]:
        """
        获取所有物品数据

        Returns:
            包含所有物品信息的字典，键为物品ID
        """
        return self._item_details

    def get_item_objects(self) -> dict[str, Item]:
        """
        获取所有物品对象

        Returns:
            包含所有物品对象的字典，键为物品ID
        """
        items = {}
        for item_id, item_data in self._item_details.items():
            try:
                items[item_id] = Item.from_dict(item_data)
            except ValueError:
                continue
        return items

    def add_item(self, item_data: dict[str, Any]) -> bool:
        """
        添加物品到数据库

        Args:
            item_data: 包含物品信息的字典

        Returns:
            成功返回True，失败返回False
        """
        result = self.db_ops.add_item(item_data, self.table_name)
        if result:
            # 更新内存缓存
            self._item_details[item_data["external_id"]] = item_data
        return result

    def add_items_batch(self, items_data: list) -> bool:
        """
        批量添加物品到数据库

        Args:
            items_data: 包含多个物品信息字典的列表

        Returns:
            成功返回True，失败返回False
        """
        result = self.db_ops.add_items_batch(items_data, self.table_name)
        if result:
            # 更新内存缓存
            for item_data in items_data:
                # 使用external_id作为键
                external_id = item_data.get("external_id")
                if external_id:
                    self._item_details[external_id] = item_data
        return result

    def update_item(self, item_id: str, update_data: dict[str, Any]) -> bool:
        """
        更新物品信息

        Args:
            item_id: 要更新的物品ID
            update_data: 包含要更新字段的字典

        Returns:
            成功返回True，失败返回False
        """
        result = self.db_ops.update_item(item_id, update_data, self.table_name)
        if result and item_id in self._item_details:
            # 更新内存缓存
            self._item_details[item_id].update(update_data)
        return result

    def delete_item(self, item_id: str) -> bool:
        """
        从数据库中删除物品

        Args:
            item_id: 要删除的物品ID

        Returns:
            成功返回True，失败返回False
        """
        result = self.db_ops.delete_item(item_id, self.table_name)
        if result and item_id in self._item_details:
            # 更新内存缓存
            del self._item_details[item_id]
        return result

    def get_items_by_rarity(self, rarity: str) -> list:
        """
        根据稀有度获取物品列表

        Args:
            rarity: 稀有度（'5star', '4star', '3star'等）

        Returns:
            符合条件的物品列表
        """
        return self.db_ops.get_items_by_rarity(rarity, self.table_name)

    def get_items_by_type(self, item_type: str) -> list:
        """
        根据物品类型获取物品列表

        Args:
            item_type: 物品类型（character, weapon等）

        Returns:
            符合条件的物品列表
        """
        return self.db_ops.get_items_by_type(item_type, self.table_name)

    def search_items_by_name(self, name: str) -> list:
        """
        根据名称搜索物品

        Args:
            name: 要搜索的物品名称

        Returns:
            匹配的物品列表
        """
        return self.db_ops.search_items_by_name(name, self.table_name)
