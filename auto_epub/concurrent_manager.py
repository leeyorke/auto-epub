"""
并发翻译管理器 - 控制速率和并发数
"""

import asyncio
import time
from typing import Any, Callable, List, TypeVar

T = TypeVar("T")


class ConcurrentManager:
    """并发翻译管理器，支持速率限制和并发控制"""

    def __init__(self, max_workers: int = 3, rate_limit: int = 10):
        """
        Args:
            max_workers: 最大并发任务数
            rate_limit: 每分钟最大请求数
        """
        self.max_workers = max_workers
        self.rate_limit = rate_limit
        self.semaphore = asyncio.Semaphore(max_workers)
        self.request_times: List[float] = []
        self.lock = asyncio.Lock()

    async def execute_tasks(
        self, tasks: List[Callable], task_names: List[str] | None = None
    ) -> List[Any]:
        """
        并发执行任务，带速率限制

        Args:
            tasks: 任务函数列表
            task_names: 任务名称列表（用于日志）

        Returns:
            任务结果列表
        """
        if task_names is None:
            task_names = [f"Task-{i}" for i in range(len(tasks))]

        async_tasks = [
            self._rate_limited_task(task, name) for task, name in zip(tasks, task_names)
        ]

        results = await asyncio.gather(*async_tasks, return_exceptions=True)
        return results

    async def _rate_limited_task(self, task: Callable, name: str) -> Any:
        """执行单个带速率限制的任务"""
        async with self.semaphore:
            await self._wait_for_rate_limit()

            try:
                # 如果任务是协程函数，直接 await
                if asyncio.iscoroutinefunction(task):
                    result = await task()
                else:
                    # 如果是普通函数，在线程池中执行
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(None, task)

                return result
            except Exception as e:
                print(f"❌ 任务 {name} 执行失败: {e}")
                return e

    async def _wait_for_rate_limit(self) -> None:
        """等待以遵守速率限制"""
        async with self.lock:
            now = time.time()

            # 移除 60 秒前的请求记录
            self.request_times = [t for t in self.request_times if now - t < 60]

            # 如果超过速率限制，等待
            if len(self.request_times) >= self.rate_limit:
                sleep_time = 60 - (now - self.request_times[0])
                if sleep_time > 0:
                    print(f"⏳ 达到速率限制，等待 {sleep_time:.1f} 秒...")
                    await asyncio.sleep(sleep_time)
                    # 重新清理
                    now = time.time()
                    self.request_times = [t for t in self.request_times if now - t < 60]

            # 记录当前请求
            self.request_times.append(now)

    async def batch_execute(
        self,
        items: List[T],
        process_func: Callable[[T], Any],
        batch_size: int | None = None,
    ) -> List[Any]:
        """
        分批并发执行任务

        Args:
            items: 要处理的项目列表
            process_func: 处理函数
            batch_size: 批次大小（默认为 max_workers）

        Returns:
            所有结果列表
        """
        if batch_size is None:
            batch_size = self.max_workers

        all_results = []

        for i in range(0, len(items), batch_size):
            batch = items[i : i + batch_size]
            tasks = [lambda item=item: process_func(item) for item in batch]
            task_names = [f"Item-{i + j}" for j in range(len(batch))]

            results = await self.execute_tasks(tasks, task_names)
            all_results.extend(results)

        return all_results
