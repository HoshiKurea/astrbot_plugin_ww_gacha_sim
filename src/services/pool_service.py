"""Validated, versioned card-pool publication shared by chat and WebUI."""

import copy
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path

from ..gacha.cardpool_manager import CardPoolConfig, CardPoolManager
from ..gacha.gacha_mechanics import GachaMechanics
from ..item_data.item_manager import ItemManager
from ..db.item_db_operations import ItemDBOperations
from ..web.path_security import managed_path


class RevisionConflict(ValueError):
    """The configuration changed since the editor loaded it."""


class PoolService:
    def __init__(self, manager: CardPoolManager, item_ops: ItemDBOperations):
        self.manager = manager
        self.item_ops = item_ops
        self._lock = threading.RLock()
        self._snapshot = (0, "", {}, {})
        manager.validator = self.validate
        self.reload()

    @property
    def revision(self):
        return self._snapshot[0]

    @property
    def version(self):
        return self._snapshot[1]

    @property
    def config_dir(self) -> Path:
        return self.manager.config_dir

    def validate(self, config: CardPoolConfig) -> None:
        if not config.cp_id or not isinstance(config.cp_id, str):
            raise ValueError("卡池 ID 不能为空")
        if not config.name or not config.name.strip():
            raise ValueError("卡池名称不能为空")
        group = config.pity_group_id or config.cp_id
        if not isinstance(group, str) or not group.strip() or len(group) > 128:
            raise ValueError("保底组 ID 无效")
        items = ItemManager(self.item_ops, config.config_group)
        GachaMechanics(items).validate_pool(config)

    def reload(self) -> int:
        with self._lock:
            configs = self.manager.reload_all()  # validates before publishing
            encoded = json.dumps(
                {key: value.to_dict() for key, value in sorted(configs.items())},
                ensure_ascii=False, sort_keys=True,
            ).encode("utf-8")
            # Publish one immutable snapshot only after validation succeeds.
            # Chat reads never wait for the writer's disk/DB lock.
            self._snapshot = (self.revision + 1, hashlib.sha256(encoded).hexdigest(),
                              copy.deepcopy(configs), dict(self.manager._file_path_to_cp_id))
            return self.revision

    def get_with_version(self, identifier: str):
        snapshot = self._snapshot
        configs = snapshot[2]
        normalized = identifier.replace("\\", "/").removesuffix(".json")
        found = configs.get(identifier) or configs.get(snapshot[3].get(normalized)) or next((pool for pool in configs.values()
                                                if pool.name == identifier), None)
        return (copy.deepcopy(found) if found else None, snapshot[1])

    def get(self, identifier: str) -> CardPoolConfig | None:
        return self.get_with_version(identifier)[0]

    def all(self) -> list[CardPoolConfig]:
        return list(copy.deepcopy(self._snapshot[2]).values())

    def list_configs(self) -> list[dict]:
        snapshot = self._snapshot
        return [{"filename": name, "content": copy.deepcopy(snapshot[2][cp_id]).to_dict()}
                for name, cp_id in snapshot[3].items()]

    def _write_and_publish(self, path: Path, content: bytes | None,
                           expected_revision: int | None = None) -> int:
        with self._lock:
            if expected_revision is not None and expected_revision != self.revision:
                raise RevisionConflict("卡池版本已变化，请刷新后重试")
            previous = path.read_bytes() if path.exists() else None
            temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                if content is None:
                    path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temp.write_bytes(content)
                    os.replace(temp, path)
                return self.reload()
            except BaseException:
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    temp.write_bytes(previous)
                    os.replace(temp, path)
                raise
            finally:
                temp.unlink(missing_ok=True)

    def save(self, name: str, data: dict, expected_revision: int | None = None) -> int:
        path = managed_path(self.config_dir, name, suffix=".json")
        data = dict(data)
        if not data.get("cp_id"):
            if not data.get("name"):
                raise ValueError("卡池名称不能为空")
            data["cp_id"] = self.manager._generate_cp_id(
                path.relative_to(self.config_dir).with_suffix("").as_posix(), data["name"]
            )
        candidate = CardPoolConfig.from_dict(data)
        if candidate.enable:
            self.validate(candidate)
        payload = json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
        return self._write_and_publish(path, payload, expected_revision)

    def set_enabled(self, name: str, enabled: bool,
                    expected_revision: int | None = None) -> int:
        path = managed_path(self.config_dir, name, suffix=".json")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["enable"] = enabled
        return self.save(name, data, expected_revision)

    def delete(self, name: str, expected_revision: int | None = None) -> int:
        path = managed_path(self.config_dir, name, suffix=".json")
        return self._write_and_publish(path, None, expected_revision)
