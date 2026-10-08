"""Admission-controlled workers; cancellation never releases a running slot."""

import asyncio
from concurrent.futures import ThreadPoolExecutor


class WorkBusy(RuntimeError):
    pass


class WorkQueue:
    def __init__(self, workers=1, queue_limit=32, name="WwWork", cleanup=None):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self.capacity = workers + queue_limit
        self.pending = 0
        self.closed = False
        self.cleanup = cleanup

    async def run(self, function, *args):
        if self.closed or self.pending >= self.capacity:
            raise WorkBusy("任务队列繁忙或正在关闭，请稍后重试")
        self.pending += 1

        def work():
            try:
                return function(*args)
            finally:
                if self.cleanup:
                    self.cleanup()

        try:
            future = asyncio.get_running_loop().run_in_executor(self.executor, work)
        except BaseException:
            self.pending -= 1
            raise

        def release(done):
            self.pending -= 1
            # Also consume errors when the requesting HTTP/chat task was cancelled.
            if not done.cancelled():
                done.exception()

        future.add_done_callback(release)
        return await asyncio.shield(future)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await asyncio.to_thread(self.executor.shutdown, wait=True)
