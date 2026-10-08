"""
抽卡机制模块
实现核心的随机抽取算法，包括概率计算、保底机制、稀有度分布逻辑等核心业务规则
"""

import math
import random


from ..item_data.item_manager import Item, ItemManager
from .cardpool_manager import CardPoolConfig


class GachaMechanics:
    """Calculate a draw without changing persistent state."""

    RARITIES = ("3star", "4star", "5star")

    def __init__(self, item_data_manager: ItemManager, rng=None):
        self.item_data_manager = item_data_manager
        self.rng = rng if rng is not None else random

    @staticmethod
    def _rate(value, name: str) -> float:
        try:
            rate = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是数字") from exc
        if not math.isfinite(rate) or not 0 <= rate <= 1:
            raise ValueError(f"{name} 必须在 0 到 1 之间")
        return rate

    @staticmethod
    def _hard_pity(config, rarity: str) -> int:
        value = config.probability_progression[rarity].get(
            "hard_pity_pull", 80 if rarity == "5star" else 10
        )
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{rarity} 硬保底次数必须是正整数")
        hard_rate = GachaMechanics._rate(
            config.probability_progression[rarity].get("hard_pity_rate", 1),
            f"{rarity} hard_pity_rate",
        )
        if hard_rate != 1:
            raise ValueError(f"{rarity} 硬保底概率必须为 1")
        return value

    @classmethod
    def _four_star_weapon_rate(cls, settings: dict) -> float:
        if "_4star_weapon_rate" in settings:
            value = settings["_4star_weapon_rate"]
        elif "four_star_character_rate" in settings:
            value = 1 - cls._rate(
                settings["four_star_character_rate"], "four_star_character_rate"
            )
        else:
            value = settings.get("_4star_role_rate", 0.5)
        return cls._rate(value, "_4star_weapon_rate")

    def validate_pool(self, config: CardPoolConfig) -> dict[str, list[Item]]:
        """Reject invalid pools before consuming a draw or changing pity."""
        available = self.item_data_manager.get_item_objects()
        if not isinstance(config.included_item_ids, dict) or not isinstance(
            config.rate_up_item_ids, dict
        ):
            raise ValueError("卡池物品配置格式错误")
        selected: dict[str, list[Item]] = {}
        for rarity in self.RARITIES:
            ids = config.included_item_ids.get(rarity, [])
            if (
                not isinstance(ids, list)
                or any(not isinstance(item_id, str) for item_id in ids)
                or len(ids) != len(set(ids))
            ):
                raise ValueError(f"{rarity} 物品列表无效或存在重复")
            selected[rarity] = []
            for item_id in ids:
                item = available.get(item_id)
                if item is None or item.rarity != rarity:
                    raise ValueError(f"{rarity} 包含不存在或稀有度不符的物品: {item_id}")
                selected[rarity].append(item)
            up_ids = config.rate_up_item_ids.get(rarity, [])
            if (
                not isinstance(up_ids, list)
                or any(not isinstance(item_id, str) for item_id in up_ids)
                or len(up_ids) != len(set(up_ids))
            ):
                raise ValueError(f"{rarity} UP 列表无效或存在重复")
            if not set(up_ids).issubset(ids):
                raise ValueError(f"{rarity} UP 物品不在卡池中")
        for rarity in ("3star", "4star", "5star"):
            if not selected[rarity]:
                raise ValueError(f"{rarity} 候选物品为空")
        five = self._rate(config.probability_settings.get("base_5star_rate", 0.008), "base_5star_rate")
        four = self._rate(config.probability_settings.get("base_4star_rate", 0.06), "base_4star_rate")
        if five + four > 1:
            raise ValueError("五星与四星基础概率之和不能超过 1")
        for rarity in ("4star", "5star"):
            self._hard_pity(config, rarity)
            self._rate(config.probability_settings.get(f"up_{rarity}_rate", 0.5), f"up_{rarity}_rate")
        self._four_star_weapon_rate(config.probability_settings)
        intervals = config.probability_progression["5star"].get("soft_pity", [])
        if not isinstance(intervals, list):
            raise ValueError("五星软保底区间必须是列表")
        if any(
            not isinstance(interval, dict)
            or not isinstance(interval.get("start_pull"), int)
            or not isinstance(interval.get("end_pull"), int)
            or "increment" not in interval
            for interval in intervals
        ):
            raise ValueError("五星软保底区间格式错误")
        previous_end = 0
        for interval in sorted(intervals, key=lambda part: part["start_pull"]):
            start, end = interval["start_pull"], interval["end_pull"]
            if (
                isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, int) or not isinstance(end, int)
                or start <= previous_end or end < start
                or end >= self._hard_pity(config, "5star")
            ):
                raise ValueError("五星软保底区间无效或重叠")
            self._rate(interval["increment"], "soft_pity.increment")
            previous_end = end
        return selected

    def calculate_rate_5star(self, rate_number: int, pool_config: CardPoolConfig) -> float:
        current = rate_number + 1
        if current >= self._hard_pity(pool_config, "5star"):
            return 1.0
        probability = self._rate(
            pool_config.probability_settings.get("base_5star_rate", 0.008),
            "base_5star_rate",
        )
        intervals = sorted(
            pool_config.probability_progression["5star"].get("soft_pity", []),
            key=lambda part: part["start_pull"],
        )
        for interval in intervals:
            if current < interval["start_pull"]:
                break
            steps = min(current, interval["end_pull"]) - interval["start_pull"] + 1
            probability += steps * interval["increment"]
        return min(probability, 1.0)

    def calculate_rate_4star(self, rate_number: int, pool_config: CardPoolConfig) -> float:
        if rate_number + 1 >= self._hard_pity(pool_config, "4star"):
            return 1.0
        return self._rate(
            pool_config.probability_settings.get("base_4star_rate", 0.06),
            "base_4star_rate",
        )

    def execute_pull(
        self,
        cardpool_config: CardPoolConfig,
        pity_5star: int,
        pity_4star: int,
        _5star_guaranteed: bool,
        _4star_guaranteed: bool,
        *,
        selected: dict[str, list[Item]] | None = None,
    ) -> tuple[Item, int, int, bool, bool]:
        if selected is None:
            selected = self.validate_pool(cardpool_config)
        five_rate = self.calculate_rate_5star(pity_5star, cardpool_config)
        four_rate = self.calculate_rate_4star(pity_4star, cardpool_config)
        roll = self.rng.random()
        if roll < five_rate:
            rarity = "5star"
        elif pity_4star + 1 >= self._hard_pity(cardpool_config, "4star"):
            rarity = "4star"
        elif roll < min(1.0, five_rate + four_rate):
            rarity = "4star"
        else:
            rarity = "3star"

        candidates = selected[rarity]
        up_ids = set(cardpool_config.rate_up_item_ids.get(rarity, []))
        up = [item for item in candidates if item.external_id in up_ids]
        ordinary = [item for item in candidates if item.external_id not in up_ids]
        guaranteed = _5star_guaranteed if rarity == "5star" else _4star_guaranteed
        up_rate = self._rate(
            cardpool_config.probability_settings.get(f"up_{rarity}_rate", 0.5),
            f"up_{rarity}_rate",
        ) if rarity != "3star" else 0
        choose_up = bool(up) and (guaranteed or not ordinary or self.rng.random() < up_rate)
        pool = up if choose_up else ordinary
        if rarity == "4star":
            weapon_rate = self._four_star_weapon_rate(cardpool_config.probability_settings)
            wanted_type = "weapon" if self.rng.random() < weapon_rate else "character"
            by_type = [item for item in pool if item.type == wanted_type]
            if by_type:
                pool = by_type
        item = self.rng.choice(pool)
        if rarity == "5star":
            pity_5star = 0
            pity_4star = 0  # A five-star also satisfies the four-star-or-better pity.
            _5star_guaranteed = bool(up and ordinary and not choose_up)
        elif rarity == "4star":
            pity_5star += 1
            pity_4star = 0
            _4star_guaranteed = bool(up and ordinary and not choose_up)
        else:
            pity_5star += 1
            pity_4star += 1
        return item, pity_5star, pity_4star, _5star_guaranteed, _4star_guaranteed
