"""
卡池配置管理模块
负责卡池配置文件的加载、保存和CRUD操作
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.star import StarTools


@dataclass
class CardPoolConfig:
    """卡池配置数据类

    用于封装卡池配置文件的结构化数据
    """

    cp_id: str  # 卡池ID，用于唯一标识卡池
    name: str  # 卡池名称
    probability_settings: dict[str, float]  # 概率设置
    rate_up_item_ids: dict[str, Any]  # UP物品配置
    included_item_ids: dict[str, list[str]]  # 包含物品配置
    probability_progression: dict[str, dict[str, Any]]  # 概率递增配置
    config_group: str = "default"  # 配置组名称，用于确定使用哪个物品表
    enable: bool = True  # 是否启用该配置，默认为True
    pity_group_id: str = ""  # Empty means the pool has its own pity group.

    def __post_init__(self):
        """初始化后处理"""

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        # 使用asdict转换为字典
        result = asdict(self)

        # 确保rate_up_item_ids中的值是列表类型
        for rarity, items in result.get("rate_up_item_ids", {}).items():
            if isinstance(items, str):
                # 如果是字符串，尝试解析为JSON数组
                try:
                    result["rate_up_item_ids"][rarity] = json.loads(items)
                except (json.JSONDecodeError, TypeError):
                    # 如果解析失败，使用空列表
                    result["rate_up_item_ids"][rarity] = []
            elif items is None:
                result["rate_up_item_ids"][rarity] = []

        # 确保included_item_ids中的值是列表类型
        for rarity, items in result.get("included_item_ids", {}).items():
            if isinstance(items, str):
                # 如果是字符串，尝试解析为JSON数组
                try:
                    result["included_item_ids"][rarity] = json.loads(items)
                except (json.JSONDecodeError, TypeError):
                    # 如果解析失败，使用空列表
                    result["included_item_ids"][rarity] = []
            elif items is None:
                result["included_item_ids"][rarity] = []

        # 确保probability_progression中的值是正确的字典格式
        for rarity, progression in result.get("probability_progression", {}).items():
            if isinstance(progression, dict):
                # 确保soft_pity是列表类型
                if "soft_pity" in progression and isinstance(
                    progression["soft_pity"], str
                ):
                    try:
                        progression["soft_pity"] = json.loads(progression["soft_pity"])
                    except (json.JSONDecodeError, TypeError):
                        progression["soft_pity"] = []
            else:
                # 如果是字符串，尝试解析为JSON字典
                try:
                    result["probability_progression"][rarity] = json.loads(progression)
                except (json.JSONDecodeError, TypeError):
                    # 如果解析失败，使用默认值
                    result["probability_progression"][rarity] = {
                        "hard_pity_pull": 80,
                        "hard_pity_rate": 1,
                        "soft_pity": [],
                    }

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CardPoolConfig:
        # 只保留CardPoolConfig类支持的字段
        valid_fields = {
            "cp_id",
            "name",
            "probability_settings",
            "rate_up_item_ids",
            "included_item_ids",
            "probability_progression",
            "config_group",
            "enable",
            "pity_group_id",
        }
        filtered_data = {k: v for k, v in data.items() if k in valid_fields}
        # 如果没有config_group字段，默认为'default'
        if "config_group" not in filtered_data:
            filtered_data["config_group"] = "default"
        # 如果没有enable字段，默认为True
        if "enable" not in filtered_data:
            filtered_data["enable"] = True
        return cls(**filtered_data)


class CardPoolManager:
    """卡池配置管理类

    负责卡池配置文件的加载、保存和CRUD操作
    """

    def __init__(self, config_dir_path: Path | None = None):
        """初始化卡池配置管理器

        参数:
            config_dir: 配置文件所在目录
        """
        if config_dir_path is None:
            # 使用 StarTools 获取标准数据目录
            self.config_dir = (
                Path(StarTools.get_data_dir("astrbot_plugin_ww_gacha_sim"))
                / "card_pool_configs"
            )
        else:
            self.config_dir = config_dir_path
        self._configs: dict[str, CardPoolConfig] = {}  # 内存中的配置数据，键为 cp_id
        self._file_path_to_cp_id: dict[str, str] = {}  # 文件路径到 cp_id 的映射
        self._lock = threading.Lock()  # 保护 _configs 和 _file_path_to_cp_id 的线程安全
        self.validator = None
        # 确保配置目录存在
        self._ensure_dir_exists()
        # 初始化默认配置
        self._init_default_configs()
        # 加载所有配置文件到内存
        self.load_all_configs()

    def _init_default_configs(self):
        """初始化默认配置"""
        try:
            # 检查配置目录下是否有json文件
            has_json = next(self.config_dir.rglob("*.json"), None) is not None

            if has_json:
                return

            logger.info("检测到配置目录为空，正在初始化默认卡池配置...")

            # 预置配置目录
            presets_dir = Path(__file__).parent.parent / "assets" / "presets"

            if not presets_dir.exists():
                logger.warning(f"预置配置目录不存在: {presets_dir}")
                return

            # 复制所有预置文件到配置目录
            for item in presets_dir.iterdir():
                if item.is_file() and item.suffix == ".json":
                    shutil.copy2(item, self.config_dir / item.name)
                    logger.info(f"已复制预置配置: {item.name}")

        except Exception as e:
            logger.error(f"初始化默认配置失败: {e}")

    def sync_new_presets(self) -> int:
        """同步新增的预置配置到配置目录。

        仅复制 presets 中有而配置目录中没有的 .json 文件，
        不会覆盖用户已有的配置。

        Returns:
            int: 本次同步新增的配置数量
        """
        presets_dir = Path(__file__).parent.parent / "assets" / "presets"
        if not presets_dir.exists():
            return 0

        added = 0
        existing_names = {
            p.name for p in self.config_dir.iterdir() if p.suffix == ".json"
        }

        for item in presets_dir.iterdir():
            if (
                item.is_file()
                and item.suffix == ".json"
                and item.name not in existing_names
            ):
                shutil.copy2(item, self.config_dir / item.name)
                logger.info(f"已同步新预置配置: {item.name}")
                added += 1

        if added > 0:
            logger.info(f"预置配置同步完成，新增 {added} 个配置")
            # 重新加载配置到内存
            self.load_all_configs()
        return added

    def find_config_by_identifier(self, identifier: str) -> CardPoolConfig | None:
        """
        根据标识符查找卡池配置

        参数:
            identifier: 卡池标识符（cp_id、配置文件路径或卡池名称）

        返回:
            匹配的卡池配置，如果没有找到则返回 None
        """
        if not identifier:
            return None

        # 1. 尝试直接通过 cp_id 匹配
        if identifier in self._configs:
            return self._configs[identifier]

        # 2. 尝试通过文件路径匹配
        # 标准化路径分隔符，移除 .json 后缀
        normalized_path = identifier.replace("\\", "/").replace(".json", "")
        if normalized_path in self._file_path_to_cp_id:
            cp_id = self._file_path_to_cp_id[normalized_path]
            return self._configs[cp_id]

        # 3. 尝试通过名称模糊匹配
        for config in self._configs.values():
            if config.name == identifier:
                return config

        return None

    def _ensure_dir_exists(self):
        """确保配置目录存在，不存在则创建"""
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            logger.info(f"配置目录已确保存在: {self.config_dir}")
        except OSError as e:
            logger.error(f"创建配置目录失败: {e}")
            raise RuntimeError(f"创建配置目录失败: {e}")

    def _generate_cp_id(self, file_path: str, pool_name: str) -> str:
        """
        根据相对路径+卡池名称生成唯一的 cp_id

        参数:
            file_path: 配置文件的相对路径（不含.json后缀）
            pool_name: 卡池名称

        返回:
            12位的十六进制哈希值
        """
        combined = f"{file_path}:{pool_name}"
        hash_obj = hashlib.md5(combined.encode("utf-8"))
        return hash_obj.hexdigest()[:12]

    def load_all_configs(self) -> dict[str, CardPoolConfig]:
        """加载指定目录及其子目录下的所有JSON配置文件到内存

        返回:
            加载的配置字典，键为 cp_id，值为配置数据类实例
        """
        try:
            next_configs: dict[str, CardPoolConfig] = {}
            next_paths: dict[str, str] = {}

            # 深度扫描配置目录及其子目录
            for root, dirs, files in os.walk(self.config_dir):
                for filename in files:
                    if filename.endswith(".json"):
                        # 跳过文件名为 .json 的配置文件（即 .json 后缀前是空白字符）
                        if filename == ".json":
                            logger.debug("跳过文件名为 .json 的配置文件")
                            continue

                        # 计算相对于配置目录的路径
                        full_path = os.path.join(root, filename)
                        rel_path = os.path.relpath(full_path, self.config_dir)
                        file_path = rel_path[
                            :-5
                        ]  # 移除.json后缀，将路径分隔符统一为正斜杠
                        file_path = file_path.replace("\\", "/")

                        try:
                            with open(full_path, encoding="utf-8") as f:
                                config_data = json.load(f)

                                # A malformed pool must not silently disappear on reload.
                                if (
                                    "name" not in config_data
                                    or not config_data["name"]
                                    or not config_data["name"].strip()
                                ):
                                    raise ValueError(f"卡池名称无效: {file_path}")

                                # 确保cp_id存在，根据相对路径+卡池名称生成唯一ID
                                if "cp_id" not in config_data:
                                    config_data["cp_id"] = self._generate_cp_id(
                                        file_path, config_data["name"]
                                    )
                                # 转换为数据类实例
                                config_instance = CardPoolConfig.from_dict(
                                    config_data
                                )
                                if config_instance.cp_id in next_configs:
                                    raise ValueError(f"重复卡池 ID: {config_instance.cp_id}")
                                next_configs[config_instance.cp_id] = config_instance
                                next_paths[file_path] = config_instance.cp_id
                                logger.info(
                                    f"已加载配置文件: {file_path}, cp_id: {config_instance.cp_id}"
                                )
                        except json.JSONDecodeError as e:
                            logger.error(f"JSON格式错误: {file_path} - {e}")
                            raise ValueError(f"配置文件 {file_path} 格式错误: {e}")
                        except OSError as e:
                            logger.error(f"读取文件失败: {file_path} - {e}")
                            raise OSError(f"读取配置文件 {file_path} 失败: {e}")
                        except Exception as e:
                            logger.error(f"处理配置文件 {file_path} 失败: {e}")
                            raise RuntimeError(
                                f"处理配置文件 {file_path} 失败: {e}"
                            )

            if self.validator is not None:
                for candidate in next_configs.values():
                    if candidate.enable:
                        self.validator(candidate)
            with self._lock:
                self._configs = next_configs
                self._file_path_to_cp_id = next_paths
            logger.info(f"共加载 {len(next_configs)} 个配置文件")
            return next_configs.copy()
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            raise RuntimeError(f"加载配置文件失败: {e}")

    def reload_all(self) -> dict[str, CardPoolConfig]:
        """重新扫描配置目录并重新加载所有配置

        返回:
            加载的配置字典，键为 cp_id，值为配置数据类实例
        """
        logger.info("正在重新加载所有卡池配置...")
        return self.load_all_configs()

    def get_config_ids(self) -> list[str]:
        """获取所有配置的 cp_id 列表

        返回:
            cp_id 列表
        """
        return list(self._configs.keys())

    def get_config(self, config_identifier: str) -> CardPoolConfig:
        """获取指定配置

        参数:
            config_identifier: 配置标识符（可以是文件路径或cp_id）

        返回:
            配置数据类实例

        异常:
            KeyError: 配置不存在
        """
        # 首先尝试通过 cp_id 查找
        if config_identifier in self._configs:
            return self._configs[config_identifier]

        # 如果没找到，尝试通过文件路径查找
        if config_identifier in self._file_path_to_cp_id:
            cp_id = self._file_path_to_cp_id[config_identifier]
            return self._configs[cp_id]

        logger.error(f"配置不存在: {config_identifier}")
        raise KeyError(f"配置 {config_identifier} 不存在")

    def get_config_by_name(self, name: str) -> list[CardPoolConfig]:
        """通过卡池名称查找所有匹配的配置

        参数:
            name: 卡池名称

        返回:
            匹配的配置列表
        """
        matched_configs = []
        for cp_id, config in self._configs.items():
            if config.name == name:
                matched_configs.append(config)

        return matched_configs

    def get_config_by_cp_id(self, cp_id: str) -> CardPoolConfig:
        """通过 cp_id (UUID) 查找配置

        参数:
            cp_id: 卡池ID (UUID)

        返回:
            配置数据类实例

        异常:
            KeyError: 配置不存在
        """
        if cp_id in self._configs:
            return self._configs[cp_id]

        logger.error(f"找不到 cp_id 为 {cp_id} 的卡池配置")
        raise KeyError(f"找不到 cp_id 为 {cp_id} 的卡池配置")

    def add_config(self, file_path: str, config_data: dict[str, Any]) -> CardPoolConfig:
        """添加新配置文件

        参数:
            file_path: 配置文件路径（不含.json后缀）
            config_data: 配置数据字典

        返回:
            创建的配置数据类实例

        异常:
            ValueError: 配置已存在或数据格式错误
            IOError: 保存文件失败
        """
        # 检查文件路径是否已存在
        if file_path in self._file_path_to_cp_id:
            logger.error(f"配置文件已存在: {file_path}")
            raise ValueError(f"配置文件 {file_path} 已存在")

        try:
            # 确保cp_id存在，根据相对路径+卡池名称生成唯一ID
            if "cp_id" not in config_data:
                if "name" not in config_data:
                    logger.error("配置数据中缺少 name 字段")
                    raise ValueError("配置数据中缺少 name 字段")
                config_data["cp_id"] = self._generate_cp_id(
                    file_path, config_data["name"]
                )

            # 检查 cp_id 是否已存在
            if config_data["cp_id"] in self._configs:
                logger.error(f"cp_id 已存在: {config_data['cp_id']}")
                raise ValueError(f"cp_id {config_data['cp_id']} 已存在")

            # 创建配置数据类实例
            config_instance = CardPoolConfig.from_dict(config_data)

            # 根据配置组构建完整文件路径
            # 无论配置组是什么，都将配置文件放在对应的子目录下，包括'default'配置组
            full_file_path = os.path.join(config_instance.config_group, file_path)

            # 保存到文件
            self._save_config(full_file_path, config_instance)

            # 写入内存（线程安全）
            with self._lock:
                self._configs[config_instance.cp_id] = config_instance
                self._file_path_to_cp_id[full_file_path] = config_instance.cp_id

            logger.info(f"已添加配置: {full_file_path}, cp_id: {config_instance.cp_id}")
            return config_instance
        except Exception as e:
            logger.error(f"添加配置失败: {file_path} - {e}")
            raise RuntimeError(f"添加配置 {file_path} 失败: {e}")

    def update_config(
        self, file_path: str, config_data: dict[str, Any]
    ) -> CardPoolConfig:
        """更新现有配置文件

        参数:
            file_path: 配置文件路径（不含.json后缀）
            config_data: 新的配置数据字典

        返回:
            更新后的配置数据类实例

        异常:
            KeyError: 配置不存在
            ValueError: 数据格式错误
            IOError: 保存文件失败
        """
        # 检查配置是否存在
        cp_id = None
        actual_file_path = None

        # 先尝试直接查找
        if file_path in self._file_path_to_cp_id:
            cp_id = self._file_path_to_cp_id[file_path]
            actual_file_path = file_path
        else:
            # 尝试查找匹配的配置ID
            for path, cpid in self._file_path_to_cp_id.items():
                if os.path.basename(path) == file_path:
                    cp_id = cpid
                    actual_file_path = path
                    break

        if not cp_id:
            logger.error(f"配置文件不存在: {file_path}")
            raise KeyError(f"配置文件 {file_path} 不存在")

        assert actual_file_path is not None
        assert cp_id is not None

        try:
            # 如果更新数据中没有cp_id，保留原有的cp_id
            if "cp_id" not in config_data:
                config_data["cp_id"] = cp_id

            # 创建配置数据类实例
            config_instance = CardPoolConfig.from_dict(config_data)

            # 根据配置组构建新的文件路径
            # 无论配置组是什么，都将配置文件放在对应的子目录下，包括'default'配置组
            new_file_path = os.path.join(
                config_instance.config_group, os.path.basename(str(actual_file_path))
            )

            # 如果配置组变化，需要先删除旧文件
            if new_file_path != actual_file_path:
                # 删除旧文件
                old_full_path = os.path.join(
                    self.config_dir, f"{actual_file_path}.json"
                )
                if os.path.exists(old_full_path):
                    os.remove(old_full_path)

                # 更新内存映射（线程安全）
                with self._lock:
                    del self._file_path_to_cp_id[actual_file_path]
                    self._file_path_to_cp_id[new_file_path] = cp_id

            # 保存到文件
            self._save_config(new_file_path, config_instance)

            # 更新内存中的配置（线程安全）
            with self._lock:
                self._configs[config_instance.cp_id] = config_instance

            logger.info(f"已更新配置: {new_file_path}, cp_id: {config_instance.cp_id}")
            return config_instance
        except Exception as e:
            logger.error(f"更新配置失败: {file_path} - {e}")
            raise RuntimeError(f"更新配置 {file_path} 失败: {e}")

    def update_config_property(
        self, file_path: str, property_path: str, value: Any
    ) -> CardPoolConfig:
        """修改配置文件内的属性

        参数:
            file_path: 配置文件路径（不含.json后缀）
            property_path: 属性路径，如 "rate_up_items.5star" 或 "probability_settings.base_5star_rate"
            value: 新的属性值

        返回:
            更新后的配置数据类实例

        异常:
            KeyError: 配置不存在
            ValueError: 属性路径无效
            IOError: 保存文件失败
        """
        # 检查配置是否存在
        cp_id = None
        actual_file_path = None

        # 先尝试直接查找
        if file_path in self._file_path_to_cp_id:
            cp_id = self._file_path_to_cp_id[file_path]
            actual_file_path = file_path
        else:
            # 尝试查找匹配的配置ID
            for path, cpid in self._file_path_to_cp_id.items():
                if os.path.basename(path) == file_path:
                    cp_id = cpid
                    actual_file_path = path
                    break

        if not cp_id:
            logger.error(f"配置文件不存在: {file_path}")
            raise KeyError(f"配置文件 {file_path} 不存在")

        assert actual_file_path is not None
        assert cp_id is not None

        try:
            # 获取当前配置
            config_instance = self._configs[cp_id]

            # 将数据类转换为字典以便修改
            config_dict = asdict(config_instance)

            # 处理属性路径
            keys = property_path.split(".")
            current = config_dict

            # 遍历属性路径，直到最后一个键
            for i, key in enumerate(keys[:-1]):
                if key not in current:
                    raise ValueError(
                        f"属性路径无效: {property_path} (在 {key} 处找不到)"
                    )
                current = current[key]

                if not isinstance(current, dict):
                    raise ValueError(
                        f"属性路径无效: {property_path} (在 {key} 处不是字典)"
                    )

            # 设置属性值
            last_key = keys[-1]
            current[last_key] = value

            # 转换回数据类实例
            updated_instance = CardPoolConfig.from_dict(config_dict)

            # 保存到文件
            self._save_config(actual_file_path, updated_instance)

            # 更新内存中的配置（线程安全）
            with self._lock:
                self._configs[updated_instance.cp_id] = updated_instance

            logger.info(f"已修改配置属性: {actual_file_path}.{property_path}")
            return updated_instance
        except Exception as e:
            logger.error(f"修改配置属性失败: {file_path}.{property_path} - {e}")
            if isinstance(e, (KeyError, ValueError)):
                raise
            raise RuntimeError(f"修改配置属性 {file_path}.{property_path} 失败: {e}")

    def delete_config(self, file_path: str) -> bool:
        """删除配置文件

        参数:
            file_path: 配置文件路径（不含.json后缀）

        返回:
            删除是否成功

        异常:
            KeyError: 配置不存在
            IOError: 删除文件失败
        """
        # 检查配置是否存在
        cp_id = None
        actual_file_path = None

        # 先尝试直接查找
        if file_path in self._file_path_to_cp_id:
            cp_id = self._file_path_to_cp_id[file_path]
            actual_file_path = file_path
        else:
            # 尝试查找匹配的配置ID
            for path, cpid in self._file_path_to_cp_id.items():
                if os.path.basename(path) == file_path:
                    cp_id = cpid
                    actual_file_path = path
                    break

        if not cp_id:
            logger.error(f"配置文件不存在: {file_path}")
            raise KeyError(f"配置文件 {file_path} 不存在")

        assert actual_file_path is not None
        assert cp_id is not None

        try:
            # 删除配置文件
            full_path = os.path.join(self.config_dir, f"{actual_file_path}.json")
            if os.path.exists(full_path):
                os.remove(full_path)
                logger.info(f"已删除配置文件: {actual_file_path}.json")
            else:
                logger.warning(f"配置文件不存在: {actual_file_path}.json")

            # 从内存中删除（线程安全）
            with self._lock:
                del self._configs[cp_id]
                del self._file_path_to_cp_id[actual_file_path]
            logger.info(f"已删除配置: {actual_file_path}, cp_id: {cp_id}")
            return True
        except Exception as e:
            logger.error(f"删除配置失败: {file_path} - {e}")
            raise RuntimeError(f"删除配置 {file_path} 失败: {e}")

    def _save_config(self, file_path: str, config_instance: CardPoolConfig):
        """保存配置到文件

        参数:
            file_path: 配置文件路径（不含.json后缀）
            config_instance: 配置数据类实例

        异常:
            IOError: 保存文件失败
        """
        try:
            # 转换为字典，使用to_dict()方法确保格式正确
            config_dict = config_instance.to_dict()

            # 保存到文件
            full_path = os.path.join(self.config_dir, f"{file_path}.json")

            # 创建必要的目录
            os.makedirs(os.path.dirname(full_path), exist_ok=True)

            with open(full_path, "w", encoding="utf-8") as f:
                json.dump(config_dict, f, ensure_ascii=False, indent=2)

            logger.info(f"已保存配置: {file_path} 到 {full_path}")
        except OSError as e:
            logger.error(f"保存配置失败: {file_path} - {e}")
            raise OSError(f"保存配置 {file_path} 失败: {e}")
        except Exception as e:
            logger.error(f"保存配置时发生错误: {file_path} - {e}")
            raise RuntimeError(f"保存配置 {file_path} 时发生错误: {e}")

    def exists(self, file_path: str) -> bool:
        """检查配置是否存在

        参数:
            file_path: 配置文件路径（不含.json后缀）

        返回:
            配置是否存在
        """
        return file_path in self._file_path_to_cp_id

    def save_config(self, file_path: str, config_instance: CardPoolConfig):
        """保存配置到文件

        参数:
            file_path: 配置文件路径（不含.json后缀）
            config_instance: 配置数据类实例

        异常:
            IOError: 保存文件失败
        """
        self._save_config(file_path, config_instance)

    def reload_config(self, file_path: str) -> CardPoolConfig:
        """重新加载指定配置文件

        参数:
            file_path: 配置文件路径（不含.json后缀）

        返回:
            重新加载的配置数据类实例

        异常:
            KeyError: 配置不存在
            ValueError: JSON格式错误
            IOError: 读取文件失败
        """
        if file_path not in self._file_path_to_cp_id:
            logger.error(f"配置文件不存在: {file_path}")
            raise KeyError(f"配置文件 {file_path} 不存在")

        self._file_path_to_cp_id[file_path]
        full_path = os.path.join(self.config_dir, f"{file_path}.json")

        try:
            with open(full_path, encoding="utf-8") as f:
                config_data = json.load(f)
                config_instance = CardPoolConfig.from_dict(config_data)
                with self._lock:
                    self._configs[config_instance.cp_id] = config_instance
                logger.info(
                    f"已重新加载配置: {file_path}, cp_id: {config_instance.cp_id}"
                )
                return config_instance
        except json.JSONDecodeError as e:
            logger.error(f"JSON格式错误: {file_path} - {e}")
            raise ValueError(f"配置文件 {file_path} 格式错误: {e}")
        except OSError as e:
            logger.error(f"读取文件失败: {file_path} - {e}")
            raise OSError(f"读取配置文件 {file_path} 失败: {e}")

    def set_config_enable(self, file_path: str, enable: bool) -> CardPoolConfig:
        """启用或禁用配置

        参数:
            file_path: 配置文件路径（不含.json后缀）
            enable: 是否启用

        返回:
            更新后的配置数据类实例

        异常:
            KeyError: 配置不存在
            IOError: 保存文件失败
        """
        # 检查配置是否存在
        cp_id = None
        actual_file_path = None

        # 先尝试直接查找
        if file_path in self._file_path_to_cp_id:
            cp_id = self._file_path_to_cp_id[file_path]
            actual_file_path = file_path
        else:
            # 尝试查找匹配的配置ID
            for path, cpid in self._file_path_to_cp_id.items():
                if os.path.basename(path) == file_path:
                    cp_id = cpid
                    actual_file_path = path
                    break

        if not cp_id:
            logger.error(f"配置文件不存在: {file_path}")
            raise KeyError(f"配置文件 {file_path} 不存在")

        assert actual_file_path is not None
        assert cp_id is not None

        try:
            # 获取当前配置
            config_instance = self._configs[cp_id]

            # 将数据类转换为字典以便修改
            config_dict = asdict(config_instance)

            # 设置 enable 字段
            config_dict["enable"] = enable

            # 转换回数据类实例
            updated_instance = CardPoolConfig.from_dict(config_dict)

            # 保存到文件
            self._save_config(actual_file_path, updated_instance)

            # 更新内存中的配置（线程安全）
            with self._lock:
                self._configs[updated_instance.cp_id] = updated_instance

            logger.info(
                f"已{'启用' if enable else '禁用'}配置: {actual_file_path}, cp_id: {updated_instance.cp_id}"
            )
            return updated_instance
        except Exception as e:
            logger.error(f"{'启用' if enable else '禁用'}配置失败: {file_path} - {e}")
            if isinstance(e, (KeyError, ValueError)):
                raise
            raise RuntimeError(
                f"{'启用' if enable else '禁用'}配置 {file_path} 失败: {e}"
            )

    def get_enabled_configs(self) -> dict[str, CardPoolConfig]:
        """获取所有启用的配置

        返回:
            启用的配置字典，键为 cp_id，值为配置数据类实例
        """
        return {
            cp_id: config for cp_id, config in self._configs.items() if config.enable
        }
