"""Bounded renderer that retains capacity until timed-out workers actually finish."""

import asyncio
from concurrent.futures import ThreadPoolExecutor


class RenderBusy(RuntimeError):
    pass


class RenderTimedOut(RuntimeError):
    pass


class RenderService:
    def __init__(self, workers: int = 2, queue_limit: int = 20,
                 timeout_seconds: int = 8):
        self.executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="WwGachaRender"
        )
        self.capacity = workers + queue_limit
        self.timeout_seconds = timeout_seconds
        self.pending = 0
        self.closed = False

    async def run(self, function, *args, **kwargs):
        if self.closed:
            raise RenderBusy("渲染服务已关闭")
        # All submissions and callbacks run on the owner event loop. There is no
        # await between checking and reserving the slot.
        if self.pending >= self.capacity:
            raise RenderBusy("图片生成繁忙，请稍后重试")
        self.pending += 1
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(
                self.executor, lambda: function(*args, **kwargs)
            )
        except BaseException:
            self.pending -= 1
            raise

        def release(_future):
            self.pending -= 1

        future.add_done_callback(release)
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise RenderTimedOut("图片生成超时") from exc

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await asyncio.to_thread(self.executor.shutdown, True)
