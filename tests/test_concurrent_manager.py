"""Tests for concurrent_manager.py - 并发控制器。

该模块当前未被主流程使用（ARCHITECTURE.md「写盘与配置」），
这里只钉住其公共行为，防止未来启用时无声劣化。
用例全部控制在速率上限之内，不触发真实等待。
"""

import asyncio
import time


from auto_epub.concurrent_manager import ConcurrentManager


class TestExecuteTasks:
    def test_returns_results_in_submission_order(self):
        async def scenario():
            mgr = ConcurrentManager(max_workers=3, rate_limit=100)
            return await mgr.execute_tasks([lambda i=i: i * 2 for i in range(5)])

        results = asyncio.run(scenario())
        assert [r for r in results if not isinstance(r, BaseException)] == [
            0,
            2,
            4,
            6,
            8,
        ]

    def test_task_exception_is_captured_not_raised(self):
        """gather(return_exceptions=True) 契约：单个任务失败不打垮整批。"""

        def boom():
            raise RuntimeError("boom")

        async def scenario():
            mgr = ConcurrentManager(max_workers=2, rate_limit=100)
            return await mgr.execute_tasks([lambda: "ok", boom])

        results = asyncio.run(scenario())
        assert results[0] == "ok"
        assert isinstance(results[1], RuntimeError)

    def test_max_workers_bounds_concurrency(self):
        """max_workers=1 时信号量把并发压成串行。"""
        state = {"current": 0, "peak": 0}

        def track():
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
            time.sleep(0.01)
            state["current"] -= 1
            return True

        async def scenario():
            mgr = ConcurrentManager(max_workers=1, rate_limit=100)
            return await mgr.execute_tasks([track] * 4)

        asyncio.run(scenario())
        assert state["peak"] == 1

    def test_accepts_coroutine_functions(self):
        async def native_coro():
            return "async-ok"

        async def scenario():
            mgr = ConcurrentManager(max_workers=2, rate_limit=100)
            return await mgr.execute_tasks([native_coro])

        assert asyncio.run(scenario()) == ["async-ok"]

    def test_default_task_names_do_not_crash(self):
        """task_names 缺省时自动编号（日志用），不影响执行。"""

        async def scenario():
            mgr = ConcurrentManager(max_workers=2, rate_limit=100)
            return await mgr.execute_tasks([lambda: 1, lambda: 2])

        assert asyncio.run(scenario()) == [1, 2]


class TestBatchExecute:
    def test_batches_all_results_preserved(self):
        items = list(range(7))

        async def scenario():
            mgr = ConcurrentManager(max_workers=3, rate_limit=100)
            return await mgr.batch_execute(items, lambda x: x + 100, batch_size=3)

        # 分批只是调度方式：结果集合必须完整且保序
        assert asyncio.run(scenario()) == [100 + i for i in items]

    def test_default_batch_size_is_max_workers(self):
        items = list(range(5))

        async def scenario():
            mgr = ConcurrentManager(max_workers=2, rate_limit=100)
            return await mgr.batch_execute(items, str)

        assert asyncio.run(scenario()) == [str(i) for i in items]
