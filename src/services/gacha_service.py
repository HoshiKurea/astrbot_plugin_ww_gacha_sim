"""Single-owner DB worker for draw transactions and history reads."""

import hashlib
from .work_queue import WorkQueue, WorkBusy

from ..db.gacha_db_operations import GachaDBOperations
from ..db.item_db_operations import ItemDBOperations
from ..gacha.gacha_flow import GachaFlow
from ..item_data.item_manager import ItemManager

GachaBusy = WorkBusy


class GachaService:
    def __init__(self, db_ops: GachaDBOperations, item_ops: ItemDBOperations,
                 record_history: bool, queue_limit=64, pools=None):
        self.db_ops = db_ops
        self.item_ops = item_ops
        self.record_history = record_history
        self.pools = pools
        self.worker = WorkQueue(workers=1, queue_limit=queue_limit, name="WwGachaDB",
                                cleanup=db_ops.db.close_thread_local_connection)
        self._closed = False

    @staticmethod
    def request_key(platform_id: str, umo: str, sender_id: str,
                    message_id: str | None, operation: str) -> str | None:
        if not message_id:
            return None
        return hashlib.sha256("\0".join((platform_id, umo, sender_id,
                                          message_id, operation)).encode()).hexdigest()

    async def _submit(self, function, *args):
        if self._closed:
            raise GachaBusy("抽卡服务正在关闭")
        return await self.worker.run(function, *args)

    async def run(self, function, *args):
        """Serialize management writes with draw transactions on the same worker."""
        return await self._submit(function, *args)

    @property
    def pending(self):
        return self.worker.pending

    async def draw(self, platform_id, sender_id, pool, revision, count, request_key):
        def work():
            if self.pools is not None:
                latest, version = self.pools.get_with_version(pool.cp_id)
                if latest is None or not latest.enable or version != revision:
                    raise ValueError("卡池在排队期间发生变化，请重新选择或重试")
            manager = ItemManager(self.item_ops, pool.config_group)
            flow = GachaFlow(sender_id, self.db_ops, manager, self.record_history,
                             platform_id, request_key, revision)
            return flow.single_pull(pool) if count == 1 else flow.ten_consecutive_pulls(pool)
        return await self._submit(work)

    async def history(self, platform_id, sender_id, limit, offset, pool_id):
        return await self._submit(self.db_ops.load_pull_history_v2,
                                  platform_id, sender_id, limit, offset, pool_id)

    async def history_count(self, platform_id, sender_id, pool_id):
        return await self._submit(self.db_ops.get_pull_history_count_v2,
                                  platform_id, sender_id, pool_id)

    async def state(self, platform_id, sender_id, pity_group_id):
        return await self._submit(self.db_ops.load_state_v2,
                                  platform_id, sender_id, pity_group_id)

    async def database_status(self):
        """Probe write access on the DB worker without changing user data."""
        def work():
            db = self.db_ops.db
            with db.get_connection() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("SELECT 1")
                    conn.rollback()
                except BaseException:
                    conn.rollback()
                    raise
            return db.get_schema_version()
        return await self._submit(work)

    async def claim_legacy(self, legacy_user_id, platform_id, sender_id):
        return await self._submit(self.db_ops.claim_legacy,
                                  legacy_user_id, platform_id, sender_id)

    async def prune_receipts(self):
        return await self._submit(self.db_ops.prune_receipts)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        await self.worker.close()
