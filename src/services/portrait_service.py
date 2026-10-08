"""Bounded portrait preparation, independent of the CPU render queue."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor


class PortraitUnavailable(RuntimeError):
    pass


class PortraitService:
    def __init__(self, resources, workers=3, queue_limit=32):
        self.resources = resources
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="WwPortrait")
        self.capacity = workers + queue_limit
        self.tasks = {}
        self.failures = {}
        self.stop = threading.Event()
        self.closed = False

    @property
    def pending(self):
        return len(self.tasks)

    def missing(self, items):
        return [item for item in items if item.portrait_url
                and not self.resources.has_item_portrait(item)]

    async def prepare(self, items, timeout_seconds=45):
        if self.closed:
            raise PortraitUnavailable("立绘准备服务已关闭")
        futures = {}
        for item in self.missing(items):
            key = self.resources.portrait_cache_key(item)
            if key in futures:
                continue
            future = self.tasks.get(key)
            if future is None:
                if self.failures.get(key, 0) > time.monotonic():
                    raise PortraitUnavailable("部分立绘暂时无法下载，本次请以文字结果为准")
                if len(self.tasks) >= self.capacity:
                    raise PortraitUnavailable("立绘准备队列繁忙，本次请以文字结果为准")
                loop = asyncio.get_running_loop()
                future = loop.run_in_executor(
                    self.executor,
                    lambda current=item: self.resources.download_item_portrait(current, self.stop),
                )
                self.tasks[key] = future

                def finished(done, current_key=key):
                    self.tasks.pop(current_key, None)
                    if not done.cancelled() and done.exception() is None and done.result():
                        self.failures.pop(current_key, None)
                    else:
                        if len(self.failures) >= 256:
                            self.failures.pop(next(iter(self.failures)))
                        self.failures[current_key] = time.monotonic() + 60

                future.add_done_callback(finished)
            futures[key] = future
        if not futures:
            return
        # Timeout/cancellation of one chat must not cancel another chat's shared
        # download, nor free a slot while its worker still owns a connection.
        group = asyncio.gather(*futures.values(), return_exceptions=True)
        try:
            results = await asyncio.wait_for(asyncio.shield(group), timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise PortraitUnavailable(
                "立绘下载等待超时，资源仍在后台准备，本次请以文字结果为准"
            ) from exc
        if not all(result is True for result in results) or self.missing(items):
            raise PortraitUnavailable("部分立绘暂时无法下载，本次请以文字结果为准")

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
