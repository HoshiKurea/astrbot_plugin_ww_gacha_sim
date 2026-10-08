"""Transaction-backed gacha flow. The database owns state until a batch commits."""

from ..db.gacha_db_operations import GachaDBOperations
from ..item_data.item_manager import Item, ItemManager
from .cardpool_manager import CardPoolConfig
from .gacha_mechanics import GachaMechanics


class GachaFlow:
    def __init__(
        self,
        user_id: str,
        db_ops: GachaDBOperations,
        item_data_manager: ItemManager,
        record_history: bool = True,
        platform_id: str | None = None,
        request_key: str | None = None,
        config_revision: str = "",
    ):
        self.user_id = user_id
        self.db_ops = db_ops
        self.mechanics = GachaMechanics(item_data_manager)
        self.record_history = record_history
        self.platform_id = platform_id
        self.request_key = request_key
        self.config_revision = config_revision

    def _draw_batch(self, pool_config: CardPoolConfig, count: int) -> list[Item]:
        def draw(state):
            selected = self.mechanics.validate_pool(pool_config)
            current = state.copy()
            items = []
            for _ in range(count):
                item, pity5, pity4, guaranteed5, guaranteed4 = self.mechanics.execute_pull(
                    pool_config,
                    current["pity_5star"],
                    current["pity_4star"],
                    current["_5star_guaranteed"],
                    current["_4star_guaranteed"],
                    selected=selected,
                )
                items.append(item)
                current.update(
                    pity_5star=pity5,
                    pity_4star=pity4,
                    _5star_guaranteed=guaranteed5,
                    _4star_guaranteed=guaranteed4,
                    pull_count=current["pull_count"] + 1,
                )
            return items, current

        if self.platform_id is not None:
            return self.db_ops.commit_draws_v2(
                self.platform_id, self.user_id,
                pool_config.pity_group_id or pool_config.cp_id,
                pool_config.cp_id, pool_config.config_group,
                self.config_revision, "single" if count == 1 else "ten",
                self.request_key, count, draw, self.record_history,
            )
        return self.db_ops.commit_draws(
            self.user_id, pool_config.cp_id, count, draw, self.record_history
        )

    def single_pull(self, pool_config: CardPoolConfig):
        item = self._draw_batch(pool_config, 1)[0]
        return {"item": item.name, "rarity": item.rarity, "item_obj": item}

    def ten_consecutive_pulls(self, pool_config: CardPoolConfig) -> list[Item]:
        items = self._draw_batch(pool_config, 10)
        # History keeps the actual draw order; only presentation is sorted.
        return sorted(
            items,
            key=lambda item: (
                -{"5star": 5, "4star": 4, "3star": 3}[item.rarity],
                0 if item.type == "character" else 1,
            ),
        )
